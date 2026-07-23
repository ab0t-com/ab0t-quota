"""K-5 — the data-integrity migration test (spec §9-V3, the centerpiece) +
V5 boot refusals. Environment: fakeredis[lua]; time injected via now_fn.

Invariants proven across (1,false)→(1,true)→backfill→verify→(2,true)→(2,false)
→reap: (a) no counter reads zero while its v1 twin holds a value; (b) no op is
counted twice — including a retry latched pre-dual; (c) interruption at each
phase is the normal case (verbs idempotent/resumable). Planted offenders per
D-14: broken dual-write caught by verify; seed-then-add caught by the value
assertion; a skipped flip gate caught by the gate refusal test.
"""
import pytest
import pytest_asyncio
import fakeredis.aioredis

from ab0t_quota.keyspace import Keyspace, IDEM_TTL_SECONDS
from ab0t_quota.keyspace_migration import (
    KeyspaceMigrator, KeyspaceMigrationError, check_boot_keyspace,
    classify_v1_counter_key,
)
from ab0t_quota.errors import QuotaConfigError
from ab0t_quota.counters.gauge import GaugeCounter
from ab0t_quota.counters.accumulator import AccumulatorCounter
from ab0t_quota.models.core import ResetPeriod

SVC = "sandbox-platform"
ORG = "org-1"
ORG2 = "org-cold"          # untouched during dual — only backfill migrates it
RK = "sandbox.concurrent"
ACC_RK = "sandbox.monthly_cost"
UID = "user-9"
TAG = "{" + f"{SVC}/{ORG}" + "}"


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.flushall()
    await r.aclose()


async def _seed_v1_world(redis):
    """The spec's V3 fixture: gauge=3 (2 for UID), seq=7, latches claimed,
    acc mid-period, plus a cold org only backfill will ever touch."""
    g = GaugeCounter(redis, ORG, RK)
    await g.increment_user(UID, 2.0, idempotency_key="create-1")
    await g.increment(1.0, idempotency_key="create-2")
    await redis.set(f"quota:{ORG}:{RK}:gauge:seq:user:{UID}", "7")
    a = AccumulatorCounter(redis, ORG, ACC_RK, ResetPeriod.MONTHLY)
    await a.increment(41.5, idempotency_key="charge-1")
    await GaugeCounter(redis, ORG2, RK).increment(9.0)
    return g, a


def _dual1(): return Keyspace(service=SVC, version=1, dual_write=True)
def _dual2(): return Keyspace(service=SVC, version=2, dual_write=True)
def _final(): return Keyspace(service=SVC, version=2, dual_write=False)


@pytest.mark.asyncio
async def test_full_state_machine_no_zero_no_double(redis):
    clock = Clock()
    await _seed_v1_world(redis)
    mig = KeyspaceMigrator(redis, SVC, now_fn=clock)

    # phase 1: dual-on (idempotent — a second call keeps dual_since)
    m1 = await mig.dual_on()
    clock.t += 5
    assert (await mig.dual_on())["dual_since"] == m1["dual_since"]

    # dual traffic: replaying a PRE-dual latch must not double-charge (b)
    g = GaugeCounter(redis, ORG, RK, keyspace=_dual1())
    assert await g.increment(1.0, idempotency_key="create-2") == 3.0
    # live traffic keeps both shapes moving; no read is ever zero (a)
    assert await g.increment(1.0, idempotency_key="create-3") == 4.0
    assert float(await redis.get(f"quota:v2:{TAG}:{RK}:gauge")) == 4.0

    # phase 2: backfill — interrupted run (budget=1) then resume; idempotent
    part = await mig.backfill(budget=1)
    assert part["seeded"] == 1
    full = await mig.backfill()
    again = await mig.backfill()
    assert again["seeded"] == 0, "backfill re-run re-seeded — not idempotent"
    cold_tag = "{" + f"{SVC}/{ORG2}" + "}"
    assert float(await redis.get(f"quota:v2:{cold_tag}:{RK}:gauge")) == 9.0
    # the hot-path-seeded key was NOT re-copied over (seed-if-absent)
    assert float(await redis.get(f"quota:v2:{TAG}:{RK}:gauge")) == 4.0

    # phase 3: verify green
    rep = await mig.verify()
    assert rep["ok"], rep["divergent"]

    # phase 4: flip REFUSED before the idem-TTL gate (plant (c): the gate is
    # load-bearing — with it bypassed the flip would proceed)
    with pytest.raises(KeyspaceMigrationError, match="dual window too young"):
        await mig.flip()
    clock.t += IDEM_TTL_SECONDS + 1
    await mig.verify()
    await mig.flip()

    # (2,true): values identical from the flipped side; replay still deduped
    g2 = GaugeCounter(redis, ORG, RK, keyspace=_dual2())
    assert await g2.get() == 4.0
    assert await g2.increment(1.0, idempotency_key="create-3") == 4.0  # dup
    assert await g2.increment(1.0, idempotency_key="create-4") == 5.0
    assert float(await redis.get(f"quota:{ORG}:{RK}:gauge")) == 5.0

    # phase 6: reap — guarded three ways
    with pytest.raises(KeyspaceMigrationError, match="confirm"):
        await mig.reap()                      # no shared-v1 confirmation
    fresh = KeyspaceMigrator(redis, SVC, now_fn=clock)   # no verify this run
    with pytest.raises(KeyspaceMigrationError, match="verify"):
        await fresh.reap(i_confirm_no_other_scope_reads_v1=True)
    await mig.verify()
    out = await mig.reap(i_confirm_no_other_scope_reads_v1=True)
    assert out["marker"]["high_water"] == "v2-final"

    # post-reap: zero v1 counter keys; v2 values unchanged (a)
    async for key in redis.scan_iter(match="quota:*"):
        ks = key.decode()
        assert classify_v1_counter_key(ks) is None, f"v1 straggler survived reap: {ks}"
    final = GaugeCounter(redis, ORG, RK, keyspace=_final())
    assert await final.get() == 5.0
    a2 = AccumulatorCounter(redis, ORG, ACC_RK, ResetPeriod.MONTHLY, keyspace=_final())
    assert await a2.get() == 41.5


@pytest.mark.asyncio
async def test_backfill_requires_dual_on(redis):
    await _seed_v1_world(redis)
    with pytest.raises(KeyspaceMigrationError, match="dual-on"):
        await KeyspaceMigrator(redis, SVC).backfill()


@pytest.mark.asyncio
async def test_plant_broken_dual_write_is_caught_by_verify(redis, monkeypatch):
    """V3 plant (a): drop the secondary mutation — verify must go RED."""
    import ab0t_quota.counters.gauge as gm
    clock = Clock()
    await _seed_v1_world(redis)
    mig = KeyspaceMigrator(redis, SVC, now_fn=clock)
    await mig.dual_on()
    await mig.backfill()
    sab = gm._INCR.replace("if DUAL then redis.call('INCRBYFLOAT', KEYS[NK+i], d) end", "")
    assert sab != gm._INCR
    monkeypatch.setattr(gm, "_INCR", sab)
    await GaugeCounter(redis, ORG, RK, keyspace=_dual1()).increment(1.0)
    rep = await mig.verify()
    assert not rep["ok"] and rep["divergent"], (
        "verify stayed green over a broken dual-write — the instrument is blind")


@pytest.mark.asyncio
async def test_plant_seed_then_add_is_caught(redis, monkeypatch):
    """V3 plant (b): replace seed-if-absent with copy-then-add — the value
    assertion (v2 == v1) must catch the double-count."""
    import ab0t_quota.keyspace_migration as km
    clock = Clock()
    await _seed_v1_world(redis)
    mig = KeyspaceMigrator(redis, SVC, now_fn=clock)
    await mig.dual_on()
    # hot path already seeded this key
    await GaugeCounter(redis, ORG, RK, keyspace=_dual1()).increment(1.0)
    corrupt = """
local v = redis.call('GET', KEYS[1])
if v then
  redis.call('INCRBYFLOAT', KEYS[2], v)
  return 1
end
return 0
"""
    monkeypatch.setattr(km, "_SEED", corrupt)
    await mig.backfill()
    rep = await mig.verify()
    assert not rep["ok"], (
        "seed-then-add doubled the counter and verify did not catch it")


@pytest.mark.asyncio
async def test_flip_gate_respects_rate_window(redis):
    clock = Clock()
    await _seed_v1_world(redis)
    mig = KeyspaceMigrator(redis, SVC, max_rate_window_seconds=IDEM_TTL_SECONDS * 2,
                           now_fn=clock)
    await mig.dual_on()
    clock.t += IDEM_TTL_SECONDS + 1     # enough for idem, not for the window
    await mig.verify()
    with pytest.raises(KeyspaceMigrationError, match="too young"):
        await mig.flip()


# ------------------------------------------------------------- V5 refusals

@pytest.mark.asyncio
async def test_boot_refuses_version_regression_cfg011(redis):
    clock = Clock()
    mig = KeyspaceMigrator(redis, SVC, now_fn=clock)
    await mig.dual_on()
    clock.t += IDEM_TTL_SECONDS + 1
    await mig.verify()
    await mig.flip()
    await mig.reap(i_confirm_no_other_scope_reads_v1=True)
    with pytest.raises(QuotaConfigError) as ei:
        await check_boot_keyspace(redis, Keyspace(service=SVC, version=1))
    assert "QUOTA-CFG-011" in str(ei.value)
    with pytest.raises(QuotaConfigError):    # dual with v2-final is also a regression
        await check_boot_keyspace(redis, _dual2())
    await check_boot_keyspace(redis, _final())  # the one legal state


@pytest.mark.asyncio
async def test_boot_refuses_brownfield_orphaning_cfg012(redis):
    await _seed_v1_world(redis)
    with pytest.raises(QuotaConfigError) as ei:
        await check_boot_keyspace(redis, _final())
    assert "QUOTA-CFG-012" in str(ei.value)


@pytest.mark.asyncio
async def test_boot_greenfield_v2_pays_nothing(redis):
    """Empty keyspace + no marker boots straight into v2 (spec §3.3)."""
    await check_boot_keyspace(redis, _final())


@pytest.mark.asyncio
async def test_boot_guard_negative_control(redis):
    """D-14: v1 config with NO completed migration is legal — the guard can
    tell the difference (it refuses regression, not v1 itself)."""
    await _seed_v1_world(redis)
    await check_boot_keyspace(redis, Keyspace(service=SVC, version=1))
    await check_boot_keyspace(redis, Keyspace())
