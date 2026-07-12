"""D-30 — DDBOutboxStore against REAL DynamoDB Local (ticket 20260709).

The wired-in-prod-preference store is DDB (D-30), yet it had NO test — only Redis
(an evictable cache) was exercised. This suite runs the FULL OutboxStore contract
plus crash-resumption against DynamoDB Local on localhost:8000, using a THROWAWAY
table created + deleted per run (never platform data), the same pattern W-GO and
W-PY-B used.

Real backend, not a stub: this exercises the botocore ConditionalCheckFailed path
(the idempotent put), the `gsi_status` GSI query for list_pending, and number/string
attribute round-tripping — none of which a FakeDDB proves.

The `gsi_status` GSI a real table MUST define (documented for operators):
    KeySchema:  gsi_status_pk (HASH, S)  = "OUTBOXSTATUS#{status}"
                gsi_status_sk (RANGE, N)  = first_ts (epoch seconds)
    Projection: ALL
    Base table: PK (HASH, S), SK (RANGE, S)
"""
from __future__ import annotations

import socket
import uuid
from decimal import Decimal  # noqa: F401 (kept for parity with money-shaped fixtures)

import pytest
import pytest_asyncio

from ab0t_quota.billing.outbox import DDBOutboxStore, OutboxRecord, PENDING, VOIDED

ENDPOINT = "http://localhost:8000"


def _ddb_up() -> bool:
    try:
        s = socket.create_connection(("localhost", 8000), timeout=1)
        s.close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _ddb_up(), reason="DynamoDB Local not reachable on :8000")


@pytest_asyncio.fixture
async def ddb_store():
    import aioboto3
    session = aioboto3.Session()
    table = "ab0t_quota_outbox_test_" + uuid.uuid4().hex[:10]
    async with session.client(
        "dynamodb", region_name="us-east-1", endpoint_url=ENDPOINT,
        aws_access_key_id="test", aws_secret_access_key="test",
    ) as client:
        await client.create_table(
            TableName=table,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "gsi_status_pk", "AttributeType": "S"},
                {"AttributeName": "gsi_status_sk", "AttributeType": "N"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "gsi_status",
                "KeySchema": [
                    {"AttributeName": "gsi_status_pk", "KeyType": "HASH"},
                    {"AttributeName": "gsi_status_sk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        try:
            yield DDBOutboxStore(client, table_name=table), client, table
        finally:
            try:
                await client.delete_table(TableName=table)
            except Exception:
                pass


def _rec(key, ts=1.0):
    rid = key.split(":")[0]
    return OutboxRecord(
        key=key, event={"reservation_id": rid, "amount": "0.10"},
        event_type="resource.stopped", resource_type="sandbox",
        reservation_id=rid, first_ts=ts,
    )


class TestDDBOutboxContract:
    @pytest.mark.asyncio
    async def test_put_list_delivered(self, ddb_store):
        store, _, _ = ddb_store
        await store.put_intent(_rec("rsv-1:resource.stopped", ts=5.0))
        pend = await store.list_pending()
        assert len(pend) == 1 and pend[0].key == "rsv-1:resource.stopped"
        assert pend[0].status == PENDING and pend[0].first_ts == 5.0
        assert pend[0].event["amount"] == "0.10"   # event round-trips through DDB
        await store.mark_delivered("rsv-1:resource.stopped")
        assert await store.list_pending() == []

    @pytest.mark.asyncio
    async def test_conditional_put_is_idempotent_preserves_first_ts(self, ddb_store):
        """The real botocore ConditionalCheckFailedException path — a re-emit must
        NOT overwrite the horizon anchor."""
        store, _, _ = ddb_store
        await store.put_intent(_rec("k:e", ts=100.0))
        await store.put_intent(_rec("k:e", ts=999.0))
        pend = await store.list_pending()
        assert len(pend) == 1 and pend[0].first_ts == 100.0

    @pytest.mark.asyncio
    async def test_void_excludes_from_pending(self, ddb_store):
        store, _, _ = ddb_store
        await store.put_intent(_rec("k:e"))
        await store.mark_voided("k:e", reason="past_retry_horizon")
        assert await store.list_pending() == []
        rec = await store._get("k:e")
        assert rec is not None and rec.status == VOIDED and rec.reason == "past_retry_horizon"

    @pytest.mark.asyncio
    async def test_bump_attempt(self, ddb_store):
        store, _, _ = ddb_store
        await store.put_intent(_rec("k:e"))
        await store.bump_attempt("k:e")
        await store.bump_attempt("k:e")
        assert (await store.list_pending())[0].attempts == 2

    @pytest.mark.asyncio
    async def test_list_pending_ordered_and_bounded(self, ddb_store):
        store, _, _ = ddb_store
        await store.put_intent(_rec("a:e", ts=3.0))
        await store.put_intent(_rec("b:e", ts=1.0))
        await store.put_intent(_rec("c:e", ts=2.0))
        pend = await store.list_pending(limit=2)
        assert [r.key for r in pend] == ["b:e", "c:e"]

    @pytest.mark.asyncio
    async def test_ensure_table_creates_if_absent_and_is_idempotent(self):
        """`outbox.store=ddb` must work without a manual table-create step:
        ensure_table creates the table+GSI if absent and is a no-op if present."""
        import aioboto3
        session = aioboto3.Session()
        table = "ab0t_quota_outbox_ensure_" + uuid.uuid4().hex[:10]
        async with session.client(
            "dynamodb", region_name="us-east-1", endpoint_url=ENDPOINT,
            aws_access_key_id="test", aws_secret_access_key="test",
        ) as client:
            store = DDBOutboxStore(client, table_name=table)
            try:
                await store.ensure_table()                 # creates
                await store.ensure_table()                 # idempotent no-op
                # And it's actually usable end-to-end.
                await store.put_intent(_rec("x:e", ts=1.0))
                assert len(await store.list_pending()) == 1
            finally:
                try:
                    await client.delete_table(TableName=table)
                except Exception:
                    pass

    @pytest.mark.asyncio
    async def test_self_provisions_a_client_without_app_state(self):
        """D-32 Claim 1: the library builds its own durable DDB client from
        standard config — `app.state.ddb_client` is an optional override, not a
        precondition. A caller who does nothing gets DURABILITY, not a cache."""
        from ab0t_quota.billing.outbox import connect_ddb_outbox_store
        table = "ab0t_quota_outbox_selfprov_" + uuid.uuid4().hex[:10]
        store, aclose = await connect_ddb_outbox_store(
            region="us-east-1", endpoint_url=ENDPOINT, table_name=table,
        )
        try:
            await store.ensure_table()   # self-created table + GSI, waited ACTIVE
            await store.put_intent(_rec("rsv-sp:resource.stopped", ts=1.0))
            assert len(await store.list_pending()) == 1
        finally:
            try:
                await store.ddb.delete_table(TableName=table)
            except Exception:
                pass
            await aclose()

    @pytest.mark.asyncio
    async def test_crash_resumption_from_fresh_store_object(self, ddb_store):
        """The store object holds NO state — a brand-new DDBOutboxStore over the
        same table (the 'restarted process') sees the pending intent. With DDB as
        a separate service this survives a real process restart; here we prove the
        state lives in DynamoDB, not the object."""
        store1, client, table = ddb_store
        await store1.put_intent(_rec("rsv-restart:resource.stopped", ts=7.0))

        # "restart" — discard store1, build a fresh store over the same table.
        del store1
        store2 = DDBOutboxStore(client, table_name=table)
        pend = await store2.list_pending()
        assert len(pend) == 1 and pend[0].key == "rsv-restart:resource.stopped"
        await store2.mark_delivered("rsv-restart:resource.stopped")
        assert await store2.list_pending() == []
