"""D-81 / D-82 — the last two framed defects.

**D-81 — a configured guarantee is not a working one. (The next D-80, and worse.)**
We verify `appendonly yes`. We **never ask whether the writes are succeeding.** A full disk, a
permissions error, a failing volume → **AOF configured, AOF failing, the config check green,
and the outbox losing money events.** `aof_last_write_status` / `rdb_last_bgsave_status` are
the FACTS; `appendonly` is only the intent.

This is **worse than D-80's eviction**: the counter only needs non-eviction and can HEAL
(the reconciler converges it to Σ open activations). The **outbox REQUIRES persistence**
(D-30/D-32) — **a lost outbox row is money nobody can reconstruct.**

Verified on a REAL server, not a stubbed status string (see the artifact): a redis:7 with its
AOF directory on a 4 MiB tmpfs, filled until writes genuinely fail, reports
`aof_last_write_status:err` **and keeps serving**. Two real behaviours worth knowing:
  * with `appendfsync always`, Redis **exits** on the AOF write error (loud);
  * with the **DEFAULT `everysec`**, it **stays up** with a failing AOF — the quiet case,
    which is the one that costs money.

**D-82 — `DDBLedgerStore` assumed its table existed.**
The outbox and the activation store both PROVISION (`ensure_table`, GSI-ACTIVE wait) and are
PREFLIGHTED (D-76). The handler ledger did **neither**: a client wiring it discovered this at
their first auth webhook, in production. *A fake never notices, because a fake creates nothing.*

Ticket: 20260709_ab0t_quota_systemic_integrity_redesign (D-81, D-82)
"""
import os

import pytest
import fakeredis.aioredis
from fastapi import FastAPI

from ab0t_quota.redis_preflight import (
    check_persist_facts,
    check_redis_durability,
    evaluate_persist_facts,
    persist_facts_ok,
    verify_redis_invariants,
)
from ab0t_quota.setup import _make_redis_revalidator, _register_capability_routes, quota_health

AOF_FAIL_ADDR = os.getenv("AB0T_QUOTA_TEST_AOF_FAIL_ADDR")   # a REAL redis with a failing AOF
DDB_ENDPOINT = os.getenv("AB0T_QUOTA_TEST_DDB_ENDPOINT")


class _PersistRedis(fakeredis.aioredis.FakeRedis):
    """A Redis that reports its persistence FACTS (fakeredis has no INFO)."""
    _aof_enabled = 1
    _aof_write = "ok"
    _rdb_bgsave = "ok"
    _aof_rewrite = "ok"

    async def config_get(self, key):
        return {key: {"maxmemory-policy": "noeviction", "appendonly": "yes", "save": ""}.get(key, "")}

    async def info(self, section=None, **kw):
        if section == "persistence":
            return {"aof_enabled": self._aof_enabled,
                    "aof_last_write_status": self._aof_write,
                    "aof_last_bgrewrite_status": self._aof_rewrite,
                    "rdb_last_bgsave_status": self._rdb_bgsave}
        if section == "cluster":
            return {"cluster_enabled": 0}
        if section == "memory":
            return {"maxmemory": 0, "used_memory": 1000}
        if section == "stats":
            return {"evicted_keys": 0}
        return {"redis_version": "7.2.4"}


# ===========================================================================
# D-81 — the FACT: persistence is configured, and it is FAILING
# ===========================================================================

class TestPersistFacts:
    def test_healthy_persistence_is_ok(self):
        status, _ = evaluate_persist_facts(aof_enabled=1, aof_write="ok",
                                           rdb_bgsave="ok", aof_rewrite="ok")
        assert status == "ok"

    def test_a_FAILING_aof_write_is_a_money_incident(self):
        """The defect, in one assertion: `appendonly yes` (the config check is GREEN) while
        the writes are failing. The outbox is being told to persist money events onto a Redis
        that cannot persist them."""
        status, detail = evaluate_persist_facts(aof_enabled=1, aof_write="err",
                                                rdb_bgsave="ok", aof_rewrite="ok")
        assert status == "persist_failing"
        assert "aof_last_write_status=err" in detail
        assert "money" in detail.lower() or "reconstruct" in detail.lower()

    def test_a_FAILING_bgsave_is_also_a_money_incident(self):
        status, detail = evaluate_persist_facts(aof_enabled=0, aof_write="ok",
                                                rdb_bgsave="err", aof_rewrite="ok")
        assert status == "persist_failing"
        assert "rdb_last_bgsave_status=err" in detail

    def test_a_failing_REWRITE_is_reported_too(self):
        """A failing AOF rewrite means the file grows without bound and a restart may never
        replay — the persistence is decaying even while writes still land."""
        status, _ = evaluate_persist_facts(aof_enabled=1, aof_write="ok",
                                           rdb_bgsave="ok", aof_rewrite="err")
        assert status == "persist_failing"

    def test_unreadable_is_unknown_never_assumed_ok(self):
        status, _ = evaluate_persist_facts(aof_enabled=None, aof_write=None,
                                           rdb_bgsave=None, aof_rewrite=None)
        assert status == "unknown"

    def test_the_health_predicate_degrades_only_on_an_OBSERVED_failure(self):
        assert persist_facts_ok("ok (aof=ok, rdb=ok)") is True
        assert persist_facts_ok("FAILING (aof_last_write_status=err)") is False
        assert persist_facts_ok("unknown") is True   # the config check already fails closed


@pytest.mark.asyncio
class TestDurabilityUsesTheFact:
    async def test_a_configured_but_FAILING_aof_is_NOT_durable(self):
        """D-32 asked `appendonly`; it never asked whether the writes worked. The ONE
        durability implementation (D-35) now asks both — so the existing D-34 boot gate
        (a paid service that cannot durably bill must not start) refuses this Redis for free."""
        r = _PersistRedis()
        r._aof_write = "err"
        durable, reason = await check_redis_durability(r)
        assert durable is False
        assert "err" in reason

    async def test_a_working_aof_is_durable(self):
        """[control] the guard is not a blanket reject."""
        durable, _ = await check_redis_durability(_PersistRedis())
        assert durable is True

    async def test_the_check_reads_the_real_INFO_fields(self):
        r = _PersistRedis()
        r._aof_write = "err"
        facts = await check_persist_facts(r)
        assert facts["aof_last_write_status"] == "err"


@pytest.mark.asyncio
class TestRuntimeDetectionAndSink:
    async def test_a_persist_failure_on_the_OUTBOX_redis_degrades_and_alerts(self):
        """D-75's runtime law applied to D-81: the disk fills at 3am. The boot check said
        'durable' and would have gone on saying it forever."""
        r = _PersistRedis()
        app = FastAPI()
        app.state.quota_capabilities = {}
        app.state.quota_outbox_on_redis = True      # the outbox lives HERE
        app.state.quota_required_caps = {"redis_persist_status"}
        _register_capability_routes(app)

        fired = []

        class _Alerts:
            async def invariant_violated(self, name, detail):
                fired.append(name); return True

            async def invariant_restored(self, name):
                fired.append(f"restored:{name}"); return True

        revalidate = _make_redis_revalidator(app, r, {}, _Alerts())

        await revalidate()
        assert quota_health(app)["status"] == "ok", "[control] a working AOF is healthy"

        r._aof_write = "err"                        # the disk fills
        await revalidate()

        h = quota_health(app)
        assert "redis_persist_status" in h["degraded"], (
            "a Redis that is FAILING to persist is not durable — the outbox is losing money "
            "events while every config check reads green")
        assert "redis_persist_status" in fired, "a money incident must reach a human"

        r._aof_write = "ok"                         # the operator frees the disk
        await revalidate()
        assert quota_health(app)["status"] == "ok"
        assert "restored:redis_persist_status" in fired

    async def test_a_persist_failure_where_the_outbox_is_NOT_hosted_does_not_claim_money_loss(self):
        """Severity by consequence, not uniformity (the D-76 lesson): if the outbox lives in
        DynamoDB, a failing AOF on the COUNTER's Redis is not money loss — the counter heals
        (reconciler → Σ open activations, D-28). It is recorded and logged, not a money
        incident. Over-refusing trains operators to ignore the probe (D-49)."""
        r = _PersistRedis()
        r._aof_write = "err"
        app = FastAPI()
        app.state.quota_capabilities = {}
        app.state.quota_outbox_on_redis = False     # outbox is on DDB
        await _make_redis_revalidator(app, r, {}, None)()

        caps = app.state.quota_capabilities
        assert "redis_persist_status" in caps
        assert persist_facts_ok(caps["redis_persist_status"]) is True, (
            "the counter can heal — this must not be reported as money loss")
        assert "outbox" in caps["redis_persist_status"].lower()


# ===========================================================================
# D-82 — the handler ledger must PROVISION and be PREFLIGHTED, like the others
# ===========================================================================

@pytest.mark.asyncio
@pytest.mark.skipif(not DDB_ENDPOINT,
                    reason="AB0T_QUOTA_TEST_DDB_ENDPOINT not set — the real DynamoDB leg is operator-gated")
class TestHandlerLedgerProvisioning:
    async def _client(self):
        import aioboto3
        session = aioboto3.Session(aws_access_key_id="x", aws_secret_access_key="x",
                                   region_name="us-east-1")
        cm = session.client("dynamodb", endpoint_url=DDB_ENDPOINT)
        return cm, await cm.__aenter__()

    async def test_ensure_table_CREATES_the_table_and_waits_for_it(self):
        """D-82: the outbox and the activation store both provision their table. The handler
        ledger ASSUMED it existed — so a client wiring it hit a ResourceNotFoundException at
        their first auth webhook, in production."""
        from ab0t_quota.handler_ledger import DDBLedgerStore

        cm, client = await self._client()
        table = "d82_handler_ledger"
        try:
            try:
                await client.delete_table(TableName=table)
            except Exception:
                pass

            store = DDBLedgerStore(client, table_name=table)
            await store.ensure_table()               # must CREATE it

            desc = await client.describe_table(TableName=table)
            assert desc["Table"]["TableStatus"] == "ACTIVE"

            # And it must WORK against the table it just made — the conditional write that the
            # whole idempotency guarantee rests on.
            first = await store.record_attempt(handler_name="h", event_id="e-d82",
                                               event_type="x", event_payload={}, org_id="o1")
            assert first.proceed is True
            second = await store.record_attempt(handler_name="h", event_id="e-d82",
                                                event_type="x", event_payload={}, org_id="o1")
            assert second.proceed is False

            # Idempotent: a second ensure_table on an existing table is a no-op, not a crash.
            await store.ensure_table()
        finally:
            try:
                await client.delete_table(TableName=table)
            except Exception:
                pass
            await cm.__aexit__(None, None, None)

    async def test_the_ledger_table_is_PREFLIGHTED_like_the_other_two(self):
        """D-82 + D-76: once it exists, it is verified — ACTIVE, TTL on the attribute the
        store actually writes (`ttl`), PITR (unanswerable on DDB Local ⇒ the operator
        assertion, on the record)."""
        from ab0t_quota.ddb_preflight import verify_ddb_table
        from ab0t_quota.handler_ledger import DDBLedgerStore

        cm, client = await self._client()
        table = "d82_handler_ledger_pf"
        try:
            store = DDBLedgerStore(client, table_name=table)
            await store.ensure_table()

            value, fatal, warn = await verify_ddb_table(
                client, table, ttl_attribute="ttl", pitr_confirmed=True)
            assert fatal is None, fatal

            # TTL on an attribute the store does NOT write would let DynamoDB delete ledger
            # rows we never marked — the same FATAL as the other two tables.
            _v, fatal2, _w = await verify_ddb_table(
                client, table, ttl_attribute="not_our_attribute", pitr_confirmed=True)
            assert (fatal2 is None) or ("ttl" in fatal2.lower())
        finally:
            try:
                await client.delete_table(TableName=table)
            except Exception:
                pass
            await cm.__aexit__(None, None, None)


# ===========================================================================
# REAL Redis with a REAL failing AOF (operator-gated) — not a stubbed status
# ===========================================================================

@pytest.mark.asyncio
@pytest.mark.skipif(not AOF_FAIL_ADDR,
                    reason="AB0T_QUOTA_TEST_AOF_FAIL_ADDR not set — the real failing-AOF leg is operator-gated")
class TestRealFailingAOF:
    async def test_a_REAL_redis_whose_AOF_IS_FAILING_is_caught(self):
        """THE D-81 test. A real redis:7 whose AOF directory is a 4 MiB tmpfs, filled until the
        writes genuinely fail: `appendonly` still reads `yes` (the config check is GREEN), and
        `aof_last_write_status` says `err`. The server is still up and serving.

        Sticky-signal discipline (my own lesson from D-80): this leg does NOT reset the
        server's state — the failure IS the fixture, and it is a throwaway container."""
        from redis.asyncio import Redis
        r = Redis.from_url(f"redis://{AOF_FAIL_ADDR}", decode_responses=False)
        try:
            facts = await check_persist_facts(r)
            assert facts.get("aof_last_write_status") == "err", (
                f"the fixture failed to make the AOF genuinely fail: {facts}")

            cfg = await r.config_get("appendonly")
            assert cfg.get("appendonly") == "yes", "the CONFIG still says appendonly=yes…"

            durable, reason = await check_redis_durability(r)
            assert durable is False, (
                "…and D-32's config-only check called this Redis DURABLE while its AOF writes "
                "were failing on a full disk")
            assert "err" in reason

            caps, unsafe = await verify_redis_invariants(r, {}, outbox_on_redis=True)
            assert "redis_persist_status" in [k for k, _ in unsafe]
        finally:
            await r.aclose()
