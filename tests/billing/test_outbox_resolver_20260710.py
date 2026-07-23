"""D-32 — the setup-lifespan orchestration `_resolve_outbox_durability`
(ticket 20260709). Exercises Claim 2 (Redis durability self-check) + Claim 3
(refuse to bill onto an ephemeral store) with a fake app + a real
LifecycleEmitter + fakeredis. store=redis avoids the DDB self-provision branch
(Claim 1) which is covered by connect_ddb_outbox_store's DDB-Local test.
"""
from __future__ import annotations

import types

import fakeredis.aioredis
import pytest

from ab0t_quota.billing.lifecycle import LifecycleEmitter
from ab0t_quota.setup import _emit_capabilities_snapshot, _resolve_outbox_durability


def _app():
    return types.SimpleNamespace(state=types.SimpleNamespace())


@pytest.mark.asyncio
async def test_no_durable_store_paid_fails_to_start():
    """D-34 Option A: fakeredis has no CONFIG (ElastiCache case), unconfirmed +
    paid + not allow_ephemeral → the service must RAISE at startup, not come up
    admitting billable work it will never bill for."""
    r = fakeredis.aioredis.FakeRedis()
    try:
        app, em = _app(), LifecycleEmitter(sns_topic_arn=None)
        with pytest.raises(RuntimeError, match="must not start"):
            await _resolve_outbox_durability(
                app, em, redis=r, config={"outbox": {"store": "redis"}},
                storage={}, enable_paid=True,
            )
        # Capability still records the OFF state even though we raised.
        assert app.state.quota_capabilities["billing"].startswith("OFF")
    finally:
        await r.aclose()


@pytest.mark.asyncio
async def test_redis_confirmed_keeps_billing_on():
    """Operator assertion on the record (redis_durability_confirmed=true) → ON."""
    r = fakeredis.aioredis.FakeRedis()
    try:
        app, em = _app(), LifecycleEmitter(sns_topic_arn=None)
        await _resolve_outbox_durability(
            app, em, redis=r,
            config={"outbox": {"store": "redis", "redis_durability_confirmed": True}},
            storage={}, enable_paid=True,
        )
        assert em._billing_disabled is False
        assert app.state.quota_capabilities["billing"].startswith("ON")
    finally:
        await r.aclose()


@pytest.mark.asyncio
async def test_allow_ephemeral_starts_with_billing_disabled():
    """D-34: allow_ephemeral is the on-the-record dev escape — the process STARTS
    (no raise) but billing is DISABLED (OFF), never silently run onto a cache."""
    r = fakeredis.aioredis.FakeRedis()
    try:
        app, em = _app(), LifecycleEmitter(sns_topic_arn=None)
        await _resolve_outbox_durability(
            app, em, redis=r,
            config={"outbox": {"store": "redis", "allow_ephemeral": True}},
            storage={}, enable_paid=True,
        )
        assert em._billing_disabled is True
        assert app.state.quota_capabilities["billing"].startswith("OFF")
    finally:
        await r.aclose()


@pytest.mark.asyncio
async def test_not_paid_records_off_and_does_not_raise():
    """enable_paid=False → billing was never on; record OFF, do not raise."""
    r = fakeredis.aioredis.FakeRedis()
    try:
        app, em = _app(), LifecycleEmitter(sns_topic_arn=None)
        await _resolve_outbox_durability(
            app, em, redis=r, config={"outbox": {"store": "redis"}},
            storage={}, enable_paid=False,
        )
        assert em._billing_disabled is False
        assert app.state.quota_capabilities["billing"].startswith("OFF")
    finally:
        await r.aclose()


@pytest.mark.asyncio
async def test_transient_ddb_failure_then_success_starts_normally(monkeypatch):
    """D-34: a TRANSIENT DDB blip must not stop a deploy — bounded retry recovers.
    Proves retry, not luck: connect fails once, succeeds on the 2nd attempt, and
    the service comes up billing ON."""
    import ab0t_quota.billing.outbox as obx

    calls = {"n": 0}

    class _FakeDurableStore:
        async def ensure_table(self, **kw):
            # T-6 grew the real interface with `create=` (call-site policy);
            # this fake mirrors it. No assertion changed.
            return None
        def durable(self):
            return True
        async def list_pending(self, limit=100):
            return []

    async def _flaky_connect(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("transient DDB blip")
        async def _noop():
            return None
        return _FakeDurableStore(), _noop

    monkeypatch.setattr(obx, "connect_ddb_outbox_store", _flaky_connect)

    app, em = _app(), LifecycleEmitter(sns_topic_arn=None)
    # redis=None so only the DDB path runs; tiny backoff so the test is fast.
    await _resolve_outbox_durability(
        app, em, redis=None,
        config={"outbox": {"store": "ddb",
                           "provision_retry": {"attempts": 3, "initial_seconds": 0.001}}},
        storage={}, enable_paid=True,
    )
    assert calls["n"] == 2, "must have retried the transient failure (not luck)"
    assert em._billing_disabled is False
    assert app.state.quota_capabilities["billing"].startswith("ON")


def test_capabilities_snapshot_preserves_resolver_values_and_reports_real_seams():
    """P6.2 — the snapshot preserves the resolver's billing/outbox verdict and reads
    the enforcement knobs + ledger backend.

    UPDATED per D-40 (was: `…_and_marks_peer_seams`). The original asserted the
    `unknown(owned:W-PY-B)` / `unknown(owned:W-PY-C)` placeholders, which were an
    honest wiring TODO while those lanes were in flight. D-40 removes them: the
    snapshot is now CONSUMED by `/quota/health`, and a money-critical capability
    reading `unknown(...)` would neither degrade the probe nor tell an operator
    anything. Every field now reports a real, readable value. Intent preserved
    (the snapshot must never guess), assertion updated to the shipped contract.
    """
    app = _app()
    app.state.quota_capabilities = {"billing": "ON (outbox=DDB)", "outbox": "DDB"}
    eng = types.SimpleNamespace(_enforcement=types.SimpleNamespace(
        enabled=True, shadow_mode=False, global_kill_switch=True))
    caps = _emit_capabilities_snapshot(app, eng, {}, enable_paid=True)
    assert caps["billing"] == "ON (outbox=DDB)"      # resolver verdict preserved
    assert caps["outbox"] == "DDB"
    assert "kill=True" in caps["enforcement"]
    assert caps["ledger_store"] == "none"            # no ledger on app.state here
    assert caps["activations"] == "on"               # real value, not a placeholder
    assert caps["reconciler"] == "off (not started)"  # honest: none on app.state here
    # The placeholder must never ship — an operator cannot act on `unknown(owned:…)`.
    assert not any("unknown(owned:" in str(v) for v in caps.values())
