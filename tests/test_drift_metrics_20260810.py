"""F9/P4.1 — the gauge-drift METRIC + sustained-drift ALERT.

Ticket 20260810_quota_drift_live_recurrence_permanent_fix, Phase 4.1.

CONTEXT: the reconciler (`ab0t_quota/reconcile.py`) already logged
`gauge_drift_detected` / `gauge_drift_resolved` as human-facing QuotaAlert
messages (proven live in prod). What was MISSING was a feedback loop that
does not require a human to be reading logs — a structured METRIC a
dashboard/alert rule can consume, and an escalation when drift RECURS
instead of healing once and staying healed. That gap is exactly why this
bug class recurred four times (TICKET.md `Recurrence of:` chain) — a
reconciler that quietly re-heals every pass looks, from a log-scanning
human's perspective, indistinguishable from a reconciler that healed once
and is now fine.

This file pins:
  1. `drift_detected` emits a `quota.drift_detected` QuotaMetric with the
     exact field set the ticket specifies (org, resource_key, observed,
     ledger, counter_before, counter_after, delta) — for EVERY gauge
     resource sandbox-platform ships (concurrent, gpu_instances,
     browser_sessions, desktop_sessions, home_storage_gb).
  2. `drift_resolved` emits a distinct `quota.drift_resolved` metric
     (delta=0.0), paired 1:1 with the existing `gauge_drift_resolved` alert.
  3. The metric is NEVER rate-limited (unlike the human alert) — a dashboard
     needs every observation even while the alert stays cooldown-gated.
  4. The library's zero-config default (`metric_dispatchers=None`) still
     gives a consumer with NO metrics infrastructure a real, scrapeable
     Redis counter — the "if there's no metrics sink" fallback (P4.1's ask).
  5. Sustained drift (the SAME (org, resource) re-healed N consecutive
     passes with no intervening resolve) raises a DISTINCT CRITICAL alert —
     proving the wiring end-to-end with a fake sink, per the ticket's test
     ask — and the counter resets the moment it resolves.
  6. End-to-end through `LibraryReconciler.reconcile_org` (not just the
     `DriftAlertManager` unit), covering every gauge resource, so the metric
     fires from the real code path the reconciler uses on every heal.

fakeredis[lua] (lupa) is NOT real Redis; these are logic proofs, consistent
with the rest of the reconcile/alerts test suite (test_reconcile_20260710.py).
"""
from __future__ import annotations

import fakeredis.aioredis
import pytest
import pytest_asyncio

from ab0t_quota.activations import Activation, ActivationState, InMemoryActivationStore
from ab0t_quota.alerts import (
    AlertDispatcher, AlertSeverity, DriftAlertManager, LogMetricDispatcher,
    MetricDispatcher, RedisCounterMetricDispatcher,
)
from ab0t_quota.counters.gauge import GaugeCounter
from ab0t_quota.engine import QuotaEngine
from ab0t_quota.models.core import CounterType, QuotaMetric, ResourceDef, TierConfig, TierLimits
from ab0t_quota.providers import StaticTierProvider
from ab0t_quota.reconcile import LibraryReconciler, ReconcileConfig
from ab0t_quota.registry import ResourceRegistry

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# fixtures — mirrors sandbox-platform's real quota-config.json gauge set
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.flushall()
    await r.aclose()


# The 5 gauge resources sandbox-platform ships (quota-config.json), the exact
# population the ticket requires coverage for.
GAUGE_RESOURCE_KEYS = [
    "sandbox.concurrent",
    "sandbox.gpu_instances",
    "sandbox.browser_sessions",
    "sandbox.desktop_sessions",
    "sandbox.home_storage_gb",
]


def _sandbox_registry() -> ResourceRegistry:
    registry = ResourceRegistry()
    defs = [
        ResourceDef(service="sandbox-platform", resource_key=rk,
                    display_name=rk, counter_type=CounterType.GAUGE, unit="unit")
        for rk in GAUGE_RESOURCE_KEYS
    ]
    registry.register(*defs)
    return registry


def _sandbox_engine(redis, store=None) -> QuotaEngine:
    registry = _sandbox_registry()
    tiers = {
        "free": TierConfig(
            tier_id="free", display_name="Free",
            limits={rk: TierLimits(limit=100) for rk in GAUGE_RESOURCE_KEYS},
        ),
    }
    return QuotaEngine(
        redis=redis, tier_provider=StaticTierProvider({"org-1": "free"}),
        registry=registry, tiers=tiers,
        activation_store=store or InMemoryActivationStore(),
    )


async def _set_gauge(redis, org_id, resource_key, value):
    await GaugeCounter(redis, org_id, resource_key).reset(value)


async def _gauge(redis, org_id, resource_key) -> float:
    return await GaugeCounter(redis, org_id, resource_key).get()


class _RecordingAlertDispatcher(AlertDispatcher):
    """Captures every dispatched QuotaAlert."""
    def __init__(self):
        self.alerts = []

    async def dispatch(self, alert) -> None:
        self.alerts.append(alert)

    def messages(self):
        return [a.message for a in self.alerts]


class _RecordingMetricDispatcher(MetricDispatcher):
    """The fake metrics SINK the ticket asks the wiring be proved against."""
    def __init__(self):
        self.metrics: list[QuotaMetric] = []

    async def emit(self, metric: QuotaMetric) -> None:
        self.metrics.append(metric)

    def named(self, name: str):
        return [m for m in self.metrics if m.name == name]


def _drift_mgr(redis, *, sustained_alert_threshold: int = 3, cooldown_seconds: int = 600):
    alert_rec = _RecordingAlertDispatcher()
    metric_rec = _RecordingMetricDispatcher()
    mgr = DriftAlertManager(
        redis=redis, dispatchers=[alert_rec], cooldown_seconds=cooldown_seconds,
        metric_dispatchers=[metric_rec], sustained_alert_threshold=sustained_alert_threshold,
    )
    return mgr, alert_rec, metric_rec


# ===========================================================================
# 1. drift_detected emits the quota.drift_detected metric with the exact
#    field set — for EVERY gauge resource sandbox-platform ships.
# ===========================================================================

@pytest.mark.parametrize("resource_key", GAUGE_RESOURCE_KEYS)
async def test_drift_detected_emits_metric_with_correct_fields(redis, resource_key):
    mgr, _alert_rec, metric_rec = _drift_mgr(redis)

    fired = await mgr.drift_detected(
        "org-1", resource_key, observed=5.0, ledger=2.0,
        before=2.0, after=5.0, source="provider",
    )
    assert fired is True

    detected = metric_rec.named("quota.drift_detected")
    assert len(detected) == 1
    m = detected[0]
    assert m.org_id == "org-1"
    assert m.resource_key == resource_key
    assert m.observed == 5.0
    assert m.ledger == 2.0
    assert m.counter_before == 2.0
    assert m.counter_after == 5.0
    assert m.delta == 3.0            # abs(after - before) — the healed amount
    assert m.delta > 0               # "delta>0" is the ticket's detected signature
    assert m.source == "provider"


async def test_drift_detected_delta_is_absolute_healed_amount(redis):
    """A DOWN-heal (over-count -> Σ open) must report a positive delta too —
    delta is the SIZE of the correction, not its sign."""
    mgr, _alert_rec, metric_rec = _drift_mgr(redis)
    await mgr.drift_detected(
        "org-1", "sandbox.concurrent", observed=1.0, ledger=1.0,
        before=99.0, after=1.0, source="activations",
    )
    m = metric_rec.named("quota.drift_detected")[0]
    assert m.delta == 98.0


# ===========================================================================
# 2. drift_resolved emits a DISTINCT quota.drift_resolved metric (delta=0.0),
#    paired 1:1 with the gauge_drift_resolved alert — only when a drift was
#    actually previously active (never for a perpetually-healthy org).
# ===========================================================================

@pytest.mark.parametrize("resource_key", GAUGE_RESOURCE_KEYS)
async def test_resolved_drift_emits_resolved_metric(redis, resource_key):
    mgr, alert_rec, metric_rec = _drift_mgr(redis)

    await mgr.drift_detected(
        "org-1", resource_key, observed=5.0, ledger=5.0,
        before=2.0, after=5.0, source="provider",
    )
    fired = await mgr.drift_resolved("org-1", resource_key, value=5.0)

    assert fired is True
    resolved = metric_rec.named("quota.drift_resolved")
    assert len(resolved) == 1
    m = resolved[0]
    assert m.org_id == "org-1"
    assert m.resource_key == resource_key
    assert m.delta == 0.0            # "resolved" is the ticket's delta==0 signature
    assert m.counter_before == m.counter_after == 5.0
    # paired with the human alert
    assert any("gauge_drift_resolved" in a for a in alert_rec.messages())


async def test_resolve_with_no_prior_drift_emits_no_metric(redis):
    """A steady-state, never-drifted (org, resource) must NOT emit a resolved
    metric every pass — that would be noise, not signal, for every org that
    is simply fine. Only a genuine detected->resolved transition counts."""
    mgr, _alert_rec, metric_rec = _drift_mgr(redis)
    fired = await mgr.drift_resolved("org-1", "sandbox.concurrent", value=0.0)
    assert fired is False
    assert metric_rec.named("quota.drift_resolved") == []


# ===========================================================================
# 3. The metric is NEVER rate-limited, unlike the human alert (which the
#    existing gauge_drift_detected cooldown behaviour already pins).
# ===========================================================================

async def test_metric_emitted_every_pass_even_when_alert_is_cooldown_suppressed(redis):
    mgr, alert_rec, metric_rec = _drift_mgr(redis, cooldown_seconds=600)

    await mgr.drift_detected("org-1", "sandbox.concurrent", observed=3, ledger=1,
                              before=1, after=3, source="provider")
    await mgr.drift_detected("org-1", "sandbox.concurrent", observed=3, ledger=1,
                              before=1, after=3, source="provider")

    # the human alert is cooldown-gated: only the first call dispatched.
    assert sum("gauge_drift_detected" in m for m in alert_rec.messages()) == 1
    # the metric is NOT: both calls emitted a quota.drift_detected metric.
    assert len(metric_rec.named("quota.drift_detected")) == 2


# ===========================================================================
# 4. Zero-config fallback (P4.1's explicit ask): with NO metric_dispatchers
#    supplied, the library still gives a scrapeable Redis counter — "if there
#    is no metrics sink, at minimum expose a counter the platform can scrape."
# ===========================================================================

async def test_default_metric_dispatchers_expose_a_scrapeable_redis_counter(redis):
    alert_rec = _RecordingAlertDispatcher()
    mgr = DriftAlertManager(redis=redis, dispatchers=[alert_rec])  # metric_dispatchers OMITTED

    await mgr.drift_detected("org-1", "sandbox.concurrent", observed=3, ledger=1,
                              before=1, after=3, source="provider")

    total = await redis.get("quota:metrics:drift_detected:total")
    per_resource = await redis.get("quota:metrics:drift_detected:org-1:sandbox.concurrent")
    assert int(total) == 1
    assert int(per_resource) == 1

    # A resolve clears the running "sustained" streak and bumps the resolved total.
    await mgr.drift_resolved("org-1", "sandbox.concurrent", value=1.0)
    resolved_total = await redis.get("quota:metrics:drift_resolved:total")
    assert int(resolved_total) == 1
    sustained = await redis.get("quota:metrics:drift:org-1:sandbox.concurrent:sustained")
    assert sustained is None


async def test_redis_counter_metric_dispatcher_standalone(redis):
    """RedisCounterMetricDispatcher used directly (not via DriftAlertManager) —
    the seam a consumer wires into their OWN dispatcher list."""
    d = RedisCounterMetricDispatcher(redis)
    await d.emit(QuotaMetric(
        name="quota.drift_detected", org_id="org-9", resource_key="sandbox.gpu_instances",
        observed=2.0, ledger=1.0, counter_before=1.0, counter_after=2.0, delta=1.0,
        source="provider",
    ))
    assert int(await redis.get("quota:metrics:drift_detected:total")) == 1
    assert int(await redis.get("quota:metrics:drift:org-9:sandbox.gpu_instances:sustained")) == 1


async def test_log_metric_dispatcher_does_not_raise(redis):
    """LogMetricDispatcher is the other zero-config default; smoke-test it
    doesn't blow up (a broken dispatcher must never break reconcile)."""
    d = LogMetricDispatcher()
    await d.emit(QuotaMetric(
        name="quota.drift_detected", org_id="org-1", resource_key="sandbox.concurrent",
        observed=3.0, ledger=1.0, counter_before=1.0, counter_after=3.0, delta=2.0,
        source="activations",
    ))


# ===========================================================================
# 5. Sustained drift (delta>0 across N consecutive passes, no intervening
#    resolve) raises a DISTINCT CRITICAL alert — proves the wiring with a
#    fake sink, per the ticket's explicit test ask.
# ===========================================================================

async def test_sustained_drift_raises_distinct_critical_alert(redis):
    mgr, alert_rec, _metric_rec = _drift_mgr(redis, sustained_alert_threshold=3)

    for _ in range(2):
        await mgr.drift_detected("org-1", "sandbox.concurrent", observed=5, ledger=1,
                                  before=1, after=5, source="provider")
    # below threshold: no sustained alert yet (only the plain per-detect alert,
    # itself cooldown-suppressed after the first call).
    assert not any("quota_drift_sustained" in m for m in alert_rec.messages())

    await mgr.drift_detected("org-1", "sandbox.concurrent", observed=5, ledger=1,
                              before=1, after=5, source="provider")   # 3rd consecutive
    sustained = [a for a in alert_rec.alerts if "quota_drift_sustained" in a.message]
    assert len(sustained) == 1
    assert sustained[0].severity == AlertSeverity.CRITICAL
    assert "consecutive_passes=3" in sustained[0].message


async def test_sustained_counter_resets_on_resolve(redis):
    """A drift that heals once and then STAYS healed must never accumulate
    toward the sustained-alert threshold — only a RECURRING drift (never
    resolved between detects) does."""
    mgr, alert_rec, _metric_rec = _drift_mgr(redis, sustained_alert_threshold=3)

    await mgr.drift_detected("org-1", "sandbox.concurrent", observed=5, ledger=1,
                              before=1, after=5, source="provider")
    await mgr.drift_resolved("org-1", "sandbox.concurrent", value=5.0)  # healed and STAYED healed

    for _ in range(2):
        await mgr.drift_detected("org-1", "sandbox.concurrent", observed=5, ledger=1,
                                  before=1, after=5, source="provider")
    # only 2 consecutive since the reset — below threshold=3, no sustained alert.
    assert not any("quota_drift_sustained" in m for m in alert_rec.messages())


async def test_sustained_alert_is_per_org_resource_independent(redis):
    """A sustained streak on one (org, resource) must not bleed into another —
    each (org, resource) pair tracks its own consecutive-pass count."""
    mgr, alert_rec, _metric_rec = _drift_mgr(redis, sustained_alert_threshold=2)

    await mgr.drift_detected("org-1", "sandbox.concurrent", observed=5, ledger=1,
                              before=1, after=5, source="provider")
    await mgr.drift_detected("org-1", "sandbox.desktop_sessions", observed=5, ledger=1,
                              before=1, after=5, source="provider")
    # each resource has only 1 consecutive detect so far; neither should have
    # crossed threshold=2 yet.
    assert not any("quota_drift_sustained" in m for m in alert_rec.messages())

    await mgr.drift_detected("org-1", "sandbox.concurrent", observed=5, ledger=1,
                              before=1, after=5, source="provider")  # 2nd for concurrent only
    sustained = [a for a in alert_rec.alerts if "quota_drift_sustained" in a.message]
    assert len(sustained) == 1
    assert sustained[0].resource_key == "sandbox.concurrent"


# ===========================================================================
# 6. End-to-end through LibraryReconciler.reconcile_org — the real code path
#    the reconciler uses on every heal, not just the DriftAlertManager unit —
#    covering every gauge resource, in provider mode (sandbox-platform's
#    reconcile.truth_source="provider" configuration).
# ===========================================================================

@pytest.mark.parametrize("resource_key", GAUGE_RESOURCE_KEYS)
async def test_reconciler_heal_emits_metric_per_gauge_resource(redis, resource_key):
    store = InMemoryActivationStore()
    eng = _sandbox_engine(redis, store)
    await _set_gauge(redis, "org-1", resource_key, 99.0)  # drifted: stuck-high gauge

    def provider(org_id):
        # reality: 0 live (mirrors cd790b95's "0 sandboxes ⇒ 5 used" live bug).
        return {rk: {"total": 0.0, "per_user": {}} for rk in GAUGE_RESOURCE_KEYS}

    mgr, alert_rec, metric_rec = _drift_mgr(redis)
    r = LibraryReconciler(
        eng, observed_usage_provider=provider, drift_alerts=mgr,
        config=ReconcileConfig(truth_source="provider"),
    )

    res = await r.reconcile_org("org-1")

    assert await _gauge(redis, "org-1", resource_key) == 0.0
    assert res.changes[resource_key]["source"] == "provider"

    detected = metric_rec.named("quota.drift_detected")
    match = [m for m in detected if m.resource_key == resource_key]
    assert len(match) == 1
    assert match[0].counter_before == 99.0
    assert match[0].counter_after == 0.0
    assert match[0].delta == 99.0
    assert match[0].observed == 0.0
    assert any("gauge_drift_detected" in m for m in alert_rec.messages())


async def test_reconciler_two_passes_detect_then_resolve_across_all_gauges(redis):
    """create -> drift -> heal (pass 1, metric+alert detected) -> stays in
    sync (pass 2, metric+alert resolved) — the exact drift-injection ->
    auto-heal shape the ticket's Phase 5 live/e2e plan calls for, proven here
    at the reconciler-unit level for concurrent AND desktop_sessions (the two
    representative resources named in the tasklist SCOPE)."""
    store = InMemoryActivationStore()
    eng = _sandbox_engine(redis, store)
    for rk in ("sandbox.concurrent", "sandbox.desktop_sessions"):
        await _set_gauge(redis, "org-1", rk, 5.0)  # stuck-high, like cd790b95 live

    def provider(org_id):
        return {rk: {"total": 0.0, "per_user": {}} for rk in GAUGE_RESOURCE_KEYS}

    mgr, alert_rec, metric_rec = _drift_mgr(redis)
    r = LibraryReconciler(
        eng, observed_usage_provider=provider, drift_alerts=mgr,
        config=ReconcileConfig(truth_source="provider"),
    )

    await r.reconcile_org("org-1")   # pass 1: heals both gauges to 0
    for rk in ("sandbox.concurrent", "sandbox.desktop_sessions"):
        assert await _gauge(redis, "org-1", rk) == 0.0
    assert len(metric_rec.named("quota.drift_detected")) == 2  # one per gauge

    await r.reconcile_org("org-1")   # pass 2: already in sync -> resolved
    resolved = metric_rec.named("quota.drift_resolved")
    resolved_keys = {m.resource_key for m in resolved}
    assert resolved_keys == {"sandbox.concurrent", "sandbox.desktop_sessions"}
    assert any("gauge_drift_resolved" in m for m in alert_rec.messages())


async def test_reconciler_sustained_recurring_drift_raises_sustained_alert(redis):
    """The bug class this ticket exists to catch: a gauge that keeps
    re-drifting (e.g. an idempotency-key collision on a reused container id)
    heals every pass but NEVER resolves — reconcile_org called repeatedly
    with the underlying write bug re-introducing the same stuck-high value
    between passes. That must raise the sustained-drift CRITICAL alert, not
    just a string of identical-looking 'gauge_drift_detected' log lines a
    human has to notice."""
    store = InMemoryActivationStore()
    eng = _sandbox_engine(redis, store)

    def provider(org_id):
        return {rk: {"total": 0.0, "per_user": {}} for rk in GAUGE_RESOURCE_KEYS}

    mgr, alert_rec, _metric_rec = _drift_mgr(redis, sustained_alert_threshold=3)
    r = LibraryReconciler(
        eng, observed_usage_provider=provider, drift_alerts=mgr,
        config=ReconcileConfig(truth_source="provider"),
    )

    for _ in range(3):
        # simulates the collision bug: something re-increments the gauge
        # between reconcile passes, so it is ALWAYS found drifted.
        await _set_gauge(redis, "org-1", "sandbox.concurrent", 1.0)
        await r.reconcile_org("org-1")

    sustained = [a for a in alert_rec.alerts if "quota_drift_sustained" in a.message]
    assert len(sustained) == 1
    assert sustained[0].resource_key == "sandbox.concurrent"
    assert sustained[0].severity == AlertSeverity.CRITICAL
