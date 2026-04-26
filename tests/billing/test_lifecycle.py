"""Tests for LifecycleEmitter cost auto-recording.

The emitter, when wired to a QuotaEngine, must increment the configured
monthly-cost accumulator on terminal lifecycle events (resource.stopped /
resource.deleted) BEFORE publishing to SNS, idempotently per resource_id.
Heartbeat events must NOT touch the accumulator (would double-count).
SNS publish must still happen even if the cost increment fails.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import fakeredis.aioredis
import pytest
import pytest_asyncio

from ab0t_quota.billing.lifecycle import LifecycleEmitter
from ab0t_quota.engine import QuotaEngine
from ab0t_quota.models.core import (
    CounterType, ResetPeriod, ResourceDef, TierConfig, TierLimits,
)
from ab0t_quota.providers import StaticTierProvider
from ab0t_quota.registry import ResourceRegistry


# ---------------------------------------------------------------------------
# Fixtures
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

TIERS = {
    "free": TierConfig(
        tier_id="free", display_name="Free",
        limits={"sandbox.monthly_cost": TierLimits(limit=10.00)},
    ),
}


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.flushall()
    await r.aclose()


@pytest_asyncio.fixture
async def engine(redis):
    registry = ResourceRegistry()
    registry.register(COST_RESOURCE)
    return QuotaEngine(
        redis=redis,
        tier_provider=StaticTierProvider({"org-1": "free"}),
        registry=registry,
        tiers=TIERS,
    )


def _read_acc(emitter_engine, redis):
    """Read the current accumulator value via the engine's counter."""
    from ab0t_quota.counters.factory import create_counter
    counter = create_counter(redis, "org-1", COST_RESOURCE)
    return counter


# ---------------------------------------------------------------------------
# Tests — cost recorded on terminal events
# ---------------------------------------------------------------------------

class TestCostRecording:
    @pytest.mark.asyncio
    async def test_stopped_event_records_cost(self, engine, redis):
        """1 hour at $0.10/hr + $0.01 alloc = $0.11 in the accumulator."""
        emitter = LifecycleEmitter(
            engine=engine, cost_resource_key="sandbox.monthly_cost",
        )
        # No SNS topic configured → SNS publish returns False, but the cost
        # recorder should run regardless.
        started = datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc)
        stopped = started + timedelta(hours=1)
        await emitter.emit(
            event_type="resource.stopped",
            org_id="org-1", user_id="alice",
            resource_id="sb-1", resource_type="sandbox",
            hourly_rate=Decimal("0.10"),
            allocation_fee=Decimal("0.01"),
            started_at=started, stopped_at=stopped,
        )
        counter = _read_acc(engine, redis)
        value = await counter.get()
        # 1.0 * 0.10 + 0.01 = 0.11
        assert abs(value - 0.11) < 1e-6

    @pytest.mark.asyncio
    async def test_deleted_event_records_cost(self, engine, redis):
        """resource.deleted is also terminal — should record."""
        emitter = LifecycleEmitter(
            engine=engine, cost_resource_key="sandbox.monthly_cost",
        )
        started = datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc)
        stopped = started + timedelta(hours=2)
        await emitter.emit(
            event_type="resource.deleted",
            org_id="org-1", user_id="alice",
            resource_id="sb-2", resource_type="sandbox",
            hourly_rate=Decimal("0.50"),
            allocation_fee=Decimal("0.00"),
            started_at=started, stopped_at=stopped,
        )
        counter = _read_acc(engine, redis)
        assert abs(await counter.get() - 1.00) < 1e-6  # 2hr * $0.50

    @pytest.mark.asyncio
    async def test_partial_hour_prorates(self, engine, redis):
        """30 minutes at $0.40/hr should record $0.20."""
        emitter = LifecycleEmitter(
            engine=engine, cost_resource_key="sandbox.monthly_cost",
        )
        started = datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc)
        stopped = started + timedelta(minutes=30)
        await emitter.emit(
            event_type="resource.stopped",
            org_id="org-1", user_id="alice",
            resource_id="sb-3", resource_type="sandbox",
            hourly_rate=Decimal("0.40"),
            allocation_fee=Decimal("0.00"),
            started_at=started, stopped_at=stopped,
        )
        counter = _read_acc(engine, redis)
        assert abs(await counter.get() - 0.20) < 1e-6

    @pytest.mark.asyncio
    async def test_replay_is_idempotent(self, engine, redis):
        """Same resource_id stop event twice (SNS at-least-once) → only one charge."""
        emitter = LifecycleEmitter(
            engine=engine, cost_resource_key="sandbox.monthly_cost",
        )
        started = datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc)
        stopped = started + timedelta(hours=1)
        for _ in range(3):
            await emitter.emit(
                event_type="resource.stopped",
                org_id="org-1", user_id="alice",
                resource_id="sb-replay", resource_type="sandbox",
                hourly_rate=Decimal("0.10"),
                allocation_fee=Decimal("0.01"),
                started_at=started, stopped_at=stopped,
            )
        counter = _read_acc(engine, redis)
        assert abs(await counter.get() - 0.11) < 1e-6  # only one charge

    @pytest.mark.asyncio
    async def test_heartbeat_does_not_record(self, engine, redis):
        """Heartbeats must NOT increment the accumulator (would double-count)."""
        emitter = LifecycleEmitter(
            engine=engine, cost_resource_key="sandbox.monthly_cost",
        )
        started = datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc)
        for _ in range(5):
            await emitter.emit(
                event_type="resource.heartbeat",
                org_id="org-1", user_id="alice",
                resource_id="sb-hb", resource_type="sandbox",
                hourly_rate=Decimal("0.10"),
                allocation_fee=Decimal("0.01"),
                started_at=started, stopped_at=started + timedelta(hours=1),
            )
        counter = _read_acc(engine, redis)
        assert await counter.get() == 0.0

    @pytest.mark.asyncio
    async def test_started_event_does_not_record(self, engine, redis):
        """resource.started shouldn't charge — duration is zero anyway, but be explicit."""
        emitter = LifecycleEmitter(
            engine=engine, cost_resource_key="sandbox.monthly_cost",
        )
        started = datetime.now(timezone.utc)
        await emitter.emit(
            event_type="resource.started",
            org_id="org-1", user_id="alice",
            resource_id="sb-start", resource_type="sandbox",
            hourly_rate=Decimal("0.10"),
            allocation_fee=Decimal("0.01"),
            started_at=started,
        )
        counter = _read_acc(engine, redis)
        assert await counter.get() == 0.0

    @pytest.mark.asyncio
    async def test_no_pricing_skips_record(self, engine, redis):
        """Resource without hourly_rate or allocation_fee should not crash or record."""
        emitter = LifecycleEmitter(
            engine=engine, cost_resource_key="sandbox.monthly_cost",
        )
        started = datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc)
        await emitter.emit(
            event_type="resource.stopped",
            org_id="org-1", user_id="alice",
            resource_id="sb-free", resource_type="sandbox",
            started_at=started, stopped_at=started + timedelta(hours=1),
        )
        counter = _read_acc(engine, redis)
        assert await counter.get() == 0.0

    @pytest.mark.asyncio
    async def test_no_started_at_skips_record(self, engine, redis):
        """Without started_at we can't compute duration; skip silently."""
        emitter = LifecycleEmitter(
            engine=engine, cost_resource_key="sandbox.monthly_cost",
        )
        await emitter.emit(
            event_type="resource.stopped",
            org_id="org-1", user_id="alice",
            resource_id="sb-nostart", resource_type="sandbox",
            hourly_rate=Decimal("0.10"),
            allocation_fee=Decimal("0.01"),
        )
        counter = _read_acc(engine, redis)
        assert await counter.get() == 0.0

    @pytest.mark.asyncio
    async def test_naive_datetime_treated_as_utc(self, engine, redis):
        """tz-naive datetimes from older callers must not crash; assumed UTC."""
        emitter = LifecycleEmitter(
            engine=engine, cost_resource_key="sandbox.monthly_cost",
        )
        started = datetime(2026, 4, 1, 10, 0, 0)  # naive
        stopped = started + timedelta(hours=1)    # naive
        await emitter.emit(
            event_type="resource.stopped",
            org_id="org-1", user_id="alice",
            resource_id="sb-naive", resource_type="sandbox",
            hourly_rate=Decimal("0.10"),
            allocation_fee=Decimal("0.00"),
            started_at=started, stopped_at=stopped,
        )
        counter = _read_acc(engine, redis)
        assert abs(await counter.get() - 0.10) < 1e-6


# ---------------------------------------------------------------------------
# Tests — emitter without engine still works
# ---------------------------------------------------------------------------

class TestBackwardsCompatible:
    @pytest.mark.asyncio
    async def test_no_engine_no_cost_recording(self, redis):
        """Old-style construction (no engine) still works — pure SNS emitter."""
        emitter = LifecycleEmitter()
        # Without SNS topic, emit returns False but never crashes
        result = await emitter.emit(
            event_type="resource.stopped",
            org_id="org-1", user_id="alice",
            resource_id="sb-1", resource_type="sandbox",
            hourly_rate=Decimal("0.10"),
            allocation_fee=Decimal("0.01"),
            started_at=datetime.now(timezone.utc),
        )
        assert result is False  # no SNS topic configured

    @pytest.mark.asyncio
    async def test_engine_set_but_no_resource_key(self, engine, redis):
        """If cost_resource_key is None, no recording happens even with engine set."""
        emitter = LifecycleEmitter(engine=engine, cost_resource_key=None)
        started = datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc)
        await emitter.emit(
            event_type="resource.stopped",
            org_id="org-1", user_id="alice",
            resource_id="sb-1", resource_type="sandbox",
            hourly_rate=Decimal("0.10"),
            allocation_fee=Decimal("0.01"),
            started_at=started, stopped_at=started + timedelta(hours=1),
        )
        counter = _read_acc(engine, redis)
        assert await counter.get() == 0.0


# ---------------------------------------------------------------------------
# Tests — engine failure must not block SNS publish
# ---------------------------------------------------------------------------

class TestFailureIsolation:
    @pytest.mark.asyncio
    async def test_engine_increment_failure_does_not_raise(self, engine, redis, caplog):
        """If engine.increment throws, emit() logs + continues (returns SNS result)."""
        emitter = LifecycleEmitter(
            engine=engine, cost_resource_key="sandbox.monthly_cost",
        )
        started = datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc)

        async def boom(*a, **kw):
            raise RuntimeError("redis exploded")

        # Patch the engine's increment to fail
        with patch.object(engine, "increment", side_effect=boom):
            # Should not raise
            result = await emitter.emit(
                event_type="resource.stopped",
                org_id="org-1", user_id="alice",
                resource_id="sb-fail", resource_type="sandbox",
                hourly_rate=Decimal("0.10"),
                allocation_fee=Decimal("0.01"),
                started_at=started, stopped_at=started + timedelta(hours=1),
            )
            # No SNS topic configured → False, but no crash
            assert result is False
        # Cost was not recorded (because the increment threw) — accumulator stays 0
        counter = _read_acc(engine, redis)
        assert await counter.get() == 0.0

    @pytest.mark.asyncio
    async def test_unknown_resource_key_logs_does_not_raise(self, redis):
        """Misconfigured cost_resource_key (not registered) must not crash."""
        registry = ResourceRegistry()  # empty — no resources registered
        engine = QuotaEngine(
            redis=redis,
            tier_provider=StaticTierProvider({"org-1": "free"}),
            registry=registry,
            tiers=TIERS,
        )
        emitter = LifecycleEmitter(
            engine=engine, cost_resource_key="nonexistent.cost",
        )
        started = datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc)
        # Should not raise even though the resource_key is unknown to the registry
        await emitter.emit(
            event_type="resource.stopped",
            org_id="org-1", user_id="alice",
            resource_id="sb-x", resource_type="sandbox",
            hourly_rate=Decimal("0.10"),
            allocation_fee=Decimal("0.01"),
            started_at=started, stopped_at=started + timedelta(hours=1),
        )
