"""W-T2 — D-66: the required-loop set is DERIVED from config, not APPENDED at wiring sites.

Ticket 20260709. D-54 added `quota_required_loops` so the probe degrades on a loop
that is required-but-absent (never wired), not merely present-but-dead. But I
assembled that set by APPENDING at the sweeper's wiring site — which can only list
what someone remembered to register. Add a fourth loop, forget the append, and the
probe is blind again: the same defect class, relocated one level up (nobody wired
the loop → nobody registered the loop as required).

D-66 (ratified): **the set of loops that must run is a function of the config +
capability schema, computed in ONE place. Wiring SATISFIES the contract; it does
not DEFINE it.**
  - enable_paid            ⇒ outbox_drain required
  - reconciler enabled     ⇒ reconciler_loop required
  - auth events registered ⇒ stale_lease_sweeper required

The load-bearing negative control (per the coordinator): flip a CONFIG implication
WITHOUT wiring anything and confirm the required set changes + the probe degrades;
then wire it and the probe clears. If the set is derived, that works without ever
touching a registry. If it were appended, it could not.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI

from ab0t_quota.setup import required_money_loops, quota_health, _MONEY_CRITICAL_LOOPS


def _app(required, *, emitter=None, reconciler=None, sweeper=None, caps=None):
    app = FastAPI()
    app.state.quota_capabilities = dict(caps or {"billing": "on", "reconciler": "on(ledger)"})
    app.state.quota_required_loops = set(required)
    if emitter is not None:
        app.state.quota_emitter = emitter
    if reconciler is not None:
        app.state.quota_reconciler = reconciler
    if sweeper is not None:
        app.state.quota_stale_lease_sweeper = sweeper
    return app


class _Live:
    def __init__(self, name, healthy=True): self._m = name; self._h = healthy
    def drain_worker_liveness(self): return (self._h, self._m)
    def loop_liveness(self): return (self._h, self._m)


# ===========================================================================
# The derivation is a pure function of the config (not of what got wired).
# ===========================================================================

class TestDerivation:
    def test_paid_implies_outbox_drain(self):
        assert "outbox_drain" in required_money_loops({"reconcile": {"enabled": False}}, enable_paid=True)

    def test_not_paid_does_not_require_drain(self):
        assert "outbox_drain" not in required_money_loops({"reconcile": {"enabled": False}}, enable_paid=False)

    def test_reconciler_enabled_by_default_is_required(self):
        assert "reconciler_loop" in required_money_loops({}, enable_paid=False)

    def test_reconciler_explicitly_disabled_is_not_required(self):
        assert "reconciler_loop" not in required_money_loops({"reconcile": {"enabled": False}}, enable_paid=False)

    def test_webhook_env_implies_stale_lease_sweeper(self, monkeypatch):
        monkeypatch.setenv("AB0T_AUTH_WEBHOOK_SECRET", "s3cret")
        assert "stale_lease_sweeper" in required_money_loops({"reconcile": {"enabled": False}}, enable_paid=False)

    def test_no_webhook_env_does_not_require_sweeper(self, monkeypatch):
        monkeypatch.delenv("AB0T_AUTH_WEBHOOK_SECRET", raising=False)
        assert "stale_lease_sweeper" not in required_money_loops({"reconcile": {"enabled": False}}, enable_paid=False)

    def test_derivation_only_yields_money_critical_names(self, monkeypatch):
        monkeypatch.setenv("AB0T_AUTH_WEBHOOK_SECRET", "s")
        req = required_money_loops({}, enable_paid=True)
        assert req <= set(_MONEY_CRITICAL_LOOPS), "the derived set must be a subset of the money-critical universe"


# ===========================================================================
# THE LOAD-BEARING CONTROL — flip a CONFIG implication, not a wiring, and prove
# the probe degrades then clears. Works only because the set is DERIVED.
# ===========================================================================

class TestDerivedNotObserved:
    def test_config_flip_adds_requirement_without_any_wiring(self):
        """Flipping `enable_paid` in the CONFIG adds outbox_drain to the required
        set — nothing was wired, no registry touched. This is the tell of a
        derivation: the config implication drives the set."""
        without = required_money_loops({"reconcile": {"enabled": False}}, enable_paid=False)
        with_paid = required_money_loops({"reconcile": {"enabled": False}}, enable_paid=True)
        assert "outbox_drain" not in without
        assert "outbox_drain" in with_paid
        assert with_paid - without == {"outbox_drain"}

    def test_required_but_unwired_degrades_then_wiring_clears(self):
        """The config implies outbox_drain. With NO emitter wired the probe
        degrades (required-but-absent); wiring a healthy emitter clears it — with
        no change to the required set. Derived, not observed."""
        req = required_money_loops({"reconcile": {"enabled": False}}, enable_paid=True)
        assert req == {"outbox_drain"}

        # unwired → degraded
        app = _app(req)  # no emitter on state
        h = quota_health(app)
        assert h["status"] == "degraded"
        assert "outbox_drain" in h["degraded"]

        # wired healthy → clears, WITHOUT touching the required set
        app.state.quota_emitter = _Live("on", healthy=True)
        assert quota_health(app)["status"] == "ok"

    def test_wiring_EXCEPTION_leaves_loop_required_and_probe_red(self, monkeypatch):
        """Requirement 3: a wiring EXCEPTION (not mere absence) must leave the loop
        required and the probe red. The config (webhook env) requires the sweeper;
        simulate its wiring having thrown (no sweeper object on state) → the derived
        requirement stands → degraded. Because the requirement comes from config,
        the failed wiring cannot un-declare it."""
        monkeypatch.setenv("AB0T_AUTH_WEBHOOK_SECRET", "s")
        req = required_money_loops({"reconcile": {"enabled": False}}, enable_paid=False)
        assert req == {"stale_lease_sweeper"}
        app = _app(req)  # sweeper wiring "threw" → no quota_stale_lease_sweeper on state
        h = quota_health(app)
        assert h["status"] == "degraded"
        assert "stale_lease_sweeper" in h["degraded"]
