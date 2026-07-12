"""W-T2 — P3.4 / QB-04 / D-67: HeartbeatMonitor is superseded, dormant, and
un-feedable as wired.

Ticket 20260709. The D-64 rule found `HeartbeatMonitor`'s public API has ZERO
callers — not the library, not a test. Worse: its only feed, `record()`, lives in
internal `paid_state`, is exposed to no consumer, and is called by nothing — it is
**un-feedable as wired**. A running loop scanning an always-empty keyspace is
dormant safety code, and dormant safety code creates FALSE CONFIDENCE (an operator
believes crash-detection is active when it structurally cannot be).

DECISION D-67 = Hypothesis A (SUPERSEDED). The `observed_usage_provider` +
reconciler (D-33) are the authoritative, provider-PULLED existence signal — a
crashed process cannot PUSH its own "I'm dead" heartbeat, but a provider reads the
world regardless. `stale_open_activations` (D2) is the "open too long" alarm. So:
  - DEPRECATE `HeartbeatMonitor` (warning → provider seam); do NOT remove (D-65,
    owner sign-off — it is public API on a shipped library).
  - DE-DORMANT: setup only constructs/starts it when `heartbeat.enabled` (default
    False), so the default path carries no un-feedable loop / false confidence.
  - REFUSE-GATE for anyone who still opts in: `heartbeat.enabled ⇒ required loop`
    (D-66), and a CONFIGURED-BUT-UNFED monitor degrades /quota/health — "you turned
    it on and never fed it" is loud, not dormant.

Negative controls are marked; each is run both ways (see the artifact).
This suite is **self-attested** — with the campaign winding down it may not get an
independent gate. The negative controls are written to be airtight.
"""
from __future__ import annotations

import warnings

import fakeredis.aioredis
import pytest
from fastapi import FastAPI

from ab0t_quota.billing.heartbeat import HeartbeatMonitor
from ab0t_quota.setup import (
    required_money_loops, quota_health, quota_loop_liveness, _MONEY_CRITICAL_LOOPS,
)


class _NullEmitter:
    async def resource_stopped(self, **kwargs):  # never reached in these tests
        return True


def _monitor():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return HeartbeatMonitor(redis=fakeredis.aioredis.FakeRedis(), emitter=_NullEmitter())


# ===========================================================================
# A — DEPRECATION (superseded by the provider seam).
# ===========================================================================

class TestDeprecation:
    def test_construction_emits_deprecation_warning_naming_the_provider_seam(self):
        with pytest.warns(DeprecationWarning) as rec:
            HeartbeatMonitor(redis=fakeredis.aioredis.FakeRedis(), emitter=_NullEmitter())
        msg = str(rec[0].message)
        assert "observed_usage_provider" in msg, "the warning must name the replacement seam"


# ===========================================================================
# REFUSE-GATE — a configured-but-UNFED monitor is unhealthy; a fed one is healthy.
# (The two are each other's control.)
# ===========================================================================

class TestFedGate:
    @pytest.mark.asyncio
    async def test_configured_unfed_monitor_reports_unhealthy(self):
        m = _monitor()
        healthy, detail = m.monitor_liveness()
        assert healthy is False, "a monitor that has never been fed must report unhealthy"
        assert "unfed" in detail.lower()

    @pytest.mark.asyncio
    async def test_fed_monitor_reports_healthy(self):
        """[control] Once a heartbeat is recorded, the monitor is healthy — proving
        the unfed signal is load-bearing, not always-red."""
        m = _monitor()
        await m.record("res-1", {"org_id": "o", "reservation_id": "r",
                                 "started_at": "2026-04-01T00:00:00Z"})
        assert m.monitor_liveness()[0] is True


# ===========================================================================
# D-66 DERIVATION — heartbeat.enabled ⇒ heartbeat_monitor required.
# ===========================================================================

class TestDerivation:
    def test_enabled_requires_the_monitor(self):
        req = required_money_loops({"heartbeat": {"enabled": True}, "reconcile": {"enabled": False}},
                                   enable_paid=False)
        assert "heartbeat_monitor" in req

    def test_disabled_by_default_not_required(self):
        req = required_money_loops({"reconcile": {"enabled": False}}, enable_paid=False)
        assert "heartbeat_monitor" not in req

    def test_heartbeat_monitor_is_money_critical(self):
        """[negative control] If it were not money-critical, an opted-in but unfed
        monitor's dormancy would be invisible to the probe."""
        assert "heartbeat_monitor" in _MONEY_CRITICAL_LOOPS


# ===========================================================================
# HEALTH GATE — configured+unfed degrades; configured+fed clears. Both ways.
# ===========================================================================

class _FakeMonitor:
    def __init__(self, healthy): self._h = healthy
    def monitor_liveness(self): return (self._h, "fed" if self._h else "configured but UNFED")


def _app(*, required, monitor=None):
    app = FastAPI()
    app.state.quota_capabilities = {"billing": "on", "reconciler": "on(ledger)"}
    app.state.quota_required_loops = set(required)
    if monitor is not None:
        app.state.quota_heartbeat_monitor = monitor
    return app


class TestHealthGate:
    def test_configured_unfed_monitor_degrades_health(self):
        app = _app(required={"heartbeat_monitor"}, monitor=_FakeMonitor(False))
        h = quota_health(app)
        assert h["status"] == "degraded"
        assert "heartbeat_monitor" in h["degraded"]

    def test_configured_fed_monitor_is_healthy(self):
        """[control, the other way] A fed monitor does not degrade — no false 503."""
        app = _app(required={"heartbeat_monitor"}, monitor=_FakeMonitor(True))
        assert quota_health(app)["status"] == "ok"

    def test_disabled_absent_monitor_does_not_degrade(self):
        """Default path (heartbeat off, no monitor): not required, not present → ok.
        The deprecated mechanism does NOT 503 a healthy paid deployment (D-49)."""
        app = _app(required=set())  # heartbeat not required, no monitor object
        assert quota_health(app)["status"] == "ok"

    def test_liveness_surfaces_the_monitor(self):
        app = _app(required={"heartbeat_monitor"}, monitor=_FakeMonitor(False))
        loops = quota_loop_liveness(app)
        assert loops["heartbeat_monitor"]["healthy"] is False
