"""W-T1 — adversarial test-hardening of the activation-core ALGEBRA.

Ticket 20260709_ab0t_quota_systemic_integrity_redesign. Companion artifact:
information_tests_activation_algebra_20260710.md.

This module attacks ``engine.acquire`` / ``engine.release`` / ``engine.settle`` and
the activation ledger with diverse, edge-case-heavy, adversarial cases the happy-path
suite (test_activations_20260710, test_crash_fail_closed_20260710) cannot see. It is
NOT a re-run of those; every test here targets a state-machine corner, a numeric edge,
a bundle-algebra corner, or the fail-direction invariant (D-31), and each property
ships with a NEGATIVE CONTROL that proves the assertion has teeth.

Governing rule under test — D-31 (the fail-direction invariant):
    An IO error or a weird input may never silently WIDEN a limit or ERASE a spend.
    Over-count / DENY is the only acceptable silent direction.

Defects this module found and reproduces (red-first, then fixed in engine.acquire):
  * FAIL-OPEN 1: acquire() admitted an UNKNOWN bundle / unregistered resource_key
    (a config typo), silently disabling enforcement — while check_for_bundle() denies
    it (D-14/QP-02). Fixed: acquire honors the same unknown-bundle law.
  * FAIL-OPEN 2: acquire() ignored global_kill_switch=True and kept ADMITTING while
    an operator's incident "halt everything" lever was flipped (D-15). check() denies.
    Fixed: acquire fails closed on the kill switch.

Framed (not decided here — see the artifact / DECISIONS D-43..D-45 candidates):
  * duplicate resource_key in a bundle over-spends the counter past its limit while
    the ledger records only one unit (set vs multiset semantics) — xfail(strict).
  * settle() twice with DIFFERENT costs: first cost wins silently, no conflict signal.
  * settle() accepts non-finite / negative cost strings verbatim (nan/inf/-5).
"""
from __future__ import annotations

import asyncio
import math
import random

import fakeredis.aioredis
import pytest
import pytest_asyncio

from ab0t_quota.activations import (
    Activation, ActivationState, InMemoryActivationStore, RedisActivationStore,
    mint_activation_id,
)
from ab0t_quota.counters.gauge import GaugeCounter
from ab0t_quota.engine import QuotaEngine
from ab0t_quota.models.core import (
    CounterType, EnforcementConfig, ResourceDef, TierConfig, TierLimits,
)
from ab0t_quota.providers import StaticTierProvider
from ab0t_quota.registry import ResourceRegistry


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.flushall()
    await r.aclose()


CONC = ResourceDef(service="t", resource_key="rk.c", display_name="C",
                   counter_type=CounterType.GAUGE, unit="u")
GPU = ResourceDef(service="t", resource_key="rk.g", display_name="G",
                  counter_type=CounterType.GAUGE, unit="u")

# The bundles: "one" = single gauge; "both" = two DISTINCT gauges; "dup" = the same
# gauge listed TWICE (the multiset-semantics landmine); "empty" = declared-but-empty.
BUNDLES = {
    "one": ["rk.c"],
    "both": ["rk.c", "rk.g"],
    "dup": ["rk.c", "rk.c"],
    "empty": [],
}


def _engine(redis, *, store=None, limit=1, enforcement=None, bundles=None):
    reg = ResourceRegistry()
    reg.register(CONC, GPU)
    tiers = {"free": TierConfig(
        tier_id="free", display_name="F",
        limits={"rk.c": TierLimits(limit=limit), "rk.g": TierLimits(limit=limit)})}
    return QuotaEngine(
        redis=redis, tier_provider=StaticTierProvider({"o": "free"}),
        registry=reg, tiers=tiers, resource_bundles=bundles or BUNDLES,
        activation_store=store or InMemoryActivationStore(),
        enforcement=enforcement,
    )


async def _counter(redis, rk="rk.c"):
    return await GaugeCounter(redis, "o", rk).get()


# ===========================================================================
# DEFECT 1 — acquire() must FAIL CLOSED on an unknown bundle / resource (D-14).
# ===========================================================================
class TestAcquireUnknownBundleFailsClosed:
    """QP-02/D-14 lives in check_for_bundle(); it was MISSING from acquire(), the
    atomic gate the redesign promotes as the retry-safe replacement. A typo'd bundle
    name silently disabled enforcement on the create path. RED before the fix."""

    @pytest.mark.asyncio
    async def test_unknown_bundle_is_denied_by_default(self, redis):
        engine = _engine(redis)  # default enforcement: unknown_bundle="deny"
        res = await engine.acquire("o", "typo_bundle_xyz")
        assert res.admitted is False, (
            "acquire ADMITTED an undeclared bundle — a config typo silently "
            "disabled enforcement (QP-02/D-14 fail-open)")
        assert res.activation_id is None
        assert res.reason == "unknown_bundle"

    @pytest.mark.asyncio
    async def test_unknown_single_resource_key_is_denied(self, redis):
        engine = _engine(redis)
        res = await engine.acquire("o", resource_key="not.registered")
        assert res.admitted is False, "acquire admitted an UNREGISTERED resource_key"
        assert res.reason == "unknown_bundle"

    @pytest.mark.asyncio
    async def test_declared_empty_bundle_is_still_admitted(self, redis):
        """Distinguish an UNKNOWN bundle (typo → deny) from a DELIBERATELY empty one
        (declared []→ nothing to gate → admit). The fix must not conflate them."""
        engine = _engine(redis)
        res = await engine.acquire("o", "empty")
        assert res.admitted is True and res.reason != "unknown_bundle"

    @pytest.mark.asyncio
    async def test_negative_control_allow_warn_flips_to_admit(self, redis):
        """NEGATIVE CONTROL for the deny assertion: the same unknown bundle under
        unknown_bundle='allow_warn' must ADMIT. If deny and allow_warn produced the
        same outcome, the deny test above would be decorative."""
        engine = _engine(redis, enforcement=EnforcementConfig(unknown_bundle="allow_warn"))
        res = await engine.acquire("o", "typo_bundle_xyz")
        assert res.admitted is True, "allow_warn must admit — proves the knob is live"

    @pytest.mark.asyncio
    async def test_shadow_mode_admits_unknown_bundle(self, redis):
        engine = _engine(redis, enforcement=EnforcementConfig(shadow_mode=True))
        res = await engine.acquire("o", "typo_bundle_xyz")
        assert res.admitted is True, "shadow_mode forces allow_warn (D-14 rollout path)"


# ===========================================================================
# DEFECT 2 — acquire() must FAIL CLOSED under the global kill switch (D-15).
# ===========================================================================
class TestAcquireHonorsGlobalKillSwitch:
    """global_kill_switch=True means 'halt every admission' (fail closed). check()
    honors it; acquire() bypassed it and kept granting during an incident. RED
    before the fix. This is the forbidden fail-open direction (D-31): a config that
    means DENY silently widened to ALLOW."""

    @pytest.mark.asyncio
    async def test_kill_switch_denies_acquire(self, redis):
        engine = _engine(redis, limit=100,
                         enforcement=EnforcementConfig(global_kill_switch=True))
        res = await engine.acquire("o", "one")
        assert res.admitted is False, (
            "kill switch flipped but acquire ADMITTED — the incident lever is a no-op")
        assert res.reason == "global_kill_switch"
        assert await _counter(redis) == 0.0, "denied acquire must not spend"

    @pytest.mark.asyncio
    async def test_negative_control_kill_switch_off_admits(self, redis):
        """NEGATIVE CONTROL: with the switch OFF the SAME acquire must admit — proves
        the deny above is caused by the switch, not by some unrelated denial."""
        engine = _engine(redis, limit=100,
                         enforcement=EnforcementConfig(global_kill_switch=False))
        res = await engine.acquire("o", "one")
        assert res.admitted is True


# ===========================================================================
# All-or-nothing bundles.
# ===========================================================================
class TestAllOrNothingBundles:
    @pytest.mark.asyncio
    async def test_nth_over_limit_spends_nothing(self, redis):
        """A 2-resource bundle where the SECOND exceeds its limit: NOTHING spent —
        not the first. (Not N-1 things.)"""
        engine = _engine(redis, limit=1)
        # exhaust rk.g via its own single acquire so the bundle's 2nd leg fails
        assert (await engine.acquire("o", resource_key="rk.g")).admitted
        res = await engine.acquire("o", "both")
        assert res.admitted is False and res.denied_resource == "rk.g"
        assert await _counter(redis, "rk.c") == 0.0, "first leg must NOT be spent"

    @pytest.mark.asyncio
    async def test_two_bundle_racers_at_limit_one_loser_spends_nothing(self, redis):
        """Two bundle acquires race at limit-1 → exactly one admitted; the loser
        spent NOTHING on either leg."""
        engine = _engine(redis, limit=1)
        barrier = asyncio.Barrier(2)
        out = []

        async def go():
            await barrier.wait()
            out.append(await engine.acquire("o", "both"))

        await asyncio.gather(go(), go())
        admitted = [r for r in out if r.admitted]
        assert len(admitted) == 1
        assert await _counter(redis, "rk.c") == 1.0
        assert await _counter(redis, "rk.g") == 1.0

    @pytest.mark.asyncio
    async def test_empty_bundle_admits_and_release_is_clean(self, redis):
        store = InMemoryActivationStore()
        engine = _engine(redis, store=store)
        res = await engine.acquire("o", "empty")
        assert res.admitted and res.activation_id is not None
        # nothing to decrement; release still marks it released idempotently
        assert await engine.release(res.activation_id) is True
        assert await engine.release(res.activation_id) is False

    @pytest.mark.asyncio
    async def test_duplicate_resource_key_preserves_core_invariant(self, redis):
        """PROMOTED from xfail — D-45 fixed. A duplicate resource_key in a bundle used
        to over-spend the counter past its limit (2 for limit=1) while the ledger
        recorded ONE unit — breaking counter == Σ open activations in steady state.
        Runtime dedup (engine._gauge_specs) now counts it once; config load rejects it
        outright (see TestBundleConfigRejectsDuplicateKey)."""
        store = InMemoryActivationStore()
        engine = _engine(redis, store=store, limit=1, bundles={"dup": ["rk.c", "rk.c"]})
        res = await engine.acquire("o", "dup")
        counter = await _counter(redis, "rk.c")
        row = await store.get(res.activation_id) if res.activation_id else None
        ledger_spend = float((row.spend or {}).get("rk.c", 0.0)) if row else 0.0
        assert res.admitted is True
        assert counter <= 1.0, f"counter {counter} exceeded limit 1 via duplicate key"
        assert counter == ledger_spend == 1.0, (
            f"counter {counter} != ledger spend {ledger_spend} after a clean acquire")

    @pytest.mark.asyncio
    async def test_negative_control_duplicate_would_overspend_without_dedup(self, redis):
        """NEGATIVE CONTROL for D-45: reach past the engine's dedup by driving the
        atomic Lua with two specs for the same gauge directly. It over-spends to 2.0 —
        proving the dedup is what holds the invariant, not luck."""
        engine = _engine(redis, limit=1)
        g = GaugeCounter(redis, "o", "rk.c")
        spec = {"resource_key": "rk.c", "gauge": g, "delta": 1.0,
                "org_limit": 1.0, "user_limit": None, "has_user": False}
        admitted, _ = await engine._atomic_bundle_spend([spec, dict(spec)], None, None)
        assert admitted is True and await g.get() == 2.0, (
            "two un-deduped specs for one gauge over-spend past the limit — the exact "
            "defect D-45's dedup prevents")


class TestBundleConfigRejectsDuplicateKey:
    """D-45 (config half): a bundle naming a resource twice is a config error and is
    REJECTED at load (D-41), not silently interpreted."""

    def test_load_resource_bundles_rejects_duplicate(self):
        from ab0t_quota.config import load_resource_bundles
        with pytest.raises(ValueError, match="duplicate"):
            load_resource_bundles({"resource_bundles": {"dup": ["rk.c", "rk.c"]}})

    def test_load_resource_bundles_accepts_distinct(self):
        from ab0t_quota.config import load_resource_bundles
        out = load_resource_bundles({"resource_bundles": {"ok": ["rk.c", "rk.g"]}})
        assert out == {"ok": ["rk.c", "rk.g"]}


# ===========================================================================
# State machine + idempotence FOREVER (no TTL horizon) — D-27 / QI-05.
# ===========================================================================
class TestStateMachineIdempotence:
    @pytest.mark.asyncio
    async def test_release_of_never_minted_id(self, redis):
        engine = _engine(redis)
        assert await engine.release("act_does_not_exist") is False
        assert await engine.release(mint_activation_id()) is False  # minted, never put

    @pytest.mark.asyncio
    async def test_release_ten_times(self, redis):
        engine = _engine(redis, limit=100)
        r = await engine.acquire("o", "one", user_id="u1")
        results = [await engine.release(r.activation_id) for _ in range(10)]
        assert results == [True] + [False] * 9, "exactly one real release; 9 no-ops"
        assert await GaugeCounter(redis, "o", "rk.c").get_user("u1") == 0.0

    @pytest.mark.asyncio
    async def test_release_ten_times_concurrently_releases_once(self, redis):
        """Concurrent duplicate delivery of the same release: the gauge returns to
        exactly zero, never below (no phantom headroom)."""
        engine = _engine(redis, limit=100)
        r = await engine.acquire("o", "one", user_id="u1")
        trues = sum(await asyncio.gather(*[engine.release(r.activation_id)
                                           for _ in range(10)]))
        assert trues == 1, "exactly one concurrent release performed the transition"
        assert await GaugeCounter(redis, "o", "rk.c").get_user("u1") == 0.0

    @pytest.mark.asyncio
    async def test_settle_before_release_then_release_is_noop_overcounts(self, redis):
        """settle from OPEN (before release) drops the ledger row to SETTLED; a later
        release then finds no OPEN row → no-op, counter NOT decremented → OVER-count.
        Fail-closed direction (D-31): the slot heals via converge, never under-counts."""
        store = InMemoryActivationStore()
        engine = _engine(redis, store=store, limit=100)
        r = await engine.acquire("o", "one")
        assert await engine.settle(r.activation_id, "0.10") is True
        assert await engine.release(r.activation_id) is False, "already settled → no-op"
        counter = await _counter(redis)
        n_open = await store.count_open("o")
        assert counter == 1.0 and n_open == 0
        assert counter >= n_open, "settle-before-release is over-count, never under"

    @pytest.mark.asyncio
    async def test_settle_after_release_records_cost(self, redis):
        store = InMemoryActivationStore()
        engine = _engine(redis, store=store, limit=100)
        r = await engine.acquire("o", "one")
        assert await engine.release(r.activation_id) is True
        assert await engine.settle(r.activation_id, "0.42") is True
        row = await store.get(r.activation_id)
        assert row.state == ActivationState.SETTLED.value and row.cost == "0.42"
        assert await _counter(redis) == 0.0, "normal order settles cleanly at zero"

    @pytest.mark.asyncio
    async def test_settle_twice_same_cost_idempotent(self, redis):
        engine = _engine(redis, limit=100)
        r = await engine.acquire("o", "one")
        assert await engine.settle(r.activation_id, "0.30") is True
        assert await engine.settle(r.activation_id, "0.30") is False

    @pytest.mark.asyncio
    async def test_settle_of_never_minted_id(self, redis):
        engine = _engine(redis)
        assert await engine.settle("act_ghost", "1.00") is False

    @pytest.mark.asyncio
    async def test_acquire_crash_release_of_never_persisted_id(self, redis):
        """acquire → crash before persist → the minted id never landed. release() of
        that id is a clean no-op (not an exception, not an under-count)."""
        class _Boom(InMemoryActivationStore):
            async def put_open(self, a):
                raise RuntimeError("persist crashed")

        store = _Boom()
        engine = _engine(redis, store=store, limit=100)
        with pytest.raises(RuntimeError):
            await engine.acquire("o", "one", user_id="u1")
        # the caller has no activation_id (acquire re-raised, D-28). A speculative
        # release of a freshly-minted id is a safe no-op.
        assert await engine.release(mint_activation_id()) is False


class TestSettleDifferentCostAlerts:
    """D-46: settle twice with DIFFERENT costs keeps first-wins idempotence but emits a
    loud `settle_conflict` alert carrying both values — never silently discarded."""

    @pytest.mark.asyncio
    async def test_first_cost_wins_and_conflict_is_alerted(self, redis):
        from ab0t_quota.alerts import AlertManager
        store = InMemoryActivationStore()
        engine = _engine(redis, store=store, limit=100)
        fired = []

        class _Cap(AlertManager):
            def __init__(self):
                pass

            async def maybe_alert(self, alert):
                fired.append(alert)

        engine._alert_manager = _Cap()
        r = await engine.acquire("o", "one")
        assert await engine.settle(r.activation_id, "0.10") is True
        assert await engine.settle(r.activation_id, "9.99") is False, "second is a no-op"
        row = await store.get(r.activation_id)
        assert row.cost == "0.10", "first cost wins; 9.99 is NOT overwritten"
        assert [a.message for a in fired] == ["settle_conflict"], (
            "a differing re-settle must fire settle_conflict, not vanish (D-46)")

    @pytest.mark.asyncio
    async def test_same_cost_resettle_is_not_a_conflict(self, redis):
        """NEGATIVE CONTROL: an identical re-settle must NOT fire a conflict — proves
        the alert distinguishes a real cost disagreement from a benign replay."""
        from ab0t_quota.alerts import AlertManager
        store = InMemoryActivationStore()
        engine = _engine(redis, store=store, limit=100)
        fired = []

        class _Cap(AlertManager):
            def __init__(self):
                pass

            async def maybe_alert(self, alert):
                fired.append(alert)

        engine._alert_manager = _Cap()
        r = await engine.acquire("o", "one")
        assert await engine.settle(r.activation_id, "0.10") is True
        assert await engine.settle(r.activation_id, "0.10") is False
        assert fired == [], "an identical re-settle is a benign replay, not a conflict"


# ===========================================================================
# Numeric edges — the side-effects live here (D-31).
# ===========================================================================
class TestSettleNumericEdges:
    """D-47: settle() rejects non-finite / negative costs fail-closed (typed error)
    BEFORE any ledger write. NaN poisons a money accumulator irrecoverably; a negative
    cost is a refund wearing usage's clothes. Valid finite non-negative costs store."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cost", ["0", "1e30", "0.1", "0.30", "12345.6789"])
    async def test_settle_stores_valid_cost(self, redis, cost):
        store = InMemoryActivationStore()
        engine = _engine(redis, store=store, limit=100)
        r = await engine.acquire("o", "one")
        assert await engine.settle(r.activation_id, cost) is True
        row = await store.get(r.activation_id)
        assert row.cost == cost

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cost", ["nan", "inf", "-inf", "-5.0", "-0.01", "not_a_number"])
    async def test_settle_rejects_bad_cost_fail_closed(self, redis, cost):
        from ab0t_quota.engine import InvalidSettlementCost
        store = InMemoryActivationStore()
        engine = _engine(redis, store=store, limit=100)
        r = await engine.acquire("o", "one")
        with pytest.raises(InvalidSettlementCost):
            await engine.settle(r.activation_id, cost)
        # fail-closed: the activation stays UNsettled (the drift alarm), no poison landed
        row = await store.get(r.activation_id)
        assert row.state == ActivationState.OPEN.value and row.cost is None

    @pytest.mark.asyncio
    async def test_settle_float_nan_is_rejected_not_stringified(self, redis):
        """`str(float('nan'))` == 'nan' — the rejection must catch the float form too,
        not just the literal string."""
        from ab0t_quota.engine import InvalidSettlementCost
        engine = _engine(redis, limit=100)
        r = await engine.acquire("o", "one")
        for bad in (float("nan"), float("inf"), -5.0):
            with pytest.raises(InvalidSettlementCost):
                await engine.settle(r.activation_id, bad)

    @pytest.mark.asyncio
    async def test_settle_does_not_touch_the_counter(self, redis):
        """A settle must never move the gauge — it touches only the ledger.
        D-31: a weird input may not erase a spend."""
        store = InMemoryActivationStore()
        engine = _engine(redis, store=store, limit=100)
        r = await engine.acquire("o", "one")
        before = await _counter(redis)
        await engine.settle(r.activation_id, "0.30")
        assert await _counter(redis) == before == 1.0

    @pytest.mark.asyncio
    async def test_release_bigger_than_acquire_floors_at_zero(self, redis):
        """A gauge decrement larger than the outstanding level floors at zero — it can
        never manufacture negative (= free) headroom (QG-06 class, D-31)."""
        g = GaugeCounter(redis, "o", "rk.c")
        await g.increment(1.0)
        v = await g.decrement(5.0)  # over-release
        assert v == 0.0, "decrement floors at zero, never negative"
        assert await g.get() == 0.0

    @pytest.mark.asyncio
    async def test_gauge_negative_delta_is_magnitude_only(self, redis):
        """The gauge takes |delta|: a negative increment can NOT secretly decrement
        (which would erase a spend — forbidden). Pins the abs() contract."""
        g = GaugeCounter(redis, "o", "rk.c")
        await g.increment(2.0)
        await g.increment(-1.0)   # abs → +1, NOT -1
        assert await g.get() == 3.0, "negative delta treated as magnitude, never erases"

    @pytest.mark.asyncio
    async def test_float_precision_release_returns_to_zero(self, redis):
        """0.1 + 0.2 != 0.3 in IEEE754. A gauge driven by such deltas must still floor
        cleanly to zero on full release, not leave a phantom epsilon residue."""
        g = GaugeCounter(redis, "o", "rk.c")
        await g.increment(0.1)
        await g.increment(0.2)
        await g.decrement(0.3)
        # may land on a tiny epsilon; assert it is within float noise of zero and the
        # floor guarantees it can never be negative headroom.
        v = await g.get()
        assert abs(v) < 1e-9, f"precision residue {v} not ~0"
        assert v >= 0.0


# ===========================================================================
# The core invariant, adversarially — metamorphic + property angles.
# ===========================================================================
class TestCoreInvariantMetamorphic:
    @pytest.mark.asyncio
    async def test_acquire_then_release_is_counter_noop(self, redis):
        """Metamorphic: acquire immediately followed by release is a no-op on the
        counter, for a run of pairs."""
        store = InMemoryActivationStore()
        engine = _engine(redis, store=store, limit=100)
        for _ in range(20):
            r = await engine.acquire("o", "one", user_id="u1")
            await engine.release(r.activation_id)
        assert await _counter(redis) == 0.0
        assert await store.count_open("o") == 0

    @pytest.mark.parametrize("seed", [3, 17, 55, 233, 999])
    @pytest.mark.asyncio
    async def test_N_acquires_then_N_releases_any_permutation_returns_to_start(
            self, redis, seed):
        """N acquires, then those N releases in a RANDOM permutation, return the
        counter to the start value — release order is irrelevant (identity-keyed)."""
        store = InMemoryActivationStore()
        engine = _engine(redis, store=store, limit=100)
        N = 12
        ids = []
        for _ in range(N):
            r = await engine.acquire("o", "one", user_id="u1")
            assert r.admitted
            ids.append(r.activation_id)
        assert await _counter(redis) == float(N)
        rng = random.Random(seed)
        rng.shuffle(ids)
        # assert AFTER EVERY release that the invariant holds, not only at the end
        for i, aid in enumerate(ids):
            assert await engine.release(aid) is True
            counter = await _counter(redis)
            n_open = await store.count_open("o")
            assert counter == float(n_open), (
                f"invariant broke mid-permutation at step {i}, seed={seed}: "
                f"counter={counter} Σopen={n_open}")
        assert await _counter(redis) == 0.0

    @pytest.mark.asyncio
    async def test_negative_control_nonidempotent_release_undercounts(self, redis):
        """NEGATIVE CONTROL (the naiveLedger analogue). A release that decrements the
        gauge on EVERY call — bypassing the activation state transition — DOES
        under-count under replay. This proves the idempotent-release property tested
        above (test_release_ten_times*) has teeth: the broken variant fails the exact
        assertion the real tests pass."""
        engine = _engine(redis, limit=100)
        r = await engine.acquire("o", "one", user_id="u1")
        g = GaugeCounter(redis, "o", "rk.c")

        # naive: decrement directly, no idempotency key, no activation mark.
        for _ in range(3):
            await g.decrement_user("u1", 1.0)   # first floors to 0; rest stay 0 via floor
        # The floor SAVES the org value, but observe the property that WOULD fail
        # without identity: a second *distinct* activation's slot is erasable.
        r2 = await engine.acquire("o", "one", user_id="u1")   # a NEW, still-open slot
        # naive replay of the FIRST teardown now erases the SECOND's live slot:
        await g.decrement_user("u1", 1.0)
        naive_level = await g.get_user("u1")
        # r2 is still OPEN in the ledger, yet the naive decrement drove the gauge below
        # Σ open — the forbidden under-count. This is what identity-keyed release prevents.
        assert naive_level < await InMemoryActivationStore().count_open("o") + 1
        # concretely: the live r2 slot was erased to 0 by a replayed unrelated teardown
        assert naive_level == 0.0, (
            "naive non-idempotent release UNDER-counts a live activation — the "
            "negative control confirms the idempotent path is what prevents it")
        assert r2.admitted  # r2 was genuinely admitted and open


# ===========================================================================
# TTL semantics — an OPEN activation must NEVER TTL away (it is the drift alarm).
# ===========================================================================
class TestActivationTTLSemantics:
    @pytest.mark.asyncio
    async def test_open_row_has_no_expiry_released_does(self, redis):
        """OPEN rows are load-bearing (the drift alarm QB-03) and must persist
        forever; a RELEASED row gets a finite TTL and reaps itself."""
        store = RedisActivationStore(redis, released_ttl_s=123)
        aid = mint_activation_id()
        await store.put_open(Activation(activation_id=aid, org_id="o", user_id=None,
                                        resource_key="one", spend={"rk.c": 1.0}))
        rowkey = store._row_key(aid)
        assert await redis.ttl(rowkey) == -1, "OPEN row must have NO expiry"
        assert (await store.mark_released(aid)) is not None
        ttl = await redis.ttl(rowkey)
        assert 0 < ttl <= 123, f"RELEASED row must expire (ttl={ttl})"

    @pytest.mark.asyncio
    async def test_settled_from_open_also_gets_ttl(self, redis):
        store = RedisActivationStore(redis, released_ttl_s=77)
        aid = mint_activation_id()
        await store.put_open(Activation(activation_id=aid, org_id="o", user_id=None,
                                        resource_key="one", spend={"rk.c": 1.0}))
        assert await redis.ttl(store._row_key(aid)) == -1
        assert (await store.mark_settled(aid, "0.10")) is not None
        assert 0 < await redis.ttl(store._row_key(aid)) <= 77, "SETTLED row reaps"

    @pytest.mark.asyncio
    async def test_negative_control_open_never_expires_even_after_long_wait(self, redis):
        """NEGATIVE CONTROL: an OPEN row still has ttl==-1 no matter what; contrast a
        released row that DOES carry a positive ttl. If put_open ever set a TTL, this
        would fail — proving the 'open never TTLs' assertion is not vacuous."""
        store = RedisActivationStore(redis, released_ttl_s=5)
        open_id, rel_id = mint_activation_id(), mint_activation_id()
        for aid in (open_id, rel_id):
            await store.put_open(Activation(activation_id=aid, org_id="o", user_id=None,
                                            resource_key="one", spend={"rk.c": 1.0}))
        await store.mark_released(rel_id)
        assert await redis.ttl(store._row_key(open_id)) == -1       # never expires
        assert await redis.ttl(store._row_key(rel_id)) > 0          # does expire
