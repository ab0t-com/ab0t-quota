"""Numeric + structural edge cases on the Lua/counter surface (W-T3).

Ticket 20260709_ab0t_quota_systemic_integrity_redesign. Governing rule D-31:

    An IO error — or a WEIRD INPUT — may never silently widen a limit or
    erase a spend. Over-count / deny is the only acceptable silent direction.

Emulator caveat (per ticket standard, stated at the top, not in a footnote):
every Redis behaviour asserted here ran on **fakeredis (lupa Lua)** — NOT a
real Redis. In particular:
  * INCRBYFLOAT precision: real Redis computes in C long double and formats
    to 17 significant digits; fakeredis computes in Python float64. The
    "never negative / floored" assertions hold by construction of our Lua;
    the exact residuals (e.g. 1.4e-16) are emulator-specific.
  * The "value is not a valid float" / "increment would produce NaN or
    Infinity" ResponseErrors match real Redis's messages but were observed
    under fakeredis only.
  * Script-effects-persist-on-mid-script-error (no rollback) is real-Redis
    semantics that fakeredis emulates; the idempotency-burn tests below rely
    on it and should be re-run against a real Redis at the standing pre-deploy
    gate (V-BATCH blocker A1).
  * Lazy-expiry-during-script consistency (Redis freezes the clock inside a
    script) is NOT observable under fakeredis at all — only a real Redis can
    confirm it.

Defects found by this suite (each was RED against the pre-fix library —
red→green evidence in information_tests_lua_crossruntime_20260710.md):
  ET-01  NaN limit ADMITS past the limit (silently widened limit).
  ET-02  accumulator.increment(negative) ERASES period spend — on the class
         whose docstring says it "cannot be decremented".
  ET-03  a non-finite delta with an idempotency key BURNS the key: the Lua
         claims (SET NX / HSETNX) then errors on INCRBYFLOAT; effects persist,
         so the caller's corrected retry is swallowed as a duplicate → the
         spend never lands → under-count (QI-01's crash window reopened by an
         input instead of a crash).
  ET-04  engine.acquire(deltas={rk: negative}) is ADMITTED and drives the
         gauge NEGATIVE (observed -4.0) — the admission API itself erasing
         spend and manufacturing headroom below the QG-06 floor.
"""

from __future__ import annotations

import asyncio
import math

import fakeredis.aioredis
import pytest
import pytest_asyncio

from ab0t_quota.counters.accumulator import AccumulatorCounter
from ab0t_quota.counters.gauge import GaugeCounter
from ab0t_quota.engine import QuotaEngine
from ab0t_quota.models.core import CounterType, ResetPeriod, ResourceDef, TierConfig, TierLimits
from ab0t_quota.providers import StaticTierProvider
from ab0t_quota.registry import ResourceRegistry


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.flushall()
    await r.aclose()


RK = "sandbox.concurrent"
RD = ResourceDef(service="test", resource_key=RK, display_name="SB",
                 counter_type=CounterType.GAUGE, unit="sandboxes")
TIERS = {"free": TierConfig(tier_id="free", display_name="Free",
                            limits={RK: TierLimits(limit=10)})}


def _engine(redis) -> QuotaEngine:
    reg = ResourceRegistry()
    reg.register(RD)
    return QuotaEngine(redis=redis,
                       tier_provider=StaticTierProvider({"org-1": "free"}),
                       registry=reg, tiers=TIERS)


# ---------------------------------------------------------------------------
# Numeric edges — the D-31 fail-direction on weird deltas
# ---------------------------------------------------------------------------

class TestNumericDeltaEdges:
    @pytest.mark.asyncio
    async def test_negative_delta_increment_never_erases_spend(self, redis):
        """increment(-3) must never reduce the gauge. Library convention is
        MAGNITUDE semantics (abs): it over-counts by 3 — the safe silent
        direction. (Go engine.Spend diverged here pre-fix: it applied -3.)"""
        g = GaugeCounter(redis, "org-1", RK)
        await g.reset(5)
        v = await g.increment(-3)
        assert v == 8.0, "negative increment must over-count (abs), never erase"

    @pytest.mark.asyncio
    async def test_negative_delta_decrement_is_magnitude(self, redis):
        """decrement(-2) == decrement(2) (magnitude semantics, floored)."""
        g = GaugeCounter(redis, "org-1", RK)
        await g.reset(5)
        assert await g.decrement(-2) == 3.0

    @pytest.mark.asyncio
    async def test_zero_delta_is_a_noop(self, redis):
        g = GaugeCounter(redis, "org-1", RK)
        await g.reset(5)
        assert await g.increment(0) == 5.0
        assert await g.get() == 5.0

    @pytest.mark.asyncio
    async def test_huge_delta_over_counts_and_denies(self, redis):
        """1e308 is representable; it lands (a colossal over-count only ever
        DENIES capacity — annoying, safe)."""
        g = GaugeCounter(redis, "org-1", RK)
        await g.reset(0)
        assert await g.increment(1e308) == 1e308
        _, admitted = await g.try_increment(1, 10.0)
        assert not admitted

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    async def test_non_finite_delta_raises_loud(self, redis, bad):
        """A non-finite delta must fail LOUD (deny direction), never mutate."""
        g = GaugeCounter(redis, "org-1", RK)
        await g.reset(5)
        with pytest.raises((ValueError, Exception)):
            await g.increment(bad)
        assert await g.get() == 5.0, "a rejected delta must not mutate the gauge"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [float("nan"), float("inf")])
    async def test_non_finite_delta_must_not_burn_the_idempotency_key(self, redis, bad):
        """ET-03 — THE sharp one. Pre-fix: increment(nan, key) ran the Lua,
        which claimed the idem key (SET NX) and THEN errored on INCRBYFLOAT.
        Redis scripts do not roll back, so the claim persisted; the caller's
        corrected retry was then swallowed as a duplicate and the spend NEVER
        landed — an under-count / phantom headroom (the forbidden direction),
        triggered by one weird input. The boundary must validate BEFORE Lua."""
        g = GaugeCounter(redis, "org-1", RK)
        await g.reset(0)
        with pytest.raises((ValueError, Exception)):
            await g.increment(bad, idempotency_key="create-x")
        claimed = await redis.get(f"quota:org-1:{RK}:idem:create-x")
        assert claimed is None, (
            "idempotency key was BURNED by a rejected delta — the corrected "
            "retry will be swallowed as a duplicate and the spend erased"
        )
        # The corrected retry must APPLY.
        assert await g.increment(1, idempotency_key="create-x") == 1.0
        assert await g.get() == 1.0

    @pytest.mark.asyncio
    async def test_non_finite_delta_user_paths_reject_before_lua(self, redis):
        """Per-user variants share the boundary guard (claim keys unburned,
        seq generation unbumped, gauges unmutated)."""
        g = GaugeCounter(redis, "org-1", RK)
        for op in (
            lambda: g.increment_user("u1", float("nan"), idempotency_key="k"),
            lambda: g.decrement_user("u1", float("inf"), idempotency_key="k"),
            lambda: g.try_increment(float("nan"), 10.0, idempotency_key="k"),
            lambda: g.try_increment_user("u1", float("inf"), 10.0, 5.0, idempotency_key="k"),
        ):
            with pytest.raises((ValueError, Exception)):
                await op()
        assert await g.get() == 0.0
        assert await g.get_user("u1") == 0.0
        assert await redis.get(f"quota:org-1:{RK}:gauge:seq:user:u1") is None, (
            "a rejected delta must not bump the create generation"
        )


class TestNumericLimitEdges:
    @pytest.mark.asyncio
    async def test_nan_limit_must_not_widen(self, redis):
        """ET-01 — a NaN limit made every comparison false and ADMITTED
        everything: a corrupted limit silently widened to infinity. D-31: it
        must fail LOUD (raise) or deny — never admit."""
        g = GaugeCounter(redis, "org-1", RK)
        await g.reset(10)
        with pytest.raises((ValueError, Exception)):
            val, admitted = await g.try_increment(1, float("nan"))
            # If no raise, it must at least have denied and not mutated:
            assert not admitted, "NaN limit ADMITTED — silently widened limit"
        assert await g.get() == 10.0

    @pytest.mark.asyncio
    async def test_inf_limit_is_unlimited_semantics(self, redis):
        """+inf as a limit admits (mathematically consistent with 'unlimited',
        same observable as limit=None). Documented, not a defect."""
        g = GaugeCounter(redis, "org-1", RK)
        await g.reset(1e6)
        val, admitted = await g.try_increment(1, float("inf"))
        assert admitted and val == 1e6 + 1

    @pytest.mark.asyncio
    async def test_negative_inf_limit_denies_everything(self, redis):
        """-inf as a limit denies all (deny direction — safe)."""
        g = GaugeCounter(redis, "org-1", RK)
        await g.reset(0)
        val, admitted = await g.try_increment(1, float("-inf"))
        assert not admitted
        assert await g.get() == 0.0

    @pytest.mark.asyncio
    async def test_nan_limit_rejected_on_user_path_too(self, redis):
        g = GaugeCounter(redis, "org-1", RK)
        await g.reset(0)
        with pytest.raises((ValueError, Exception)):
            val, admitted = await g.try_increment_user("u1", 1, float("nan"), 5.0)
            assert not admitted
        with pytest.raises((ValueError, Exception)):
            val, admitted = await g.try_increment_user("u1", 1, 5.0, float("nan"))
            assert not admitted
        assert await g.get() == 0.0


class TestAccumulatorEdges:
    @pytest.mark.asyncio
    async def test_negative_delta_never_erases_period_spend(self, redis):
        """ET-02 — the accumulator's own decrement() raises TypeError
        ("cannot be decremented"), yet increment(-4) did exactly that,
        silently. Monthly SPEND was erasable by a sign flip. Magnitude
        semantics (matching the gauge): |delta| is added."""
        a = AccumulatorCounter(redis, "org-1", RK, ResetPeriod.MONTHLY)
        await a.reset(10)
        v = await a.increment(-4)
        assert v == 14.0, (
            f"accumulator.increment(-4) yielded {v} — a negative delta must "
            "never erase recorded spend (D-31)"
        )

    @pytest.mark.asyncio
    async def test_non_finite_delta_rejected_without_burning_idem(self, redis):
        a = AccumulatorCounter(redis, "org-1", RK, ResetPeriod.MONTHLY)
        await a.reset(0)
        with pytest.raises((ValueError, Exception)):
            await a.increment(float("nan"), idempotency_key="bill-1")
        assert await redis.get(f"quota:org-1:{RK}:idem:bill-1") is None
        assert await a.increment(2, idempotency_key="bill-1") == 2.0


class TestEngineAcquireDeltaEdges:
    @pytest.mark.asyncio
    async def test_acquire_negative_delta_cannot_erase_spend(self, redis):
        """ET-04 — pre-fix, acquire(deltas={rk: -5}) was ADMITTED and drove
        the org gauge to -4.0: the admission API erasing spend AND breaching
        the QG-06 zero floor in one call. Magnitude semantics now apply."""
        engine = _engine(redis)
        r1 = await engine.acquire("org-1", resource_key=RK)
        assert r1.admitted
        r2 = await engine.acquire("org-1", resource_key=RK, deltas={RK: -5.0})
        g = GaugeCounter(redis, "org-1", RK)
        val = await g.get()
        assert val >= 1.0, (
            f"acquire with a negative delta left the gauge at {val} — it "
            "erased spend / went below the floor (D-31 forbidden direction)"
        )

    @pytest.mark.asyncio
    async def test_acquire_non_finite_delta_rejected_before_spend(self, redis):
        """A NaN delta in a bundle would spend earlier gauges then error
        mid-script (partial spend, burned idem). Must be rejected up front."""
        engine = _engine(redis)
        with pytest.raises((ValueError, Exception)):
            await engine.acquire("org-1", resource_key=RK,
                                 deltas={RK: float("nan")},
                                 idempotency_key="acq-1")
        g = GaugeCounter(redis, "org-1", RK)
        assert await g.get() == 0.0
        assert await redis.get(f"quota:org-1:{RK}:idem:acq-1") is None, (
            "acquire idem key burned by a rejected delta"
        )


# ---------------------------------------------------------------------------
# Float precision through INCRBYFLOAT (emulator-scoped; see module docstring)
# ---------------------------------------------------------------------------

class TestFloatPrecision:
    @pytest.mark.asyncio
    async def test_point_one_plus_point_two(self, redis):
        """0.1 + 0.2 accumulates the classic float64 residual under fakeredis.
        We assert the INVARIANT (approximately 0.3, never a widened value),
        not the exact bits — real Redis long-double may differ in the last
        digits (pre-deploy gate A1)."""
        g = GaugeCounter(redis, "org-1", RK)
        await g.increment(0.1)
        v = await g.increment(0.2)
        assert v == pytest.approx(0.3, abs=1e-12)

    @pytest.mark.asyncio
    async def test_repeated_small_decrements_never_go_negative(self, redis):
        """1.0 - 10×0.1 leaves a tiny POSITIVE residual (1.4e-16 under
        fakeredis) — an over-count, the safe direction. The floor guarantees
        it can never be negative; D-31 says the residual direction matters,
        not its size."""
        g = GaugeCounter(redis, "org-1", RK)
        await g.reset(1.0)
        v = 1.0
        for _ in range(10):
            v = await g.decrement(0.1)
        assert v >= 0.0, "gauge went negative through float residue"
        assert v < 1e-9, f"residual {v} unexpectedly large"
        # And ten more decrements stay floored at zero:
        for _ in range(10):
            v = await g.decrement(0.1)
        assert v == 0.0

    @pytest.mark.asyncio
    async def test_fractional_boundary_denies_through_float_residue(self, redis):
        """DOCUMENTED REALITY (discovered red): at a fractional limit the float
        residue bites at the BOUNDARY CHECK — after spending 0.1 against limit
        0.3, a 0.2 spend computes 0.1+0.2 = 0.30000000000000004 > 0.3 and is
        DENIED, even though exact arithmetic would admit it exactly at the
        limit. Integer deltas admit exactly at the limit (1+1 > 2 is false).
        The error direction is DENY — the only acceptable silent direction
        (D-31) — so this is recorded as a sharp edge, not fixed. Consumers
        using fractional gauge limits must expect boundary denies.
        (Emulator-scoped: real Redis long-double INCRBYFLOAT may round the
        stored 0.1 differently — pre-deploy gate A1.)"""
        g = GaugeCounter(redis, "org-1", RK)
        _, a1 = await g.try_increment(0.1, 0.3)
        assert a1
        _, a2 = await g.try_increment(0.2, 0.3)
        assert not a2, (
            "0.1 then 0.2 at limit 0.3 was ADMITTED — float residue direction "
            "flipped from deny to admit; investigate (this documents deny)"
        )
        # And the gauge was not mutated by the denied spend:
        assert await g.get() == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# Structural edges — wrong types, missing keys, arity
# ---------------------------------------------------------------------------

class TestStructuralEdges:
    @pytest.mark.asyncio
    async def test_missing_key_decrement_floors_at_zero(self, redis):
        g = GaugeCounter(redis, "org-1", RK)
        assert await g.decrement(3) == 0.0

    @pytest.mark.asyncio
    async def test_wrongtype_gauge_key_fails_loud_not_silent(self, redis):
        """A hash where the gauge string is expected: WRONGTYPE must surface
        as an error (deny direction), never a silent success or a reset."""
        await redis.hset(f"quota:org-1:{RK}:gauge", "f", "1")
        g = GaugeCounter(redis, "org-1", RK)
        with pytest.raises(Exception):
            await g.increment(1)
        assert await redis.type(f"quota:org-1:{RK}:gauge") == b"hash", (
            "the wrongly-typed key was clobbered instead of erroring"
        )

    @pytest.mark.asyncio
    async def test_wrongtype_idemgen_key_fails_toward_overcount(self, redis):
        """A string where the :idemgen: HASH is expected: HSETNX raises
        WRONGTYPE, the script aborts BEFORE the decrement → the gauge keeps
        its (over-)count. Loud + over-count = both acceptable directions."""
        g = GaugeCounter(redis, "org-1", RK)
        await g.increment_user("u1", 5)
        await redis.set(f"quota:org-1:{RK}:idemgen:u1:k1", "oops")
        with pytest.raises(Exception):
            await g.decrement_user("u1", 1, idempotency_key="k1")
        assert await g.get() == 5.0, "WRONGTYPE mid-script erased spend"
        assert await g.get_user("u1") == 5.0

    @pytest.mark.asyncio
    async def test_script_with_zero_declared_keys_errors_loud(self, redis):
        """A script that reads KEYS[1] but is invoked with numkeys=0 gets nil
        and must error, not half-run. (Library call sites all pass literal
        arity — the QI-09 static audit pins that — this documents the runtime
        failure mode under the emulator.)"""
        from ab0t_quota.counters.gauge import _INCR
        with pytest.raises(Exception):
            await redis.eval(_INCR, 0, 1, 86400, "0")

    @pytest.mark.asyncio
    async def test_expired_idem_claim_means_reapply(self, redis):
        """The 24h idempotency horizon is a CONTRACT: once the claim expires,
        the same key re-applies. (This is why release dedup moved to the
        forever-idempotent activation_id — D-22/P5.2.)"""
        g = GaugeCounter(redis, "org-1", RK)
        await g.increment(1, idempotency_key="create-1")
        assert await g.increment(1, idempotency_key="create-1") == 1.0  # dup swallowed
        await redis.delete(f"quota:org-1:{RK}:idem:create-1")  # simulate TTL expiry
        assert await g.increment(1, idempotency_key="create-1") == 2.0, (
            "post-expiry the key must re-apply (the documented 24h horizon)"
        )


# ---------------------------------------------------------------------------
# Concurrency at the release boundary (single activation id)
# ---------------------------------------------------------------------------

class TestConcurrentReleaseOneId:
    @pytest.mark.asyncio
    async def test_ten_concurrent_releases_apply_exactly_once(self, redis):
        """Ten racers release ONE activation id. Exactly one performs it; the
        gauge decrements exactly once (to 1.0, from 2 open activations)."""
        engine = _engine(redis)
        r1 = await engine.acquire("org-1", resource_key=RK)
        r2 = await engine.acquire("org-1", resource_key=RK)
        assert r1.admitted and r2.admitted and r1.activation_id

        results = await asyncio.gather(
            *[engine.release(r1.activation_id) for _ in range(10)]
        )
        assert sum(1 for x in results if x) == 1, (
            f"{sum(results)} of 10 concurrent releases performed — "
            "duplicate release applied more than once"
        )
        g = GaugeCounter(redis, "org-1", RK)
        assert await g.get() == 1.0
