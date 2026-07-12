"""Claim 1 (DECISIONS D-27) — prove the spend↔persist drift is FAIL-CLOSED.

`acquire` spends the counter (Lua) THEN persists the activation row; `release`
marks the row RELEASED THEN decrements the counter — two stores, no 2PC. A crash in
either window drifts `counter` vs `Σ open activations`. D-27 accepts that bounded,
self-healing staleness INSTEAD of two DDB writes on the hot path — but the trade is
sound ONLY because the drift can go exactly one way:

    counter >= Σ open activations   — ALWAYS, at every crash point.

Over-count only ever DENIES capacity (safe, healed within one reconcile interval).
The forbidden direction is under-count → phantom free headroom → over-admission →
money loss (the QI-02/QG-06/QI-05 class this whole ticket exists to kill).

This module injects a crash at EVERY step of acquire / release / settle and asserts
the counter never drops below the ledger, deterministically AND under randomized
concurrent interleaving with varied seeds. It also pins the ledger-completeness
guarantee the fail-closed property depends on: **acquire succeeds ⟹ activation
persisted** (a persist failure fails the acquire, it is not swallowed).

The orderings are the proof:
  * acquire: counter goes UP first (spend), ledger goes UP second (persist)  → counter >= ledger
  * release: ledger goes DOWN first (mark),  counter goes DOWN second (decr)  → counter >= ledger
Invert either and the window under-counts. So these tests also guard the ordering.
"""
from __future__ import annotations

import asyncio
import random

import fakeredis.aioredis
import pytest
import pytest_asyncio

from ab0t_quota.activations import (
    Activation, ActivationState, InMemoryActivationStore, converge_gauge,
)
from ab0t_quota.counters.gauge import GaugeCounter
from ab0t_quota.engine import QuotaEngine
from ab0t_quota.models.core import CounterType, ResourceDef, TierConfig, TierLimits
from ab0t_quota.providers import StaticTierProvider
from ab0t_quota.registry import ResourceRegistry

RK = "sandbox.concurrent"


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.flushall()
    await r.aclose()


def _engine(redis, store, *, limit=1000):
    reg = ResourceRegistry()
    reg.register(ResourceDef(service="t", resource_key=RK, display_name="C",
                             counter_type=CounterType.GAUGE))
    return QuotaEngine(
        redis=redis, tier_provider=StaticTierProvider({"org-1": "free"}), registry=reg,
        tiers={"free": TierConfig(tier_id="free", display_name="F",
                                  limits={RK: TierLimits(limit=limit)})},
        resource_bundles={"box": [RK]}, activation_store=store)


async def _counter_ge_ledger(redis, store, user="u1") -> tuple[float, int]:
    counter = await GaugeCounter(redis, "org-1", RK).get()
    n_open = await store.count_open("org-1")
    return counter, n_open


class _BoomStore(InMemoryActivationStore):
    """InMemory store that raises on a chosen op — simulates a crash at a real
    store call inside engine.acquire/release/settle. `arm(op)` raises once."""
    def __init__(self):
        super().__init__()
        self._armed: set = set()

    def arm(self, op: str):
        self._armed.add(op)

    async def put_open(self, activation):
        if "put_open" in self._armed:
            self._armed.discard("put_open")
            raise RuntimeError("injected crash: put_open")
        return await super().put_open(activation)

    async def mark_released(self, activation_id):
        if "mark_released" in self._armed:
            self._armed.discard("mark_released")
            raise RuntimeError("injected crash: mark_released")
        return await super().mark_released(activation_id)

    async def mark_settled(self, activation_id, cost):
        if "mark_settled" in self._armed:
            self._armed.discard("mark_settled")
            raise RuntimeError("injected crash: mark_settled")
        return await super().mark_settled(activation_id, cost)


class _EvalBoom:
    """Wraps redis.eval to raise once when armed — simulates a crash at the counter
    mutation (the Lua) INSIDE the real engine path, so the ordering around it is
    exercised (not hand-simulated)."""
    def __init__(self, redis):
        self._redis = redis
        self._orig = redis.eval
        self._armed = False

    def arm(self):
        self._armed = True

    async def eval(self, *a, **k):
        if self._armed:
            self._armed = False
            raise RuntimeError("injected crash: counter mutation (Lua)")
        return await self._orig(*a, **k)


class TestAcquireCrashPoints:
    @pytest.mark.asyncio
    async def test_crash_after_spend_before_persist_overcounts_not_under(self, redis):
        """CP: spend done, put_open dies. Counter=1, ledger=0 → OVER-count. And the
        acquire FAILS (ledger completeness: success ⟹ persisted)."""
        store = _BoomStore()
        engine = _engine(redis, store)
        store.arm("put_open")
        with pytest.raises(RuntimeError):
            await engine.acquire("org-1", "box", user_id="u1")
        counter, n_open = await _counter_ge_ledger(redis, store)
        assert counter == 1.0 and n_open == 0, "spend landed, row did not"
        assert counter >= n_open, "must be over-count, never under"
        # heals: converge to Σ open (0) — and because the acquire FAILED, the caller
        # never provisioned, so 0 is correct (no phantom live resource).
        await converge_gauge(activation_store=store, org_id="org-1", resource_key=RK,
                             counter=GaugeCounter(redis, "org-1", RK))
        c2, n2 = await _counter_ge_ledger(redis, store)
        assert c2 == 0.0 == n2

    @pytest.mark.asyncio
    async def test_clean_acquire_is_equal(self, redis):
        store = InMemoryActivationStore()
        engine = _engine(redis, store)
        await engine.acquire("org-1", "box", user_id="u1")
        counter, n_open = await _counter_ge_ledger(redis, store)
        assert counter == 1.0 == n_open


class TestReleaseCrashPoints:
    """Crashes injected INSIDE the real engine.release (not a hand-simulated store
    call), so these tests are SENSITIVE to release's ordering. The negative control
    `test_release_ordering_guard_is_real` proves it: invert the ordering and
    test_release_mark_crash_is_order_sensitive_guard goes RED."""

    @pytest.mark.asyncio
    async def test_release_decrement_crash_overcounts_not_under(self, redis):
        """Correct order [mark, decrement]: crash AT the decrement (real path) →
        ledger dropped, counter not → OVER-count. Heals via converge."""
        store = InMemoryActivationStore()
        engine = _engine(redis, store)
        a = await engine.acquire("org-1", "box", user_id="u1")
        boom = _EvalBoom(redis)
        redis.eval = boom.eval
        boom.arm()                              # the release's decrement Lua will raise
        with pytest.raises(RuntimeError):
            await engine.release(a.activation_id)
        redis.eval = boom._orig
        counter, n_open = await _counter_ge_ledger(redis, store)
        assert counter == 1.0 and n_open == 0, "mark done, decrement crashed → over-count"
        assert counter >= n_open
        assert await engine.release(a.activation_id) is False   # already released → no-op
        await converge_gauge(activation_store=store, org_id="org-1", resource_key=RK,
                             counter=GaugeCounter(redis, "org-1", RK))
        c2, n2 = await _counter_ge_ledger(redis, store)
        assert c2 == 0.0 == n2

    @pytest.mark.asyncio
    async def test_release_mark_crash_is_order_sensitive_guard(self, redis):
        """Crash AT store.mark_released inside the real engine.release, asserting
        counter >= Σ open. Under the CORRECT order [mark, decrement], mark is first,
        so it raises before any decrement → nothing changes → counter==ledger (safe).
        Under the FORBIDDEN order [decrement, mark], the decrement would have already
        run (counter dropped) before mark raises → counter < ledger → UNDER-count.
        This is the assertion that must FAIL when the ordering is inverted."""
        store = _BoomStore()
        engine = _engine(redis, store)
        a = await engine.acquire("org-1", "box", user_id="u1")
        store.arm("mark_released")
        with pytest.raises(RuntimeError):
            await engine.release(a.activation_id)
        counter, n_open = await _counter_ge_ledger(redis, store)
        assert counter >= n_open, (
            f"release under-counted (counter={counter} < Σopen={n_open}) — the "
            f"decrement ran before the ledger drop. RELEASE ORDERING IS WRONG.")
        assert counter == 1.0 and n_open == 1   # correct order: nothing changed

    @pytest.mark.asyncio
    async def test_clean_release_is_equal(self, redis):
        store = InMemoryActivationStore()
        engine = _engine(redis, store)
        a = await engine.acquire("org-1", "box", user_id="u1")
        await engine.release(a.activation_id)
        counter, n_open = await _counter_ge_ledger(redis, store)
        assert counter == 0.0 == n_open


class TestSettleCrashPoints:
    @pytest.mark.asyncio
    async def test_settle_mark_crash_no_drift(self, redis):
        """settle touches only the ledger (mark_settled) — no counter op, so no
        ordering. A crash at mark_settled changes nothing → counter >= ledger."""
        store = _BoomStore()
        engine = _engine(redis, store)
        a = await engine.acquire("org-1", "box", user_id="u1")
        await engine.release(a.activation_id)
        store.arm("mark_settled")
        with pytest.raises(RuntimeError):
            await engine.settle(a.activation_id, "0.10")
        counter, n_open = await _counter_ge_ledger(redis, store)
        assert counter == 0.0 == n_open and counter >= n_open

    @pytest.mark.asyncio
    async def test_settle_from_open_drops_ledger_not_counter_overcounts(self, redis):
        """settle from OPEN (skipping release) drops Σ open without decrementing the
        counter → OVER-count, never under."""
        store = InMemoryActivationStore()
        engine = _engine(redis, store)
        a = await engine.acquire("org-1", "box", user_id="u1")
        assert await engine.settle(a.activation_id, "0.10") is True
        counter, n_open = await _counter_ge_ledger(redis, store)
        assert counter == 1.0 and n_open == 0 and counter >= n_open


class TestOverrideLoadFailClosed:
    """FE2 — a per-org override can RESTRICT a limit below the tier. If the override
    store errors, the engine must NOT fall back to the wider base limit (that lets
    the org over-admit — fail-OPEN). It must fail closed."""

    @pytest.mark.asyncio
    async def test_override_load_error_fails_closed_on_admission_path(self, redis):
        from ab0t_quota.models.requests import QuotaCheckRequest
        reg = ResourceRegistry()
        reg.register(ResourceDef(service="t", resource_key=RK, display_name="C",
                                 counter_type=CounterType.GAUGE))

        async def boom_loader(org_id, resource_key):
            raise RuntimeError("override store unreachable")

        engine = QuotaEngine(
            redis=redis, tier_provider=StaticTierProvider({"org-1": "free"}), registry=reg,
            tiers={"free": TierConfig(tier_id="free", display_name="F",
                                      limits={RK: TierLimits(limit=100)})},  # wide base limit
            resource_bundles={"box": [RK]}, activation_store=InMemoryActivationStore(),
            override_loader=boom_loader)

        # check() must NOT return an ALLOW computed against the base limit of 100
        # (which would ignore a possibly-downward override). It fails closed.
        with pytest.raises(RuntimeError):
            await engine.check(QuotaCheckRequest(org_id="org-1", resource_key=RK))
        # acquire() (the admission gate) also fails closed — no phantom grant.
        with pytest.raises(RuntimeError):
            await engine.acquire("org-1", resource_key=RK)
        assert await GaugeCounter(redis, "org-1", RK).get() == 0.0, "no spend on a failed acquire"


class TestRandomizedCrashInjectionNeverUndercounts:
    @pytest.mark.parametrize("seed", [1, 13, 99, 404, 2026])
    @pytest.mark.asyncio
    async def test_counter_never_below_ledger_under_random_crashes(self, redis, seed):
        """8 concurrent workers issue clean AND crash-truncated acquire/release; a
        crash is a spend-without-persist or a mark-without-decrement. After EVERY
        operation the global invariant `counter >= Σ open activations` is asserted —
        under concurrency, so transient mid-flight states are checked too. If any
        interleaving under-counts, the ordering is wrong. Ends by converging and
        showing the accumulated over-count heals to equality."""
        store = InMemoryActivationStore()
        engine = _engine(redis, store, limit=1000)
        g = GaugeCounter(redis, "org-1", RK)
        held: list[str] = []
        lock = asyncio.Lock()
        violations: list[tuple] = []

        async def check():
            c = await g.get()
            n = await store.count_open("org-1")
            if c < n:
                violations.append((c, n))

        async def worker(wseed: int):
            r = random.Random(wseed)
            for _ in range(50):
                roll = r.random()
                if roll < 0.35:                      # clean acquire
                    res = await engine.acquire("org-1", "box", user_id="u1")
                    if res.admitted and res.activation_id:
                        async with lock:
                            held.append(res.activation_id)
                elif roll < 0.5:                     # CRASH: spend without persist
                    await g.increment_user("u1", 1.0)
                elif roll < 0.85:                    # clean release
                    async with lock:
                        aid = r.choice(held) if held else None
                    if aid is not None:
                        if await engine.release(aid):
                            async with lock:
                                if aid in held:
                                    held.remove(aid)
                else:                                # CRASH: mark without decrement
                    async with lock:
                        aid = r.choice(held) if held else None
                    if aid is not None:
                        if await store.mark_released(aid):
                            async with lock:
                                if aid in held:
                                    held.remove(aid)
                await check()
                await asyncio.sleep(0)

        await asyncio.gather(*[worker(seed * 100 + i) for i in range(8)])
        await check()

        assert not violations, (
            f"UNDER-COUNT detected (counter < Σ open) — fail-OPEN! seed={seed} "
            f"first={violations[0]}")
        # accumulated over-count from injected crashes heals to equality:
        await converge_gauge(activation_store=store, org_id="org-1", resource_key=RK, counter=g)
        c, n = await g.get(), await store.count_open("org-1")
        assert c == float(n), f"converge did not heal: counter={c} Σopen={n} seed={seed}"
