"""K-3 — dual-write counters + dual-claimed idempotency latches.

The money bug this file exists to prevent (spec §6.2, board row K-3):
migrating the counter WITHOUT dual-claiming both latch shapes double-charges
on retry across the flip. Environment: fakeredis[lua] (no real Redis).
"""
import pytest
import pytest_asyncio
import fakeredis.aioredis

from ab0t_quota.keyspace import Keyspace

SVC = "sandbox-platform"
ORG = "org-1"
RK = "sandbox.concurrent"
UID = "user-9"

V1_GAUGE = f"quota:{ORG}:{RK}:gauge"
V2_GAUGE = "quota:v2:{" + f"{SVC}/{ORG}" + "}:" + f"{RK}:gauge"


def _gauge(redis, ks):
    from ab0t_quota.counters.gauge import GaugeCounter
    return GaugeCounter(redis, ORG, RK, keyspace=ks)


DUAL_V1 = Keyspace(service=SVC, version=1, dual_write=True)   # (1,true)
DUAL_V2 = Keyspace(service=SVC, version=2, dual_write=True)   # (2,true)


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.flushall()
    await r.aclose()


# ------------------------------------------------------------ dual mechanics

@pytest.mark.asyncio
async def test_dual_write_maintains_both_shapes(redis):
    g = _gauge(redis, DUAL_V1)
    assert await g.increment(2.0) == 2.0
    assert float(await redis.get(V1_GAUGE)) == 2.0
    assert float(await redis.get(V2_GAUGE)) == 2.0


@pytest.mark.asyncio
async def test_seed_if_absent_never_zero(redis):
    """v1 holds 3 pre-dual; the first dual mutation seeds v2 from v1 inside
    the same Lua — v2 must read 4, never 1 (spec §6.1 invariant (a))."""
    await redis.set(V1_GAUGE, "3")
    g = _gauge(redis, DUAL_V1)
    assert await g.increment(1.0) == 4.0
    assert float(await redis.get(V2_GAUGE)) == 4.0
    assert float(await redis.get(V1_GAUGE)) == 4.0


@pytest.mark.asyncio
async def test_dual_read_fallback_after_flip(redis):
    """(2,true): an untouched counter has no v2 twin yet — get() must fall
    back to v1, not report a spurious zero."""
    await redis.set(V1_GAUGE, "7")
    g = _gauge(redis, DUAL_V2)
    assert await g.get() == 7.0


# ------------------------------------------- K-3 proper: the latch dual-claim

@pytest.mark.asyncio
async def test_pre_dual_v1_idem_latch_recognised_across_flip(redis):
    """An increment first claimed PRE-dual (v1-only latch) retried AFTER the
    flip to (2,true) must be a dup — NOT a second charge. This is the
    double-charge-on-retry money bug (spec §6.2)."""
    v1 = _gauge(redis, Keyspace())                 # pre-dual world
    assert await v1.increment(1.0, idempotency_key="op-1") == 1.0
    flipped = _gauge(redis, DUAL_V2)               # post-flip, within idem TTL
    val = await flipped.increment(1.0, idempotency_key="op-1")
    assert val == 1.0, f"double-charged across the flip: {val}"
    assert float(await redis.get(V1_GAUGE)) == 1.0
    assert float(await redis.get(V2_GAUGE) or 1.0) == 1.0


@pytest.mark.asyncio
async def test_dual_claim_writes_both_latch_shapes(redis):
    """A claim made DURING dual claims both shapes, so a retry after reap
    (v1 latches gone) is still recognised on the v2 side."""
    g = _gauge(redis, DUAL_V1)
    await g.increment(1.0, idempotency_key="op-2")
    assert await redis.get(f"quota:{ORG}:{RK}:idem:op-2") is not None
    assert await redis.get(
        "quota:v2:{" + f"{SVC}/{ORG}" + "}:" + f"{RK}:idem:op-2") is not None


@pytest.mark.asyncio
async def test_seq_generation_migrates_and_teardown_not_suppressed(redis):
    """seq=7 pre-dual: the generation must migrate with the seq key so
    generation-scoped teardown claims stay valid (QI-05.1, spec §6.2)."""
    await redis.set(f"quota:{ORG}:{RK}:gauge:seq:user:{UID}", "7")
    await redis.set(V1_GAUGE, "1")
    await redis.set(f"quota:{ORG}:{RK}:gauge:user:{UID}", "1")
    g = _gauge(redis, DUAL_V2)
    # teardown under the migrated generation releases exactly once
    assert await g.decrement_user(UID, 1.0, idempotency_key="rel-1") == 0.0
    v2seq = await redis.get(
        "quota:v2:{" + f"{SVC}/{ORG}" + "}:" + f"{RK}:gauge:seq:user:{UID}")
    assert v2seq is not None and int(v2seq) == 7
    # replay of the same teardown (before any new create) is suppressed
    assert await g.decrement_user(UID, 1.0, idempotency_key="rel-1") == 0.0
    await g.increment_user(UID, 5.0)  # a NEW create under gen 8 still works
    assert await g.get_user(UID) == 5.0


@pytest.mark.asyncio
async def test_pre_dual_idemgen_latch_recognised(redis):
    """A teardown claimed pre-dual (v1 idemgen hash) replayed during dual
    must be suppressed — the gen claim is checked on BOTH shapes."""
    v1 = _gauge(redis, Keyspace())
    await v1.increment_user(UID, 2.0)              # seq=1, level 2
    assert await v1.decrement_user(UID, 1.0, idempotency_key="rel-9") == 1.0
    dual = _gauge(redis, DUAL_V1)
    assert await dual.decrement_user(UID, 1.0, idempotency_key="rel-9") == 1.0


@pytest.mark.asyncio
async def test_acc_dual_and_latch(redis):
    from ab0t_quota.counters.accumulator import AccumulatorCounter
    from ab0t_quota.models.core import ResetPeriod
    v1 = AccumulatorCounter(redis, ORG, RK, ResetPeriod.MONTHLY)
    await v1.increment(41.5, idempotency_key="chg-1")
    dual = AccumulatorCounter(redis, ORG, RK, ResetPeriod.MONTHLY, keyspace=DUAL_V2)
    # pre-dual latch recognised: no double-charge
    assert await dual.increment(41.5, idempotency_key="chg-1") == 41.5
    # fresh charge lands on both shapes
    assert await dual.increment(0.5, idempotency_key="chg-2") == 42.0
    assert float(await redis.get(v1._redis_key)) == 42.0


@pytest.mark.asyncio
async def test_rate_dual_writes_both_windows(redis):
    from ab0t_quota.counters.rate import RateCounter
    dual = RateCounter(redis, ORG, RK, 3600, keyspace=DUAL_V1)
    await dual.increment(1.0, idempotency_key="r1")
    v2key = "quota:v2:{" + f"{SVC}/{ORG}" + "}:" + f"{RK}:rate"
    assert await redis.zcard(f"quota:{ORG}:{RK}:rate") == 1
    assert await redis.zcard(v2key) == 1


# --------------------------------------------------- engine _ACQUIRE bundle

@pytest.mark.asyncio
async def test_acquire_dual_spends_both_and_dedups_across_flip(redis):
    from ab0t_quota.engine import QuotaEngine
    from ab0t_quota.registry import ResourceRegistry
    from ab0t_quota.models.core import ResourceDef, CounterType, TierConfig, TierLimits
    from ab0t_quota.providers import StaticTierProvider

    reg = ResourceRegistry()
    reg.register(ResourceDef(service=SVC, resource_key=RK, display_name="sb",
                             unit="sandboxes", counter_type=CounterType.GAUGE))
    tiers = {"free": TierConfig(tier_id="free", display_name="Free",
                                limits={RK: TierLimits(limit=10)})}

    def eng(ks):
        return QuotaEngine(redis=redis, tier_provider=StaticTierProvider(),
                           registry=reg, tiers=tiers, keyspace=ks)

    e1 = eng(Keyspace())                            # pre-dual
    r1 = await e1.acquire(ORG, resource_key=RK, user_id=UID, idempotency_key="a-1")
    assert r1.admitted and r1.reason == "ok"
    e2 = eng(DUAL_V2)                               # post-flip, within TTL
    r2 = await e2.acquire(ORG, resource_key=RK, user_id=UID, idempotency_key="a-1")
    assert r2.admitted and r2.reason == "dup", f"double-spend: {r2.reason}"
    assert float(await redis.get(V1_GAUGE)) == 1.0
    r3 = await e2.acquire(ORG, resource_key=RK, user_id=UID, idempotency_key="a-2")
    assert r3.admitted and r3.reason == "ok"
    assert float(await redis.get(V1_GAUGE)) == 2.0  # dual keeps v1 maintained
    assert float(await redis.get(V2_GAUGE)) == 2.0


# ------------------------------------------------- D-14 negative controls

@pytest.mark.asyncio
async def test_negative_control_dropped_v1_maintenance_is_caught(redis, monkeypatch):
    """Plant: sabotage the dual script so only the PRIMARY side mutates.
    The both-shapes assertion must catch it (proves the instrument can fail)."""
    import ab0t_quota.counters.gauge as gm
    sabotaged = gm._INCR.replace(
        "if DUAL then redis.call('INCRBYFLOAT', KEYS[NK+i], d) end", "")
    assert sabotaged != gm._INCR, "plant did not apply — helper text changed?"
    monkeypatch.setattr(gm, "_INCR", sabotaged)
    g = _gauge(redis, DUAL_V1)
    await g.increment(2.0)
    v2 = await redis.get(V2_GAUGE)
    assert v2 is None or float(v2) != 2.0, (
        "sabotaged dual-write still maintained both shapes — control invalid")


@pytest.mark.asyncio
async def test_negative_control_single_shape_latch_is_caught(redis, monkeypatch):
    """Plant: drop the secondary latch CHECK — a pre-dual v1 claim must then
    be missed (double-charge), proving the dual-claim test bites."""
    import ab0t_quota.counters.gauge as gm
    sabotaged = gm._INCR.replace(
        "if DUAL and redis.call('GET', KEYS[NK+i]) then return true end", "")
    assert sabotaged != gm._INCR
    v1 = _gauge(redis, Keyspace())
    await v1.increment(1.0, idempotency_key="op-x")
    monkeypatch.setattr(gm, "_INCR", sabotaged)
    flipped = _gauge(redis, DUAL_V2)
    val = await flipped.increment(1.0, idempotency_key="op-x")
    assert val == 2.0, "control invalid: sabotaged latch still deduped"
