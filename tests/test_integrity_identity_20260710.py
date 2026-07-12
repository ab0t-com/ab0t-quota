"""P0.3 — Identity red suite (ticket 20260709_ab0t_quota_systemic_integrity_redesign).

RED-BY-DESIGN. Reproduces the caller-composed-idempotency-key collision class
against the REAL library code paths and asserts the fixed behaviour, so each
test FAILS on current code.

Findings covered:
  - QI-05.1  Resource-id reuse collision. Idempotency keys are caller-composed
             with a fixed 24h TTL (gauge.py:30-35, ex=86400). When a resource_id
             is reused (pool re-claim / stop->start / restore) within 24h, the
             second activation's `end` decrement collides on the already-claimed
             key and is suppressed → the gauge is stuck +1 forever.
             This is a PERMANENT-LIBRARY promotion of the empirical proof
             `tickets/20260626_quota_counter_drift_reconciliation/prove_collision_20260703.py`.
             Fix: library-minted activation identity (P2.2). RED asserts the
             gauge returns to 0 after both activations tear down.
  - QB-02    The cost-cap accumulator has its own un-generationed idempotency
             key `cost:lifecycle:{resource_id}` (billing/lifecycle.py:226) — the
             same collision class, on the MONEY cap. stop->resume->stop of one
             resource_id within 24h records only the FIRST activation's cost →
             the "$X/month" tier cap silently under-counts.
             Fix: settlement keyed on activation identity (P3.2). RED asserts
             both activations' costs are recorded.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import fakeredis.aioredis
import pytest
import pytest_asyncio

from ab0t_quota.billing.lifecycle import LifecycleEmitter
from ab0t_quota.counters.gauge import GaugeCounter
from ab0t_quota.engine import QuotaEngine
from ab0t_quota.models.core import (
    CounterType, ResetPeriod, ResourceDef, TierConfig, TierLimits,
)
from ab0t_quota.providers import StaticTierProvider
from ab0t_quota.registry import ResourceRegistry


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.flushall()
    await r.aclose()


# ---------------------------------------------------------------------------
# QI-05.1 — reused-resource-id gauge collision (ports prove_collision_20260703)
# ---------------------------------------------------------------------------

USER = "user-1"
RK = "sandbox.desktop_sessions"
CID = "desktop-abc123"  # reused across pool release -> re-claim, <24h apart

# Exactly what engine.decrement_for_bundle builds:  f"{idempotency_key}:{rk}"
# with the consumer's teardown key "counter:lifecycle:end:{container_id}".
END_IDEM = f"counter:lifecycle:end:{CID}:{RK}"


class TestReusedResourceIdCollision:
    @pytest.mark.asyncio
    async def test_two_activations_of_reused_id_return_gauge_to_zero(self, redis):
        """Two DISTINCT activations of the SAME reused container_id within 24h,
        driven through the real GaugeCounter with the real key construction:

          create  -> increment_user(no idem key)
          teardown-> decrement_user(idem="counter:lifecycle:end:{cid}:{rk}")

        The second teardown's decrement collides on END_IDEM (already claimed,
        24h TTL) and is suppressed. RED today: gauge stuck at 1.0 after the
        second teardown. GREEN target (activation identity, P2.2): 0.0.
        """
        g = GaugeCounter(redis, "org-1", RK)

        # Activation #1
        await g.increment_user(USER, 1.0, idempotency_key=None)
        await g.decrement_user(USER, 1.0, idempotency_key=END_IDEM)
        assert await g.get_user(USER) == 0.0, "activation #1 tears down cleanly"

        # Activation #2 — pool re-claimed the SAME container_id, <24h later.
        await g.increment_user(USER, 1.0, idempotency_key=None)
        assert await g.get_user(USER) == 1.0, "activation #2 created"
        await g.decrement_user(USER, 1.0, idempotency_key=END_IDEM)  # collides -> no-op

        final = await g.get_user(USER)
        assert final == 0.0, (
            f"QI-05.1: reused-id collision left the gauge stuck at {final} — the "
            f"second teardown's decrement was suppressed by the already-claimed "
            f"idempotency key '{END_IDEM}'. The slot never released."
        )


# ---------------------------------------------------------------------------
# QB-02 — cost accumulator un-generationed key (the money-cap variant)
# ---------------------------------------------------------------------------

COST_RESOURCE = ResourceDef(
    service="test",
    resource_key="sandbox.monthly_cost",
    display_name="Monthly Cost",
    counter_type=CounterType.ACCUMULATOR,
    unit="USD",
    reset_period=ResetPeriod.MONTHLY,
    precision=2,
)

COST_TIERS = {
    "free": TierConfig(
        tier_id="free", display_name="Free",
        limits={"sandbox.monthly_cost": TierLimits(limit=10.00)},
    ),
}


class TestCostAccumulatorCollision:
    @pytest.mark.asyncio
    async def test_stop_resume_stop_records_both_activations_cost(self, redis):
        """One resource_id lives two lifetimes in the same month
        (stop -> resume -> stop). Each terminal event drives LifecycleEmitter
        ._record_cost with idempotency_key='cost:lifecycle:{resource_id}'
        (no generation). The second stop collides on that key → its cost is
        dropped from the monthly cap.

        Activation #1: 1h @ $0.10/h = $0.10.  Activation #2: 2h @ $0.10/h = $0.20.
        RED today: accumulator == $0.10 (second activation's cost swallowed).
        GREEN target (activation-keyed settlement, P3.2): $0.30.
        """
        registry = ResourceRegistry()
        registry.register(COST_RESOURCE)
        engine = QuotaEngine(
            redis=redis,
            tier_provider=StaticTierProvider({"org-1": "free"}),
            registry=registry,
            tiers=COST_TIERS,
        )
        emitter = LifecycleEmitter(engine=engine, cost_resource_key="sandbox.monthly_cost")

        base = datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc)
        resource_id = "sb-reused-1"

        # Activation #1 — 1 hour.
        await emitter.emit(
            event_type="resource.stopped",
            org_id="org-1", user_id=USER,
            resource_id=resource_id, resource_type="sandbox",
            hourly_rate=Decimal("0.10"), allocation_fee=Decimal("0.00"),
            started_at=base, stopped_at=base + timedelta(hours=1),
        )
        # Activation #2 — same resource_id, resumed then stopped, 2 hours, same month.
        resume = base + timedelta(hours=3)
        await emitter.emit(
            event_type="resource.stopped",
            org_id="org-1", user_id=USER,
            resource_id=resource_id, resource_type="sandbox",
            hourly_rate=Decimal("0.10"), allocation_fee=Decimal("0.00"),
            started_at=resume, stopped_at=resume + timedelta(hours=2),
        )

        from ab0t_quota.counters.factory import create_counter
        counter = create_counter(redis, "org-1", COST_RESOURCE)
        recorded = await counter.get()
        assert abs(recorded - 0.30) < 1e-6, (
            f"QB-02: monthly-cost cap recorded ${recorded:.2f} but two activations "
            f"cost $0.30 — the second stop collided on 'cost:lifecycle:{resource_id}' "
            f"and was dropped. The '$/month' cap silently under-counts."
        )
