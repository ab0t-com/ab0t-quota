"""Tests for QuotaStore.start_sync_worker — periodic Redis → DynamoDB snapshot loop.

We mock `snapshot_counter` so the tests don't need a real DynamoDB; the
goal is to verify the SCAN traversal, no-op skip, key-parsing, and
worker lifecycle (start, run, stop).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
import pytest_asyncio

from ab0t_quota.models.core import (
    CounterType, ResetPeriod, ResourceDef,
)
from ab0t_quota.persistence import QuotaStore
from ab0t_quota.registry import ResourceRegistry


GAUGE = ResourceDef(
    service="test", resource_key="sandbox.concurrent",
    display_name="Concurrent Sandboxes",
    counter_type=CounterType.GAUGE, unit="sandboxes",
)
ACC = ResourceDef(
    service="test", resource_key="sandbox.monthly_cost",
    display_name="Monthly Cost",
    counter_type=CounterType.ACCUMULATOR, unit="USD",
    reset_period=ResetPeriod.MONTHLY, precision=2,
)
RATE = ResourceDef(
    service="test", resource_key="api.requests",
    display_name="API Requests",
    counter_type=CounterType.RATE, unit="requests",
    window_seconds=3600,
)


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.flushall()
    await r.aclose()


@pytest_asyncio.fixture
def store():
    """A QuotaStore with snapshot_counter mocked — never touches DynamoDB."""
    s = QuotaStore(table_name="test", region="us-east-1")
    s.snapshot_counter = AsyncMock()
    return s


@pytest_asyncio.fixture
def registry():
    r = ResourceRegistry()
    r.register(GAUGE, ACC, RATE)
    return r


# ---------------------------------------------------------------------------
# Key parsing
# ---------------------------------------------------------------------------

class TestKeyParsing:
    def test_gauge_key(self):
        assert QuotaStore._parse_quota_key(
            "quota:org-1:sandbox.concurrent:gauge"
        ) == ("org-1", "sandbox.concurrent", "gauge")

    def test_gauge_user_partition(self):
        assert QuotaStore._parse_quota_key(
            "quota:org-1:sandbox.concurrent:gauge:user:alice"
        ) == ("org-1", "sandbox.concurrent", "user")

    def test_accumulator_period(self):
        assert QuotaStore._parse_quota_key(
            "quota:org-1:sandbox.monthly_cost:acc:2026-04"
        ) == ("org-1", "sandbox.monthly_cost", "acc")

    def test_idempotency_key_skipped(self):
        assert QuotaStore._parse_quota_key(
            "quota:org-1:sandbox.concurrent:idem:create-abc"
        ) is None

    def test_alert_cooldown_key_skipped(self):
        assert QuotaStore._parse_quota_key(
            "quota:alert:org-1:sandbox.concurrent"
        ) is None  # resource_key piece "alert" doesn't contain '.'

    def test_tier_cache_key_skipped(self):
        assert QuotaStore._parse_quota_key(
            "quota:tier:org-1"
        ) is None

    def test_unknown_prefix_skipped(self):
        assert QuotaStore._parse_quota_key("foo:bar:baz") is None


# ---------------------------------------------------------------------------
# snapshot_all — single pass behavior
# ---------------------------------------------------------------------------

class TestSnapshotAll:
    @pytest.mark.asyncio
    async def test_snapshots_gauge_and_accumulator(self, store, registry, redis):
        await redis.set("quota:org-1:sandbox.concurrent:gauge", "3")
        await redis.set("quota:org-1:sandbox.monthly_cost:acc:2026-04", "47.52")
        n = await store.snapshot_all(redis, registry)
        assert n == 2
        # snapshot_counter was called once per counter
        calls = {tuple(c.args[:2]) for c in store.snapshot_counter.call_args_list}
        assert ("org-1", "sandbox.concurrent") in calls
        assert ("org-1", "sandbox.monthly_cost") in calls

    @pytest.mark.asyncio
    async def test_skips_rate_counters(self, store, registry, redis):
        # Rate counters use sorted sets — even if we put a string here, we
        # still want them skipped because their counter_type is RATE.
        await redis.set("quota:org-1:api.requests:rate", "999")
        n = await store.snapshot_all(redis, registry)
        assert n == 0
        store.snapshot_counter.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_user_partition_keys(self, store, registry, redis):
        await redis.set("quota:org-1:sandbox.concurrent:gauge", "5")
        await redis.set("quota:org-1:sandbox.concurrent:gauge:user:alice", "2")
        await redis.set("quota:org-1:sandbox.concurrent:gauge:user:bob", "3")
        n = await store.snapshot_all(redis, registry)
        # Only the org-level total snapshots; user partitions are skipped
        assert n == 1
        store.snapshot_counter.assert_called_once()
        args = store.snapshot_counter.call_args.args
        assert args[:2] == ("org-1", "sandbox.concurrent")

    @pytest.mark.asyncio
    async def test_skips_idempotency_alert_tier_keys(self, store, registry, redis):
        await redis.set("quota:org-1:sandbox.concurrent:gauge", "2")
        await redis.set("quota:org-1:sandbox.concurrent:idem:abc", "1")
        await redis.set("quota:alert:org-1:sandbox.concurrent", "warning")
        await redis.set("quota:tier:org-1", "pro")
        n = await store.snapshot_all(redis, registry)
        assert n == 1  # only the gauge

    @pytest.mark.asyncio
    async def test_skips_unregistered_resources(self, store, registry, redis):
        await redis.set("quota:org-1:unknown.thing:gauge", "42")
        n = await store.snapshot_all(redis, registry)
        assert n == 0

    @pytest.mark.asyncio
    async def test_no_op_skip_on_unchanged_value(self, store, registry, redis):
        """Second pass with same value writes 0 records."""
        await redis.set("quota:org-1:sandbox.concurrent:gauge", "3")
        first = await store.snapshot_all(redis, registry)
        assert first == 1
        second = await store.snapshot_all(redis, registry)
        assert second == 0
        # Third pass after value changes → writes again
        await redis.set("quota:org-1:sandbox.concurrent:gauge", "4")
        third = await store.snapshot_all(redis, registry)
        assert third == 1

    @pytest.mark.asyncio
    async def test_snapshot_failure_does_not_break_pass(self, store, registry, redis):
        """A single failing snapshot doesn't stop the rest of the pass."""
        await redis.set("quota:org-1:sandbox.concurrent:gauge", "3")
        await redis.set("quota:org-2:sandbox.concurrent:gauge", "5")
        # First call raises, second succeeds
        store.snapshot_counter.side_effect = [RuntimeError("boom"), None]
        n = await store.snapshot_all(redis, registry)
        # Only the successful one counts
        assert n == 1
        assert store.snapshot_counter.call_count == 2

    @pytest.mark.asyncio
    async def test_handles_many_keys_via_scan(self, store, registry, redis):
        """100 orgs × 1 gauge each — SCAN traverses the whole keyspace."""
        for i in range(100):
            await redis.set(f"quota:org-{i}:sandbox.concurrent:gauge", str(i + 1))
        n = await store.snapshot_all(redis, registry)
        assert n == 100

    @pytest.mark.asyncio
    async def test_empty_registry_short_circuits(self, store, redis):
        empty = ResourceRegistry()
        await redis.set("quota:org-1:sandbox.concurrent:gauge", "3")
        n = await store.snapshot_all(redis, empty)
        assert n == 0
        store.snapshot_counter.assert_not_called()


# ---------------------------------------------------------------------------
# Worker lifecycle
# ---------------------------------------------------------------------------

class TestSyncWorker:
    @pytest.mark.asyncio
    async def test_start_then_stop_is_clean(self, store, registry, redis):
        store.start_sync_worker(redis, registry, interval_seconds=60)
        assert store._sync_task is not None
        assert not store._sync_task.done()
        await store.stop_sync_worker()
        assert store._sync_task is None

    @pytest.mark.asyncio
    async def test_double_start_returns_same_task(self, store, registry, redis):
        t1 = store.start_sync_worker(redis, registry, interval_seconds=60)
        t2 = store.start_sync_worker(redis, registry, interval_seconds=60)
        assert t1 is t2
        await store.stop_sync_worker()

    @pytest.mark.asyncio
    async def test_close_stops_worker(self, store, registry, redis):
        store.start_sync_worker(redis, registry, interval_seconds=60)
        task = store._sync_task
        await store.close()
        assert task.done()
        assert store._sync_task is None

    @pytest.mark.asyncio
    async def test_worker_runs_snapshot_after_interval(self, store, registry, redis):
        """Set a tiny interval; verify snapshot_counter actually fires."""
        await redis.set("quota:org-1:sandbox.concurrent:gauge", "7")
        # 0.05s interval — well within test timeout
        store.start_sync_worker(redis, registry, interval_seconds=0.05)
        # Wait long enough for at least one tick
        await asyncio.sleep(0.15)
        await store.stop_sync_worker()
        assert store.snapshot_counter.call_count >= 1
        first_call = store.snapshot_counter.call_args_list[0]
        assert first_call.args[:2] == ("org-1", "sandbox.concurrent")
        assert first_call.args[2] == 7.0

    @pytest.mark.asyncio
    async def test_stop_when_not_started_is_safe(self, store):
        # Should not raise
        await store.stop_sync_worker()
