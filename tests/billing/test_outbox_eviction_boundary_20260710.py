"""W-T2 side-effects hardening — the OUTBOX ↔ EVICTION boundary.

Ticket 20260709_ab0t_quota_systemic_integrity_redesign.
Boundary crossed (D-40 table, row #4): **eviction** — "survives memory pressure".
D-30 says DDB is the default outbox store precisely because Redis is a CACHE that
can silently evict pending money events under memory pressure.

WHAT PYTEST CANNOT DO (be blunt): a pytest process cannot put a real Redis under
real memory pressure and watch `maxmemory-policy allkeys-lru` reap a key. fakeredis
has no eviction at all. So this suite does TWO reachable things instead:

  1. SIMULATE an eviction by deleting a pending intent key mid-flight (the exact
     post-condition an LRU reap leaves) and PROVE the failure mode: the money event
     vanishes from `list_pending` with NO error. That is the silent loss D-30 exists
     to prevent — and the proof of why the startup guard matters.
  2. Assert the startup GUARD (`check_redis_outbox_durability`) is LOUD: an evicting
     policy is rejected, a non-evicting one is accepted. This is the machine check
     that stops a deployment from ever reaching case (1).

fakeredis[lua] (lupa) is NOT Redis; the config self-check is exercised against a
hand fake because fakeredis has no CONFIG GET.
"""
from __future__ import annotations

import time

import fakeredis.aioredis
import pytest

from ab0t_quota.billing.outbox import (
    RedisOutboxStore, OutboxRecord, check_redis_outbox_durability,
)

pytestmark = pytest.mark.asyncio


async def _redis():
    return fakeredis.aioredis.FakeRedis()


def _rec(rid: str) -> OutboxRecord:
    return OutboxRecord(
        key=f"{rid}:resource.stopped", event={"reservation_id": rid},
        event_type="resource.stopped", resource_type="sandbox",
        reservation_id=rid, first_ts=time.time())


# ===========================================================================
# (1) The failure mode — an evicted intent key is SILENTLY skipped.
# ===========================================================================

class TestEvictionSilentlyDropsMoney:
    async def test_evicted_intent_key_vanishes_from_list_pending_with_no_error(self):
        """SIMULATED eviction: the pending-index zset entry survives but the intent
        payload key is reaped (allkeys-lru could evict either independently). The
        money event then silently disappears from `list_pending` — no exception, no
        log. This is the loss D-30 forbids by making DDB the default; the outbox
        cannot itself detect it, which is the whole point of the startup guard."""
        redis = await _redis()
        try:
            store = RedisOutboxStore(redis)
            await store.put_intent(_rec("rsv-evict"))
            assert len(await store.list_pending()) == 1

            # Reap the payload key (what an LRU eviction leaves behind).
            await redis.delete(store._intent_key("rsv-evict:resource.stopped"))

            pending = await store.list_pending()
            assert pending == [], (
                "an evicted money intent silently vanished from the drain's view — "
                "no error was raised. This is why an evicting Redis is unsafe (D-30)."
            )
        finally:
            await redis.flushall(); await redis.aclose()

    async def test_evicted_index_entry_also_strands_the_event(self):
        """The other direction: the zset index entry is evicted but the payload
        survives. `list_pending` reads the index first, so the event is never
        enumerated → never drained. Same silent loss, orphaned payload."""
        redis = await _redis()
        try:
            store = RedisOutboxStore(redis)
            await store.put_intent(_rec("rsv-orphan"))
            await redis.zrem(store._pending_idx, "rsv-orphan:resource.stopped")
            assert await store.list_pending() == []
            # the payload is now an orphan the drain will never see
            assert await redis.get(store._intent_key("rsv-orphan:resource.stopped")) is not None
        finally:
            await redis.flushall(); await redis.aclose()


# ===========================================================================
# (2) The GUARD is LOUD — the machine check that prevents ever reaching (1).
# ===========================================================================

class _FakeRedisConfig:
    """Minimal CONFIG GET fake for check_redis_outbox_durability."""

    def __init__(self, policy: str, appendonly: str = "no", save: str = ""):
        self._m = {"maxmemory-policy": policy, "appendonly": appendonly, "save": save}

    async def config_get(self, key):
        return {key: self._m.get(key, "")}


class _NoConfigRedis:
    """CONFIG GET raises — the ElastiCache case (config disabled)."""

    async def config_get(self, key):
        raise Exception("ERR unknown command 'config|get'")


class TestDurabilityGuardIsLoud:
    async def test_evicting_policy_is_rejected(self):
        """allkeys-lru → NOT durable, with a reason naming the eviction risk."""
        durable, reason = await check_redis_outbox_durability(
            _FakeRedisConfig("allkeys-lru", appendonly="yes"))
        assert durable is False
        assert "evict" in reason.lower()

    async def test_non_evicting_persisted_policy_is_accepted(self):
        """[control] noeviction + appendonly=yes → durable. Proves the guard is
        not a blanket reject — a correctly configured Redis passes."""
        durable, reason = await check_redis_outbox_durability(
            _FakeRedisConfig("noeviction", appendonly="yes"))
        assert durable is True

    async def test_no_persistence_is_rejected(self):
        """noeviction but no AOF and no save points → a restart loses pending
        events → NOT durable."""
        durable, reason = await check_redis_outbox_durability(
            _FakeRedisConfig("noeviction", appendonly="no", save=""))
        assert durable is False
        assert "persist" in reason.lower()

    async def test_config_unavailable_requires_explicit_operator_assertion(self):
        """ElastiCache disables CONFIG GET. Unconfirmed → NOT durable (never a
        silent assumption); confirmed=True → durable on the operator's record."""
        durable_unconfirmed, _ = await check_redis_outbox_durability(_NoConfigRedis())
        durable_confirmed, _ = await check_redis_outbox_durability(_NoConfigRedis(), confirmed=True)
        assert durable_unconfirmed is False
        assert durable_confirmed is True
