"""P2.1 — DDBActivationStore against DynamoDB Local (ticket 20260709).

Coordinator directive: the DDB store shipped with NO backing test. DynamoDB Local
runs on localhost:8000 (W-GO verified its Go ledger the same way). This creates a
UNIQUELY-NAMED THROWAWAY table, exercises the store, and deletes it — it NEVER
touches `ab0t_quota_state` or any existing platform table.

Skips cleanly when aioboto3 is absent (it is an optional `dynamo` extra) or DDB
Local is unreachable, so the normal `dev` test run is unaffected. Still NOT real
AWS DynamoDB (IAM, real throughput/backoff) — that stays on the pre-prod gate.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

aioboto3 = pytest.importorskip("aioboto3")

from ab0t_quota.activations import (
    Activation, ActivationState, DDBActivationStore, mint_activation_id,
)

DDB_ENDPOINT = "http://localhost:8000"
_CREDS = dict(aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")


async def _client_ctx():
    return aioboto3.Session().client("dynamodb", endpoint_url=DDB_ENDPOINT, **_CREDS)


@pytest_asyncio.fixture
async def ddb_table():
    """Create a throwaway table with the GSI the store needs; drop it after."""
    table = f"ab0t_quota_activations_test_{uuid.uuid4().hex[:12]}"
    try:
        async with await _client_ctx() as c:
            await c.create_table(
                TableName=table,
                AttributeDefinitions=[
                    {"AttributeName": "PK", "AttributeType": "S"},
                    {"AttributeName": "SK", "AttributeType": "S"},
                    {"AttributeName": "GSI1PK", "AttributeType": "S"},
                    {"AttributeName": "GSI1SK", "AttributeType": "S"},
                ],
                KeySchema=[
                    {"AttributeName": "PK", "KeyType": "HASH"},
                    {"AttributeName": "SK", "KeyType": "RANGE"},
                ],
                GlobalSecondaryIndexes=[{
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }],
                BillingMode="PAY_PER_REQUEST",
            )
            await c.get_waiter("table_exists").wait(TableName=table)
    except Exception as e:  # DDB Local not reachable → skip, don't fail the run
        pytest.skip(f"DynamoDB Local unavailable at {DDB_ENDPOINT}: {e}")
    yield table
    async with await _client_ctx() as c:
        await c.delete_table(TableName=table)


class TestDDBActivationStore:
    @pytest.mark.asyncio
    async def test_full_lifecycle_against_ddb_local(self, ddb_table):
        async with await _client_ctx() as c:
            store = DDBActivationStore(c, table_name=ddb_table)
            aid = mint_activation_id()

            await store.put_open(Activation(
                activation_id=aid, org_id="org-1", user_id="u1",
                resource_key="sandbox", spend={"sandbox.concurrent": 1.0}))
            # put is idempotent (conditional write) — a replay does not clobber.
            await store.put_open(Activation(
                activation_id=aid, org_id="org-1", user_id="u1",
                resource_key="sandbox", spend={"sandbox.concurrent": 2.0}))

            got = await store.get(aid)
            assert got is not None
            assert got.state == ActivationState.OPEN.value
            assert got.spend == {"sandbox.concurrent": 1.0}, "replay must not overwrite"
            assert got.user_id == "u1"

            # list_open via the GSI (ORGOPEN#org)
            opens = await store.list_open("org-1")
            assert [a.activation_id for a in opens] == [aid]

            # release is idempotent (conditional state transition), drops from GSI
            assert (await store.mark_released(aid)) is not None
            assert (await store.mark_released(aid)) is None          # replay no-op
            assert await store.list_open("org-1") == []              # off the open index

            # settle from RELEASED, idempotent
            assert (await store.mark_settled(aid, "0.30")) is not None
            assert (await store.mark_settled(aid, "0.30")) is None
            final = await store.get(aid)
            assert final.state == ActivationState.SETTLED.value
            assert final.cost == "0.30"

    @pytest.mark.asyncio
    async def test_seed_gauge_from_ledger_ignores_drifted_snapshot(self, ddb_table):
        """P2.5 / D-10 / QI-07: a DRIFTED gauge snapshot (5) must NOT be resurrected
        on seed — the gauge restores from the ledger (Σ open activations = 3). Uses
        a throwaway snapshot table + an InMemory activation ledger."""
        import fakeredis.aioredis
        from ab0t_quota.persistence import QuotaStore
        from ab0t_quota.registry import ResourceRegistry
        from ab0t_quota.models.core import CounterType, ResourceDef
        from ab0t_quota.activations import InMemoryActivationStore, Activation
        from ab0t_quota.counters.gauge import GaugeCounter

        RK = "sandbox.concurrent"
        # Write a DRIFTED counter snapshot straight into the throwaway table.
        async with await _client_ctx() as c:
            await c.put_item(TableName=ddb_table, Item={
                "PK": {"S": "ORG#org-1"}, "SK": {"S": f"COUNTER#{RK}"},
                "GSI1PK": {"S": "COUNTER"}, "GSI1SK": {"S": f"ORG#org-1#{RK}"},
                "value": {"S": "5"},  # drift: ledger will say 3
            })

        # Ledger: 3 open activations (the justified level).
        ledger = InMemoryActivationStore()
        for i in range(3):
            await ledger.put_open(Activation(
                activation_id=f"act-{i}", org_id="org-1", user_id="u1",
                resource_key=RK, spend={RK: 1.0}))

        reg = ResourceRegistry()
        reg.register(ResourceDef(service="t", resource_key=RK, display_name="C",
                                 counter_type=CounterType.GAUGE))
        store = QuotaStore(table_name=ddb_table, endpoint_url=DDB_ENDPOINT)
        # Point the store's resource at the throwaway table (async resource handle).
        session = aioboto3.Session()
        async with session.resource("dynamodb", endpoint_url=DDB_ENDPOINT, **_CREDS) as res:
            store._table = await res.Table(ddb_table)
            redis = fakeredis.aioredis.FakeRedis()
            n = await store.seed_redis(redis, reg, activation_store=ledger)
            gauge = await GaugeCounter(redis, "org-1", RK).get()
            await redis.aclose()

        assert n == 1
        assert gauge == 3.0, f"seed resurrected drift: gauge={gauge}, expected ledger's 3.0 not snapshot's 5"

    @pytest.mark.asyncio
    async def test_seed_gauge_with_NO_snapshot_row_restored_from_ledger(self, ddb_table):
        """E3: a gauge with open activations but NO counter-snapshot row must still
        be restored on seed (previously it seeded to 0 = undercount / phantom
        headroom). Enumeration is ledger-authoritative, not snapshot-driven."""
        import fakeredis.aioredis
        from ab0t_quota.persistence import QuotaStore
        from ab0t_quota.registry import ResourceRegistry
        from ab0t_quota.models.core import CounterType, ResourceDef
        from ab0t_quota.activations import InMemoryActivationStore, Activation
        from ab0t_quota.counters.gauge import GaugeCounter

        RK = "sandbox.concurrent"
        # NOTE: deliberately write NO COUNTER# snapshot row for RK. The snapshot
        # table (ddb_table) is empty of counter rows.
        ledger = InMemoryActivationStore()
        for i in range(2):
            await ledger.put_open(Activation(
                activation_id=f"act-{i}", org_id="org-1", user_id="alice",
                resource_key=RK, spend={RK: 1.0}))

        reg = ResourceRegistry()
        reg.register(ResourceDef(service="t", resource_key=RK, display_name="C",
                                 counter_type=CounterType.GAUGE))
        store = QuotaStore(table_name=ddb_table, endpoint_url=DDB_ENDPOINT)
        session = aioboto3.Session()
        async with session.resource("dynamodb", endpoint_url=DDB_ENDPOINT, **_CREDS) as res:
            store._table = await res.Table(ddb_table)
            redis = fakeredis.aioredis.FakeRedis()      # simulate a wiped Redis
            n = await store.seed_redis(redis, reg, activation_store=ledger)
            g = GaugeCounter(redis, "org-1", RK)
            org_level = await g.get()
            user_level = await g.get_user("alice")
            await redis.aclose()

        assert n == 1, "the ledger-only gauge was enumerated and restored"
        assert org_level == 2.0, f"undercount: gauge={org_level}, ledger says 2 (no snapshot row)"
        assert user_level == 2.0, "per-user partition derived from the ledger too"

    @pytest.mark.asyncio
    async def test_settle_direct_from_open(self, ddb_table):
        async with await _client_ctx() as c:
            store = DDBActivationStore(c, table_name=ddb_table)
            aid = mint_activation_id()
            await store.put_open(Activation(activation_id=aid, org_id="o", user_id=None,
                                            resource_key="x", spend={}))
            # settle without a prior release (OPEN -> SETTLED transition path)
            assert (await store.mark_settled(aid, "1.00")) is not None
            assert (await store.get(aid)).state == ActivationState.SETTLED.value
            assert await store.list_open("o") == []
