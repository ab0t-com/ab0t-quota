"""P2.1 / P2.2 — activation entity + store + acquire/release/settle.

Ticket 20260709_ab0t_quota_systemic_integrity_redesign.

Covers:
  * the generic activation entity + store CRUD/idempotency (InMemory + Redis),
  * engine.acquire() as the atomic gate: two racers at limit-1 admit exactly one,
    a whole bundle is admitted-or-denied together (kills the fake-atomic batch),
  * release() keyed ONLY on the minted activation_id — reused resource-id can
    never collide (QI-05.1), replay is a no-op,
  * settle() idempotent on activation_id (QB-02),
  * the invariant the counter is a CACHE of: counter == Σ open activations,
    including under crash-replay of acquire.
"""
from __future__ import annotations

import asyncio

import fakeredis.aioredis
import pytest
import pytest_asyncio

from ab0t_quota.activations import (
    Activation, ActivationState, InMemoryActivationStore, RedisActivationStore,
    mint_activation_id,
)
from ab0t_quota.counters.gauge import GaugeCounter
from ab0t_quota.engine import QuotaEngine
from ab0t_quota.models.core import CounterType, ResourceDef, TierConfig, TierLimits
from ab0t_quota.providers import StaticTierProvider
from ab0t_quota.registry import ResourceRegistry


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.flushall()
    await r.aclose()


CONCURRENT = ResourceDef(
    service="test", resource_key="sandbox.concurrent",
    display_name="Concurrent", counter_type=CounterType.GAUGE, unit="sandboxes",
)
GPU = ResourceDef(
    service="test", resource_key="sandbox.gpu",
    display_name="GPU", counter_type=CounterType.GAUGE, unit="gpu",
)

# free: 1 concurrent, 1 gpu
TIERS = {
    "free": TierConfig(
        tier_id="free", display_name="Free",
        limits={"sandbox.concurrent": TierLimits(limit=1),
                "sandbox.gpu": TierLimits(limit=1)},
    ),
}
BUNDLES = {"sandbox": ["sandbox.concurrent"], "gpu_box": ["sandbox.concurrent", "sandbox.gpu"]}


def _engine(redis, store=None):
    registry = ResourceRegistry()
    registry.register(CONCURRENT, GPU)
    return QuotaEngine(
        redis=redis, tier_provider=StaticTierProvider({"org-1": "free"}),
        registry=registry, tiers=TIERS, resource_bundles=BUNDLES,
        activation_store=store or InMemoryActivationStore(),
    )


# ---------------------------------------------------------------------------
# Store contract (InMemory + Redis run the same assertions)
# ---------------------------------------------------------------------------

def _stores(redis):
    return [("inmem", InMemoryActivationStore()), ("redis", RedisActivationStore(redis))]


class TestActivationStore:
    @pytest.mark.asyncio
    async def test_put_get_release_settle_idempotent(self, redis):
        for name, store in _stores(redis):
            aid = mint_activation_id()
            await store.put_open(Activation(
                activation_id=aid, org_id="org-1", user_id="u1",
                resource_key="sandbox", spend={"sandbox.concurrent": 1.0}))
            # put is idempotent (replayed acquire with same minted id)
            await store.put_open(Activation(
                activation_id=aid, org_id="org-1", user_id="u1",
                resource_key="sandbox", spend={"sandbox.concurrent": 1.0}))
            got = await store.get(aid)
            assert got is not None and got.state == ActivationState.OPEN.value, name
            assert await store.count_open("org-1") == 1, name

            assert (await store.mark_released(aid)) is not None, name    # first wins
            assert (await store.mark_released(aid)) is None, name        # replay no-op
            assert await store.count_open("org-1") == 0, name

            assert (await store.mark_settled(aid, "0.30")) is not None, name
            assert (await store.mark_settled(aid, "0.30")) is None, name  # replay no-op
            final = await store.get(aid)
            assert final.state == ActivationState.SETTLED.value and final.cost == "0.30", name

    @pytest.mark.asyncio
    async def test_list_open_only_returns_open(self, redis):
        for name, store in _stores(redis):
            a1, a2 = mint_activation_id(), mint_activation_id()
            await store.put_open(Activation(activation_id=a1, org_id="o", user_id=None,
                                            resource_key="x", spend={}))
            await store.put_open(Activation(activation_id=a2, org_id="o", user_id=None,
                                            resource_key="x", spend={}))
            await store.mark_released(a1)
            opens = await store.list_open("o")
            assert [a.activation_id for a in opens] == [a2], name


# ---------------------------------------------------------------------------
# acquire — the atomic gate
# ---------------------------------------------------------------------------

class TestAcquire:
    @pytest.mark.asyncio
    async def test_two_racers_at_limit_one_admit_exactly_one(self, redis):
        engine = _engine(redis)
        both = asyncio.Barrier(2)
        results = []

        async def create():
            await both.wait()
            results.append(await engine.acquire("org-1", "sandbox"))

        await asyncio.gather(create(), create())
        admitted = [r for r in results if r.admitted]
        assert len(admitted) == 1, "exactly one racer admitted at limit-1"
        assert await GaugeCounter(redis, "org-1", "sandbox.concurrent").get() == 1.0
        assert admitted[0].activation_id is not None
        # the loser names the resource it was denied on
        denied = [r for r in results if not r.admitted]
        assert denied and denied[0].denied_resource == "sandbox.concurrent"

    @pytest.mark.asyncio
    async def test_bundle_is_all_or_nothing(self, redis):
        """gpu_box needs BOTH concurrent AND gpu. Fill gpu; the whole bundle must be
        denied and NOTHING spent (no partial over-admission — kills fake-atomic
        batch_check)."""
        engine = _engine(redis)
        # take the single gpu slot via its own acquire
        assert (await engine.acquire("org-1", resource_key="sandbox.gpu")).admitted
        # now gpu_box (concurrent+gpu) must fail on gpu and NOT spend concurrent
        res = await engine.acquire("org-1", "gpu_box")
        assert res.admitted is False
        assert res.denied_resource == "sandbox.gpu"
        assert await GaugeCounter(redis, "org-1", "sandbox.concurrent").get() == 0.0, \
            "concurrent must NOT be spent when the bundle is denied"

    @pytest.mark.asyncio
    async def test_acquire_idempotent_on_caller_key(self, redis):
        engine = _engine(redis)
        r1 = await engine.acquire("org-1", "sandbox", idempotency_key="op-1")
        r2 = await engine.acquire("org-1", "sandbox", idempotency_key="op-1")  # replay
        assert r1.admitted and r2.admitted
        assert await GaugeCounter(redis, "org-1", "sandbox.concurrent").get() == 1.0, \
            "replayed acquire must not double-spend"


# ---------------------------------------------------------------------------
# release — reused-id safety + replay
# ---------------------------------------------------------------------------

class TestRelease:
    @pytest.mark.asyncio
    async def test_reused_lifecycle_returns_to_zero(self, redis):
        """Two DISTINCT activations, each acquire->release; the gauge returns to 0
        BOTH times. The identity is the minted activation_id, not any reused
        resource id (QI-05.1 by construction)."""
        engine = _engine(redis)
        for _ in range(2):
            r = await engine.acquire("org-1", "sandbox", user_id="u1")
            assert r.admitted
            assert await GaugeCounter(redis, "org-1", "sandbox.concurrent").get_user("u1") == 1.0
            assert await engine.release(r.activation_id) is True
            assert await GaugeCounter(redis, "org-1", "sandbox.concurrent").get_user("u1") == 0.0

    @pytest.mark.asyncio
    async def test_release_replay_is_noop(self, redis):
        engine = _engine(redis)
        r = await engine.acquire("org-1", "sandbox", user_id="u1")
        assert await engine.release(r.activation_id) is True
        assert await engine.release(r.activation_id) is False   # replay
        assert await GaugeCounter(redis, "org-1", "sandbox.concurrent").get_user("u1") == 0.0

    @pytest.mark.asyncio
    async def test_settle_idempotent(self, redis):
        engine = _engine(redis)
        r = await engine.acquire("org-1", "sandbox")
        assert await engine.settle(r.activation_id, "0.30") is True
        assert await engine.settle(r.activation_id, "0.30") is False


# ---------------------------------------------------------------------------
# The invariant the counter is a cache of
# ---------------------------------------------------------------------------

class TestStaleOpenObservability:
    @pytest.mark.asyncio
    async def test_stale_open_surfaces_missed_release(self, redis):
        """A long-open activation (missed release) becomes an OBSERVABLE fact
        (QB-03) instead of invisible drift."""
        from datetime import datetime, timedelta, timezone
        store = InMemoryActivationStore()
        engine = _engine(redis, store)
        engine._tiers["free"].limits["sandbox.concurrent"] = TierLimits(limit=1000)
        r = await engine.acquire("org-1", "sandbox", user_id="u1")
        # back-date its opened_at to simulate a resource open for a day
        row = await store.get(r.activation_id)
        row.opened_at = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        stale = await engine.stale_open_activations("org-1", older_than_s=24 * 3600)
        assert [a.activation_id for a in stale] == [r.activation_id]
        # a fresh one is NOT stale
        r2 = await engine.acquire("org-1", "sandbox", user_id="u2")
        assert r2.activation_id not in [
            a.activation_id for a in
            await engine.stale_open_activations("org-1", older_than_s=24 * 3600)
        ]


class TestCounterEqualsSumOpenActivations:
    @pytest.mark.parametrize("seed", [1, 7, 42, 101, 2026])
    @pytest.mark.asyncio
    async def test_invariant_under_concurrency_and_replay(self, redis, seed):
        """The property the whole redesign rests on: at quiescence,
        `counter == Σ open activations`.

        This exercises it for real (fixing the earlier weak version, which was a
        sequential loop with admission disabled):
          * CONCURRENT — 8 workers under one asyncio.gather, interleaving at every
            redis await;
          * ADMISSION ENABLED — org limit 5 (a limit of 1000 would test nothing):
            acquire genuinely denies at the ceiling, so the counter must never
            exceed it;
          * CRASH-REPLAY — a fraction of releases are issued twice (duplicate
            delivery); idempotent release must make the replay a no-op;
          * VARIED SEEDS — 5 seeds, different interleavings each.
        Ground truth is the store's own open index (count_open), independent of any
        app-side bookkeeping. Asserted at quiescence (all tasks joined)."""
        import asyncio
        import random

        LIMIT = 5
        store = InMemoryActivationStore()
        engine = _engine(redis, store)
        engine._tiers["free"].limits["sandbox.concurrent"] = TierLimits(limit=LIMIT)

        held: list[str] = []
        lock = asyncio.Lock()
        breached = []

        async def worker(wseed: int):
            r = random.Random(wseed)
            for _ in range(40):
                if r.random() < 0.5:
                    async with lock:
                        aid = r.choice(held) if held else None
                    if aid is not None:
                        did = await engine.release(aid)
                        if did:
                            async with lock:
                                if aid in held:
                                    held.remove(aid)
                        if r.random() < 0.4:                 # crash-replay
                            await engine.release(aid)
                else:
                    res = await engine.acquire("org-1", "sandbox", user_id="u1")
                    if res.admitted and res.activation_id:
                        async with lock:
                            held.append(res.activation_id)
                    # admission invariant: the counter never exceeds the limit
                    lvl = await GaugeCounter(redis, "org-1", "sandbox.concurrent").get()
                    if lvl > LIMIT:
                        breached.append(lvl)
                await asyncio.sleep(0)

        await asyncio.gather(*[worker(seed * 100 + i) for i in range(8)])

        # Quiescent invariant.
        gauge = await GaugeCounter(redis, "org-1", "sandbox.concurrent").get()
        n_open = await store.count_open("org-1")
        assert gauge == float(n_open), (
            f"counter({gauge}) != Σ open activations({n_open}) at quiescence, seed={seed}")
        assert n_open == len(held), f"store/app open sets diverged, seed={seed}"
        assert not breached, f"admission breached the limit: saw levels {breached[:3]}, seed={seed}"
        assert gauge <= LIMIT

    @pytest.mark.asyncio
    async def test_crash_between_spend_and_persist_heals_via_converge(self, redis):
        """acquire spends the counter (Lua) THEN persists the activation — two steps,
        not one transaction. A crash BETWEEN them leaves the counter ahead of the
        ledger (an over-count). The property is not 'never drifts' but 'levels heal':
        converge_gauge (the reconciler seam, D-10) restores counter = Σ open
        activations. Simulated by spending the gauge without recording an activation."""
        from ab0t_quota.activations import converge_gauge
        store = InMemoryActivationStore()
        engine = _engine(redis, store)
        engine._tiers["free"].limits["sandbox.concurrent"] = TierLimits(limit=1000)
        g = GaugeCounter(redis, "org-1", "sandbox.concurrent")

        a = await engine.acquire("org-1", "sandbox", user_id="u1")   # clean: gauge=1, 1 open
        # CRASH after spend, before persist: the counter moves but no activation lands.
        await g.increment_user("u1", 1.0)
        assert await g.get() == 2.0 and await store.count_open("org-1") == 1, "drift induced"

        v, src = await converge_gauge(
            activation_store=store, org_id="org-1",
            resource_key="sandbox.concurrent", counter=g)
        assert v == 1.0 and src == "activations"
        assert await g.get() == 1.0 == float(await store.count_open("org-1")), \
            "levels heal: converge restored counter == Σ open activations"
        assert a.activation_id  # (the surviving activation is the one open row)

    @pytest.mark.asyncio
    async def test_release_replay_storm_does_not_undercount(self, redis):
        """Replaying release() many times cannot drive the gauge below Σ open
        activations (idempotent by activation identity — no phantom headroom)."""
        store = InMemoryActivationStore()
        engine = _engine(redis, store)
        engine._tiers["free"].limits["sandbox.concurrent"] = TierLimits(limit=1000)
        a = await engine.acquire("org-1", "sandbox", user_id="u1")
        b = await engine.acquire("org-1", "sandbox", user_id="u1")
        # release a five times (replays)
        for _ in range(5):
            await engine.release(a.activation_id)
        gauge = await GaugeCounter(redis, "org-1", "sandbox.concurrent").get_user("u1")
        assert gauge == 1.0, "b is still open; replayed release of a must not undercount"
        assert await store.count_open("org-1") == 1
