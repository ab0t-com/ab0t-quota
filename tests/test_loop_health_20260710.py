"""W-T2 — D-50 part 2: loop liveness must reach a human (the scheduler ↔ the human).

Ticket 20260709. D-50 (from the drain-loop defect W-T2 found):
  1. Never test a background worker by calling the function it calls — drive the
     REAL loop and assert the effect. (These tests drive the real drain loop.)
  2. An exception inside a periodic loop must be LOUD; a swallowed exception that
     backs off forever is a dead worker inside a healthy process.
  3. Every periodic worker surfaces liveness; a money-critical loop that is
     permanently backing off / dead must FAIL /quota/health (D-40 boundary #8).

A dead drain silently re-opens QB-01 behind a 200. This suite proves the drain
and reconciler loops surface liveness, and that `quota_health` degrades on it.

fakeredis[lua] (lupa) is NOT Redis.
"""
from __future__ import annotations

import asyncio
import logging

import fakeredis.aioredis
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ab0t_quota.billing.lifecycle import LifecycleEmitter
from ab0t_quota.billing.outbox import InMemoryOutboxStore
from ab0t_quota.setup import (
    quota_health, quota_loop_liveness, _register_capability_routes, _MONEY_CRITICAL_LOOPS,
)


# --- fakes for the health-layer tests ---------------------------------------

class _FakeEmitter:
    def __init__(self, healthy, detail="x"): self._h = (healthy, detail)
    def drain_worker_liveness(self): return self._h


class _FakeReconciler:
    def __init__(self, healthy, detail="x"): self._h = (healthy, detail)
    def loop_liveness(self): return self._h


def _app(*, emitter=None, reconciler=None, caps=None) -> FastAPI:
    app = FastAPI()
    app.state.quota_capabilities = dict(caps or {"billing": "on", "reconciler": "on(ledger)"})
    if emitter is not None:
        app.state.quota_emitter = emitter
    if reconciler is not None:
        app.state.quota_reconciler = reconciler
    _register_capability_routes(app)
    return app


# ===========================================================================
# The health GATE — a dead money-critical loop fails /quota/health.
# ===========================================================================

class TestLoopHealthGate:
    def test_money_critical_loops_are_declared(self):
        """[negative control] If the money-critical loop set were emptied, every
        gate test would pass while the probe went permanently cheerful."""
        assert set(_MONEY_CRITICAL_LOOPS) >= {"outbox_drain", "reconciler_loop"}

    def test_dead_drain_loop_degrades_health(self):
        app = _app(emitter=_FakeEmitter(False, "backing off — money not draining"))
        h = quota_health(app)
        assert h["status"] == "degraded"
        assert "outbox_drain" in h["degraded"]
        assert TestClient(app).get("/quota/health").status_code == 503

    def test_dead_reconciler_loop_degrades_health(self):
        app = _app(reconciler=_FakeReconciler(False, "loop died"))
        h = quota_health(app)
        assert h["status"] == "degraded"
        assert "reconciler_loop" in h["degraded"]

    def test_healthy_loops_keep_health_ok(self):
        """[control] present + healthy loops must NOT degrade — a false 503 trains
        operators to ignore the probe."""
        app = _app(emitter=_FakeEmitter(True), reconciler=_FakeReconciler(True))
        assert quota_health(app)["status"] == "ok"

    def test_absent_loop_objects_do_not_degrade(self):
        """Backward compatible: an app that never wired the workers (the existing
        capability tests) is judged purely on caps — no loop objects, no loop
        degradation."""
        app = _app()  # no emitter/reconciler on state
        assert quota_health(app)["status"] == "ok"

    def test_capabilities_endpoint_surfaces_loop_liveness(self):
        app = _app(emitter=_FakeEmitter(False, "backing off"),
                   reconciler=_FakeReconciler(True, "on"))
        body = TestClient(app).get("/quota/capabilities").json()
        assert "loops" in body
        assert body["loops"]["outbox_drain"]["healthy"] is False
        assert body["loops"]["reconciler_loop"]["healthy"] is True

    def test_loop_liveness_only_names_keys_in_health_body(self):
        """House rule: the probe body carries capability/loop KEYS, not their
        (possibly sensitive) detail strings."""
        app = _app(emitter=_FakeEmitter(False, "secret-ish backoff detail"))
        body = quota_health(app)
        assert "outbox_drain" in body["degraded"]
        assert "secret-ish" not in str(body)


# ===========================================================================
# DRIVE THE REAL DRAIN LOOP (D-50 rule 1) — liveness + loudness on the real thing.
# ===========================================================================

class _RaisingStore(InMemoryOutboxStore):
    async def list_pending(self, limit: int = 100):
        raise RuntimeError("store outage")


class _RecoverableStore(InMemoryOutboxStore):
    def __init__(self): super().__init__(); self.fail = True
    async def list_pending(self, limit: int = 100):
        if self.fail:
            raise RuntimeError("transient outage")
        return await super().list_pending(limit)


TOPIC = "arn:aws:sns:us-east-1:123456789012:resource-lifecycle"


@pytest.mark.asyncio
async def test_real_drain_loop_reports_unhealthy_and_is_loud_after_sustained_failure(caplog):
    """Drive the REAL `_drain_loop` against a failing store. After sustained
    failures it reports UNHEALTHY and logs an ERROR (rule 2) — not just a repeating
    warning nobody reads. Nothing here calls `drain()` directly."""
    emitter = LifecycleEmitter(sns_topic_arn=TOPIC, outbox_store=_RaisingStore())
    with caplog.at_level(logging.ERROR, logger="ab0t_quota.billing.lifecycle"):
        emitter.start_drain_worker(interval_seconds=0.01, max_per_pass=10)
        # wait for the fail-streak to cross the unhealthy threshold
        healthy = True
        for _ in range(200):
            healthy, _detail = emitter.drain_worker_liveness()
            if not healthy:
                break
            await asyncio.sleep(0.01)
        await emitter.stop_drain_worker()

    assert healthy is False, "a permanently-failing drain loop must report unhealthy"
    assert any(r.levelno >= logging.ERROR for r in caplog.records), (
        "a loop backing off forever must be LOUD (ERROR), not a silent warning loop")


@pytest.mark.asyncio
async def test_real_drain_loop_recovers_to_healthy_after_failures_clear():
    """A transient outage that clears returns the loop to HEALTHY — the liveness
    signal is not sticky."""
    store = _RecoverableStore()
    emitter = LifecycleEmitter(sns_topic_arn=TOPIC, outbox_store=store)
    emitter.start_drain_worker(interval_seconds=0.01, max_per_pass=10)
    try:
        for _ in range(100):
            if not emitter.drain_worker_liveness()[0]:
                break
            await asyncio.sleep(0.01)
        assert emitter.drain_worker_liveness()[0] is False, "precondition: went unhealthy"
        store.fail = False   # outage clears
        recovered = False
        for _ in range(200):
            if emitter.drain_worker_liveness()[0]:
                recovered = True
                break
            await asyncio.sleep(0.01)
        assert recovered, "liveness must recover once the failures clear"
    finally:
        await emitter.stop_drain_worker()


@pytest.mark.asyncio
async def test_manual_drain_emitter_is_not_falsely_unhealthy():
    """A consumer that never started the background worker (manual drain mode) is
    NOT reported unhealthy — the gate is for a worker that WAS started and died,
    not for the absence of the worker."""
    emitter = LifecycleEmitter(sns_topic_arn=TOPIC, outbox_store=InMemoryOutboxStore())
    healthy, _ = emitter.drain_worker_liveness()
    assert healthy is True


@pytest.mark.asyncio
async def test_billing_disabled_drain_is_not_unhealthy():
    """When billing is disabled (no money events, no drain required), the drain
    loop's absence is a KNOWN visible state (billing cap = OFF), not a loop fault."""
    emitter = LifecycleEmitter(sns_topic_arn=TOPIC, outbox_store=InMemoryOutboxStore())
    emitter.disable_billing("no durable outbox")
    assert emitter.drain_worker_liveness()[0] is True


# ===========================================================================
# REAL RECONCILER loop liveness — refused/unsafe reads as not-live.
# ===========================================================================

@pytest.mark.asyncio
async def test_real_reconciler_refused_store_reports_unhealthy_loop():
    """A reconciler that REFUSED to start (in-memory ledger, D-37) reports its loop
    as not-live — so /quota/health degrades, not just a log line at startup."""
    from ab0t_quota.activations import InMemoryActivationStore
    from ab0t_quota.engine import QuotaEngine
    from ab0t_quota.models.core import CounterType, ResourceDef, TierConfig, TierLimits
    from ab0t_quota.providers import StaticTierProvider
    from ab0t_quota.registry import ResourceRegistry
    from ab0t_quota.reconcile import LibraryReconciler

    redis = fakeredis.aioredis.FakeRedis()
    try:
        reg = ResourceRegistry()
        reg.register(ResourceDef(service="test", resource_key="r.g", display_name="G",
                                 counter_type=CounterType.GAUGE, unit="u"))
        eng = QuotaEngine(redis=redis, tier_provider=StaticTierProvider({"o": "free"}),
                          registry=reg, tiers={"free": TierConfig(tier_id="free",
                          display_name="F", limits={"r.g": TierLimits(limit=10)})},
                          activation_store=InMemoryActivationStore())
        r = LibraryReconciler(eng)
        started = r.start()              # refuses: in-memory ledger with shared counter
        assert started is False
        healthy, detail = r.loop_liveness()
        assert healthy is False, f"a refused reconciler loop must read unhealthy: {detail}"
    finally:
        await redis.flushall(); await redis.aclose()
