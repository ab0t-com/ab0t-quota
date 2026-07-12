"""W-T2 — D1/D2: two guarantees that were implemented but never WIRED.

Ticket 20260709. The verifier's rule, run across the library:
  A public function whose only call site is a test is either dead code or a
  DISCONNECTED GUARANTEE. Two turned up, both downstream of W-T2's D-50 audit:

  D1 (QC-02) — `handler_ledger.drain_stale_leases` is implemented 3× (Redis/DDB/
       in-memory) and its only caller is a test. No scheduler. A handler that
       crashes mid-delivery strands its `in_progress` ledger row; auth already got
       its 200; nothing reclaims the lease → the credit grant is stranded FOREVER,
       invisible to `events --status failed` (the status is `in_progress`).
       Fix: a library-owned periodic sweeper (D-50 contract) started by setup,
       driven by a REAL-loop test, loud on exception, liveness in Capabilities,
       and in `_MONEY_CRITICAL_LOOPS`.

  D2 (QB-03) — `engine.stale_open_activations` has no periodic caller, so the
       missed-decrement ALARM never fires — the alarm for the bug that shipped
       three times in the reference consumer. Fix: call it from the reconciler
       pass and route it to the money-incident alert channel.

Both are tested here by driving the REAL periodic path, not by calling the
function directly (that is exactly how these stayed hidden — D-50 rule 1).

fakeredis[lua] (lupa) is NOT Redis.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta

import fakeredis.aioredis
import pytest
import pytest_asyncio

from ab0t_quota.activations import Activation, ActivationState, InMemoryActivationStore
from ab0t_quota.engine import QuotaEngine
from ab0t_quota.models.core import CounterType, ResourceDef, TierConfig, TierLimits
from ab0t_quota.providers import StaticTierProvider
from ab0t_quota.registry import ResourceRegistry
from ab0t_quota.reconcile import LibraryReconciler, ReconcileConfig
from ab0t_quota.alerts import AlertDispatcher, DriftAlertManager

pytestmark = pytest.mark.asyncio

RK = "sandbox.concurrent"


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.flushall()
    await r.aclose()


class _Recorder(AlertDispatcher):
    def __init__(self): self.alerts = []
    async def dispatch(self, alert): self.alerts.append(alert)
    def messages(self): return [a.message for a in self.alerts]


def _engine(redis, store):
    reg = ResourceRegistry()
    reg.register(ResourceDef(service="test", resource_key=RK, display_name="C",
                             counter_type=CounterType.GAUGE, unit="u"))
    return QuotaEngine(redis=redis, tier_provider=StaticTierProvider({"org-1": "free"}),
                       registry=reg, tiers={"free": TierConfig(tier_id="free",
                       display_name="F", limits={RK: TierLimits(limit=100)})},
                       activation_store=store)


async def _open(store, org, *, age_seconds, n=1):
    opened = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
    for i in range(n):
        await store.put_open(Activation(
            activation_id=f"a_{org}_{i}_{id(object())}", org_id=org, user_id=None,
            resource_key=RK, spend={RK: 1.0}, state=ActivationState.OPEN.value, opened_at=opened))


# ===========================================================================
# D2 (QB-03) — the missed-decrement alarm fires FROM THE RECONCILER PASS.
# ===========================================================================

class TestStaleActivationAlarm:
    async def test_reconciler_pass_alarms_on_stale_open_activation(self, redis):
        """An activation OPEN far past the stale threshold (a missed decrement) is
        surfaced as a MONEY incident during the reconciler pass — not left as
        invisible drift (QB-03). Driven via run_once, not by calling
        engine.stale_open_activations directly."""
        store = InMemoryActivationStore()
        eng = _engine(redis, store)
        await _open(store, "org-1", age_seconds=3600)   # 1h old, threshold below
        mgr, rec = _Recorder(), None
        dmgr = DriftAlertManager(redis=redis, dispatchers=[mgr], cooldown_seconds=600)
        r = LibraryReconciler(eng, drift_alerts=dmgr, config=ReconcileConfig(
            activity_guard_seconds=0, stale_activation_seconds=1, require_durable_ledger=False))
        res = await r.run_once(["org-1"])
        assert any("stale_open_activation" in m for m in [a.message for a in mgr.alerts]), (
            "a stale OPEN activation must alarm from the reconciler pass (QB-03)")

    async def test_fresh_activation_does_not_alarm(self, redis):
        """[control] An activation within the threshold does NOT alarm — the alarm
        is load-bearing, not always-on."""
        store = InMemoryActivationStore()
        eng = _engine(redis, store)
        await _open(store, "org-1", age_seconds=1)
        mgr = _Recorder()
        dmgr = DriftAlertManager(redis=redis, dispatchers=[mgr], cooldown_seconds=600)
        r = LibraryReconciler(eng, drift_alerts=dmgr, config=ReconcileConfig(
            activity_guard_seconds=0, stale_activation_seconds=3600, require_durable_ledger=False))
        await r.run_once(["org-1"])
        assert not any("stale_open_activation" in a.message for a in mgr.alerts)

    async def test_stale_alarm_disabled_by_zero_threshold(self, redis):
        """stale_activation_seconds=0 disables the alarm (opt-out for a consumer
        with legitimately long-lived resources)."""
        store = InMemoryActivationStore()
        eng = _engine(redis, store)
        await _open(store, "org-1", age_seconds=100000)
        mgr = _Recorder()
        dmgr = DriftAlertManager(redis=redis, dispatchers=[mgr], cooldown_seconds=600)
        r = LibraryReconciler(eng, drift_alerts=dmgr, config=ReconcileConfig(
            activity_guard_seconds=0, stale_activation_seconds=0, require_durable_ledger=False))
        await r.run_once(["org-1"])
        assert not any("stale_open_activation" in a.message for a in mgr.alerts)


# ===========================================================================
# D1 (QC-02) — the stale-lease SWEEPER runs as a real periodic loop.
# ===========================================================================

class _Store(InMemoryActivationStore):  # placeholder to keep imports symmetric
    pass


class TestStaleLeaseSweeper:
    async def test_sweeper_reprocesses_a_stranded_lease_via_the_real_loop(self):
        """A crashed handler strands an in_progress row with an expired lease. The
        library-owned sweeper LOOP (not a hand-called drain) reclaims it and re-runs
        the handler exactly once. Nothing here calls drain_stale_leases directly."""
        from ab0t_quota.handler_ledger import (
            InMemoryLedgerStore, LedgerStatus, StaleLeaseSweeper,
        )
        store = InMemoryLedgerStore()
        # Seed a stranded in_progress row with an already-expired lease.
        await store.record_attempt(
            handler_name="grant_credit", event_id="evt-1", event_type="payment.succeeded",
            event_payload={"event_id": "evt-1"}, user_id="u", org_id="o", lease_seconds=60)
        # Back-date the lease into the past (the crashed worker never returned).
        # get_row returns the stored object reference, so mutating it is enough.
        row = await store.get_row(handler_name="grant_credit", event_id="evt-1")
        row.lease_expires_at = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()

        runs = {"n": 0}
        async def redispatch(r):
            runs["n"] += 1
            await store.record_outcome(handler_name=r.handler_name, event_id=r.event_id,
                                       status=LedgerStatus.SUCCESS)

        sweeper = StaleLeaseSweeper(store, redispatch, interval_seconds=0.01)
        sweeper.start()
        try:
            for _ in range(100):
                if runs["n"]:
                    break
                await asyncio.sleep(0.01)
        finally:
            await sweeper.stop()
        assert runs["n"] == 1, "the sweeper loop must reclaim + re-run the stranded handler once"

    async def test_sweeper_reports_liveness_and_is_loud_on_failure(self, caplog):
        """A sweeper whose store keeps failing reports UNHEALTHY and is LOUD (D-50
        rule 2) — a dead sweeper silently strands credit grants."""
        from ab0t_quota.handler_ledger import InMemoryLedgerStore, StaleLeaseSweeper

        class _FailingStore(InMemoryLedgerStore):
            async def drain_stale_leases(self, *, handler, now=None, limit=100):
                raise RuntimeError("ledger outage")

        async def _noop(r): pass
        sweeper = StaleLeaseSweeper(_FailingStore(), _noop, interval_seconds=0.01)
        with caplog.at_level(logging.ERROR, logger="ab0t_quota.handler_ledger"):
            sweeper.start()
            healthy = True
            for _ in range(200):
                healthy, _d = sweeper.loop_liveness()
                if not healthy:
                    break
                await asyncio.sleep(0.01)
            await sweeper.stop()
        assert healthy is False, "a permanently-failing sweeper must report unhealthy"
        assert any(r.levelno >= logging.ERROR for r in caplog.records), "must be LOUD (rule 2)"

    async def test_healthy_sweeper_reports_live(self):
        from ab0t_quota.handler_ledger import InMemoryLedgerStore, StaleLeaseSweeper
        async def _noop(r): pass
        sweeper = StaleLeaseSweeper(InMemoryLedgerStore(), _noop, interval_seconds=0.01)
        sweeper.start()
        try:
            await asyncio.sleep(0.05)
            assert sweeper.loop_liveness()[0] is True
        finally:
            await sweeper.stop()

    async def test_sweeper_recovers_a_REAL_registered_handler_end_to_end(self):
        """END-TO-END: a real `@idempotent` handler crashed mid-delivery (row
        stranded in_progress, lease expired). The sweeper loop + the REAL
        `auth_events.redispatch_stale_row` reclaim it, re-run the handler once, and
        record SUCCESS — so the stranded credit grant is actually recovered, not
        just detectable."""
        from ab0t_quota.handler_ledger import (
            InMemoryLedgerStore, LedgerStatus, StaleLeaseSweeper, idempotent,
        )
        from ab0t_quota import auth_events as ae

        ae.clear_handlers()
        runs = {"n": 0}

        @idempotent(handler="grant_credit")
        async def grant(event, ctx):
            runs["n"] += 1

        ae.register_handler("payment.succeeded", grant)
        try:
            store = InMemoryLedgerStore()
            await store.record_attempt(
                handler_name="grant_credit", event_id="evt-9", event_type="payment.succeeded",
                event_payload={"event_id": "evt-9"}, user_id="u", org_id="o", lease_seconds=60)
            row = await store.get_row(handler_name="grant_credit", event_id="evt-9")
            row.lease_expires_at = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()

            async def redispatch(r):
                await ae.redispatch_stale_row(r, store)

            sweeper = StaleLeaseSweeper(store, redispatch, interval_seconds=0.01)
            sweeper.start()
            try:
                for _ in range(100):
                    if runs["n"]:
                        break
                    await asyncio.sleep(0.01)
            finally:
                await sweeper.stop()

            assert runs["n"] == 1, "the real handler must be re-run exactly once"
            final = await store.get_row(handler_name="grant_credit", event_id="evt-9")
            assert final.status == LedgerStatus.SUCCESS, "the stranded row must now be resolved"
        finally:
            ae.clear_handlers()

    async def test_redispatch_with_no_registered_handler_is_loud_and_leaves_row(self, caplog):
        """If no handler is registered for a stranded row, redispatch must be LOUD
        (ERROR) and leave the row in_progress — silently dropping it would re-hide
        the guarantee (the exact QC-02 failure)."""
        from ab0t_quota.handler_ledger import InMemoryLedgerStore, LedgerStatus
        from ab0t_quota import auth_events as ae

        ae.clear_handlers()
        store = InMemoryLedgerStore()
        await store.record_attempt(
            handler_name="orphan", event_id="evt-x", event_type="nobody.listens",
            event_payload={}, user_id="u", org_id="o", lease_seconds=60)
        row = await store.get_row(handler_name="orphan", event_id="evt-x")
        with caplog.at_level(logging.ERROR, logger="ab0t_quota.auth_events"):
            await ae.redispatch_stale_row(row, store)
        assert any("no registered handler" in r.getMessage().lower() for r in caplog.records)
        still = await store.get_row(handler_name="orphan", event_id="evt-x")
        assert still.status == LedgerStatus.IN_PROGRESS, "unrecoverable row stays visible, not dropped"


# ===========================================================================
# D1 health gate — a dead sweeper FAILS /quota/health (it is money-critical).
# ===========================================================================

class TestSweeperInHealthGate:
    async def test_stale_lease_sweeper_is_money_critical(self):
        """[negative control] If the sweeper is not money-critical, its death is
        invisible to the very probe built to catch absent guarantees."""
        from ab0t_quota.setup import _MONEY_CRITICAL_LOOPS
        assert "stale_lease_sweeper" in _MONEY_CRITICAL_LOOPS

    async def test_dead_sweeper_degrades_health(self):
        from fastapi import FastAPI
        from ab0t_quota.setup import quota_health, _register_capability_routes

        class _FakeSweeper:
            def loop_liveness(self): return (False, "sweeper died")

        app = FastAPI()
        app.state.quota_capabilities = {"billing": "on", "reconciler": "on(ledger)"}
        app.state.quota_stale_lease_sweeper = _FakeSweeper()
        _register_capability_routes(app)
        h = quota_health(app)
        assert h["status"] == "degraded"
        assert "stale_lease_sweeper" in h["degraded"]

    async def test_D54_required_but_never_wired_loop_degrades(self):
        """D-54 — the systemic turn: a loop DECLARED required (should run given
        config) but ABSENT from live liveness (never wired) has no capability entry
        at all. A probe can only degrade on entries it knows — so it must degrade on
        DECLARED-but-missing too. This is exactly how the sweeper stayed invisible.
        """
        from fastapi import FastAPI
        from ab0t_quota.setup import quota_health
        app = FastAPI()
        app.state.quota_capabilities = {"billing": "on", "reconciler": "on(ledger)"}
        app.state.quota_required_loops = {"stale_lease_sweeper"}  # declared required
        # ...but NO quota_stale_lease_sweeper object → never wired.
        h = quota_health(app)
        assert h["status"] == "degraded"
        assert "stale_lease_sweeper" in h["degraded"], (
            "a required-but-never-wired loop must fail the probe (absence of a loop "
            "is a level below absence of a value)")

    async def test_D54_required_and_wired_healthy_is_ok(self):
        """[control] A declared-required loop that IS wired + healthy does not
        degrade — the derivation is not a blanket fail."""
        from fastapi import FastAPI
        from ab0t_quota.setup import quota_health

        class _LiveSweeper:
            def loop_liveness(self): return (True, "on")

        app = FastAPI()
        app.state.quota_capabilities = {"billing": "on", "reconciler": "on(ledger)"}
        app.state.quota_required_loops = {"stale_lease_sweeper"}
        app.state.quota_stale_lease_sweeper = _LiveSweeper()
        assert quota_health(app)["status"] == "ok"
