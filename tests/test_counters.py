"""2.1.2 — Counter tests using fakeredis."""

import pytest
import pytest_asyncio
import fakeredis.aioredis

from ab0t_quota.counters.gauge import GaugeCounter
from ab0t_quota.counters.rate import RateCounter
from ab0t_quota.counters.accumulator import AccumulatorCounter
from ab0t_quota.counters.factory import create_counter
from ab0t_quota.models.core import ResourceDef, CounterType, ResetPeriod


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.flushall()
    await r.aclose()


# ---------------------------------------------------------------------------
# GaugeCounter
# ---------------------------------------------------------------------------

class TestGaugeCounter:
    @pytest.mark.asyncio
    async def test_inc_dec(self, redis):
        g = GaugeCounter(redis, "org-1", "sandbox.concurrent")
        assert await g.get() == 0.0
        assert await g.increment(1) == 1.0
        assert await g.increment(2) == 3.0
        assert await g.decrement(1) == 2.0
        assert await g.get() == 2.0

    @pytest.mark.asyncio
    async def test_floor_at_zero(self, redis):
        g = GaugeCounter(redis, "org-1", "sandbox.concurrent")
        await g.increment(2)
        val = await g.decrement(5)  # would go to -3
        assert val == 0.0
        assert await g.get() == 0.0

    @pytest.mark.asyncio
    async def test_idempotency(self, redis):
        g = GaugeCounter(redis, "org-1", "sandbox.concurrent")
        await g.increment(1, idempotency_key="create-abc")
        await g.increment(1, idempotency_key="create-abc")  # duplicate
        assert await g.get() == 1.0  # not 2.0

    @pytest.mark.asyncio
    async def test_per_user_inc_dec(self, redis):
        g = GaugeCounter(redis, "org-1", "sandbox.concurrent")
        await g.increment_user("alice", 1)
        await g.increment_user("bob", 2)
        assert await g.get() == 3.0            # org total
        assert await g.get_user("alice") == 1.0
        assert await g.get_user("bob") == 2.0
        await g.decrement_user("bob", 1)
        assert await g.get() == 2.0
        assert await g.get_user("bob") == 1.0

    @pytest.mark.asyncio
    async def test_reset(self, redis):
        g = GaugeCounter(redis, "org-1", "sandbox.concurrent")
        await g.increment(5)
        await g.reset(0)
        assert await g.get() == 0.0


# ---------------------------------------------------------------------------
# RateCounter
# ---------------------------------------------------------------------------

class TestRateCounter:
    @pytest.mark.asyncio
    async def test_increment_and_get(self, redis):
        r = RateCounter(redis, "org-1", "api.requests", window_seconds=3600)
        assert await r.get() == 0.0
        await r.increment(1)
        assert await r.get() == 1.0
        await r.increment(1)
        assert await r.get() == 2.0

    @pytest.mark.asyncio
    async def test_reject_decrement(self, redis):
        r = RateCounter(redis, "org-1", "api.requests", window_seconds=3600)
        with pytest.raises(TypeError, match="cannot be decremented"):
            await r.decrement(1)

    @pytest.mark.asyncio
    async def test_reset(self, redis):
        r = RateCounter(redis, "org-1", "api.requests", window_seconds=3600)
        await r.increment(5)
        await r.reset()
        assert await r.get() == 0.0


# ---------------------------------------------------------------------------
# AccumulatorCounter
# ---------------------------------------------------------------------------

class TestAccumulatorCounter:
    @pytest.mark.asyncio
    async def test_increment_and_get(self, redis):
        a = AccumulatorCounter(redis, "org-1", "cost.monthly", ResetPeriod.MONTHLY)
        assert await a.get() == 0.0
        await a.increment(10.50)
        assert await a.get() == 10.50
        await a.increment(5.25)
        assert await a.get() == 15.75

    @pytest.mark.asyncio
    async def test_reject_decrement(self, redis):
        a = AccumulatorCounter(redis, "org-1", "cost.monthly", ResetPeriod.MONTHLY)
        with pytest.raises(TypeError, match="cannot be decremented"):
            await a.decrement(1)

    @pytest.mark.asyncio
    async def test_idempotency(self, redis):
        a = AccumulatorCounter(redis, "org-1", "cost.monthly", ResetPeriod.MONTHLY)
        await a.increment(10, idempotency_key="charge-001")
        await a.increment(10, idempotency_key="charge-001")  # duplicate
        assert await a.get() == 10.0


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

class TestFactory:
    def test_gauge(self, redis):
        r_def = ResourceDef(
            service="test", resource_key="t.gauge", display_name="G",
            counter_type=CounterType.GAUGE,
        )
        c = create_counter(redis, "org-1", r_def)
        assert isinstance(c, GaugeCounter)

    def test_rate(self, redis):
        r_def = ResourceDef(
            service="test", resource_key="t.rate", display_name="R",
            counter_type=CounterType.RATE, window_seconds=60,
        )
        c = create_counter(redis, "org-1", r_def)
        assert isinstance(c, RateCounter)

    def test_accumulator(self, redis):
        r_def = ResourceDef(
            service="test", resource_key="t.acc", display_name="A",
            counter_type=CounterType.ACCUMULATOR, reset_period=ResetPeriod.MONTHLY,
        )
        c = create_counter(redis, "org-1", r_def)
        assert isinstance(c, AccumulatorCounter)
