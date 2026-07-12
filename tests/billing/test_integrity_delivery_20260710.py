"""P0.4 — Delivery red suite (ticket 20260709_ab0t_quota_systemic_integrity_redesign).

RED-BY-DESIGN. Reproduces the fire-and-forget lifecycle-delivery defect and the
missing settle-once invariant, asserting the fixed behaviour so each test FAILS
on current code.

Findings covered:
  - QB-01  LifecycleEmitter.emit is fire-and-forget: it returns False when
           unconfigured (lifecycle.py:141-143) OR on publish exception
           (:175-177) — no outbox, no retry, no metric. A dropped emit means the
           reservation is never committed, the sweep expires it 24h later as a
           $0 release, and no usage row is ever written → usage is silently
           un-billed. Fix: durable outbox + drain (P3.1). RED asserts a failed
           publish is durably retained for redelivery, not silently dropped.
  - QM-02  No "one started resource => exactly one settled usage row" invariant.
           A dropped terminal event is invisible in the ledger except as a $0
           release. Fix: activation => one settlement invariant (P3.3). RED
           asserts exactly one terminal settlement is delivered per activation
           even when the first publish fails.

Scope note: QM-02's authoritative proof is an end-to-end UJ against billing
(P3.3, partly W-DISC). This is the LIBRARY-level proxy — it exercises the
emitter's delivery guarantee with a fake SNS sink, no live billing.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ab0t_quota.billing.lifecycle import LifecycleEmitter

TOPIC = "arn:aws:sns:us-east-1:123456789012:resource-lifecycle"


class _RaisingSNS:
    """Fake boto3 SNS client whose publish always raises (SNS outage)."""

    def __init__(self):
        self.publish_attempts = 0

    def publish(self, **kwargs):
        self.publish_attempts += 1
        raise RuntimeError("simulated SNS outage")


class _FlakySNS:
    """Fails the first publish, succeeds afterwards. Counts DELIVERED messages."""

    def __init__(self):
        self.publish_attempts = 0
        self.delivered = 0

    def publish(self, **kwargs):
        self.publish_attempts += 1
        if self.publish_attempts == 1:
            raise RuntimeError("simulated transient SNS failure")
        self.delivered += 1
        return {"MessageId": f"m-{self.publish_attempts}"}


def _emitter_with(sink) -> LifecycleEmitter:
    em = LifecycleEmitter(sns_topic_arn=TOPIC)
    em._client = sink            # bypass boto3 construction
    em._get_client = lambda: sink
    return em


# ---------------------------------------------------------------------------
# QB-01 — fire-and-forget emit silently drops the terminal event
# ---------------------------------------------------------------------------

class TestFireAndForgetDrop:
    @pytest.mark.asyncio
    async def test_failed_publish_is_not_silently_dropped(self):
        """A terminal event whose SNS publish raises must be durably retained
        for redelivery. Today emit() catches the exception, returns False, and
        drops it — no outbox, no retry, no metric.

        RED today: emit returns False and nothing is retained. GREEN target
        (durable outbox + drain, P3.1): the event is queued for redelivery.
        """
        sink = _RaisingSNS()
        emitter = _emitter_with(sink)

        ok = await emitter.resource_stopped(
            org_id="org-1", user_id="alice",
            resource_id="sb-1", resource_type="sandbox",
            reservation_id="rsv-1",
            hourly_rate=Decimal("0.10"), allocation_fee=Decimal("0.01"),
            started_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        )

        # Document the drop: publish was attempted and the event was lost.
        assert sink.publish_attempts == 1
        assert ok is False, "precondition: emit swallowed the publish failure and returned False"

        # Fix contract: a failed publish must leave a durable, retryable intent.
        assert await emitter.pending_count() == 1, (
            "QB-01: SNS publish failed and the terminal event was silently dropped "
            "(no outbox, no retry, no metric). The reservation will expire as a $0 "
            "release with no usage row → usage never billed. A durable outbox must "
            "retain the event for redelivery."
        )


# ---------------------------------------------------------------------------
# QM-02 — one activation => exactly one settled/delivered terminal event
# ---------------------------------------------------------------------------

class TestSettleOnceInvariant:
    @pytest.mark.asyncio
    async def test_one_activation_yields_exactly_one_settlement(self):
        """One resource starts and stops once. Its single terminal event must
        reach billing exactly once, even if the first publish fails transiently.
        With at-least-once delivery + billing idempotency the effect is
        exactly-once; today a transient failure yields ZERO deliveries with no
        retry, so the usage is invisible (only a future $0 release).

        RED today: 0 terminal events delivered and no redelivery mechanism.
        GREEN target (outbox drain + settle-once, P3.1/P3.3): exactly 1.
        """
        sink = _FlakySNS()
        emitter = _emitter_with(sink)

        # The one and only terminal event for this activation. First publish fails.
        await emitter.resource_stopped(
            org_id="org-1", user_id="alice",
            resource_id="sb-42", resource_type="sandbox",
            reservation_id="rsv-42",
            hourly_rate=Decimal("0.20"), allocation_fee=Decimal("0.00"),
            started_at=datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc),
            stopped_at=datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc),
        )

        # Drive whatever redelivery mechanism the library provides.
        drain = getattr(emitter, "drain", None) or getattr(emitter, "drain_outbox", None)
        if drain is not None:
            await drain()

        assert sink.delivered == 1, (
            f"QM-02: activation delivered {sink.delivered} terminal settlements "
            f"(expected exactly 1). A transient publish failure left the usage "
            f"un-settled with no redelivery — invisible in the ledger except as a "
            f"future $0 release."
        )


# ---------------------------------------------------------------------------
# QB-01 (Claim R1) — the drain worker must actually RUN under the lifespan and
# deliver. "The outbox retains but nothing drains" is not a fix.
# ---------------------------------------------------------------------------

class TestDrainWorkerUnderLifespan:
    @pytest.mark.asyncio
    async def test_background_drain_worker_started_by_lifespan_delivers_and_stops(self):
        """A money event whose first publish fails is enqueued. A FastAPI-style
        lifespan STARTS the library-owned drain worker; the worker (not a
        hand-called drain()) redelivers the event autonomously; the lifespan
        exit STOPS the worker. This is the distinction that makes QB-01 a fix
        rather than a table: nothing in this test calls emitter.drain() directly.
        """
        from fastapi import FastAPI

        sink = _FlakySNS()               # first publish fails, then succeeds
        emitter = _emitter_with(sink)

        await emitter.resource_stopped(
            org_id="org-1", user_id="alice",
            resource_id="sb-drain", resource_type="sandbox",
            reservation_id="rsv-drain-1",
            hourly_rate=Decimal("0.10"), allocation_fee=Decimal("0.00"),
            started_at=datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc),
            stopped_at=datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc),
        )
        assert await emitter.pending_count() == 1, "precondition: the failed event was enqueued for retry"
        assert sink.delivered == 0

        @asynccontextmanager
        async def lifespan(app):
            emitter.start_drain_worker(interval_seconds=0.02, max_per_pass=50)
            try:
                yield
            finally:
                await emitter.stop_drain_worker()

        app = FastAPI(lifespan=lifespan)
        async with app.router.lifespan_context(app):
            # The worker runs itself; we only WAIT for it (never call drain()).
            for _ in range(100):
                if sink.delivered:
                    break
                await asyncio.sleep(0.02)

        assert sink.delivered == 1, (
            "QB-01/R1: the background drain worker started by the lifespan did not "
            "deliver the stranded event — the outbox retains but nothing drains."
        )
        assert await emitter.pending_count() == 0, "outbox should be empty after delivery"
        assert emitter._drain_task is None, "drain worker must be stopped on lifespan shutdown"

    @pytest.mark.asyncio
    async def test_drain_worker_respects_runtime_kill_switch(self, monkeypatch):
        """AB0T_QUOTA_OUTBOX_DRAIN_ENABLED=false halts draining without a
        redeploy (mirrors the consumer's POOL_QUOTA_RECONCILE_ENABLED pattern)."""
        monkeypatch.setenv("AB0T_QUOTA_OUTBOX_DRAIN_ENABLED", "false")
        sink = _FlakySNS()
        emitter = _emitter_with(sink)
        await emitter.resource_stopped(
            org_id="org-1", user_id="alice",
            resource_id="sb-ks", resource_type="sandbox", reservation_id="rsv-ks",
            hourly_rate=Decimal("0.10"), allocation_fee=Decimal("0.00"),
            started_at=datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc),
            stopped_at=datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc),
        )
        emitter.start_drain_worker(interval_seconds=0.02)
        await asyncio.sleep(0.15)  # several passes would have run
        await emitter.stop_drain_worker()
        assert sink.delivered == 0, "kill-switch did not halt the drain worker"
        assert await emitter.pending_count() == 1, "event should still be pending (draining was disabled)"


class _OkSNS:
    """Fake SNS whose publish always succeeds. Counts delivered messages."""
    def __init__(self): self.delivered = 0
    def publish(self, **kwargs):
        self.delivered += 1
        return {"MessageId": f"m-{self.delivered}"}


# ---------------------------------------------------------------------------
# D-29 — the CRUX. The outbox must survive a PROCESS RESTART. This is the only
# test that distinguishes a durable outbox from a queue: it discards the whole
# in-process state (emitter + store object) and resumes delivery from the
# external store with brand-new objects.
# ---------------------------------------------------------------------------

class TestOutboxSurvivesRestart:
    @pytest.mark.asyncio
    async def test_restart_resumes_delivery_from_durable_store(self):
        """Process 1 writes a durable intent then crashes with the event still
        undelivered (SNS down). Process 2 — brand-new emitter AND a brand-new
        store object over the SAME external Redis — drains and delivers it. An
        in-memory outbox would have evaporated with process 1, silently
        un-billing the usage (QB-01). The durable store makes it survive.

        NOTE: fakeredis is in-process; it stands in for a real external Redis.
        The load-bearing fact is that the outbox state lives in the REDIS
        KEYSPACE, decoupled from the emitter/store objects — so recreating them
        (the 'restart') loses nothing. Real-Redis is unverified (pre-deploy gate).
        """
        import fakeredis.aioredis
        from ab0t_quota.billing.outbox import RedisOutboxStore

        redis = fakeredis.aioredis.FakeRedis()
        started = datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc)
        stopped = datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc)
        try:
            # --- process 1: SNS is down → intent persisted, publish fails. ---
            store1 = RedisOutboxStore(redis)
            emitter1 = LifecycleEmitter(sns_topic_arn=TOPIC, outbox_store=store1)
            down = _RaisingSNS()
            emitter1._client = down
            emitter1._get_client = lambda: down

            ok = await emitter1.resource_stopped(
                org_id="org-1", user_id="alice",
                resource_id="sb-restart", resource_type="sandbox",
                reservation_id="rsv-restart",
                hourly_rate=Decimal("0.10"), allocation_fee=Decimal("0.00"),
                started_at=started, stopped_at=stopped,
            )
            assert ok is False
            assert await emitter1.pending_count() == 1, "intent must be durably pending"

            # --- CRASH: discard ALL in-process state (emitter + store object). ---
            del emitter1, store1, down

            # --- process 2 (restart): brand-new objects, SAME external Redis. ---
            store2 = RedisOutboxStore(redis)
            up = _OkSNS()
            emitter2 = LifecycleEmitter(sns_topic_arn=TOPIC, outbox_store=store2)
            emitter2._client = up
            emitter2._get_client = lambda: up

            # The fresh process resumes delivery ENTIRELY from the store.
            delivered = await emitter2.drain()
            assert delivered == 1, (
                "D-29: a restart did NOT resume delivery — the outbox state did not "
                "survive the process. An in-memory queue re-opens QB-01 on every restart."
            )
            assert up.delivered == 1
            assert await emitter2.pending_count() == 0
        finally:
            await redis.flushall()
            await redis.aclose()
