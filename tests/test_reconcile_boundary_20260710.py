"""W-T2 side-effects hardening — the RECONCILER's boundaries.

Ticket 20260709_ab0t_quota_systemic_integrity_redesign.
Boundaries crossed (D-40 table):
  * row #6 **the replica** — a per-process ledger force-setting a SHARED counter.
  * row #8 **the human** — the ``unbillable_live=N`` money alert must reach the
    CONFIGURED dispatcher (a sink), not a log-only default.

EXTENDS ``tests/test_reconcile_20260710.py`` (W-PY-C) — cited per D-25, NOT rewritten.
That suite proves the D-33 precedence law. This one attacks the EDGES the brief calls
out: the recent-activity window edge, the six provider failure shapes (more / fewer /
zero-while-open / empty-dict / raises / times-out), per-user sums that don't reconcile
to the org total, alert-pairing under cooldown, the money alert AT THE SINK, and the
replica refusal via ``run_once`` (not only the loop).

Framed decisions raised here (see the artifact): the empty-dict wipe, the hanging
provider (no timeout), and per-user≠total precedence.

fakeredis[lua] (lupa) is NOT Redis; these are logic proofs, not real-EVAL proofs.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
from datetime import datetime, timezone, timedelta

import fakeredis.aioredis
import pytest
import pytest_asyncio

from ab0t_quota.activations import Activation, ActivationState, InMemoryActivationStore
from ab0t_quota.counters.gauge import GaugeCounter
from ab0t_quota.engine import QuotaEngine
from ab0t_quota.models.core import CounterType, ResetPeriod, ResourceDef, TierConfig, TierLimits
from ab0t_quota.providers import StaticTierProvider
from ab0t_quota.registry import ResourceRegistry
from ab0t_quota import reconcile as reconcile_mod
from ab0t_quota.reconcile import LibraryReconciler, ReconcileConfig
from ab0t_quota.alerts import AlertDispatcher, DriftAlertManager, AlertSeverity

pytestmark = pytest.mark.asyncio

RK = "sandbox.concurrent"
CONCURRENT = ResourceDef(service="test", resource_key=RK, display_name="Concurrent",
                         counter_type=CounterType.GAUGE, unit="sandboxes")
COST = ResourceDef(service="test", resource_key="sandbox.monthly_cost",
                   display_name="Cost", counter_type=CounterType.ACCUMULATOR, unit="USD",
                   reset_period=ResetPeriod.MONTHLY, precision=2)
TIERS = {"free": TierConfig(tier_id="free", display_name="Free",
                            limits={RK: TierLimits(limit=100)})}


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.flushall()
    await r.aclose()


def _engine(redis, store):
    reg = ResourceRegistry()
    reg.register(CONCURRENT, COST)
    return QuotaEngine(redis=redis, tier_provider=StaticTierProvider({"org-1": "free"}),
                       registry=reg, tiers=TIERS, activation_store=store)


class _Recorder(AlertDispatcher):
    def __init__(self): self.alerts = []
    async def dispatch(self, alert): self.alerts.append(alert)
    def messages(self): return [a.message for a in self.alerts]


def _mgr(redis):
    rec = _Recorder()
    return DriftAlertManager(redis=redis, dispatchers=[rec], cooldown_seconds=600), rec


async def _open(store, org, rk, *, n=1, user=None, age_seconds=3600):
    opened = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
    for i in range(n):
        await store.put_open(Activation(
            activation_id=f"a_{org}_{rk}_{i}_{id(object())}", org_id=org, user_id=user,
            resource_key=rk, spend={rk: 1.0}, state=ActivationState.OPEN.value,
            opened_at=opened))


async def _set(redis, org, rk, val, *, user=None):
    g = GaugeCounter(redis, org, rk)
    await (g.reset_user(user, val) if user is not None else g.reset(val))


async def _get(redis, org, rk, *, user=None):
    g = GaugeCounter(redis, org, rk)
    return await (g.get_user(user) if user is not None else g.get())


# ===========================================================================
# RECENT-ACTIVITY GUARD EDGE — just inside, exactly at, just outside (frozen clock,
# because 'exactly at' is a knife-edge that live elapse tips outward, D-9 mirror).
# ===========================================================================

class _FrozenDT:
    fixed = None
    @classmethod
    def now(cls, tz=None): return cls.fixed
    @staticmethod
    def fromisoformat(s): return _dt.datetime.fromisoformat(s)


class TestRecentActivityGuardEdge:
    @pytest.mark.parametrize("age_delta,expect_skipped", [
        (-1, True),   # just inside the window  -> recent -> SKIP the force-set
        (0, True),    # exactly at the edge     -> `<= window` -> recent -> SKIP
        (+1, False),  # just outside            -> not recent -> force-set happens
    ])
    async def test_window_edge(self, redis, monkeypatch, age_delta, expect_skipped):
        """The guard is `(now - opened) <= window`. Frozen clock makes the edge
        deterministic. Inside/at → skip (provider lags creation); outside → heal.
        The two outcomes are each other's control: the SAME drift is skipped or
        healed purely on which side of the edge the activation sits."""
        window = 100
        fixed = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        _FrozenDT.fixed = fixed
        monkeypatch.setattr(reconcile_mod, "datetime", _FrozenDT)

        store = InMemoryActivationStore()
        eng = _engine(redis, store)
        opened = (fixed - timedelta(seconds=window + age_delta)).isoformat()
        await store.put_open(Activation(
            activation_id="a1", org_id="org-1", user_id=None, resource_key=RK,
            spend={RK: 1.0}, state=ActivationState.OPEN.value, opened_at=opened))
        await _set(redis, "org-1", RK, 99.0)  # drifted over-count

        r = LibraryReconciler(eng, config=ReconcileConfig(activity_guard_seconds=window))
        res = await r.reconcile_org("org-1")

        if expect_skipped:
            assert res.skipped == "recent_activity"
            assert await _get(redis, "org-1", RK) == 99.0, "recent → must NOT force-set"
        else:
            assert res.skipped is None
            assert await _get(redis, "org-1", RK) == 1.0, "outside window → heals to Σ open"


# ===========================================================================
# PROVIDER FAILURE SHAPES — more / fewer / zero-while-open / empty / raises / hangs.
# ===========================================================================

class TestProviderDisagreementShapes:
    async def test_provider_sees_MORE_is_unbillable_money_incident_at_the_sink(self, redis):
        """provider=3, ledger=1 → counter→3, CRITICAL `unbillable_live=2` reaches
        the CONFIGURED dispatcher (D-40 row #8: assert AT THE SINK, not the emitter)."""
        store = InMemoryActivationStore()
        eng = _engine(redis, store)
        await _open(store, "org-1", RK, n=1)
        await _set(redis, "org-1", RK, 1.0)
        mgr, rec = _mgr(redis)
        r = LibraryReconciler(eng, observed_usage_provider=lambda o: {RK: {"total": 3.0}},
                              drift_alerts=mgr, config=ReconcileConfig(activity_guard_seconds=0))
        res = await r.reconcile_org("org-1")
        assert await _get(redis, "org-1", RK) == 3.0
        assert RK in res.divergences
        crit = [a for a in rec.alerts if a.severity == AlertSeverity.CRITICAL]
        assert crit and "unbillable_live=2" in crit[0].message
        assert "cannot be settled" in crit[0].message.lower()

    async def test_provider_sees_FEWER_is_phantom_records_warning(self, redis):
        """provider=1, ledger=3 → counter→1 (reality wins), WARNING phantom_rows=2."""
        store = InMemoryActivationStore()
        eng = _engine(redis, store)
        await _open(store, "org-1", RK, n=3)
        await _set(redis, "org-1", RK, 3.0)
        mgr, rec = _mgr(redis)
        r = LibraryReconciler(eng, observed_usage_provider=lambda o: {RK: {"total": 1.0}},
                              drift_alerts=mgr, config=ReconcileConfig(activity_guard_seconds=0))
        await r.reconcile_org("org-1")
        assert await _get(redis, "org-1", RK) == 1.0
        warn = [a for a in rec.alerts if a.severity == AlertSeverity.WARNING]
        assert warn and "phantom" in warn[0].message.lower()

    async def test_provider_ZERO_while_ledger_open_wipes_counter_to_zero(self, redis):
        """provider says total=0 while the ledger has 3 OPEN → the provider is
        authoritative for existence (D-33) → counter→0. This is reality saying
        'nothing is live'; the ledger rows are phantom (WARNING)."""
        store = InMemoryActivationStore()
        eng = _engine(redis, store)
        await _open(store, "org-1", RK, n=3)
        await _set(redis, "org-1", RK, 3.0)
        mgr, rec = _mgr(redis)
        r = LibraryReconciler(eng, observed_usage_provider=lambda o: {RK: {"total": 0.0}},
                              drift_alerts=mgr, config=ReconcileConfig(activity_guard_seconds=0))
        await r.reconcile_org("org-1")
        assert await _get(redis, "org-1", RK) == 0.0

    async def test_provider_EMPTY_DICT_skips_and_alerts_D51(self, redis):
        """RESOLVED as **D-51** (ratified): a provider returning ``{}`` observed
        NOTHING — absence is UNKNOWN, never an affirmative zero — so the counter is
        LEFT ALONE (not wiped) and an alert fires. Contrast the RAISES case (also
        safe). Full knob coverage in test_reconcile_absence_and_bounds_20260710.py.
        (Previously this pinned the wipe as the tripwire for the framed decision.)"""
        store = InMemoryActivationStore()
        eng = _engine(redis, store)
        await _open(store, "org-1", RK, n=3)
        await _set(redis, "org-1", RK, 3.0)
        mgr, rec = _mgr(redis)
        r = LibraryReconciler(eng, observed_usage_provider=lambda o: {}, drift_alerts=mgr,
                              config=ReconcileConfig(activity_guard_seconds=0))
        res = await r.reconcile_org("org-1")
        assert await _get(redis, "org-1", RK) == 3.0, "an empty provider result must NOT wipe (D-51)"
        assert res.skipped == "provider_incomplete"
        assert any("missing" in m.lower() or "no observation" in m.lower() for m in rec.messages())

    async def test_provider_RAISES_counter_does_not_move_and_alerts_at_sink(self, redis):
        """D-31/D-33 §5: an unreachable provider → do NOTHING (never fall back to
        the ledger) + alert. The drifted counter stays exactly where it was."""
        store = InMemoryActivationStore()
        eng = _engine(redis, store)
        await _open(store, "org-1", RK, n=1)   # ledger=1
        await _set(redis, "org-1", RK, 99.0)   # drifted

        def boom(org): raise RuntimeError("provider DB down")
        mgr, rec = _mgr(redis)
        r = LibraryReconciler(eng, observed_usage_provider=boom, drift_alerts=mgr,
                              config=ReconcileConfig(activity_guard_seconds=0))
        res = await r.reconcile_org("org-1")
        assert res.skipped == "provider_unreachable"
        assert await _get(redis, "org-1", RK) == 99.0, (
            "counter MUST NOT move — not to the ledger's 1, not anywhere (D-31)")
        assert any("unreachable" in m for m in rec.messages())

    async def test_hanging_provider_is_bounded_and_treated_as_unreachable(self, redis):
        """AUTO-PROMOTED from strict-xfail by **D-52** (ratified): a hanging async
        provider is bounded by `provider_timeout_seconds`; the timeout is handled by
        the unreachable path — do nothing + alert (D-31), never fall back to the
        ledger. The outer 2s wait only guards the test from a real hang; the inner
        0.1s bound is what fires."""
        store = InMemoryActivationStore()
        eng = _engine(redis, store)
        await _open(store, "org-1", RK, n=1)
        await _set(redis, "org-1", RK, 99.0)

        async def hang(org): await asyncio.sleep(30)
        r = LibraryReconciler(eng, observed_usage_provider=hang,
                              config=ReconcileConfig(activity_guard_seconds=0,
                                                     provider_timeout_seconds=0.1))
        res = await asyncio.wait_for(r.reconcile_org("org-1"), timeout=2.0)
        assert res.skipped == "provider_unreachable"
        assert await _get(redis, "org-1", RK) == 99.0, "timeout must not move the counter"


# ===========================================================================
# PER-USER TOTALS THAT DON'T SUM TO THE ORG TOTAL — which wins? (framed)
# ===========================================================================

class TestPerUserSumMismatch:
    async def test_org_total_and_per_user_mismatch_alerts_D53(self, redis):
        """RESOLVED as **D-53** (ratified): provider reports org total=5 but per_user
        sums to 2 — a CONSUMER bug. The reconciler converges from what it gave (org
        gauge=5, per-user {u1:2}) AND alerts, never silently picking a side. (This
        pinned the un-validated disagreement as the framed tripwire.)"""
        store = InMemoryActivationStore()
        eng = _engine(redis, store)
        await _open(store, "org-1", RK, n=1)
        await _set(redis, "org-1", RK, 1.0)
        mgr, rec = _mgr(redis)
        r = LibraryReconciler(
            eng, observed_usage_provider=lambda o: {RK: {"total": 5.0, "per_user": {"u1": 2.0}}},
            drift_alerts=mgr, config=ReconcileConfig(activity_guard_seconds=0))
        await r.reconcile_org("org-1")
        assert await _get(redis, "org-1", RK) == 5.0, "org total wins for the org gauge"
        assert await _get(redis, "org-1", RK, user="u1") == 2.0, "per-user set to its own value"
        assert any("mismatch" in m.lower() for m in rec.messages()), "the mismatch must ALERT (D-53)"


# ===========================================================================
# ALERT PAIRING (D-36 / FUTURE §5) — resolve fires on heal, is NOT suppressed by
# the detect cooldown, and repeated force-sets do NOT alert-storm.
# ===========================================================================

class TestAlertPairing:
    async def test_resolve_fires_on_heal_even_within_detect_cooldown(self, redis):
        """Pass 1 drifts → detected + active marker. Pass 2 is in-sync → resolved
        fires DESPITE the detect cooldown still being live (resolve keys on the
        active marker, never on the detect cooldown)."""
        store = InMemoryActivationStore()
        eng = _engine(redis, store)
        await _open(store, "org-1", RK, n=3)
        await _set(redis, "org-1", RK, 99.0)          # drift
        mgr, rec = _mgr(redis)
        r = LibraryReconciler(eng, drift_alerts=mgr,
                              config=ReconcileConfig(activity_guard_seconds=0))

        await r.reconcile_org("org-1")                # heals 99 -> 3, detected
        assert any("gauge_drift_detected" in m for m in rec.messages())
        await r.reconcile_org("org-1")                # now in sync -> resolved
        assert any("gauge_drift_resolved" in m for m in rec.messages()), (
            "a resolve must fire on heal and must not be swallowed by the detect cooldown")

    async def test_repeated_force_sets_do_not_alert_storm(self, redis):
        """Two consecutive passes each needing a force-set fire the DETECT alert
        only ONCE (rate-limited by the cooldown) — the counter is fixed every pass,
        the human is paged once."""
        store = InMemoryActivationStore()
        eng = _engine(redis, store)
        await _open(store, "org-1", RK, n=3)
        mgr, rec = _mgr(redis)
        r = LibraryReconciler(eng, drift_alerts=mgr,
                              config=ReconcileConfig(activity_guard_seconds=0))

        await _set(redis, "org-1", RK, 99.0)
        await r.reconcile_org("org-1")                # detected #1, counter -> 3
        await _set(redis, "org-1", RK, 88.0)          # drift AGAIN
        await r.reconcile_org("org-1")                # force-set again, but rate-limited
        detected = [m for m in rec.messages() if "gauge_drift_detected" in m
                    and "divergence" not in m]
        assert len(detected) == 1, f"alert storm: {len(detected)} detects (expected 1)"
        assert await _get(redis, "org-1", RK) == 3.0, "the counter is still corrected each pass"


# ===========================================================================
# THE REPLICA BOUNDARY (D-37) — run_once REFUSES a per-process ledger, not only
# the background loop. Two replicas, two in-memory ledgers, one shared counter.
# ===========================================================================

class TestReplicaBoundaryRunOnce:
    async def test_run_once_refuses_in_memory_ledger_and_leaves_shared_counter(self, redis):
        """Two engines, each with its OWN InMemoryActivationStore, share one Redis
        counter. A per-replica ledger sees only its own activations; converging the
        SHARED counter from that partial view UNDER-counts (D-31). `run_once` (a
        public API a cron might call) must REFUSE — guarding the operation, not just
        the scheduler."""
        store_a = InMemoryActivationStore()   # replica A sees 2 open
        eng_a = _engine(redis, store_a)
        await _open(store_a, "org-1", RK, n=2)
        await _set(redis, "org-1", RK, 5.0)   # shared counter (true level, say, 5)

        r = LibraryReconciler(eng_a, config=ReconcileConfig(activity_guard_seconds=0))
        res = await r.run_once(["org-1"])
        assert res.skipped == "ledger_not_durable", "run_once must refuse an in-memory ledger"
        assert await _get(redis, "org-1", RK) == 5.0, (
            "the shared counter must NOT be forced down to replica A's partial view of 2")

    async def test_force_true_bypasses_the_refusal(self, redis):
        """`force=True` is the explicit acknowledgement that the caller accepts a
        partial view (a single-process tool/test). It converges — proving the
        refusal above is a real gate, not an inert default."""
        store = InMemoryActivationStore()
        eng = _engine(redis, store)
        await _open(store, "org-1", RK, n=2)
        await _set(redis, "org-1", RK, 5.0)
        r = LibraryReconciler(eng, config=ReconcileConfig(activity_guard_seconds=0))
        res = await r.run_once(["org-1"], force=True)
        assert res.skipped != "ledger_not_durable"
        assert await _get(redis, "org-1", RK) == 2.0, "force=True converges to the local view"
