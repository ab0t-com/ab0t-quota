"""P0.1 — Concurrency red suite (ticket 20260709_ab0t_quota_systemic_integrity_redesign).

RED-BY-DESIGN. Every test in this module reproduces a race that exists in the
DEPLOYED library today and asserts the *fixed* (green-target) behaviour, so each
test FAILS on current code. A green here before the corresponding Phase-1/2 fix
means the finding was mis-stated — report it.

Findings covered:
  - QI-03  check->increment TOCTOU: check() is read-only and the mutation
           happens later via a separate increment(); two concurrent creates at
           limit-1 both pass check and both provision.  (engine.py:103-198,227-236)
           Fix: atomic acquire() (P2.2). RED asserts over-admission == 0.
  - QI-02  floor-at-zero is a non-atomic read-modify-write: decrement_user
           pipelines INCRBYFLOAT then a blind `SET key 0` when the result is
           negative; a concurrent increment landing in that window is erased.
           (gauge.py:48-62)  Fix: atomic Lua max(0,...) (P1.1). RED asserts the
           concurrent create survives.
  - QC-01  RedisLedgerStore.record_attempt is GET->decide->SET, not SET NX
           (despite the class comment). Two concurrent deliveries of the same
           event both read None and both proceed.  (handler_ledger.py:294-329)
           Fix: atomic ledger claim (P1.2). RED asserts exactly one proceeds.

Determinism note: cooperative asyncio + fakeredis do not by themselves guarantee
the losing interleaving, so each test injects the *known-bad* schedule (a barrier
or an event around the exact non-atomic window). The injection only makes a real,
possible interleaving deterministic for CI — it does not fabricate a race the code
does not already permit.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import fakeredis.aioredis
import pytest
import pytest_asyncio

from ab0t_quota.counters.gauge import GaugeCounter
from ab0t_quota.engine import QuotaEngine
from ab0t_quota.handler_ledger import LedgerRow, RedisLedgerStore
from ab0t_quota.models.core import CounterType, ResourceDef, TierConfig, TierLimits
from ab0t_quota.models.requests import QuotaCheckRequest, QuotaIncrementRequest
from ab0t_quota.providers import StaticTierProvider
from ab0t_quota.registry import ResourceRegistry


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.flushall()
    await r.aclose()


CONCURRENT = ResourceDef(
    service="test",
    resource_key="sandbox.concurrent",
    display_name="Concurrent Sandboxes",
    counter_type=CounterType.GAUGE,
    unit="sandboxes",
)

# Free tier allows exactly ONE concurrent sandbox → "limit-1".
TIERS = {
    "free": TierConfig(
        tier_id="free", display_name="Free",
        limits={"sandbox.concurrent": TierLimits(limit=1)},
    ),
}


def _engine(redis) -> QuotaEngine:
    registry = ResourceRegistry()
    registry.register(CONCURRENT)
    return QuotaEngine(
        redis=redis,
        tier_provider=StaticTierProvider({"org-1": "free"}),
        registry=registry,
        tiers=TIERS,
    )


# ---------------------------------------------------------------------------
# QI-03 — check->increment TOCTOU
# ---------------------------------------------------------------------------

class TestCheckIncrementTOCTOU:
    @pytest.mark.asyncio
    async def test_two_concurrent_creates_at_limit_one_do_not_over_admit(self, redis):
        """Two create flows race at limit=1. A barrier forces both to reach the
        admission point together — the exact TOCTOU window. Correct system admits
        at most 1.

        FIX RE-POINTED to the GREEN target this test's docstring already named —
        `atomic acquire, P2.2` — per DECISIONS D-24 (Option B, coordinator override
        2026-07-10): the check()->increment() pattern is INHERENTLY unsafe because
        increment() COUNTS AT THE FACT (a resource that exists must be counted, or
        the counter lies / phantom headroom). Enforcement therefore lives at
        acquire(), BEFORE provisioning, where refusal is still actionable. The
        create flow acquires; the assertion (gauge <= 1) is UNCHANGED.

        RED before fix: two racers both spend → gauge = 2.0 > limit.
        GREEN (atomic acquire): exactly one admitted → gauge <= 1.
        (The over-admission of the raw check+increment anti-pattern is now an
        observable `over_limit_admitted` event, proven in test_engine's alert path.)
        """
        engine = _engine(redis)
        at_gate = asyncio.Barrier(2)
        admitted: list[bool] = []

        async def create():
            # Reach the atomic gate together (the TOCTOU window), then acquire.
            await at_gate.wait()
            res = await engine.acquire(
                org_id="org-1", resource_key="sandbox.concurrent",
            )
            if res.admitted:
                admitted.append(True)

        await asyncio.gather(create(), create())

        counter = GaugeCounter(redis, "org-1", "sandbox.concurrent")
        final = await counter.get()
        assert final <= 1.0, (
            f"QI-03: atomic acquire over-admitted — gauge={final} > limit 1 "
            f"({len(admitted)} creates admitted). The gate must admit at most one."
        )
        assert len(admitted) == 1, "exactly one racer must be admitted at limit-1"

    @pytest.mark.asyncio
    async def test_batch_check_is_not_actually_atomic(self, redis):
        """batch_check's docstring said 'Check multiple resources atomically'
        (engine.py) but the body is a sequential per-resource loop with no counter
        mutation — no cross-resource atomicity, no reservation. The real atomic
        cross-resource gate is acquire()'s ONE-Lua check-ALL-then-spend-ALL.

        FIX RE-POINTED to the GREEN target this docstring named — an atomic acquire
        over the whole bundle — per D-24 Option B (coordinator override): the
        check_for_bundle()->increment_for_bundle() pattern counts at the fact and so
        inherently over-admits; the bundle gate is acquire(). Assertion unchanged
        (gauge <= 1). The all-or-nothing bundle atomicity is further pinned in
        test_activations_20260710::TestAcquire::test_bundle_is_all_or_nothing.

        RED before fix: two racing bundle creates both spend → gauge = 2.0.
        GREEN (atomic bundle acquire): exactly one → gauge <= 1.
        """
        engine = _engine(redis)
        engine.set_resource_bundles({"sandbox": ["sandbox.concurrent"]})
        at_gate = asyncio.Barrier(2)

        async def create():
            await at_gate.wait()
            await engine.acquire("org-1", "sandbox")

        await asyncio.gather(create(), create())
        final = await GaugeCounter(redis, "org-1", "sandbox.concurrent").get()
        assert final <= 1.0, (
            f"QI-03: atomic bundle acquire over-admitted — gauge={final} > limit 1."
        )


# ---------------------------------------------------------------------------
# QI-02 — floor-at-zero read-modify-write race
# ---------------------------------------------------------------------------

class TestFloorRace:
    @pytest.mark.asyncio
    async def test_concurrent_increment_is_not_erased_by_floor_set(self, redis):
        """gauge starts at 0. Two ops race: a release (decrement 1, which drives
        negative and triggers the blind `SET key 0` floor) and a create
        (increment 2). We inject the losing schedule: the decrement suspends
        right before its floor `SET`, the create's increment lands, then the
        floor `SET 0` runs and CLOBBERS the create.

        Whatever the order, a +2 create must survive: correct final >= 1.0.
        RED today: gauge floored to 0.0 — the create was erased. GREEN target
        (atomic Lua floor, P1.1): gauge conserves the create.
        """
        g = GaugeCounter(redis, "org-1", "sandbox.concurrent")
        # start at zero (nothing running)
        assert await g.get_user("alice") == 0.0

        inc_landed = asyncio.Event()
        orig_set = redis.set
        client_floor_sets = {"n": 0}

        async def hooked_set(*args, **kwargs):
            # Count client-level blind floor SETs (`set(key, 0)`, no NX). The
            # pre-P1.1 code issued one HERE, in a separate round trip from the
            # decrement — the erasure window. P1.1 folds the floor INTO the
            # atomic Lua op, so no client-level floor SET is issued at all.
            if not kwargs.get("nx"):
                client_floor_sets["n"] += 1
                if client_floor_sets["n"] == 1:
                    # Only reachable on the PRE-FIX code path: hold the decrement
                    # right before its blind floor write so the concurrent create
                    # lands and then gets clobbered.
                    await inc_landed.wait()
            return await orig_set(*args, **kwargs)

        redis.set = hooked_set

        async def release():
            await g.decrement_user("alice", 1.0)

        async def create():
            await g.increment_user("alice", 2.0)
            inc_landed.set()

        try:
            await asyncio.gather(release(), create())
        finally:
            redis.set = orig_set

        final_org = await g.get()
        final_user = await g.get_user("alice")
        assert final_org >= 1.0 and final_user >= 1.0, (
            f"QI-02: floor `SET 0` erased a concurrent create — "
            f"org={final_org} user={final_user} (both should be >= 1.0)."
        )
        # Atomicity proof (keeps the green meaningful, not a vanished injection):
        # a flooring decrement must NOT perform a separate client-level SET — the
        # floor is atomic with the decrement inside the Lua op.
        assert client_floor_sets["n"] == 0, (
            "QI-02: a separate client-level floor `SET` was issued — the floor is "
            "not atomic with the decrement, so the erasure race is still open."
        )


# ---------------------------------------------------------------------------
# QC-01 — ledger record_attempt read-then-write race
# ---------------------------------------------------------------------------

class TestLedgerRecordAttemptRace:
    @pytest.mark.asyncio
    async def test_duplicate_delivery_admits_exactly_one(self, redis):
        """Two concurrent deliveries of the SAME (handler, event_id) hit
        RedisLedgerStore.record_attempt. A barrier forces both GETs to observe
        the pre-write (None) state — the exact read-then-write window. A correct
        (SET NX) claim admits exactly one; the second must be short-circuited.

        RED today: GET->decide->SET (handler_ledger.py:294-329) lets both read
        None and both proceed=True → the handler body runs twice. GREEN target
        (atomic claim, P1.2): exactly one proceed.
        """
        store = RedisLedgerStore(redis)
        both_read = asyncio.Barrier(2)
        orig_get = redis.get
        gets = {"n": 0}

        async def hooked_get(*args, **kwargs):
            # Only the FIRST read from each of the two concurrent deliveries is
            # held at the barrier so both observe the pre-write None (the exact
            # read-then-write window). Any later read (e.g. the create-race
            # loser re-reading the winner's row after the atomic-claim fix)
            # passes straight through — barriering it would deadlock.
            gets["n"] += 1
            if gets["n"] <= 2:
                val = await orig_get(*args, **kwargs)
                await both_read.wait()
                return val
            return await orig_get(*args, **kwargs)

        redis.get = hooked_get

        async def deliver():
            return await store.record_attempt(
                handler_name="grant_credit",
                event_id="evt-123",
                event_type="user.org_assigned",
                event_payload={"user_id": "u1"},
            )

        try:
            r1, r2 = await asyncio.gather(deliver(), deliver())
        finally:
            redis.get = orig_get

        proceed_count = sum(1 for r in (r1, r2) if r.proceed)
        assert proceed_count == 1, (
            f"QC-01: read-then-write ledger claim admitted {proceed_count} concurrent "
            f"deliveries (expected exactly 1). The handler body runs {proceed_count} times."
        )

    @pytest.mark.asyncio
    async def test_concurrent_stale_lease_reclaim_admits_exactly_one(self, redis):
        """Claim R3 — the stale-RECLAIM path (distinct from the create race). An
        in_progress row whose lease has expired (a crashed worker) is eligible
        for re-claim. Two drains that both observe the same stale row must NOT
        both proceed — a plain overwrite lets both, double-processing the event.

        RED on plain overwrite: both reclaimers proceed=True. GREEN target
        (compare-and-swap on the stale row): exactly one wins the reclaim; the
        other re-reads the winner's live-lease row and is skipped.
        """
        store = RedisLedgerStore(redis)
        await store.record_attempt(
            handler_name="h", event_id="e-stale", event_type="x",
            event_payload={"k": "v"}, lease_seconds=60,
        )
        # Back-date the lease so the row is stale (the crashed worker never returned).
        row_key = store._row_key("h", "e-stale")
        row = LedgerRow.from_dict(json.loads(await redis.get(row_key)))
        row.lease_expires_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        await redis.set(row_key, json.dumps(row.to_dict()))

        both_read = asyncio.Barrier(2)
        gets = {"n": 0}
        orig_get = redis.get

        async def hooked_get(*args, **kwargs):
            gets["n"] += 1
            if gets["n"] <= 2:
                val = await orig_get(*args, **kwargs)
                await both_read.wait()   # both observe the SAME stale row first
                return val
            return await orig_get(*args, **kwargs)

        redis.get = hooked_get

        async def reclaim():
            return await store.record_attempt(
                handler_name="h", event_id="e-stale", event_type="x",
                event_payload={"k": "v"}, lease_seconds=60,
            )

        try:
            r1, r2 = await asyncio.gather(reclaim(), reclaim())
        finally:
            redis.get = orig_get

        proceed_count = sum(1 for r in (r1, r2) if r.proceed)
        assert proceed_count == 1, (
            f"QC-01/R3: {proceed_count} concurrent drains reclaimed the SAME stale "
            f"lease (expected exactly 1). Plain overwrite double-processes; the "
            f"reclaim needs a compare-and-swap on the stale row."
        )
