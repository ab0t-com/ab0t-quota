"""Conformance tests for the handler ledger.

Same test body runs against all three backends (InMemory, Redis, DDB)
via parametrized fixture. If a test passes on one backend and fails on
another, the backend has a contract bug — not the test.

Redis backend uses fakeredis (in-process; no real Redis required).
DDB backend uses a minimal moto-style stub built into this file.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ab0t_quota.handler_ledger import (
    InMemoryLedgerStore,
    RedisLedgerStore,
    DDBLedgerStore,
    LedgerStatus,
    LedgerRow,
    HandlerContext,
    SkipOutcome,
    SuccessOutcome,
    idempotent,
    is_idempotent_handler,
    idempotent_config,
    auto_select_store,
)


# ---------------------------------------------------------------------------
# Fake redis (no extra dep)
# ---------------------------------------------------------------------------

class FakeRedis:
    """Minimal async redis stub: GET/SET/EXISTS/DELETE + sorted-set ZADD/ZRANGE.
    Enough for our ledger; not a general redis impl."""
    def __init__(self):
        self._kv: dict[str, str] = {}
        self._z: dict[str, list[tuple[float, str]]] = {}  # key -> [(score, member)]

    async def get(self, k):
        v = self._kv.get(k)
        return v.encode() if isinstance(v, str) else v
    async def set(self, k, v, ex=None, nx=None):
        # nx support (additive): atomic claim — return None if key already exists.
        if nx and k in self._kv:
            return None
        self._kv[k] = v if isinstance(v, str) else v.decode()
        return True
    async def eval(self, script, numkeys, *args):
        # Additive: supports the ledger's stale-reclaim CAS only — compare
        # KEYS[1] to ARGV[1] and SET to ARGV[2] (EX ARGV[3]) iff equal.
        keys = args[:numkeys]
        argv = args[numkeys:]
        stored = self._kv.get(keys[0])
        stored_b = stored.encode() if isinstance(stored, str) else stored
        expected = argv[0].encode() if isinstance(argv[0], str) else argv[0]
        if stored_b == expected:
            new = argv[1]
            self._kv[keys[0]] = new if isinstance(new, str) else new.decode()
            return 1
        return 0
    async def exists(self, k):
        return 1 if k in self._kv else 0
    async def delete(self, k):
        self._kv.pop(k, None)
        self._z.pop(k, None)
    async def expire(self, k, secs):
        pass
    async def zadd(self, k, mapping):
        items = self._z.setdefault(k, [])
        for member, score in mapping.items():
            items[:] = [(s, m) for s, m in items if m != member]
            items.append((score, member))
        items.sort()
    async def zrem(self, k, *members):
        items = self._z.get(k, [])
        for m in members:
            items[:] = [(s, mm) for s, mm in items if mm != m]
    async def zrange(self, k, start, end):
        items = self._z.get(k, [])
        return [m for _, m in items[start:end + 1 if end != -1 else None]]
    async def zrevrangebyscore(self, k, max_score, min_score, start=0, num=50):
        items = sorted(self._z.get(k, []), key=lambda x: x[0], reverse=True)
        if min_score != "-inf":
            items = [(s, m) for s, m in items if s >= float(min_score)]
        return [m for _, m in items[start:start + num]]


# ---------------------------------------------------------------------------
# Fake DDB (no boto/moto dep)
# ---------------------------------------------------------------------------

class ConditionalCheckFailedException(Exception):
    """Stub analog of DynamoDB's conditional-write failure. The production
    detector (`handler_ledger._is_conditional_check_failed`) matches it by
    class name, mirroring botocore's ConditionalCheckFailedException."""


class FakeDDB:
    """Minimal async DDB stub: put_item/get_item/update_item/delete_item/query.
    Supports our 2 GSIs (gsi1, gsi2) via in-memory secondary indexes."""
    def __init__(self):
        self._items: dict[tuple, dict] = {}  # (PK, SK) -> item

    async def put_item(self, *, TableName, Item, ConditionExpression=None,
                       ExpressionAttributeValues=None):
        pk = Item["PK"]["S"]
        sk = Item["SK"]["S"]
        # ConditionExpression support (additive): honor attribute_not_exists(PK)
        # so the atomic conditional-create claim (QC-01) is exercised.
        if ConditionExpression and "attribute_not_exists(PK)" in ConditionExpression:
            if (pk, sk) in self._items:
                raise ConditionalCheckFailedException(pk)
        self._items[(pk, sk)] = Item

    async def get_item(self, *, TableName, Key):
        pk = Key["PK"]["S"]
        sk = Key["SK"]["S"]
        item = self._items.get((pk, sk))
        return {"Item": item} if item else {}

    async def update_item(self, *, TableName, Key, UpdateExpression, ExpressionAttributeNames=None, ExpressionAttributeValues=None):
        pk = Key["PK"]["S"]
        sk = Key["SK"]["S"]
        item = self._items.get((pk, sk))
        if item is None:
            return {}
        # Lazy: parse SET / REMOVE clauses
        # SET part: comma-separated "key = :ph"
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        upper = UpdateExpression.upper()
        # split into SET and REMOVE clauses
        set_part = ""
        remove_part = ""
        if "REMOVE" in upper:
            si = UpdateExpression.find("SET")
            ri = upper.find("REMOVE")
            set_part = UpdateExpression[si + 3:ri].strip() if si != -1 else ""
            remove_part = UpdateExpression[ri + 6:].strip()
        else:
            set_part = UpdateExpression.split("SET", 1)[1].strip() if "SET" in UpdateExpression else ""
        for assign in [a.strip() for a in set_part.split(",") if a.strip()]:
            k, _, v_ph = assign.partition("=")
            k = k.strip()
            v_ph = v_ph.strip()
            real_key = names.get(k, k)
            item[real_key] = values[v_ph]
        for k in [r.strip() for r in remove_part.split(",") if r.strip()]:
            real_key = names.get(k, k)
            item.pop(real_key, None)

    async def delete_item(self, *, TableName, Key):
        pk = Key["PK"]["S"]
        sk = Key["SK"]["S"]
        self._items.pop((pk, sk), None)

    async def query(self, *, TableName, IndexName=None, KeyConditionExpression, ExpressionAttributeValues,
                    Limit=50, ScanIndexForward=True, ProjectionExpression=None, ExclusiveStartKey=None):
        # very lazy parse — look for "gsi1_pk = :pk" or "gsi2_pk = :pk"
        gsi_field = "gsi1_pk" if "gsi1_pk" in KeyConditionExpression else (
            "gsi2_pk" if "gsi2_pk" in KeyConditionExpression else None
        )
        sk_field = "gsi1_sk" if gsi_field == "gsi1_pk" else ("gsi2_sk" if gsi_field == "gsi2_pk" else None)
        target_pk = ExpressionAttributeValues[":pk"]["S"]
        target_sk_min = ExpressionAttributeValues.get(":sk", {}).get("S")
        matched = []
        for item in self._items.values():
            if gsi_field and item.get(gsi_field, {}).get("S") == target_pk:
                if target_sk_min and item.get(sk_field, {}).get("S", "") < target_sk_min:
                    continue
                matched.append(item)
        matched.sort(key=lambda i: i.get(sk_field, {}).get("S", ""), reverse=not ScanIndexForward)
        return {"Items": matched[:Limit]}


# ---------------------------------------------------------------------------
# Parametrized backend fixture — same tests run against all 3
# ---------------------------------------------------------------------------

@pytest.fixture(params=["memory", "redis", "ddb"])
def store(request):
    if request.param == "memory":
        return InMemoryLedgerStore()
    if request.param == "redis":
        return RedisLedgerStore(FakeRedis())
    if request.param == "ddb":
        return DDBLedgerStore(FakeDDB())
    raise ValueError(request.param)


# ---------------------------------------------------------------------------
# Conformance: every backend must pass these
# ---------------------------------------------------------------------------

class TestConformance:
    @pytest.mark.asyncio
    async def test_first_attempt_proceeds(self, store):
        result = await store.record_attempt(
            handler_name="h", event_id="e1", event_type="x",
            event_payload={"a": 1}, user_id="u1",
        )
        assert result.proceed is True
        assert result.cached_row is None

    @pytest.mark.asyncio
    async def test_second_attempt_after_success_returns_cached(self, store):
        await store.record_attempt(handler_name="h", event_id="e1", event_type="x",
                                   event_payload={}, user_id="u1")
        await store.record_outcome(handler_name="h", event_id="e1",
                                    status=LedgerStatus.SUCCESS, side_effect_id="sid1")
        # Second attempt with same (handler, event_id) → cached
        result = await store.record_attempt(handler_name="h", event_id="e1", event_type="x",
                                            event_payload={}, user_id="u1")
        assert result.proceed is False
        assert result.cached_row is not None
        assert result.cached_row.status == LedgerStatus.SUCCESS
        assert result.cached_row.side_effect_id == "sid1"

    @pytest.mark.asyncio
    async def test_outcome_updates_row(self, store):
        await store.record_attempt(handler_name="h", event_id="e2", event_type="x",
                                   event_payload={"k": "v"}, user_id="u1", org_id="o1")
        await store.record_outcome(handler_name="h", event_id="e2",
                                    status=LedgerStatus.SUCCESS, side_effect_id="sid2",
                                    reason="ok")
        row = await store.get_row(handler_name="h", event_id="e2")
        assert row is not None
        assert row.status == LedgerStatus.SUCCESS
        assert row.side_effect_id == "sid2"
        assert row.reason == "ok"
        assert row.completed_at is not None

    @pytest.mark.asyncio
    async def test_failed_attempt_can_be_retried(self, store):
        await store.record_attempt(handler_name="h", event_id="e3", event_type="x",
                                   event_payload={}, user_id="u1")
        await store.record_outcome(handler_name="h", event_id="e3",
                                    status=LedgerStatus.FAILED, error="boom", attempts=1)
        # Retry — FAILED is not terminal; lease has expired by record_outcome.
        # InMemory store: re-record_attempt should bump attempts
        result = await store.record_attempt(handler_name="h", event_id="e3", event_type="x",
                                            event_payload={}, user_id="u1")
        # InMemory: proceed=True (FAILED not in terminal list). Redis: same.
        # DDB: same logic. All three should allow retry.
        assert result.proceed is True

    @pytest.mark.asyncio
    async def test_failed_permanent_blocks_retry(self, store):
        await store.record_attempt(handler_name="h", event_id="e4", event_type="x",
                                   event_payload={}, user_id="u1")
        await store.record_outcome(handler_name="h", event_id="e4",
                                    status=LedgerStatus.FAILED_PERMANENT, error="max attempts")
        result = await store.record_attempt(handler_name="h", event_id="e4", event_type="x",
                                            event_payload={}, user_id="u1")
        assert result.proceed is False
        assert result.cached_row.status == LedgerStatus.FAILED_PERMANENT

    @pytest.mark.asyncio
    async def test_skipped_is_terminal(self, store):
        await store.record_attempt(handler_name="h", event_id="e5", event_type="x",
                                   event_payload={}, user_id="u1")
        await store.record_outcome(handler_name="h", event_id="e5",
                                    status=LedgerStatus.SKIPPED, reason="already done")
        result = await store.record_attempt(handler_name="h", event_id="e5", event_type="x",
                                            event_payload={}, user_id="u1")
        assert result.proceed is False

    @pytest.mark.asyncio
    async def test_business_dedup_round_trip(self, store):
        key = "credit:org:o1:tier:free"
        assert await store.already_done(dedup_key=key) is False
        await store.mark_done(dedup_key=key, source_handler="h", source_event_id="e6", side_effect_id="sid6")
        assert await store.already_done(dedup_key=key) is True

    @pytest.mark.asyncio
    async def test_query_by_user_returns_rows(self, store):
        for i in range(3):
            await store.record_attempt(handler_name="h", event_id=f"u_e{i}", event_type="x",
                                       event_payload={}, user_id="alice")
            await store.record_outcome(handler_name="h", event_id=f"u_e{i}",
                                        status=LedgerStatus.SUCCESS)
        await store.record_attempt(handler_name="h", event_id="bob_e", event_type="x",
                                   event_payload={}, user_id="bob")
        rows = await store.query_by_user("alice")
        assert len(rows) == 3
        assert all(r.user_id == "alice" for r in rows)

    @pytest.mark.asyncio
    async def test_query_by_status_returns_rows(self, store):
        await store.record_attempt(handler_name="h", event_id="s1", event_type="x",
                                   event_payload={}, user_id="u1")
        await store.record_outcome(handler_name="h", event_id="s1", status=LedgerStatus.SUCCESS)
        await store.record_attempt(handler_name="h", event_id="s2", event_type="x",
                                   event_payload={}, user_id="u1")
        await store.record_outcome(handler_name="h", event_id="s2", status=LedgerStatus.FAILED)
        succ = await store.query_by_status(LedgerStatus.SUCCESS)
        fail = await store.query_by_status(LedgerStatus.FAILED)
        assert len(succ) >= 1 and all(r.status == LedgerStatus.SUCCESS for r in succ)
        assert len(fail) >= 1 and all(r.status == LedgerStatus.FAILED for r in fail)

    @pytest.mark.asyncio
    async def test_delete_user_cascade(self, store):
        await store.record_attempt(handler_name="h", event_id="del1", event_type="x",
                                   event_payload={}, user_id="gdpr_user")
        await store.record_outcome(handler_name="h", event_id="del1", status=LedgerStatus.SUCCESS)
        await store.record_attempt(handler_name="h", event_id="del2", event_type="x",
                                   event_payload={}, user_id="gdpr_user")
        await store.record_outcome(handler_name="h", event_id="del2", status=LedgerStatus.SUCCESS)
        await store.record_attempt(handler_name="h", event_id="keep", event_type="x",
                                   event_payload={}, user_id="other_user")
        await store.record_outcome(handler_name="h", event_id="keep", status=LedgerStatus.SUCCESS)

        deleted = await store.delete_user("gdpr_user")
        assert deleted == 2

        rows = await store.query_by_user("gdpr_user")
        assert len(rows) == 0

        kept = await store.query_by_user("other_user")
        assert len(kept) == 1

    @pytest.mark.asyncio
    async def test_event_payload_preserved_for_replay(self, store):
        payload = {"event_type": "x", "data": {"user_id": "u1", "extra": [1, 2, 3]}}
        await store.record_attempt(handler_name="h", event_id="payload_test", event_type="x",
                                   event_payload=payload, user_id="u1")
        row = await store.get_row(handler_name="h", event_id="payload_test")
        assert row.event_payload == payload


# ---------------------------------------------------------------------------
# Decorator metadata + helpers
# ---------------------------------------------------------------------------

class TestDecorator:
    def test_marks_handler(self):
        @idempotent(handler="h1")
        async def fn(event, ctx): pass
        assert is_idempotent_handler(fn) is True

    def test_default_retry_config(self):
        @idempotent(handler="h1")
        async def fn(event, ctx): pass
        cfg = idempotent_config(fn)
        assert cfg["retry"]["attempts"] == 3
        assert cfg["retry"]["backoff"] == "exponential"

    def test_retry_false_disables(self):
        @idempotent(handler="h1", retry=False)
        async def fn(event, ctx): pass
        assert idempotent_config(fn)["retry"] is None

    def test_custom_retry_merges(self):
        @idempotent(handler="h1", retry={"attempts": 5})
        async def fn(event, ctx): pass
        cfg = idempotent_config(fn)
        assert cfg["retry"]["attempts"] == 5
        assert cfg["retry"]["backoff"] == "exponential"  # default carried

    def test_key_callable_stored(self):
        @idempotent(handler="h1", key=lambda e: f"k:{e['data']['x']}")
        async def fn(event, ctx): pass
        cfg = idempotent_config(fn)
        assert cfg["key_fn"] is not None
        assert cfg["key_fn"]({"data": {"x": "abc"}}) == "k:abc"

    def test_non_idempotent_returns_false(self):
        async def plain(event): pass
        assert is_idempotent_handler(plain) is False


# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------

class TestHandlerContext:
    @pytest.mark.asyncio
    async def test_already_done_no_key(self):
        ctx = HandlerContext("h", "e1", "x", {}, InMemoryLedgerStore())
        assert await ctx.already_done() is False

    @pytest.mark.asyncio
    async def test_mark_done_and_check(self):
        store = InMemoryLedgerStore()
        ctx = HandlerContext("h", "e1", "x", {}, store, _dedup_key="k:1")
        assert await ctx.already_done() is False
        await ctx.mark_done(side_effect_id="sid")
        assert await ctx.already_done() is True

    def test_skip_returns_sentinel(self):
        ctx = HandlerContext("h", "e1", "x", {}, InMemoryLedgerStore())
        out = ctx.skip("because")
        assert isinstance(out, SkipOutcome)
        assert out.reason == "because"

    def test_success_returns_sentinel(self):
        ctx = HandlerContext("h", "e1", "x", {}, InMemoryLedgerStore())
        out = ctx.success(side_effect_id="sid")
        assert isinstance(out, SuccessOutcome)
        assert out.side_effect_id == "sid"


# ---------------------------------------------------------------------------
# Auto-select store
# ---------------------------------------------------------------------------

class TestAutoSelect:
    def test_prefers_ddb(self):
        fake_ddb = object()
        fake_redis = object()
        store = auto_select_store(redis=fake_redis, ddb_client=fake_ddb)
        assert isinstance(store, DDBLedgerStore)

    def test_falls_back_to_redis(self):
        fake_redis = object()
        store = auto_select_store(redis=fake_redis, ddb_client=None)
        assert isinstance(store, RedisLedgerStore)

    def test_in_memory_last_resort(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            store = auto_select_store(redis=None, ddb_client=None)
        assert isinstance(store, InMemoryLedgerStore)
        assert any("NO PERSISTENT STORE" in r.message for r in caplog.records)
