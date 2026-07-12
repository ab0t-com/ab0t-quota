"""W-T2 side-effects hardening — the OUTBOX ↔ THE PROCESS boundary (extended).

Ticket 20260709_ab0t_quota_systemic_integrity_redesign.
Boundary crossed (D-40 table, row #3): **the process** — "survives restart".

EXTENDS ``tests/billing/test_integrity_delivery_20260710.py::TestOutboxSurvivesRestart``
(W-PY-A, D-29) — cited per D-25, NOT rewritten. That test proves a single stranded
intent survives a restart. This one covers the states it did not:
  * a MARKED-DELIVERED row must NOT resurrect and re-bill on restart;
  * a VOIDED row must NOT re-deliver and must NOT re-void on restart;
  * a PARTIAL delivery (some delivered, some stranded) resumes only the remainder.

"Restart" = discard the emitter AND the store object, then build brand-new ones over
the SAME external Redis keyspace. fakeredis is in-process; it stands in for a real
external Redis (the load-bearing fact is that state lives in the KEYSPACE, decoupled
from the objects). Real-Redis restart is the pre-deploy gate — see the artifact.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal

import fakeredis.aioredis
import pytest

from ab0t_quota.billing.lifecycle import LifecycleEmitter
from ab0t_quota.billing.outbox import RedisOutboxStore, OutboxRecord

pytestmark = pytest.mark.asyncio

TOPIC = "arn:aws:sns:us-east-1:123456789012:resource-lifecycle"


class _OkSNS:
    def __init__(self): self.delivered = 0
    def publish(self, **kwargs):
        self.delivered += 1
        return {"MessageId": f"m-{self.delivered}"}


class _DownSNS:
    def __init__(self): self.attempts = 0
    def publish(self, **kwargs):
        self.attempts += 1
        raise RuntimeError("SNS down")


class _FailKeysSNS:
    """Fails publish for reservation_ids in ``fail_ids``, succeeds otherwise."""

    def __init__(self, fail_ids):
        self.fail_ids = set(fail_ids)
        self.delivered = 0
        self.delivered_ids = []

    def publish(self, **kwargs):
        import json
        msg = json.loads(kwargs["Message"])
        rid = msg.get("reservation_id")
        if rid in self.fail_ids:
            raise RuntimeError(f"transient failure for {rid}")
        self.delivered += 1
        self.delivered_ids.append(rid)
        return {"MessageId": f"m-{self.delivered}"}


def _emitter(redis, sink, *, horizon=900.0) -> LifecycleEmitter:
    em = LifecycleEmitter(sns_topic_arn=TOPIC, outbox_store=RedisOutboxStore(redis),
                          outbox_max_retry_horizon_s=horizon)
    em._client = sink
    em._get_client = lambda: sink
    return em


async def _put(redis, rid, *, first_ts=None):
    store = RedisOutboxStore(redis)
    await store.put_intent(OutboxRecord(
        key=f"{rid}:resource.stopped", event={"reservation_id": rid},
        event_type="resource.stopped", resource_type="sandbox",
        reservation_id=rid, first_ts=first_ts if first_ts is not None else time.time()))


class TestRestartWithMixedRowStates:
    async def test_delivered_row_does_not_resurrect_across_restart(self):
        """Process 1 delivers A (mark_delivered) and strands B (SNS down for B).
        Process 2 (fresh objects, same Redis) must deliver ONLY B — a delivered
        row that resurrected would DOUBLE-BILL."""
        redis = fakeredis.aioredis.FakeRedis()
        try:
            await _put(redis, "A")
            await _put(redis, "B")
            # process 1: A publishes, B fails.
            p1 = _emitter(redis, _FailKeysSNS(fail_ids={"B"}))
            delivered1 = await p1.drain()
            assert delivered1 == 1
            del p1

            # --- restart ---
            sink2 = _OkSNS()
            p2 = _emitter(redis, sink2)
            delivered2 = await p2.drain()
            assert delivered2 == 1, "only the stranded B should remain"
            assert sink2.delivered == 1
            assert await p2.pending_count() == 0
        finally:
            await redis.flushall(); await redis.aclose()

    async def test_voided_row_does_not_redeliver_or_revoid_across_restart(self):
        """Process 1 voids C (past horizon). Process 2 must NOT deliver it and
        must NOT re-void it — the durable status is VOIDED, excluded from pending.

        CAVEAT surfaced: the D-12 alert mirror (`void_ledger`) is in-RAM, so
        process 2 starts with an EMPTY void_ledger. The durable VOIDED status
        survives; the ALERT record does not. Asserted below and flagged in the
        artifact as a real restart-boundary gap in the alert path."""
        redis = fakeredis.aioredis.FakeRedis()
        try:
            await _put(redis, "C", first_ts=time.time() - 10_000)  # long past horizon
            p1 = _emitter(redis, _DownSNS(), horizon=100.0)
            await p1.drain()
            assert len(p1.void_ledger) == 1 and p1.void_ledger[0]["reason"] == "past_retry_horizon"
            del p1

            # --- restart ---
            sink2 = _OkSNS()
            p2 = _emitter(redis, sink2, horizon=100.0)
            delivered2 = await p2.drain()
            assert delivered2 == 0, "a voided row must not re-deliver"
            assert sink2.delivered == 0
            assert await p2.pending_count() == 0, "voided row is not pending"
            # The alert mirror did NOT survive the process (in-RAM). Documented gap.
            assert p2.void_ledger == [], (
                "void_ledger is process-local — the D-12 alert does not survive a "
                "restart even though the durable VOIDED status does (artifact finding)"
            )
        finally:
            await redis.flushall(); await redis.aclose()

    async def test_partial_delivery_resumes_only_the_remainder(self):
        """Process 1 delivers A and B, strands C and D (SNS fails those two).
        Process 2 delivers C and D. Each of the four is delivered EXACTLY once
        across the process boundary — no re-send of A/B, no loss of C/D."""
        redis = fakeredis.aioredis.FakeRedis()
        try:
            for rid in ("A", "B", "C", "D"):
                await _put(redis, rid)
            p1 = _emitter(redis, _FailKeysSNS(fail_ids={"C", "D"}))
            d1 = await p1.drain()
            assert d1 == 2
            assert await p1.pending_count() == 2
            del p1

            # --- restart ---
            sink2 = _FailKeysSNS(fail_ids=set())   # all succeed now
            p2 = _emitter(redis, sink2)
            d2 = await p2.drain()
            assert d2 == 2
            assert set(sink2.delivered_ids) == {"C", "D"}, "only the stranded remainder"
            assert await p2.pending_count() == 0
        finally:
            await redis.flushall(); await redis.aclose()
