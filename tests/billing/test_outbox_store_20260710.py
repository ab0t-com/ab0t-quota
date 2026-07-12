"""D-29 — OutboxStore contract (ticket 20260709). Locks the put_intent →
list_pending → mark_delivered / mark_voided / bump_attempt semantics for the two
backends that are actually wired (InMemory for degraded/tests, Redis for the
durable common path). DDBOutboxStore mirrors the same contract but is exercised
only by inspection here — real DynamoDB is UNVERIFIED (pre-deploy gate)."""
from __future__ import annotations

import fakeredis.aioredis
import pytest
import pytest_asyncio

from decimal import Decimal

from ab0t_quota.billing.outbox import (
    InMemoryOutboxStore, OutboxRecord, RedisOutboxStore, PENDING, VOIDED,
    auto_select_outbox_store, check_redis_outbox_durability,
)


@pytest_asyncio.fixture(params=["memory", "redis"])
async def store(request):
    if request.param == "memory":
        yield InMemoryOutboxStore()
        return
    r = fakeredis.aioredis.FakeRedis()
    yield RedisOutboxStore(r)
    await r.flushall()
    await r.aclose()


def _rec(key, ts=1.0):
    return OutboxRecord(
        key=key, event={"reservation_id": key.split(":")[0]}, event_type="resource.stopped",
        resource_type="sandbox", reservation_id=key.split(":")[0], first_ts=ts,
    )


class TestOutboxContract:
    @pytest.mark.asyncio
    async def test_put_then_list_pending(self, store):
        await store.put_intent(_rec("rsv-1:resource.stopped"))
        pend = await store.list_pending()
        assert len(pend) == 1 and pend[0].key == "rsv-1:resource.stopped"
        assert pend[0].status == PENDING

    @pytest.mark.asyncio
    async def test_put_is_idempotent_and_preserves_first_ts(self, store):
        await store.put_intent(_rec("k:e", ts=100.0))
        await store.put_intent(_rec("k:e", ts=999.0))   # re-emit
        pend = await store.list_pending()
        assert len(pend) == 1
        assert pend[0].first_ts == 100.0, "re-emit must not reset the horizon anchor"

    @pytest.mark.asyncio
    async def test_mark_delivered_removes_from_pending(self, store):
        await store.put_intent(_rec("k:e"))
        await store.mark_delivered("k:e")
        assert await store.list_pending() == []

    @pytest.mark.asyncio
    async def test_mark_voided_excludes_from_pending(self, store):
        await store.put_intent(_rec("k:e"))
        await store.mark_voided("k:e", reason="past_retry_horizon")
        assert await store.list_pending() == []

    @pytest.mark.asyncio
    async def test_bump_attempt(self, store):
        await store.put_intent(_rec("k:e"))
        await store.bump_attempt("k:e")
        await store.bump_attempt("k:e")
        pend = await store.list_pending()
        assert pend[0].attempts == 2

    @pytest.mark.asyncio
    async def test_list_pending_respects_limit_and_order(self, store):
        await store.put_intent(_rec("a:e", ts=3.0))
        await store.put_intent(_rec("b:e", ts=1.0))
        await store.put_intent(_rec("c:e", ts=2.0))
        pend = await store.list_pending(limit=2)
        assert [r.key for r in pend] == ["b:e", "c:e"], "oldest-first, bounded by limit"


class TestAutoSelect:
    def test_prefers_ddb_then_redis_then_none(self):
        assert auto_select_outbox_store(redis=object(), ddb_client=object()).durable()
        assert type(auto_select_outbox_store(redis=object())).__name__ == "RedisOutboxStore"
        assert auto_select_outbox_store() is None   # caller must fail loud (D-29)


class _FakeRedisCfg:
    """Redis stub exposing CONFIG GET (fakeredis does not — that gap is itself
    the ElastiCache case, covered separately)."""
    def __init__(self, **cfg):
        self._cfg = cfg
    async def config_get(self, key):
        return {key: self._cfg.get(key, "")}


class TestRedisDurabilitySelfCheck:
    """D-32 Claim 2 — OPERATOR CHECK 2 as a machine check."""

    @pytest.mark.asyncio
    async def test_evicting_policy_is_not_durable(self):
        redis = _FakeRedisCfg(**{"maxmemory-policy": "allkeys-lru", "appendonly": "yes", "save": ""})
        durable, reason = await check_redis_outbox_durability(redis)
        assert durable is False and "evict" in reason

    @pytest.mark.asyncio
    async def test_noeviction_with_aof_is_durable(self):
        redis = _FakeRedisCfg(**{"maxmemory-policy": "noeviction", "appendonly": "yes", "save": ""})
        durable, _ = await check_redis_outbox_durability(redis)
        assert durable is True

    @pytest.mark.asyncio
    async def test_no_persistence_is_not_durable(self):
        redis = _FakeRedisCfg(**{"maxmemory-policy": "noeviction", "appendonly": "no", "save": ""})
        durable, reason = await check_redis_outbox_durability(redis)
        assert durable is False and "persistence" in reason

    @pytest.mark.asyncio
    async def test_rdb_save_points_count_as_persistence(self):
        redis = _FakeRedisCfg(**{"maxmemory-policy": "noeviction", "appendonly": "no", "save": "3600 1"})
        durable, _ = await check_redis_outbox_durability(redis)
        assert durable is True

    @pytest.mark.asyncio
    async def test_config_unavailable_requires_operator_confirmation(self):
        # fakeredis raises on CONFIG GET — exactly the ElastiCache case.
        r = fakeredis.aioredis.FakeRedis()
        try:
            durable, reason = await check_redis_outbox_durability(r, confirmed=False)
            assert durable is False and "CONFIG unavailable" in reason
            durable2, _ = await check_redis_outbox_durability(r, confirmed=True)
            assert durable2 is True   # operator assertion on the record
        finally:
            await r.aclose()


class TestBillingRefusedOnEphemeral:
    """D-32 Claim 3 — refuse to emit money events onto an ephemeral store."""

    @pytest.mark.asyncio
    async def test_disabled_billing_refuses_money_event_without_publishing(self):
        from ab0t_quota.billing.lifecycle import LifecycleEmitter
        published = {"n": 0}

        class _Sink:
            def publish(self, **k):
                published["n"] += 1
                return {"MessageId": "m"}

        emitter = LifecycleEmitter(sns_topic_arn="arn:aws:sns:us-east-1:1:t")
        emitter._client = _Sink()
        emitter._get_client = lambda: emitter._client
        emitter.disable_billing("no durable outbox (test)")
        assert emitter.billing_status().startswith("OFF")

        ok = await emitter.resource_stopped(
            org_id="o", user_id="u", resource_id="r", resource_type="sandbox",
            reservation_id="rsv-x", hourly_rate=Decimal("0.10"),
            started_at=__import__("datetime").datetime(2026, 4, 1, tzinfo=__import__("datetime").timezone.utc),
        )
        assert ok is False, "money event must be refused when billing is OFF"
        assert published["n"] == 0, "must NOT publish onto an ephemeral store"
        assert await emitter.pending_count() == 0, "must NOT enqueue onto an ephemeral store"
