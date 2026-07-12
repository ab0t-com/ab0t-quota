"""P0.2 — Crash-injection red suite (ticket 20260709_ab0t_quota_systemic_integrity_redesign).

RED-BY-DESIGN. Injects a fault at the exact window between two non-atomic steps
and asserts the *fixed* behaviour (self-healing / eventual processing), so each
test FAILS on current code.

Findings covered:
  - QI-01  claim-then-mutate: GaugeCounter claims the idempotency key
           (SET NX EX 86400) and only THEN mutates the counter in a separate
           round trip. A crash between the two leaves the key claimed and the
           counter unmoved; every retry for 24h is silently swallowed.
           (gauge.py:30-35 claim, :68-72 / :37-46 mutate)
           Fix: atomic claim+mutate in one Lua script (P1.1). RED asserts a
           retry after the crash still lands the mutation.
  - QC-02  in-progress lease can permanently drop an event. A delivery that
           arrives while a prior attempt's lease is live returns proceed=False
           and the receiver still 200s to auth; if the first attempt then
           crashed (no outcome written), the row is stuck `in_progress`, auth
           believes it delivered, and NO drain/retry loop exists.
           (handler_ledger.py:181-185,301-303; auth_events.py:203-210)
           Fix: stale-lease drain sweeper (P1.3). RED asserts the event is
           eventually processed exactly once.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import fakeredis.aioredis
import pytest
import pytest_asyncio

import json

from ab0t_quota.counters.gauge import GaugeCounter
from ab0t_quota.handler_ledger import LedgerRow, LedgerStatus, RedisLedgerStore


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.flushall()
    await r.aclose()


class _InjectedCrash(RuntimeError):
    """Stands in for pod OOM / Redis error / cancelled task."""


# ---------------------------------------------------------------------------
# QI-01 — claim-then-mutate consumes the dedup budget before the write
# ---------------------------------------------------------------------------

class TestClaimThenMutateCrash:
    @pytest.mark.asyncio
    async def test_crash_between_claim_and_incr_does_not_swallow_the_retry(self, redis):
        """increment(idempotency_key=K) must claim the key AND move the counter as
        ONE atomic unit. We crash the atomic primitive itself: after the crash,
        NOTHING may be claimed (else a retry with the same key would be a silent
        no-op — the QI-01 leak). A retry then heals.

        Injection re-pointed (V-BATCH caveat C1): the pre-fix code claimed via a
        separate `SET NX` then mutated via `redis.incrbyfloat`, so the old test
        injected at `redis.incrbyfloat`. P1.1 folds claim+mutate into ONE Lua
        `redis.eval`; that call is the new fault seam. A test still injecting at
        the now-internal `incrbyfloat` would inject NOTHING and green vacuously.

        RED on the pre-P1.1 code: `redis.eval` doesn't exist there (raises
        ResponseError) — but that is a construction-style false red, so this test
        is the GREEN partner of P1.1, asserted here to run against the atomic
        primitive. Precondition proves atomicity: the crash left NO claim.
        """
        g = GaugeCounter(redis, "org-1", "sandbox.concurrent")
        idem = "counter:lifecycle:start:sb-1"

        orig_eval = redis.eval

        async def boom(*args, **kwargs):
            raise _InjectedCrash("crash inside the atomic claim+mutate script")

        redis.eval = boom
        with pytest.raises(_InjectedCrash):
            await g.increment(1.0, idempotency_key=idem)
        redis.eval = orig_eval

        # Atomicity: the crash must have claimed NOTHING (the whole point of P1.1).
        claimed = await redis.get(f"quota:org-1:sandbox.concurrent:idem:{idem}")
        assert claimed is None, (
            "QI-01: the idempotency key was claimed despite the mutation crashing "
            "— claim and mutate are not atomic; a retry would be silently swallowed."
        )
        assert await g.get() == 0.0, "the counter was not moved by the crashed attempt"

        # The retry — same idempotency key, as any at-least-once redelivery would.
        await g.increment(1.0, idempotency_key=idem)
        assert await g.get() == 1.0, (
            "QI-01: the retry did not land — the mutation was lost across the crash."
        )


# ---------------------------------------------------------------------------
# QC-02 — in-progress lease permanently drops an event on crash
# ---------------------------------------------------------------------------

class TestLeaseDropOnCrash:
    @pytest.mark.asyncio
    async def test_event_stuck_in_progress_is_eventually_processed(self, redis):
        """Sequence that permanently drops a money event today:
          1. Delivery #1: record_attempt -> proceed=True; the worker CRASHES
             before record_outcome (pod OOM). Row left `in_progress`, lease live.
          2. Delivery #2 (auth redelivery) arrives within the lease ->
             proceed=False; the receiver 200s to auth, which now considers the
             event delivered and never redelivers.
          3. Row is stuck `in_progress` forever. `events --status failed` won't
             surface it (status is in_progress). No drain loop exists.

        A correct system re-dispatches stale-lease rows so the handler runs
        exactly once. RED today: handler_runs == 0 and there is no drain.
        GREEN target (stale-lease drain sweeper, P1.3): handler_runs == 1.
        """
        store = RedisLedgerStore(redis)
        handler_runs = {"n": 0}

        attempt1 = await store.record_attempt(
            handler_name="grant_credit",
            event_id="evt-777",
            event_type="payment.succeeded",
            event_payload={"user_id": "u1", "amount": "5.00"},
            lease_seconds=60,
        )
        assert attempt1.proceed is True
        # ...worker crashes here: NO record_outcome, NO handler side effect.

        attempt2 = await store.record_attempt(
            handler_name="grant_credit",
            event_id="evt-777",
            event_type="payment.succeeded",
            event_payload={"user_id": "u1", "amount": "5.00"},
            lease_seconds=60,
        )
        assert attempt2.proceed is False, (
            "precondition: redelivery within the live lease is skipped (auth still 200s)"
        )

        # The row is stranded in_progress, invisible to `--status failed`.
        stuck = await store.query_by_status(LedgerStatus.IN_PROGRESS)
        assert any(r.event_id == "evt-777" for r in stuck), "precondition: row stranded in_progress"
        failed = await store.query_by_status(LedgerStatus.FAILED)
        assert not any(r.event_id == "evt-777" for r in failed), (
            "precondition: the drop is invisible to a `--status failed` sweep"
        )

        # --- The fix contract (P1.3): a stale-lease drain must recover the
        # stranded row and run the handler exactly once. ---
        drain = getattr(store, "drain_stale_leases", None)
        if drain is None:
            pytest.fail(
                "QC-02: no stale-lease drain exists — the event is permanently "
                "dropped (stuck in_progress, auth believes it delivered, no retry loop). "
                "P1.3 must add a drain that re-dispatches stale-lease rows."
            )

        async def _reprocess(row):
            handler_runs["n"] += 1
            await store.record_outcome(
                handler_name=row.handler_name, event_id=row.event_id,
                status=LedgerStatus.SUCCESS,
            )

        # Simulate the 60s lease elapsing (the crashed worker never returns) by
        # back-dating the stored row's lease into the past — what real wall-clock
        # would do. The drain then sees it stale under the SAME real `now` its
        # atomic re-claim uses.
        row_key = store._row_key("grant_credit", "evt-777")
        stranded = LedgerRow.from_dict(json.loads(await redis.get(row_key)))
        stranded.lease_expires_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        await redis.set(row_key, json.dumps(stranded.to_dict()))

        n = await drain(handler=_reprocess)
        assert n == 1, f"QC-02: drain reprocessed {n} stale rows (expected 1)."
        assert handler_runs["n"] == 1, (
            f"QC-02: stale event was processed {handler_runs['n']} times (expected exactly 1)."
        )
        # And it is no longer stranded in_progress.
        still_stuck = await store.query_by_status(LedgerStatus.IN_PROGRESS)
        assert not any(r.event_id == "evt-777" for r in still_stuck), (
            "QC-02: event still in_progress after the drain settled it."
        )
