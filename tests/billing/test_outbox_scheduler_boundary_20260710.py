"""W-T2 side-effects hardening — the OUTBOX ↔ THE SCHEDULER boundary.

Ticket 20260709_ab0t_quota_systemic_integrity_redesign.
Boundary crossed (D-40 table, row #1): **the scheduler** — "nothing called drain".
The library's own delivery guarantee lives in the *background loop*, not in a
hand-called ``drain()``. Every existing delivery test either calls ``drain()``
directly or breaks out of the wait the instant ``delivered==1`` — so none of them
observes what the loop does on an IDLE pass or on a SECOND pass. That near side of
the boundary is exactly where this ticket keeps finding defects.

This suite crosses it: it drives ``_drain_loop`` over multiple passes (idle, active,
budget-capped) and asserts the loop is not silently degrading.

It also pins the horizon boundary (D-9/D-12), drain idempotency, exactly-once void,
the bounded per-pass budget's deferral log, and backoff/kill-switch.

fakeredis[lua] (lupa) is NOT Redis — where a store is needed for a durability point
it is called out. The scheduler tests here use the in-memory store deliberately: the
loop bug is store-agnostic.

Negative controls are marked ``[NEG-CTRL]`` in their docstrings — each was run with
the property deliberately broken to confirm it goes RED. See the artifact.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from ab0t_quota.billing.lifecycle import LifecycleEmitter
from ab0t_quota.billing.outbox import InMemoryOutboxStore, OutboxRecord

TOPIC = "arn:aws:sns:us-east-1:123456789012:resource-lifecycle"


class _OkSNS:
    def __init__(self):
        self.delivered = 0

    def publish(self, **kwargs):
        self.delivered += 1
        return {"MessageId": f"m-{self.delivered}"}


class _RaisingSNS:
    def __init__(self):
        self.attempts = 0

    def publish(self, **kwargs):
        self.attempts += 1
        raise RuntimeError("SNS down")


class _FailNthSNS:
    """Fails publish attempt number ``fail_on`` (1-indexed), succeeds otherwise."""

    def __init__(self, fail_on: int):
        self.fail_on = fail_on
        self.attempts = 0
        self.delivered = 0

    def publish(self, **kwargs):
        self.attempts += 1
        if self.attempts == self.fail_on:
            raise RuntimeError("transient publish failure")
        self.delivered += 1
        return {"MessageId": f"m-{self.attempts}"}


def _emitter(sink, *, store=None, horizon=900.0) -> LifecycleEmitter:
    em = LifecycleEmitter(
        sns_topic_arn=TOPIC,
        outbox_store=store if store is not None else InMemoryOutboxStore(),
        outbox_max_retry_horizon_s=horizon,
    )
    em._client = sink
    em._get_client = lambda: sink
    return em


async def _enqueue_failed(emitter, sink_swap=None, *, rid="rsv-x") -> None:
    """Emit a terminal money event whose first publish fails → one PENDING intent."""
    await emitter.resource_stopped(
        org_id="org-1", user_id="alice", resource_id="sb-x",
        resource_type="sandbox", reservation_id=rid,
        hourly_rate=Decimal("0.10"), allocation_fee=Decimal("0.00"),
        started_at=datetime(2026, 4, 1, 9, tzinfo=timezone.utc),
        stopped_at=datetime(2026, 4, 1, 10, tzinfo=timezone.utc),
    )


# ===========================================================================
# THE SCHEDULER BOUNDARY — the background loop must survive an idle / a second
# pass. (Confirmed defect: `_drain_loop` referenced `self.outbox`, an attribute
# D-29 deleted → AttributeError on EVERY pass → permanent error-backoff.)
# ===========================================================================

class TestDrainLoopSurvivesEveryPass:
    @pytest.mark.asyncio
    async def test_idle_pass_does_not_raise_inside_the_loop(self, caplog):
        """[the scheduler] An EMPTY outbox is the NORMAL steady state. The loop
        must idle quietly, not error-and-backoff.

        [NEG-CTRL] With the shipped `self.outbox` reference, an empty pass
        evaluates `if delivered or self.outbox:` → AttributeError, caught by the
        loop's `except Exception` → logged as `outbox_drain_pass_error` → backoff.
        This test then goes RED. Fixed: the loop uses the durable store, no attr.
        """
        emitter = _emitter(_OkSNS())          # nothing enqueued → every pass idle
        with caplog.at_level(logging.WARNING, logger="ab0t_quota.billing.lifecycle"):
            emitter.start_drain_worker(interval_seconds=0.01, max_per_pass=50)
            await asyncio.sleep(0.08)          # several idle passes
            await emitter.stop_drain_worker()

        offending = [r for r in caplog.records
                     if "outbox_drain_pass_error" in r.getMessage()]
        assert not offending, (
            "the background drain loop errored on an idle pass — a scheduler-boundary "
            f"defect the direct-drain tests never see. First: {offending[0].getMessage() if offending else ''}"
        )

    @pytest.mark.asyncio
    async def test_delivering_pass_does_not_raise_after_the_delivery(self, caplog):
        """[the scheduler] A pass that DELIVERS must not crash at its post-drain
        log line either (`len(self.outbox)` was the same stale attribute). The
        event is delivered by drain() first, so the crash is invisible to a test
        that only checks `delivered` — this one checks the LOG stream too.

        [NEG-CTRL] Stale `self.outbox` → the post-delivery log line raises →
        `outbox_drain_pass_error`. RED with the bug, GREEN once fixed.
        """
        sink = _FailNthSNS(fail_on=1)          # first publish (during emit) fails
        emitter = _emitter(sink)
        await _enqueue_failed(emitter)
        assert await emitter.pending_count() == 1

        with caplog.at_level(logging.WARNING, logger="ab0t_quota.billing.lifecycle"):
            emitter.start_drain_worker(interval_seconds=0.01, max_per_pass=50)
            for _ in range(100):
                if sink.delivered:
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.04)           # a couple more (now idle) passes
            await emitter.stop_drain_worker()

        assert sink.delivered == 1
        offending = [r for r in caplog.records
                     if "outbox_drain_pass_error" in r.getMessage()]
        assert not offending, (
            "the loop errored after a real delivery — the post-drain log line "
            f"referenced a deleted attribute. {offending[0].getMessage() if offending else ''}"
        )

    @pytest.mark.asyncio
    async def test_loop_keeps_delivering_across_an_idle_gap(self):
        """[the scheduler] End-to-end: deliver one, go idle, then deliver a
        second enqueued later. A loop stuck in error-backoff after the idle gap
        would delay (or, with escalating backoff on a real interval, strand) the
        second event. Nothing here calls drain() — the loop owns delivery."""
        sink = _OkSNS()
        # publish path: emit's first publish SUCCEEDS for these, so to force the
        # outbox path we enqueue directly as PENDING and let the loop deliver.
        store = InMemoryOutboxStore()
        emitter = _emitter(sink, store=store)
        now = time.time()
        await store.put_intent(OutboxRecord(
            key="rsv-a:resource.stopped", event={"reservation_id": "rsv-a"},
            event_type="resource.stopped", resource_type="sandbox",
            reservation_id="rsv-a", first_ts=now))

        emitter.start_drain_worker(interval_seconds=0.01, max_per_pass=50)
        try:
            for _ in range(100):
                if sink.delivered >= 1:
                    break
                await asyncio.sleep(0.01)
            assert sink.delivered == 1
            await asyncio.sleep(0.05)           # idle passes in between
            # a second money event arrives later
            await store.put_intent(OutboxRecord(
                key="rsv-b:resource.stopped", event={"reservation_id": "rsv-b"},
                event_type="resource.stopped", resource_type="sandbox",
                reservation_id="rsv-b", first_ts=time.time()))
            for _ in range(100):
                if sink.delivered >= 2:
                    break
                await asyncio.sleep(0.01)
            assert sink.delivered == 2, "loop did not deliver a post-idle event promptly"
        finally:
            await emitter.stop_drain_worker()


# ===========================================================================
# HORIZON BOUNDARY (D-9 / D-12) — horizon−ε, exactly horizon, horizon+ε.
# ===========================================================================

class TestHorizonBoundary:
    @pytest.mark.asyncio
    async def test_inside_at_and_past_horizon_under_a_frozen_clock(self, monkeypatch):
        """horizon−ε delivers; EXACTLY at the horizon delivers; horizon+ε is
        voided+alerted. The predicate is strict `age > horizon`, so age==horizon
        is INSIDE. To test that boundary deterministically the clock is FROZEN —
        see the next test for why exactly-at is not observable on a live clock.
        """
        horizon = 100.0
        frozen = 1_000_000.0
        monkeypatch.setattr(time, "time", lambda: frozen)
        sink = _OkSNS()
        store = InMemoryOutboxStore()
        emitter = _emitter(sink, store=store, horizon=horizon)

        for rid, age in (("inside", horizon - 1), ("at", horizon), ("past", horizon + 1)):
            await store.put_intent(OutboxRecord(
                key=f"{rid}:resource.stopped", event={"reservation_id": rid},
                event_type="resource.stopped", resource_type="sandbox",
                reservation_id=rid, first_ts=frozen - age))

        delivered = await emitter.drain()
        assert delivered == 2, "inside and exactly-at-horizon must deliver (strict >)"
        assert sink.delivered == 2
        voided = {v["reservation_id"] for v in emitter.void_ledger}
        assert voided == {"past"}, "only the past-horizon event is voided"
        assert all(v["alerted"] for v in emitter.void_ledger), "a void must ALERT (D-12)"

    @pytest.mark.asyncio
    async def test_exactly_at_horizon_tips_to_past_on_a_live_clock(self):
        """FINDING: 'exactly at the horizon' is a knife-edge, not a state. The
        anchor (first_ts) and the evaluation (drain's time.time()) are read at
        DIFFERENT instants, so any real elapsed time makes age = horizon + δ >
        horizon → the event VOIDS. An event authored to sit 'exactly at' the
        horizon therefore resolves to the SAFE side (void+alert), never a silent
        delivery of a settlement that would 404 at billing. Documented, not a bug.
        """
        horizon = 0.02
        sink = _OkSNS()
        store = InMemoryOutboxStore()
        emitter = _emitter(sink, store=store, horizon=horizon)
        anchor = time.time()
        await store.put_intent(OutboxRecord(
            key="edge:resource.stopped", event={"reservation_id": "edge"},
            event_type="resource.stopped", resource_type="sandbox",
            reservation_id="edge", first_ts=anchor - horizon))  # 'exactly at' at author-time
        await asyncio.sleep(0.03)  # the clock advances before drain evaluates
        delivered = await emitter.drain()
        assert delivered == 0, "elapsed time tips an at-horizon event past it"
        assert [v["reservation_id"] for v in emitter.void_ledger] == ["edge"]


# ===========================================================================
# DRAIN IDEMPOTENCY + EXACTLY-ONCE VOID (D-12: never silent, never dropped).
# ===========================================================================

class TestDrainIdempotency:
    @pytest.mark.asyncio
    async def test_draining_twice_delivers_once(self):
        """Two full drains over the same pending intent deliver exactly one SNS
        message — mark_delivered removes it, so the second pass finds nothing."""
        sink = _OkSNS()
        store = InMemoryOutboxStore()
        emitter = _emitter(sink, store=store)
        await store.put_intent(OutboxRecord(
            key="rsv-1:resource.stopped", event={"reservation_id": "rsv-1"},
            event_type="resource.stopped", resource_type="sandbox",
            reservation_id="rsv-1", first_ts=time.time()))

        assert await emitter.drain() == 1
        assert await emitter.drain() == 0
        assert sink.delivered == 1
        assert await emitter.pending_count() == 0

    @pytest.mark.asyncio
    async def test_past_horizon_void_fires_exactly_once(self):
        """A past-horizon event is voided+alerted ONCE; a second drain does not
        re-void it (the durable status is VOIDED → excluded from list_pending)."""
        sink = _OkSNS()
        store = InMemoryOutboxStore()
        emitter = _emitter(sink, store=store, horizon=10.0)
        await store.put_intent(OutboxRecord(
            key="rsv-old:resource.stopped", event={"reservation_id": "rsv-old"},
            event_type="resource.stopped", resource_type="sandbox",
            reservation_id="rsv-old", first_ts=time.time() - 1000))

        await emitter.drain()
        await emitter.drain()
        assert sink.delivered == 0, "a past-horizon event must not be delivered"
        assert len(emitter.void_ledger) == 1, "void must fire exactly once, not per pass"
        assert emitter.void_ledger[0]["reason"] == "past_retry_horizon"

    @pytest.mark.asyncio
    async def test_interrupted_pass_resumes_the_undelivered_remainder(self):
        """[the scheduler] A pass that delivers SOME then hits a publish failure
        leaves the remainder PENDING; the next pass finishes it. Exactly-once
        overall, no double-send of the ones already delivered."""
        sink = _FailNthSNS(fail_on=2)          # 1st ok, 2nd fails, 3rd ok
        store = InMemoryOutboxStore()
        emitter = _emitter(sink, store=store)
        now = time.time()
        for i in range(3):
            await store.put_intent(OutboxRecord(
                key=f"rsv-{i}:resource.stopped", event={"reservation_id": f"rsv-{i}"},
                event_type="resource.stopped", resource_type="sandbox",
                reservation_id=f"rsv-{i}", first_ts=now + i))  # stable order

        first = await emitter.drain()
        assert first == 2, "two delivered before the transient failure"
        assert await emitter.pending_count() == 1, "the failed one stays pending"
        second = await emitter.drain()
        assert second == 1
        assert sink.delivered == 3, "each event delivered exactly once across the two passes"
        assert await emitter.pending_count() == 0


# ===========================================================================
# BOUNDED PER-PASS BUDGET — honored AND it LOGS what it deferred.
# A silent cap reads as "covered everything".
# ===========================================================================

class TestBoundedBudget:
    @pytest.mark.asyncio
    async def test_budget_is_honored_and_the_deferral_is_logged(self, caplog):
        """max_per_pass caps a single drain and LOGS `outbox_drain_budget_reached`
        so a delivery storm's remainder is visible, not silently deferred."""
        sink = _OkSNS()
        store = InMemoryOutboxStore()
        emitter = _emitter(sink, store=store)
        now = time.time()
        for i in range(5):
            await store.put_intent(OutboxRecord(
                key=f"rsv-{i}:resource.stopped", event={"reservation_id": f"rsv-{i}"},
                event_type="resource.stopped", resource_type="sandbox",
                reservation_id=f"rsv-{i}", first_ts=now + i))

        with caplog.at_level(logging.WARNING, logger="ab0t_quota.billing.lifecycle"):
            delivered = await emitter.drain(max_per_pass=2)

        assert delivered == 2, "the per-pass budget capped this pass at 2"
        assert await emitter.pending_count() == 3, "the remainder waits for the next pass"
        assert any("outbox_drain_budget_reached" in r.getMessage() for r in caplog.records), (
            "a silent cap reads as 'covered everything' — the deferral MUST be logged"
        )

    @pytest.mark.asyncio
    async def test_budget_not_logged_when_under_cap(self, caplog):
        """[NEG-CTRL for the log above] Under the cap, no deferral log — proving
        the warning is load-bearing, not always-on."""
        sink = _OkSNS()
        store = InMemoryOutboxStore()
        emitter = _emitter(sink, store=store)
        await store.put_intent(OutboxRecord(
            key="rsv-1:resource.stopped", event={"reservation_id": "rsv-1"},
            event_type="resource.stopped", resource_type="sandbox",
            reservation_id="rsv-1", first_ts=time.time()))
        with caplog.at_level(logging.WARNING, logger="ab0t_quota.billing.lifecycle"):
            await emitter.drain(max_per_pass=100)
        assert not any("outbox_drain_budget_reached" in r.getMessage()
                       for r in caplog.records)


# ===========================================================================
# BACKOFF ON REPEATED FAILURE + KILL-SWITCH (the scheduler again).
# ===========================================================================

class _RaisingStore(InMemoryOutboxStore):
    """list_pending raises — a persistent drain failure (e.g. store outage)."""

    def __init__(self):
        super().__init__()
        self.calls = 0

    async def list_pending(self, limit: int = 100):
        self.calls += 1
        raise RuntimeError("store outage")


class TestBackoffAndKillSwitch:
    @pytest.mark.asyncio
    async def test_persistent_drain_failure_backs_off_and_does_not_kill_the_loop(self, caplog):
        """A drain that keeps failing is caught, backed off, and the loop SURVIVES
        (it must not die on a transient store outage). We observe the backoff log."""
        store = _RaisingStore()
        emitter = _emitter(_OkSNS(), store=store)
        with caplog.at_level(logging.WARNING, logger="ab0t_quota.billing.lifecycle"):
            emitter.start_drain_worker(interval_seconds=0.01, max_per_pass=10)
            await asyncio.sleep(0.06)
            still_running = emitter._drain_task is not None and not emitter._drain_task.done()
            await emitter.stop_drain_worker()

        assert still_running, "a failing drain pass must not kill the loop"
        assert any("outbox_drain_pass_error" in r.getMessage() and "backoff" in r.getMessage()
                   for r in caplog.records), "a repeated failure must log a backoff"

    @pytest.mark.asyncio
    async def test_kill_switch_halts_draining(self, monkeypatch):
        """AB0T_QUOTA_OUTBOX_DRAIN_ENABLED=false stops draining without a redeploy.
        (Complements the peer test in test_integrity_delivery_20260710.py; here the
        event is pre-staged in the durable store and must stay put.)"""
        monkeypatch.setenv("AB0T_QUOTA_OUTBOX_DRAIN_ENABLED", "false")
        sink = _OkSNS()
        store = InMemoryOutboxStore()
        emitter = _emitter(sink, store=store)
        await store.put_intent(OutboxRecord(
            key="rsv-k:resource.stopped", event={"reservation_id": "rsv-k"},
            event_type="resource.stopped", resource_type="sandbox",
            reservation_id="rsv-k", first_ts=time.time()))
        emitter.start_drain_worker(interval_seconds=0.01)
        await asyncio.sleep(0.06)
        await emitter.stop_drain_worker()
        assert sink.delivered == 0, "kill-switch did not halt draining"
        assert await emitter.pending_count() == 1
