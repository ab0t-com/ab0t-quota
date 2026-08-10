"""Ticket 20260810 — robust self-correcting quota (recount-before-deny,
read-repair, reconcile_org). DESIGN_robust_quota.md.

The fast Redis counter is a CACHE of a type-specific derived truth; staleness must
never harm the user. These tests prove the two user-safety rules and the on-demand
recompute, per counter type:

  * GAUGE       — truth = observed_usage_provider (live count).   The cd790b95 "5/0" class.
  * ACCUMULATOR — truth = accumulator_usage_provider (durable ledger sum for the period).
  * RATE        — no recount (the TTL'd window counter is already truth).
"""

import pytest
import pytest_asyncio
import fakeredis.aioredis

from ab0t_quota.engine import QuotaEngine
from ab0t_quota.models.core import (
    ResourceDef, CounterType, TierConfig, TierLimits, ResetPeriod,
)
from ab0t_quota.models.requests import QuotaCheckRequest, QuotaResetRequest
from ab0t_quota.models.responses import QuotaDecision
from ab0t_quota.registry import ResourceRegistry
from ab0t_quota.providers import StaticTierProvider
from ab0t_quota.counters.factory import create_counter
from ab0t_quota.alerts import (
    DriftAlertManager, MetricDispatcher, AlertDispatcher,
)


# --------------------------------------------------------------------------- fixtures

TIERS = {
    "free": TierConfig(
        tier_id="free", display_name="Free", sort_order=0,
        limits={
            "compute.concurrent": TierLimits(limit=2),
            "billing.monthly_spend": TierLimits(limit=10.0),
            "api.requests": TierLimits(limit=100),
            "compute.per_user": TierLimits(limit=6, per_user_limit=3),
        },
    ),
}

RESOURCES = [
    ResourceDef(service="test", resource_key="compute.concurrent",
                display_name="Concurrent", counter_type=CounterType.GAUGE, unit="units"),
    ResourceDef(service="test", resource_key="billing.monthly_spend",
                display_name="Monthly Spend", counter_type=CounterType.ACCUMULATOR,
                unit="USD", reset_period=ResetPeriod.MONTHLY, precision=2),
    ResourceDef(service="test", resource_key="api.requests",
                display_name="API/hr", counter_type=CounterType.RATE,
                unit="requests", window_seconds=3600),
    ResourceDef(service="test", resource_key="compute.per_user",
                display_name="Per-user", counter_type=CounterType.GAUGE, unit="items"),
]


class _MetricRecorder(MetricDispatcher):
    def __init__(self):
        self.metrics = []

    async def emit(self, metric):
        self.metrics.append(metric)


class _AlertRecorder(AlertDispatcher):
    def __init__(self):
        self.alerts = []

    async def dispatch(self, alert):
        self.alerts.append(alert)


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.flushall()
    await r.aclose()


def _build_engine(redis, *, gauge_truth=None, acc_truth=None, throttle=0.0):
    """Build an engine with controllable truth sources + recording drift channel.

    ``gauge_truth`` / ``acc_truth`` are mutable dict holders (or callables). A dict
    is served verbatim; a callable is invoked with org_id. Raising callables
    simulate an unreachable truth source.
    """
    registry = ResourceRegistry()
    registry.register(*RESOURCES)
    provider = StaticTierProvider({"org-free": "free"})

    def _mk(holder):
        if holder is None:
            return None
        if callable(holder):
            return holder
        return lambda org_id: holder

    engine = QuotaEngine(
        redis=redis, tier_provider=provider, registry=registry, tiers=TIERS,
        observed_usage_provider=_mk(gauge_truth),
        accumulator_usage_provider=_mk(acc_truth),
        read_repair_throttle_seconds=throttle,
    )
    metrics = _MetricRecorder()
    alerts = _AlertRecorder()
    dam = DriftAlertManager(redis=redis, dispatchers=[alerts],
                            metric_dispatchers=[metrics])
    engine.set_drift_alerts(dam)
    return engine, metrics, alerts


async def _seed(redis, engine, resource_key, value):
    rd = engine._registry.require(resource_key)
    counter = create_counter(redis, "org-free", rd)
    await counter.reset(value)
    assert await counter.get() == value


# --------------------------------------------------------------------------- Phase 1

class TestRecountBeforeDenyGauge:
    @pytest.mark.asyncio
    async def test_stale_high_gauge_zero_live_allows_and_repairs(self, redis):
        """cd790b95: cache says 5, ZERO live ⇒ check() must ALLOW and repair to 0."""
        engine, metrics, _ = _build_engine(
            redis, gauge_truth={"compute.concurrent": {"total": 0.0, "per_user": {}}})
        await _seed(redis, engine, "compute.concurrent", 5.0)  # stuck-high

        result = await engine.check(QuotaCheckRequest(
            org_id="org-free", resource_key="compute.concurrent"))

        assert result.decision == QuotaDecision.ALLOW
        assert result.current == 0.0  # repaired to truth
        # counter actually repaired in Redis
        counter = create_counter(redis, "org-free",
                                 engine._registry.require("compute.concurrent"))
        assert await counter.get() == 0.0
        # drift metric fired with the gauge type label + a delta of 5
        detected = [m for m in metrics.metrics if m.name == "quota.drift_detected"]
        assert detected and detected[0].resource_type == "gauge"
        assert detected[0].delta == 5.0

    @pytest.mark.asyncio
    async def test_genuinely_over_still_denies(self, redis):
        """Cache 5, truly 5 live, limit 2 ⇒ recount confirms over ⇒ DENY (correct)."""
        engine, metrics, _ = _build_engine(
            redis, gauge_truth={"compute.concurrent": {"total": 5.0, "per_user": {}}})
        await _seed(redis, engine, "compute.concurrent", 5.0)

        result = await engine.check(QuotaCheckRequest(
            org_id="org-free", resource_key="compute.concurrent"))

        assert result.decision == QuotaDecision.DENY
        # no drift (cache matched truth) → no repair metric
        assert not [m for m in metrics.metrics if m.name == "quota.drift_detected"]

    @pytest.mark.asyncio
    async def test_over_cache_but_true_under_allows_with_honest_number(self, redis):
        """Cache 9, truly 1, limit 2 ⇒ ALLOW; the shown number is the honest 1."""
        engine, metrics, _ = _build_engine(
            redis, gauge_truth={"compute.concurrent": {"total": 1.0, "per_user": {}}})
        await _seed(redis, engine, "compute.concurrent", 9.0)

        result = await engine.check(QuotaCheckRequest(
            org_id="org-free", resource_key="compute.concurrent"))

        assert result.allowed is True  # 1+1=2 == limit → allowed (at-limit warning)
        assert result.current == 1.0

    @pytest.mark.asyncio
    async def test_no_truth_source_keeps_deny(self, redis):
        """No provider wired ⇒ behaviour unchanged: a stale-high cache still denies."""
        engine, _, _ = _build_engine(redis)  # no providers
        await _seed(redis, engine, "compute.concurrent", 5.0)

        result = await engine.check(QuotaCheckRequest(
            org_id="org-free", resource_key="compute.concurrent"))
        assert result.decision == QuotaDecision.DENY


class TestRecountBeforeDenyAccumulator:
    @pytest.mark.asyncio
    async def test_over_cache_but_true_ledger_sum_under_allows(self, redis):
        """Accumulator cache 15 (>10) but the durable ledger sums to 5 ⇒ ALLOW."""
        engine, metrics, _ = _build_engine(
            redis, acc_truth={"billing.monthly_spend": 5.0})
        await _seed(redis, engine, "billing.monthly_spend", 15.0)

        result = await engine.check(QuotaCheckRequest(
            org_id="org-free", resource_key="billing.monthly_spend", increment=1.0))

        assert result.decision == QuotaDecision.ALLOW
        assert result.current == 5.0
        detected = [m for m in metrics.metrics if m.name == "quota.drift_detected"]
        assert detected and detected[0].resource_type == "accumulator"
        assert detected[0].source == "ledger"

    @pytest.mark.asyncio
    async def test_accumulator_truly_over_denies(self, redis):
        """Ledger sum 10 == limit, +1 ⇒ genuinely over ⇒ DENY."""
        engine, _, _ = _build_engine(redis, acc_truth={"billing.monthly_spend": 10.0})
        await _seed(redis, engine, "billing.monthly_spend", 12.0)

        result = await engine.check(QuotaCheckRequest(
            org_id="org-free", resource_key="billing.monthly_spend", increment=1.0))
        assert result.decision == QuotaDecision.DENY
        assert result.current == 10.0  # repaired to the honest ledger sum


class TestFailOpenOnDrift:
    @pytest.mark.asyncio
    async def test_truth_source_down_allows_and_alerts(self, redis):
        """P1.2/D-31: truth source unreachable ⇒ ALLOW + alert, never block."""
        def _boom(org_id):
            raise RuntimeError("provider DB down")

        engine, _, alerts = _build_engine(redis, gauge_truth=_boom)
        await _seed(redis, engine, "compute.concurrent", 5.0)

        result = await engine.check(QuotaCheckRequest(
            org_id="org-free", resource_key="compute.concurrent"))

        assert result.decision == QuotaDecision.ALLOW
        assert result.reason == "recount_fail_open"
        # an alert was raised so the fail-open is visible
        assert any("unreachable" in a.message for a in alerts.alerts)

    @pytest.mark.asyncio
    async def test_async_truth_source_down_allows(self, redis):
        async def _aboom(org_id):
            raise RuntimeError("async provider down")

        engine, _, alerts = _build_engine(redis, gauge_truth=_aboom)
        await _seed(redis, engine, "compute.concurrent", 5.0)
        result = await engine.check(QuotaCheckRequest(
            org_id="org-free", resource_key="compute.concurrent"))
        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_gauge_over_cache_truth_down_stays_open(self, redis):
        """Explicit gauge contrast to the accumulator case below: source down ⇒
        gauge FAILS OPEN (allow), reason recount_fail_open."""
        def _boom(org_id):
            raise RuntimeError("gauge provider down")

        engine, _, _ = _build_engine(redis, gauge_truth=_boom)
        await _seed(redis, engine, "compute.concurrent", 5.0)
        result = await engine.check(QuotaCheckRequest(
            org_id="org-free", resource_key="compute.concurrent"))
        assert result.decision == QuotaDecision.ALLOW
        assert result.reason == "recount_fail_open"

    @pytest.mark.asyncio
    async def test_accumulator_over_cache_truth_down_keeps_deny(self, redis):
        """Money-safety: an ACCUMULATOR cache is the durable ledger sum (no
        lost-decrement drift). On a truth-source outage it must NOT fail open —
        keep the deny (fail-to-last-known), with a distinct reason + an alert."""
        def _boom(org_id):
            raise RuntimeError("ledger source down")

        engine, _, alerts = _build_engine(redis, acc_truth=_boom)
        await _seed(redis, engine, "billing.monthly_spend", 15.0)  # over the 10 cap

        result = await engine.check(QuotaCheckRequest(
            org_id="org-free", resource_key="billing.monthly_spend", increment=1.0))

        assert result.decision == QuotaDecision.DENY  # NOT allowed — no overspend
        assert result.reason == "recount_source_unavailable_kept_deny"
        # still visible: an alert was raised for the source outage
        assert any("unreachable" in a.message for a in alerts.alerts)


# --------------------------------------------------------------------------- Phase 2

class TestAcquireRecountBeforeDeny:
    @pytest.mark.asyncio
    async def test_acquire_stale_high_gauge_admits_after_repair(self, redis):
        """acquire()'s atomic spend denies on a stale-high cache; recount repairs to
        the live truth and retries ⇒ admitted with an activation_id."""
        engine, metrics, _ = _build_engine(
            redis, gauge_truth={"compute.concurrent": {"total": 0.0, "per_user": {}}})
        await _seed(redis, engine, "compute.concurrent", 5.0)

        res = await engine.acquire("org-free", resource_key="compute.concurrent")
        assert res.admitted is True
        assert res.activation_id is not None
        # repaired then spent → 0 + 1
        counter = create_counter(redis, "org-free",
                                 engine._registry.require("compute.concurrent"))
        assert await counter.get() == 1.0
        assert any(m.name == "quota.drift_detected" for m in metrics.metrics)

    @pytest.mark.asyncio
    async def test_acquire_genuinely_over_stays_denied(self, redis):
        engine, _, _ = _build_engine(
            redis, gauge_truth={"compute.concurrent": {"total": 2.0, "per_user": {}}})
        await _seed(redis, engine, "compute.concurrent", 2.0)  # truly at limit
        res = await engine.acquire("org-free", resource_key="compute.concurrent")
        assert res.admitted is False

    @pytest.mark.asyncio
    async def test_acquire_truth_down_fails_open(self, redis):
        def _boom(org_id):
            raise RuntimeError("down")

        engine, _, alerts = _build_engine(redis, gauge_truth=_boom)
        await _seed(redis, engine, "compute.concurrent", 5.0)
        res = await engine.acquire("org-free", resource_key="compute.concurrent")
        assert res.admitted is True  # never block on an unverifiable cache
        assert any("unreachable" in a.message for a in alerts.alerts)


class TestReconcileOrg:
    @pytest.mark.asyncio
    async def test_heals_gauge_and_accumulator_with_before_after(self, redis):
        engine, metrics, _ = _build_engine(
            redis,
            gauge_truth={"compute.concurrent": {"total": 1.0, "per_user": {}}},
            acc_truth={"billing.monthly_spend": 3.0})
        await _seed(redis, engine, "compute.concurrent", 5.0)
        await _seed(redis, engine, "billing.monthly_spend", 15.0)

        report = await engine.reconcile_org("org-free")

        g = report["resources"]["compute.concurrent"]
        assert g["counter_type"] == "gauge"
        assert g["before"] == 5.0 and g["after"] == 1.0
        assert g["changed"] is True and g["status"] == "repaired" and g["source"] == "provider"

        a = report["resources"]["billing.monthly_spend"]
        assert a["counter_type"] == "accumulator"
        assert a["before"] == 15.0 and a["after"] == 3.0
        assert a["changed"] is True and a["status"] == "repaired" and a["source"] == "ledger"

        # RATE is skipped (window counter is truth)
        assert report["resources"]["api.requests"]["status"] == "skipped_rate"

        # both types emitted the drift metric
        types = {m.resource_type for m in metrics.metrics if m.name == "quota.drift_detected"}
        assert types == {"gauge", "accumulator"}

        # counters actually repaired
        gc = create_counter(redis, "org-free", engine._registry.require("compute.concurrent"))
        ac = create_counter(redis, "org-free", engine._registry.require("billing.monthly_spend"))
        assert await gc.get() == 1.0
        assert await ac.get() == 3.0

    @pytest.mark.asyncio
    async def test_idempotent_second_pass_in_sync(self, redis):
        engine, _, _ = _build_engine(
            redis, gauge_truth={"compute.concurrent": {"total": 1.0, "per_user": {}}})
        await _seed(redis, engine, "compute.concurrent", 5.0)

        first = await engine.reconcile_org("org-free", resource_key="compute.concurrent")
        assert first["resources"]["compute.concurrent"]["changed"] is True

        second = await engine.reconcile_org("org-free", resource_key="compute.concurrent")
        s = second["resources"]["compute.concurrent"]
        assert s["changed"] is False and s["status"] == "in_sync"
        assert s["before"] == 1.0 and s["after"] == 1.0

    @pytest.mark.asyncio
    async def test_truth_unavailable_leaves_counter_untouched(self, redis):
        def _boom(org_id):
            raise RuntimeError("down")

        engine, _, _ = _build_engine(redis, gauge_truth=_boom)
        await _seed(redis, engine, "compute.concurrent", 5.0)
        report = await engine.reconcile_org("org-free", resource_key="compute.concurrent")
        g = report["resources"]["compute.concurrent"]
        assert g["status"] == "truth_unavailable"
        assert g["before"] == 5.0 and g["after"] == 5.0  # untouched
        counter = create_counter(redis, "org-free",
                                 engine._registry.require("compute.concurrent"))
        assert await counter.get() == 5.0


class TestReadRepair:
    @pytest.mark.asyncio
    async def test_read_after_injected_drift_returns_true_value(self, redis):
        engine, _, _ = _build_engine(
            redis, gauge_truth={"compute.concurrent": {"total": 0.0, "per_user": {}}},
            throttle=0.0)
        await _seed(redis, engine, "compute.concurrent", 5.0)

        usage = await engine.get_usage("org-free")
        item = next(i for i in usage.resources if i.resource_key == "compute.concurrent")
        assert item.current == 0.0  # read-repaired

    @pytest.mark.asyncio
    async def test_read_repair_throttled(self, redis):
        holder = {"compute.concurrent": {"total": 0.0, "per_user": {}}}
        engine, _, _ = _build_engine(redis, gauge_truth=holder, throttle=300.0)
        await _seed(redis, engine, "compute.concurrent", 5.0)

        # first read repairs
        await engine.get_usage("org-free")
        counter = create_counter(redis, "org-free",
                                 engine._registry.require("compute.concurrent"))
        assert await counter.get() == 0.0

        # inject drift again; a second read inside the throttle window must NOT repair
        await counter.reset(7.0)
        usage = await engine.get_usage("org-free")
        item = next(i for i in usage.resources if i.resource_key == "compute.concurrent")
        assert item.current == 7.0  # throttled — stale value returned as-is
