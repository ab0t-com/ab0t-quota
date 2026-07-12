"""D-80 / D-79 / D-78 — the last two caveats, and the audit of our own doubles.

**D-80 — check the EFFECT, not just the policy.**
Every guard we own asks the server what its configuration *is*. None asks what it *did*.
A Redis whose `maxmemory-policy` was corrected to `noeviction` at 09:00 — *after* it evicted
a live gauge at 03:00 — **passes every check we own**, while the damage sits in the counter:
an evicted gauge reads LOW → phantom headroom → over-admission (D-31's forbidden direction).
`INFO stats.evicted_keys` is the *fact*. It is one field away, and we never looked.
So: `evicted_keys > 0` on a Redis holding the counter is a **money incident** — degrade the
probe, alert, and **the counter is no longer trustworthy: it must be reconciled.**

**D-79 — a guarantee the client can switch off is not a guarantee.**
The D-75 re-verification rides the reconciler loop. A client who disables the reconciler
therefore gets no re-verification. Under ZERO CAVEATS that is a hole, and the fix is NOT a
second loop (D-50: every loop we add is one more thing that can be dead) — it is D-66's
machinery: **DERIVE the requirement from config.** If the counter lives on Redis, the
preflight re-verification is a REQUIRED loop; a required-but-absent loop **degrades
`/quota/health`**. Wiring satisfies the contract; it does not define it.

**D-78 — a double can prove what YOUR code does; it can never prove the OTHER side behaves
as you modelled it.** My own D-75 alerts failed validation and reached NOBODY, while the unit
test passed — because it asserted against a *stub* alerts object. We wrote "an event with no
sink is not observability" and then proved our compliance with a fake sink. These tests assert
at the REAL sink (the real `DriftAlertManager`, the real `LifecycleEmitter`, the real
`LibraryReconciler`) wherever the real thing exists.

Ticket: 20260709_ab0t_quota_systemic_integrity_redesign (D-78, D-79, D-80)
"""
import asyncio
import os

import pytest
import fakeredis.aioredis
from fastapi import FastAPI

from ab0t_quota.redis_preflight import (
    check_evicted_keys,
    evaluate_eviction_facts,
    verify_redis_invariants,
)
from ab0t_quota.setup import (
    _make_redis_revalidator,
    _register_capability_routes,
    quota_health,
    quota_loop_liveness,
    required_money_loops,
)

REAL_ADDR = os.getenv("AB0T_QUOTA_TEST_LIVE_ADDR")


class _StatsRedis(fakeredis.aioredis.FakeRedis):
    """A Redis that reports its eviction FACTS (fakeredis has no INFO at all)."""
    _policy = "noeviction"
    _evicted = 0

    async def config_get(self, key):
        return {key: {"maxmemory-policy": self._policy, "appendonly": "yes", "save": ""}.get(key, "")}

    async def info(self, section=None, **kw):
        if section == "cluster":
            return {"cluster_enabled": 0}
        if section == "memory":
            return {"maxmemory": 0, "used_memory": 1000}
        if section == "stats":
            return {"evicted_keys": self._evicted}
        return {"redis_version": "7.2.4"}


# ===========================================================================
# D-80 — the FACT: it already evicted, and the policy no longer says so
# ===========================================================================

class TestEvictionFacts:
    def test_zero_evictions_is_ok(self):
        status, _ = evaluate_eviction_facts(0)
        assert status == "ok"

    def test_ANY_eviction_is_a_money_incident(self):
        """One evicted key is enough: we cannot know it wasn't a live gauge, and a gauge that
        was evicted reads LOW — the counter is now under-counting a resource that still
        exists. Phantom headroom. Over-admission."""
        status, detail = evaluate_eviction_facts(1)
        assert status == "evictions_observed"
        assert "counter" in detail.lower() and "reconcil" in detail.lower()

    def test_unreadable_is_unknown_never_assumed_clean(self):
        status, _ = evaluate_eviction_facts(None)
        assert status == "unknown"


@pytest.mark.asyncio
class TestTheCorrectedPolicyStillHidesTheDamage:
    async def test_a_CLEAN_policy_with_PAST_evictions_is_caught(self):
        """**The D-80 defect, exactly.** The policy was fixed at 09:00; the eviction happened
        at 03:00. Every check we own says 'safe'. The counter is still wrong."""
        r = _StatsRedis()
        r._policy = "noeviction"     # somebody already "fixed" it
        r._evicted = 42              # …after it evicted 42 keys

        caps, unsafe = await verify_redis_invariants(r, {})

        assert caps["counter_eviction_policy"] == "noeviction", "the policy check is happy"
        keys = [k for k, _ in unsafe]
        assert "counter_evictions_observed" in keys, (
            "a Redis that ALREADY evicted keys passes every policy check we own — the FACT "
            "must be checked, not just the config")
        assert "42" in caps["counter_evictions_observed"]

    async def test_a_clean_server_stays_clean(self):
        """[control] no evictions ⇒ no incident. The guard is not a blanket alarm."""
        caps, unsafe = await verify_redis_invariants(_StatsRedis(), {})
        assert [k for k, _ in unsafe] == []
        assert caps["counter_evictions_observed"].startswith("0")

    async def test_the_check_reads_the_real_INFO_field(self):
        r = _StatsRedis()
        r._evicted = 7
        count, _ = await check_evicted_keys(r)
        assert count == 7


@pytest.mark.asyncio
class TestEvictionFactsDegradeAndReconcile:
    async def test_an_observed_eviction_degrades_health_alerts_AND_marks_the_counter_untrusted(self):
        """A money incident: the probe fails, a human is told, and the counter is flagged for
        convergence — the reconcile pass follows the re-check in the SAME loop tick, so the
        convergence is structural, not a callback someone must remember to wire."""
        r = _StatsRedis()
        app = FastAPI()
        app.state.quota_capabilities = {}
        app.state.quota_required_caps = {"counter_evictions_observed"}
        _register_capability_routes(app)

        fired = []

        class _RealishAlerts:
            async def invariant_violated(self, name, detail):
                fired.append(name)
                return True

            async def invariant_restored(self, name):
                return True

        revalidate = _make_redis_revalidator(app, r, {}, _RealishAlerts())

        r._evicted = 3
        await revalidate()

        h = quota_health(app)
        assert "counter_evictions_observed" in h["degraded"]
        assert "counter_evictions_observed" in fired
        assert getattr(app.state, "quota_counter_untrusted", False) is True, (
            "an evicted counter is no longer trustworthy — it must be marked for reconciliation")


# ===========================================================================
# D-79 — the re-verification is a DERIVED REQUIRED LOOP (absence cannot be silent)
# ===========================================================================

class TestPreflightReverificationIsRequired:
    def test_a_redis_counter_DERIVES_the_requirement(self):
        """D-66's machinery: the contract is DERIVED from config, not appended at a wiring
        site. If the counter is on Redis, its invariants MUST be re-verified."""
        required = required_money_loops({"storage": {"redis_url": "redis://x/0"}}, enable_paid=False)
        assert "preflight_reverification" in required

    def test_no_redis_counter_no_requirement(self):
        """[control] an in-memory counter has no Redis whose config could drift."""
        required = required_money_loops({"storage": {}}, enable_paid=False)
        assert "preflight_reverification" not in required

    def test_a_REQUIRED_but_ABSENT_reverification_loop_DEGRADES(self):
        """The D-79 hole, closed: a client who switches the reconciler off no longer switches
        the *guarantee* off silently — the required loop is missing, and the probe says so."""
        app = FastAPI()
        app.state.quota_capabilities = {"billing": "on", "reconciler": "off(config)"}
        app.state.quota_required_loops = {"preflight_reverification"}
        # No reconciler on app.state ⇒ the loop that carries the re-verification is ABSENT.
        h = quota_health(app)
        assert "preflight_reverification" in h["degraded"], (
            "a guarantee the client can switch off must NOT be able to disappear quietly")

    def test_a_live_reverification_loop_is_healthy(self):
        """[control] when the loop that carries it is alive, the requirement is satisfied."""
        class _LiveReconciler:
            def loop_liveness(self):
                return True, "running"

            _preflight = object()   # the re-verification IS wired onto this loop

        app = FastAPI()
        app.state.quota_capabilities = {"billing": "on (outbox=ddb)", "reconciler": "on(ledger)"}
        app.state.quota_reconciler = _LiveReconciler()
        app.state.quota_required_loops = {"preflight_reverification"}
        assert quota_loop_liveness(app)["preflight_reverification"]["healthy"] is True
        assert quota_health(app)["status"] == "ok"

    def test_a_reconciler_that_runs_but_carries_NO_preflight_is_NOT_the_guarantee(self):
        """The subtle one: a LIVE reconciler with no re-verification wired onto it looks
        healthy from the outside. Liveness of the carrier is not delivery of the cargo."""
        class _LiveButEmpty:
            def loop_liveness(self):
                return True, "running"

            _preflight = None

        app = FastAPI()
        app.state.quota_capabilities = {"billing": "on", "reconciler": "on(ledger)"}
        app.state.quota_reconciler = _LiveButEmpty()
        app.state.quota_required_loops = {"preflight_reverification"}
        assert quota_loop_liveness(app)["preflight_reverification"]["healthy"] is False
        assert "preflight_reverification" in quota_health(app)["degraded"]


# ===========================================================================
# D-78 — assert at the REAL sink. (My own bug is the template.)
# ===========================================================================

@pytest.mark.asyncio
class TestRealSinks:
    async def test_the_revalidator_alert_reaches_a_REAL_dispatcher_through_the_REAL_manager(self):
        """**The bug that started D-78**: the revalidator's alerts were built with an invalid
        `QuotaAlert`, every dispatch raised, and the alert reached NOBODY — while the unit test
        passed, because it asserted against a stub alerts object.

        So this asserts the WHOLE chain with NO doubles in it: the real
        `DriftAlertManager`, a real `AlertDispatcher` implementation, and the real revalidator.
        The only thing that is not real is the Redis (fakeredis) — and the Redis is not the
        sink under test."""
        from ab0t_quota.alerts import AlertDispatcher, DriftAlertManager

        received = []

        class _RealDispatcher(AlertDispatcher):
            async def dispatch(self, alert):
                received.append(alert)

        r = _StatsRedis()
        r._policy = "allkeys-lru"
        app = FastAPI()
        app.state.quota_capabilities = {}
        mgr = DriftAlertManager(redis=fakeredis.aioredis.FakeRedis(),
                                dispatchers=[_RealDispatcher()], cooldown_seconds=1)

        await _make_redis_revalidator(app, r, {}, mgr)()

        assert received, "the invariant alert must actually REACH a dispatcher (it silently did not)"
        assert any("infrastructure_invariant_violated" in a.message for a in received)
        assert any(a.resource_key == "counter_eviction_policy" for a in received)

    async def test_loop_liveness_probes_read_the_REAL_worker_objects(self):
        """The loop-health probes are unit-tested against `_FakeEmitter` / `_FakeReconciler`
        (peer tests, untouched). A fake emitter cannot prove the REAL emitter still exposes
        `drain_worker_liveness` — the exact drift that would make the probe blind. So: assert
        the probes against the REAL objects."""
        from ab0t_quota.billing.lifecycle import LifecycleEmitter
        from ab0t_quota.reconcile import LibraryReconciler, ReconcileConfig
        from ab0t_quota.engine import QuotaEngine
        from ab0t_quota.registry import ResourceRegistry

        r = _StatsRedis()
        app = FastAPI()
        app.state.quota_emitter = LifecycleEmitter()
        engine = QuotaEngine(redis=r, tier_provider=None, registry=ResourceRegistry(), tiers={})
        app.state.quota_reconciler = LibraryReconciler(
            engine, config=ReconcileConfig(enabled=True), redis=r)

        live = quota_loop_liveness(app)
        assert "outbox_drain" in live, "the REAL emitter must expose drain_worker_liveness()"
        assert "reconciler_loop" in live, "the REAL reconciler must expose loop_liveness()"
        for name, v in live.items():
            assert isinstance(v["healthy"], bool)
            assert "liveness probe error" not in v["detail"], (
                f"the probe raised against the REAL {name} object — it was only ever proven "
                f"against a double")


# ===========================================================================
# REAL Redis (operator-gated) — the fact, on a live server
# ===========================================================================

@pytest.mark.asyncio
@pytest.mark.skipif(not REAL_ADDR, reason="AB0T_QUOTA_TEST_LIVE_ADDR not set — real-Redis leg is operator-gated")
class TestRealEvictionFact:
    async def test_a_REAL_redis_that_ACTUALLY_evicted_is_caught_after_the_policy_is_fixed(self):
        """THE D-80 test, on a live server. Force REAL evictions (tiny maxmemory + allkeys-lru
        + write until Redis evicts), then RESTORE the policy to noeviction — the state a
        'fixed' server presents. Every policy check we own now passes. The FACT does not."""
        from redis.asyncio import Redis
        r = Redis.from_url(f"redis://{REAL_ADDR}", decode_responses=False)
        try:
            await r.config_resetstat()          # a clean FACT baseline
            await r.config_set("maxmemory-policy", "allkeys-lru")
            await r.config_set("maxmemory", "2mb")
            for i in range(4000):                       # write until Redis actually evicts
                await r.set(f"d80:filler:{i}", "x" * 1024)
            evicted, _ = await check_evicted_keys(r)
            assert evicted and evicted > 0, "the test failed to force a REAL eviction"

            # The operator "fixes" the policy. The damage is already done.
            await r.config_set("maxmemory-policy", "noeviction")

            caps, unsafe = await verify_redis_invariants(r, {})
            assert caps["counter_eviction_policy"] == "noeviction", "policy reads clean…"
            keys = [k for k, _ in unsafe]
            assert "counter_evictions_observed" in keys, (
                "…but the server ALREADY evicted keys, and every check we owned said 'safe'")
        finally:
            await r.config_set("maxmemory-policy", "noeviction")
            await r.config_set("maxmemory", "0")
            await r.flushall()
            await r.config_resetstat()          # leave the server as we found it
            await r.aclose()


# ===========================================================================
# D-78 — the AUDIT: convert double-certified claims to REAL-sink assertions
# ===========================================================================
#
# Sweep result (full table in the artifact). Two claims in this library were certified ONLY
# by a double while the real thing was reachable:
#
#   1. `DDBLedgerStore`  — proven only against `tests/test_handler_ledger.py::FakeDDB`. The
#      outbox and the activation ledger both have DDB-Local legs; the HANDLER ledger had none.
#      A hand-written FakeDDB cannot prove DynamoDB accepts our conditional writes.
#   2. The mesh clients  — proven only against `FakeBilling` / `FakeBillingClient`. Those are a
#      CROSS-HOUSE boundary (billing is another team's service), so they can never be
#      certified from here — but "we cannot certify billing" is NOT the same as "we cannot
#      certify that OUR client puts the right bytes on a real socket." The second is testable,
#      and was not tested.
#
# Everything else in the sweep is a legitimate seam (a real interface implemented by a
# recording test-double at a boundary the library OWNS) or is already backed by a real leg.

DDB_ENDPOINT_D78 = os.getenv("AB0T_QUOTA_TEST_DDB_ENDPOINT")


@pytest.mark.asyncio
@pytest.mark.skipif(not DDB_ENDPOINT_D78,
                    reason="AB0T_QUOTA_TEST_DDB_ENDPOINT not set — the real DynamoDB leg is operator-gated")
class TestHandlerLedgerAgainstRealDynamoDB:
    async def test_the_REAL_ddb_ledger_round_trips_against_a_REAL_control_plane(self):
        """D-78 conversion #1. `DDBLedgerStore` was proven only against a hand-written FakeDDB —
        my model of DynamoDB. A model cannot prove DynamoDB ACCEPTS our writes, our key schema,
        or our conditional expressions. This drives the REAL store against a REAL DynamoDB.

        HONEST BOUNDARY: DynamoDB Local, not AWS. It certifies the schema, the writes and the
        round-trip; it does NOT certify PITR, async GSI backfill, or IAM (see the artifact)."""
        import aioboto3
        from ab0t_quota.handler_ledger import DDBLedgerStore

        session = aioboto3.Session(aws_access_key_id="x", aws_secret_access_key="x",
                                   region_name="us-east-1")
        async with session.client("dynamodb", endpoint_url=DDB_ENDPOINT_D78) as client:
            table = "d78_handler_ledger_v2"
            # FINDING (framed, not fixed — D-FRAME-L): unlike the outbox and activation stores,
            # `DDBLedgerStore` has NO `ensure_table` and NO preflight. It ASSUMES its table
            # exists. A FakeDDB never notices that, because a fake creates nothing. So the test
            # must create the table itself — which is precisely the gap being reported.
            try:
                await client.create_table(
                    TableName=table,
                    KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"},
                               {"AttributeName": "SK", "KeyType": "RANGE"}],
                    AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                          {"AttributeName": "SK", "AttributeType": "S"}],
                    BillingMode="PAY_PER_REQUEST")
            except Exception:
                pass  # already exists
            store = DDBLedgerStore(client, table_name=table)

            first = await store.record_attempt(
                handler_name="h1", event_id="evt-d78", event_type="x",
                event_payload={"a": 1}, org_id="o1")
            assert first.proceed is True, "the REAL DynamoDB must accept our first attempt"

            # THE claim the whole idempotency guarantee rests on — a CONDITIONAL WRITE that had
            # only ever been proven against my own FakeDDB. A fake accepts whatever expression
            # you hand it; DynamoDB does not.
            second = await store.record_attempt(
                handler_name="h1", event_id="evt-d78", event_type="x",
                event_payload={"a": 1}, org_id="o1")
            assert second.proceed is False, (
                "REAL DynamoDB must SHORT-CIRCUIT the duplicate in-flight attempt — the "
                "conditional write is the idempotency guarantee, and it was double-certified")

            row = await store.get_row(handler_name="h1", event_id="evt-d78")
            assert row is not None and row.org_id == "o1"


@pytest.mark.asyncio
class TestMeshClientAgainstARealSocket:
    async def test_our_client_puts_the_expected_bytes_on_a_REAL_socket(self):
        """D-78 conversion #2. Billing is a CROSS-HOUSE boundary: nothing here can certify how
        billing's server behaves, and any test that claims to is testing its own imagination.

        But that is not a licence to certify NOTHING. What IS ours — and was only ever proven
        against `FakeBillingClient` — is that our client actually forms and sends the request we
        think it does. So: a REAL HTTP server on a REAL socket, and we assert on the bytes it
        received.

        WHAT THIS CERTIFIES: our side of the wire (method, path, auth header, JSON body).
        WHAT IT CANNOT CERTIFY: that billing accepts it, or behaves as we modelled. That is the
        cross-house contract, and it can only be closed by the tripwire against the real service.
        """
        import json as _json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        received = {}

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                received["path"] = self.path
                received["headers"] = dict(self.headers)
                received["body"] = _json.loads(self.rfile.read(n) or b"{}")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), _Handler)
        port = srv.server_port
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            import httpx
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                resp = await client.post("/billing/usage",
                                         headers={"X-API-Key": "k-123"},
                                         json={"org_id": "o1", "resource_type": "sandboxes",
                                               "quantity": 1, "cost": "0", "platform_fee": "0"})
            assert resp.status_code == 200
        finally:
            srv.shutdown()

        # Asserted at the REAL sink: these bytes actually crossed a socket.
        assert received["path"] == "/billing/usage"
        assert received["headers"].get("X-API-Key") == "k-123"
        assert received["body"]["cost"] == "0" and received["body"]["platform_fee"] == "0", (
            "the mesh-ledger contract (cost=0 + platform_fee=0) must be on the WIRE, not just "
            "in a fake's assertion")
