"""W-T2 — ratified decisions D-51 / D-52 / D-53 (ticket 20260709).

These were framed by W-T2 as D-FRAME-2/1/3 and RATIFIED by the coordinator:

  D-51  ABSENCE MEANS UNKNOWN, NEVER AN AFFIRMATIVE VALUE — and it fails closed.
        (Same root as the D-49 health bug: absence treated as an affirmative value.)
        A MISSING resource_key in the provider result = "no observation" → skip +
        alert. An EXPLICIT `total: 0` = "I observed zero" → converge. Absence is not
        zero. A provider that RAISES safely skips+alerts; one that returns `{}` must
        NOT silently wipe the counter. Knob `reconcile.empty_provider =
        skip_and_alert | converge`, default `skip_and_alert`.

  D-52  Bound the provider. `reconcile.provider_timeout_seconds`; on timeout treat
        as UNREACHABLE (do nothing + alert), never fall back to the ledger.

  D-53  `reconcile.per_user_sum = validate_and_alert | trust`, default
        validate_and_alert. An inconsistent provider is a CONSUMER bug: converge
        from what it gave, and ALERT. Never silently pick a side.

Each has a red-first history (see the artifact); the D-52 case AUTO-PROMOTED from a
strict-xfail in test_reconcile_boundary_20260710.py.

fakeredis[lua] (lupa) is NOT Redis — logic proofs, not real-EVAL proofs.
"""
from __future__ import annotations

import asyncio
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
from ab0t_quota.reconcile import LibraryReconciler, ReconcileConfig
from ab0t_quota.alerts import AlertDispatcher, DriftAlertManager

pytestmark = pytest.mark.asyncio

RK = "sandbox.concurrent"
RK2 = "sandbox.other"
CONCURRENT = ResourceDef(service="test", resource_key=RK, display_name="Concurrent",
                         counter_type=CounterType.GAUGE, unit="sandboxes")
OTHER = ResourceDef(service="test", resource_key=RK2, display_name="Other",
                    counter_type=CounterType.GAUGE, unit="things")
TIERS = {"free": TierConfig(tier_id="free", display_name="Free",
                            limits={RK: TierLimits(limit=100), RK2: TierLimits(limit=100)})}


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.flushall()
    await r.aclose()


def _engine(redis, store, *, two_gauges=False):
    reg = ResourceRegistry()
    reg.register(CONCURRENT, OTHER) if two_gauges else reg.register(CONCURRENT)
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
            resource_key=rk, spend={rk: 1.0}, state=ActivationState.OPEN.value, opened_at=opened))


async def _set(redis, org, rk, val, *, user=None):
    g = GaugeCounter(redis, org, rk)
    await (g.reset_user(user, val) if user is not None else g.reset(val))


async def _get(redis, org, rk, *, user=None):
    g = GaugeCounter(redis, org, rk)
    return await (g.get_user(user) if user is not None else g.get())


# ===========================================================================
# D-51 — ABSENCE ≠ ZERO. Fails closed.
# ===========================================================================

class TestAbsenceIsUnknown:
    async def test_empty_dict_provider_skips_and_alerts_does_not_wipe(self, redis):
        """DEFAULT skip_and_alert: a provider returning `{}` observed NOTHING →
        the counter is LEFT ALONE and an alert fires. (Was: silently wiped to 0.)"""
        store = InMemoryActivationStore()
        eng = _engine(redis, store)
        await _open(store, "org-1", RK, n=3)
        await _set(redis, "org-1", RK, 3.0)
        mgr, rec = _mgr(redis)
        r = LibraryReconciler(eng, observed_usage_provider=lambda o: {},
                              drift_alerts=mgr, config=ReconcileConfig(activity_guard_seconds=0))
        res = await r.reconcile_org("org-1")
        assert await _get(redis, "org-1", RK) == 3.0, "an empty provider result must NOT wipe the gauge"
        assert res.skipped == "provider_incomplete"
        assert any("missing" in m.lower() or "no observation" in m.lower() for m in rec.messages())

    async def test_explicit_zero_still_converges(self, redis):
        """An EXPLICIT `total: 0` is a real observation of zero → converge to 0.
        This is the distinction D-51 rests on: 0 is a value, absence is not."""
        store = InMemoryActivationStore()
        eng = _engine(redis, store)
        await _open(store, "org-1", RK, n=3)
        await _set(redis, "org-1", RK, 3.0)
        r = LibraryReconciler(eng, observed_usage_provider=lambda o: {RK: {"total": 0.0}},
                              config=ReconcileConfig(activity_guard_seconds=0))
        await r.reconcile_org("org-1")
        assert await _get(redis, "org-1", RK) == 0.0, "explicit zero is observed → converge"

    async def test_absent_key_skips_only_that_key(self, redis):
        """With two gauges, a provider that reports one and OMITS the other:
        the reported one converges; the omitted one is skipped+alerted, its counter
        untouched. Absence is per-key, not all-or-nothing."""
        store = InMemoryActivationStore()
        eng = _engine(redis, store, two_gauges=True)
        await _set(redis, "org-1", RK, 9.0)     # will be reported as 2 → converges
        await _set(redis, "org-1", RK2, 7.0)    # omitted → untouched
        mgr, rec = _mgr(redis)
        r = LibraryReconciler(eng, observed_usage_provider=lambda o: {RK: {"total": 2.0}},
                              drift_alerts=mgr, config=ReconcileConfig(activity_guard_seconds=0))
        await r.reconcile_org("org-1")
        assert await _get(redis, "org-1", RK) == 2.0, "reported key converges"
        assert await _get(redis, "org-1", RK2) == 7.0, "omitted key is left alone (no observation)"

    async def test_converge_mode_opt_in_treats_absence_as_zero(self, redis):
        """`empty_provider=converge` restores the legacy behaviour for a consumer
        who has PROVEN their provider distinguishes 'no data' from 'zero'."""
        store = InMemoryActivationStore()
        eng = _engine(redis, store)
        await _set(redis, "org-1", RK, 3.0)
        r = LibraryReconciler(eng, observed_usage_provider=lambda o: {},
                              config=ReconcileConfig(activity_guard_seconds=0, empty_provider="converge"))
        await r.reconcile_org("org-1")
        assert await _get(redis, "org-1", RK) == 0.0, "converge mode: absence = zero (opt-in)"


# ===========================================================================
# D-52 — bound the provider; timeout = unreachable (do nothing + alert).
# ===========================================================================

class TestProviderTimeout:
    async def test_hanging_provider_times_out_and_does_nothing(self, redis):
        """An async provider that hangs past `provider_timeout_seconds` is treated
        as UNREACHABLE: counter untouched, alert fired. (No real hang — 0.1s bound.)"""
        store = InMemoryActivationStore()
        eng = _engine(redis, store)
        await _open(store, "org-1", RK, n=1)
        await _set(redis, "org-1", RK, 99.0)
        mgr, rec = _mgr(redis)

        async def hang(org): await asyncio.sleep(30)
        r = LibraryReconciler(eng, observed_usage_provider=hang, drift_alerts=mgr,
                              config=ReconcileConfig(activity_guard_seconds=0,
                                                     provider_timeout_seconds=0.1))
        res = await asyncio.wait_for(r.reconcile_org("org-1"), timeout=2.0)
        assert res.skipped == "provider_unreachable"
        assert await _get(redis, "org-1", RK) == 99.0, "timeout must NOT move the counter (D-31)"
        assert any("unreachable" in m for m in rec.messages())

    async def test_fast_async_provider_within_timeout_converges(self, redis):
        """[control] A prompt async provider under the bound works normally —
        the timeout is not a blanket refusal."""
        store = InMemoryActivationStore()
        eng = _engine(redis, store)
        await _open(store, "org-1", RK, n=1)
        await _set(redis, "org-1", RK, 99.0)

        async def quick(org):
            await asyncio.sleep(0)
            return {RK: {"total": 1.0}}
        r = LibraryReconciler(eng, observed_usage_provider=quick,
                              config=ReconcileConfig(activity_guard_seconds=0,
                                                     provider_timeout_seconds=5.0))
        await r.reconcile_org("org-1")
        assert await _get(redis, "org-1", RK) == 1.0


# ===========================================================================
# D-53 — per-user sum ≠ org total: converge from what it gave, and ALERT.
# ===========================================================================

class TestPerUserSumValidation:
    async def test_mismatch_alerts_and_still_converges(self, redis):
        """DEFAULT validate_and_alert: total=5 but per_user sums to 2 → org gauge
        set to 5, per-user to {u1:2}, AND a sum-mismatch alert fires (the provider
        is buggy; make it visible, never silently pick a side)."""
        store = InMemoryActivationStore()
        eng = _engine(redis, store)
        await _open(store, "org-1", RK, n=1)
        await _set(redis, "org-1", RK, 1.0)
        mgr, rec = _mgr(redis)
        r = LibraryReconciler(
            eng, observed_usage_provider=lambda o: {RK: {"total": 5.0, "per_user": {"u1": 2.0}}},
            drift_alerts=mgr, config=ReconcileConfig(activity_guard_seconds=0))
        await r.reconcile_org("org-1")
        assert await _get(redis, "org-1", RK) == 5.0
        assert await _get(redis, "org-1", RK, user="u1") == 2.0
        assert any("sum" in m.lower() and "mismatch" in m.lower() for m in rec.messages()), (
            "a per-user/total mismatch must ALERT (consumer bug made visible)")

    async def test_matching_sum_does_not_alert(self, redis):
        """[control] total=2, per_user sums to 2 → no mismatch alert. Proves the
        alert is load-bearing, not always-on."""
        store = InMemoryActivationStore()
        eng = _engine(redis, store)
        await _open(store, "org-1", RK, n=1)
        await _set(redis, "org-1", RK, 1.0)
        mgr, rec = _mgr(redis)
        r = LibraryReconciler(
            eng, observed_usage_provider=lambda o: {RK: {"total": 2.0, "per_user": {"u1": 2.0}}},
            drift_alerts=mgr, config=ReconcileConfig(activity_guard_seconds=0))
        await r.reconcile_org("org-1")
        assert not any("mismatch" in m.lower() for m in rec.messages())

    async def test_trust_mode_suppresses_the_mismatch_alert(self, redis):
        """`per_user_sum=trust` for a consumer who accepts the provider's numbers
        verbatim — no mismatch alert even when they disagree."""
        store = InMemoryActivationStore()
        eng = _engine(redis, store)
        await _open(store, "org-1", RK, n=1)
        await _set(redis, "org-1", RK, 1.0)
        mgr, rec = _mgr(redis)
        r = LibraryReconciler(
            eng, observed_usage_provider=lambda o: {RK: {"total": 5.0, "per_user": {"u1": 2.0}}},
            drift_alerts=mgr, config=ReconcileConfig(activity_guard_seconds=0, per_user_sum="trust"))
        await r.reconcile_org("org-1")
        assert not any("mismatch" in m.lower() for m in rec.messages())
