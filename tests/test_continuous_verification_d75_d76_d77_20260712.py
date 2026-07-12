"""D-75 / D-76 / D-77 — the guards' own caveat: they check the world ONCE.

D-75 is the finding and it outranks what it fixes:

    "An assumption machine-checked once is an assumption trusted thereafter."

D-32 (durability), D-71 (topology), D-72 (counter eviction), D-73 (scripting) all verify
the world at BOOT and then trust it forever. A `CONFIG SET maxmemory-policy allkeys-lru`
at 3am — or a managed-Redis failover to a replica with a different config — is INVISIBLE
to every one of them. The counter becomes silently evictable → a live gauge is evicted →
under-count → phantom headroom → OVER-ADMISSION, the forbidden direction (D-31), behind a
green health check. Same defect shape as every other one in this ticket: a mechanism that
stops short of the boundary its guarantee must cross. Here the boundary is TIME.

  * **D-75** — re-verify on an interval, riding the RECONCILER loop (never a new worker:
    every loop we add is another thing that can be dead, D-50). A safe→unsafe transition
    at runtime is **LOUD, NOT FATAL**: a running service that suddenly refuses is its own
    outage. It degrades /quota/health immediately, fires a money-incident alert, and
    updates Capabilities. The operator decides whether to drain.
  * **D-76** — DynamoDB had NO preflight at all. Redis is checked five ways; the DDB that
    holds the activation ledger AND the outbox was checked zero. It is MORE load-bearing
    than Redis.
  * **D-77** — we check the eviction POLICY, never `maxmemory`. `noeviction` + a tight
    maxmemory fails CLOSED (safe direction) but takes the service down at 3am with no
    warning. "Dies with no warning" is not zero-caveats either.

The D-75 real leg is the one that matters: boot against a CLEAN real Redis, then
`CONFIG SET maxmemory-policy allkeys-lru` on the LIVE server mid-run, and prove the next
re-check degrades health and alerts — WHILE RUNNING, not at the next boot.

Ticket: 20260709_ab0t_quota_systemic_integrity_redesign (D-75, D-76, D-77)
"""
import asyncio
import json
import os

import pytest
import fakeredis.aioredis

from ab0t_quota.redis_preflight import verify_redis_invariants, evaluate_memory_headroom
from ab0t_quota.ddb_preflight import verify_ddb_table, DDBPreflightError
from ab0t_quota.setup import quota_health, _register_capability_routes
from fastapi import FastAPI


REAL_ADDR = os.getenv("AB0T_QUOTA_TEST_LIVE_ADDR")          # a real redis we may CONFIG SET on
DDB_ENDPOINT = os.getenv("AB0T_QUOTA_TEST_DDB_ENDPOINT")    # DynamoDB Local


# ===========================================================================
# D-77 — memory headroom (the cliff we never surfaced)
# ===========================================================================

class TestMemoryHeadroom:
    def test_unbounded_maxmemory_has_no_cliff(self):
        status, detail = evaluate_memory_headroom(maxmemory=0, used_memory=10_000_000)
        assert status == "unbounded", detail

    def test_ample_headroom_is_ok(self):
        status, _ = evaluate_memory_headroom(maxmemory=100, used_memory=10)
        assert status == "ok"

    def test_approaching_the_cliff_DEGRADES(self):
        """`noeviction` + a tight maxmemory fails CLOSED — but the service DIES. It must
        warn on the way to the cliff, not at the bottom of it."""
        status, detail = evaluate_memory_headroom(maxmemory=100, used_memory=93)
        assert status == "low_headroom"
        assert "93" in detail or "7" in detail

    def test_unreadable_is_unknown_not_assumed_ok(self):
        status, _ = evaluate_memory_headroom(maxmemory=None, used_memory=None)
        assert status == "unknown"


# ===========================================================================
# D-75 — the invariants are RE-verifiable, and a runtime flip is caught
# ===========================================================================

class _MutableRedis(fakeredis.aioredis.FakeRedis):
    """A Redis whose CONFIG can change UNDER US — which is exactly the thing every guard
    we wrote assumed could not happen."""
    _policy = "noeviction"
    _cluster = "cluster_enabled:0"

    async def config_get(self, key):
        return {key: {"maxmemory-policy": self._policy, "appendonly": "yes", "save": ""}.get(key, "")}

    async def info(self, section=None, **kw):
        if section == "cluster":
            return {"cluster_enabled": 0 if "0" in self._cluster else 1}
        return {"redis_version": "7.2.4", "maxmemory": 0, "used_memory": 1000}


@pytest.mark.asyncio
class TestVerifyRedisInvariants:
    async def test_a_clean_redis_reports_every_invariant_safe(self):
        caps, unsafe = await verify_redis_invariants(_MutableRedis(), {})
        assert unsafe == [], unsafe
        for k in ("redis_topology", "counter_eviction_policy", "redis_scripting", "redis_version"):
            assert k in caps

    async def test_a_RUNTIME_flip_to_allkeys_lru_is_CAUGHT_by_re_verification(self):
        """The D-75 defect in one test: the same connection, verified twice, with the
        policy changed in between. The boot check said 'safe' and would have gone on
        saying it forever."""
        r = _MutableRedis()
        caps, unsafe = await verify_redis_invariants(r, {})
        assert unsafe == []

        r._policy = "allkeys-lru"          # 3am. Nobody is watching.

        caps2, unsafe2 = await verify_redis_invariants(r, {})
        assert [k for k, _ in unsafe2] == ["counter_eviction_policy"]
        assert "evict" in caps2["counter_eviction_policy"].lower()

    async def test_a_runtime_flip_to_CLUSTER_is_also_caught(self):
        """A managed-Redis failover can land you on a clustered endpoint. Same law."""
        r = _MutableRedis()
        r._cluster = "cluster_enabled:1"
        _caps, unsafe = await verify_redis_invariants(r, {})
        assert "redis_topology" in [k for k, _ in unsafe]


# ---------------------------------------------------------------------------
# the re-verification RIDES THE RECONCILER LOOP (D-50: test the LOOP, not the fn)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestReconcilerRevalidates:
    async def test_the_LOOP_re_verifies_and_a_flip_degrades_health_WITHOUT_crashing(self):
        """Drives the REAL reconciler loop. A safe→unsafe transition must be LOUD, NOT
        FATAL: the process keeps serving (a service that suddenly refuses is its own
        outage), /quota/health degrades, an alert fires, Capabilities tells the truth."""
        from ab0t_quota.reconcile import LibraryReconciler, ReconcileConfig
        from ab0t_quota.engine import QuotaEngine
        from ab0t_quota.registry import ResourceRegistry
        from ab0t_quota.setup import _make_redis_revalidator

        r = _MutableRedis()
        app = FastAPI()
        app.state.quota_capabilities = {
            "billing": "ON (outbox=ddb)", "reconciler": "on(ledger)",
            "redis_topology": "single-node", "counter_eviction_policy": "noeviction",
            "redis_scripting": "on (EVAL verified)",
        }
        app.state.quota_required_caps = {"billing", "reconciler", "redis_topology",
                                         "counter_eviction_policy", "redis_scripting"}
        _register_capability_routes(app)
        assert quota_health(app)["status"] == "ok"

        alerts = []

        class _Alerts:
            async def invariant_violated(self, name, detail):
                alerts.append((name, detail))
                return True

            async def invariant_restored(self, name):
                alerts.append((name, "restored"))
                return True

        # A DURABLE ledger, so the reconcile pass itself keeps running (D-39 stops the loop
        # on a non-durable one — see the artifact: that path degrades health via the
        # `reconciler` capability, so it is loudly degraded, not silently stale).
        from ab0t_quota.activations import RedisActivationStore
        engine = QuotaEngine(redis=r, tier_provider=None, registry=ResourceRegistry(), tiers={},
                             activation_store=RedisActivationStore(r))
        revalidate = _make_redis_revalidator(app, r, {}, _Alerts())
        rec = LibraryReconciler(engine, config=ReconcileConfig(enabled=True, interval_seconds=0.05),
                                redis=r, preflight=revalidate)

        # Drive the REAL loop (not run_once, not the callback directly).
        task = asyncio.create_task(rec._loop(0.05))
        try:
            await asyncio.sleep(0.15)
            assert quota_health(app)["status"] == "ok", "clean Redis must stay healthy"

            r._policy = "allkeys-lru"      # the 3am flip, mid-run
            await asyncio.sleep(0.25)

            assert not task.done(), "a runtime flip must be LOUD, NOT FATAL — the loop lives"
            h = quota_health(app)
            assert h["status"] == "degraded", "the flip must degrade /quota/health WHILE RUNNING"
            assert "counter_eviction_policy" in h["degraded"]
            assert any(n == "counter_eviction_policy" for n, _ in alerts), \
                "a money-incident alert must reach a human (D-40)"
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def test_a_restored_policy_re_heals_the_probe(self):
        """[control] the re-check is not a one-way latch: fix the Redis, the probe recovers
        and a `restored` alert pairs with the violation (the D-26 resolve-trail law)."""
        from ab0t_quota.setup import _make_redis_revalidator
        r = _MutableRedis()
        app = FastAPI()
        app.state.quota_capabilities = {}
        events = []

        class _Alerts:
            async def invariant_violated(self, name, detail):
                events.append(("violated", name)); return True

            async def invariant_restored(self, name):
                events.append(("restored", name)); return True

        revalidate = _make_redis_revalidator(app, r, {}, _Alerts())

        r._policy = "allkeys-lru"
        await revalidate()
        assert ("violated", "counter_eviction_policy") in events

        r._policy = "noeviction"
        await revalidate()
        assert ("restored", "counter_eviction_policy") in events
        assert app.state.quota_capabilities["counter_eviction_policy"] == "noeviction"


@pytest.mark.asyncio
class TestTheAlertActuallyDISPATCHES:
    """The REAL DriftAlertManager, with a REAL dispatcher — not a fake.

    Caught by running the whole thing against a live Redis: the invariant alerts were built
    with a `QuotaAlert` that failed validation (`tier_id` is required), so every dispatch
    raised and the alert reached NOBODY. The unit test above passed anyway, because it used a
    stub alerts object. That is D-40's law biting the very code written to satisfy it: an
    event with no sink is not observability — and a fake sink proves nothing about the real
    one. So this test asserts at the DISPATCHER."""

    async def test_violation_and_restore_reach_a_real_dispatcher(self):
        from ab0t_quota.alerts import DriftAlertManager

        dispatched = []

        class _CapturingDispatcher:
            async def dispatch(self, alert):
                dispatched.append(alert)

        r = fakeredis.aioredis.FakeRedis()
        mgr = DriftAlertManager(redis=r, dispatchers=[_CapturingDispatcher()], cooldown_seconds=1)

        assert await mgr.invariant_violated("counter_eviction_policy", "maxmemory-policy=allkeys-lru")
        assert len(dispatched) == 1, "the violation must actually DISPATCH (it silently didn't)"
        assert "infrastructure_invariant_violated" in dispatched[0].message
        assert dispatched[0].resource_key == "counter_eviction_policy"

        assert await mgr.invariant_restored("counter_eviction_policy")
        assert len(dispatched) == 2, "the restore must dispatch too (D-26's resolve trail)"
        assert "infrastructure_invariant_restored" in dispatched[1].message

    async def test_a_restore_with_no_active_violation_does_not_fire(self):
        """[control] no phantom all-clears."""
        from ab0t_quota.alerts import DriftAlertManager
        mgr = DriftAlertManager(redis=fakeredis.aioredis.FakeRedis(), dispatchers=[])
        assert await mgr.invariant_restored("counter_eviction_policy") is False


# ===========================================================================
# D-76 — DynamoDB preflight (the store with NO checks at all)
# ===========================================================================

class _FakeDDB:
    """Models the DDB control-plane answers that matter."""

    def __init__(self, *, status="ACTIVE", gsi=("ACTIVE",), ttl=("ENABLED", "ttl"),
                 pitr="ENABLED", pitr_unsupported=False, missing=False):
        self._status, self._gsi, self._ttl = status, gsi, ttl
        self._pitr, self._pitr_unsupported, self._missing = pitr, pitr_unsupported, missing

    async def describe_table(self, TableName):
        if self._missing:
            raise Exception("ResourceNotFoundException: Requested resource not found")
        return {"Table": {"TableStatus": self._status,
                          "GlobalSecondaryIndexes": [
                              {"IndexName": f"gsi{i}", "IndexStatus": s}
                              for i, s in enumerate(self._gsi)]}}

    async def describe_time_to_live(self, TableName):
        status, attr = self._ttl
        d = {"TimeToLiveStatus": status}
        if attr:
            d["AttributeName"] = attr
        return {"TimeToLiveDescription": d}

    async def describe_continuous_backups(self, TableName):
        if self._pitr_unsupported:
            raise Exception("UnknownOperationException: An unknown operation was requested")
        return {"ContinuousBackupsDescription":
                {"PointInTimeRecoveryDescription": {"PointInTimeRecoveryStatus": self._pitr}}}


@pytest.mark.asyncio
class TestDDBPreflight:
    async def test_a_correct_table_passes(self):
        """[control] ACTIVE + GSIs ACTIVE + TTL on the intended attribute + PITR on."""
        value, fatal, warn = await verify_ddb_table(_FakeDDB(), "tbl", ttl_attribute="ttl")
        assert fatal is None, fatal
        assert warn is None
        assert value.startswith("ACTIVE")

    async def test_missing_table_is_FATAL(self):
        _v, fatal, _w = await verify_ddb_table(_FakeDDB(missing=True), "tbl", ttl_attribute="ttl")
        assert fatal and "not found" in fatal.lower()

    async def test_a_GSI_still_backfilling_is_FATAL(self):
        """D-32's own finding: real DynamoDB backfills a GSI asynchronously, and
        `list_pending` against a CREATING index silently misses rows — money events that
        exist and are never drained."""
        _v, fatal, _w = await verify_ddb_table(_FakeDDB(gsi=("CREATING",)), "tbl", ttl_attribute="ttl")
        assert fatal and "gsi" in fatal.lower()

    async def test_TTL_on_the_WRONG_attribute_is_FATAL(self):
        """The dangerous misconfiguration: TTL enabled on an attribute we do not control
        means DynamoDB may DELETE rows we never marked for expiry — including OPEN
        activations (the ledger that is authoritative for identity and cost)."""
        _v, fatal, _w = await verify_ddb_table(
            _FakeDDB(ttl=("ENABLED", "expires")), "tbl", ttl_attribute="ttl")
        assert fatal and "expires" in fatal

    async def test_TTL_DISABLED_degrades_but_does_not_refuse(self):
        """A disabled TTL never deletes anything — released rows simply pile up. That is a
        growth/cost problem, not a correctness leak, so it WARNS. Refusing here would be the
        D-49 false-503 mistake."""
        _v, fatal, warn = await verify_ddb_table(
            _FakeDDB(ttl=("DISABLED", None)), "tbl", ttl_attribute="ttl")
        assert fatal is None
        assert warn and "ttl" in warn.lower()

    async def test_PITR_DISABLED_on_a_money_store_is_FATAL(self):
        _v, fatal, _w = await verify_ddb_table(
            _FakeDDB(pitr="DISABLED"), "tbl", ttl_attribute="ttl")
        assert fatal and "pitr" in fatal.lower()

    async def test_PITR_unverifiable_requires_the_operator_assertion(self):
        """DynamoDB Local cannot answer DescribeContinuousBackups at all
        (UnknownOperationException) — so PITR is the one thing ONLY real AWS can confirm.
        Unverified ⇒ refuse, unless the operator puts the assertion on the record (D-32's
        shape)."""
        _v, fatal, _w = await verify_ddb_table(
            _FakeDDB(pitr_unsupported=True), "tbl", ttl_attribute="ttl")
        assert fatal and "ddb_pitr_confirmed" in fatal

        value, fatal2, _w2 = await verify_ddb_table(
            _FakeDDB(pitr_unsupported=True), "tbl", ttl_attribute="ttl", pitr_confirmed=True)
        assert fatal2 is None
        assert "assert" in value.lower()

    async def test_the_error_names_cause_and_remedy(self):
        err = DDBPreflightError("outbox", "PITR is DISABLED")
        assert "outbox" in str(err) and "PITR" in str(err)


# ---------------------------------------------------------------------------
# health sink (D-40/D-49/D-51)
# ---------------------------------------------------------------------------

def _app_with(caps, required=None):
    app = FastAPI()
    app.state.quota_capabilities = dict(caps)
    if required is not None:
        app.state.quota_required_caps = set(required)
    _register_capability_routes(app)
    return app


HEALTHY = {
    "billing": "on (outbox=ddb)", "reconciler": "on(provider)",
    "redis_topology": "single-node", "counter_eviction_policy": "noeviction",
    "redis_scripting": "on (EVAL verified)",
}


class TestHealthSink:
    def test_control(self):
        assert quota_health(_app_with(HEALTHY))["status"] == "ok"

    def test_an_unsafe_ddb_capability_degrades(self):
        h = quota_health(_app_with(dict(HEALTHY, ddb_outbox="UNSAFE (PITR is DISABLED)")))
        assert h["status"] == "degraded"
        assert h["degraded"] == ["ddb_outbox"]

    def test_a_healthy_ddb_capability_does_not_degrade(self):
        assert quota_health(_app_with(dict(HEALTHY, ddb_outbox="ACTIVE (ttl=ttl, pitr=ENABLED)")))["status"] == "ok"

    def test_low_memory_headroom_degrades(self):
        """D-77: the cliff must be visible BEFORE the service dies at it."""
        h = quota_health(_app_with(dict(HEALTHY, memory_headroom="low_headroom (93% of maxmemory used)")))
        assert h["degraded"] == ["memory_headroom"]

    def test_unknown_memory_headroom_does_NOT_degrade(self):
        """A deliberate, stated deviation (ratified for D-74 and applied here): an eviction
        policy we cannot read is a live hazard; a memory statistic we cannot read is not."""
        assert quota_health(_app_with(dict(HEALTHY, memory_headroom="unknown")))["status"] == "ok"


# ===========================================================================
# cross-runtime contract (D-43) — the structural conformance item
# ===========================================================================

CONF = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "conformance", "scenarios.json")


class TestStructuralConformance:
    def test_python_satisfies_ST_RUNTIME_1(self):
        """D-43: one data file, two runtimes. This contract is about TIME (a boot verdict
        re-checked while the process runs), which no engine scenario can express — so it is
        declared structurally and each runtime asserts its own behaviour against it."""
        with open(CONF) as f:
            doc = json.load(f)
        item = {i["id"]: i for i in doc.get("structural_conformance", [])}.get("ST-RUNTIME-1")
        assert item is not None, "the runtime re-verification contract must be declared"
        assert set(item["runtimes"]) == {"python", "go"}

        # The law, and the three properties that make it real.
        assert item["runtime_violation_is_fatal"] is False
        assert item["runtime_violation_degrades_health"] is True
        assert item["runtime_violation_alerts"] is True
        assert "reconciler" in item["reverification_rides"]

        from ab0t_quota.redis_preflight import MEMORY_WARN_RATIO
        assert item["memory_warn_ratio"] == MEMORY_WARN_RATIO
        assert item["config_key"] == "storage.ddb_pitr_confirmed"
        from ab0t_quota.ddb_preflight import PITR_CONFIRM_ENV
        assert item["env_key"] == PITR_CONFIRM_ENV

        # The library really does refuse/warn on exactly the declared DDB findings.
        assert "ttl_disabled" in item["ddb_warn_findings"]
        assert "pitr_unverifiable_without_assertion" in item["ddb_fatal_findings"]


# ===========================================================================
# REAL infrastructure (operator-gated)
# ===========================================================================

@pytest.mark.asyncio
@pytest.mark.skipif(not REAL_ADDR, reason="AB0T_QUOTA_TEST_LIVE_ADDR not set — real-Redis leg is operator-gated")
class TestRealRuntimeFlip:
    async def test_a_LIVE_CONFIG_SET_is_caught_by_the_running_loop(self):
        """THE D-75 test. Boot against a clean REAL Redis; flip its policy to allkeys-lru
        on the LIVE server; prove the running re-check catches it — degraded health + an
        alert — WHILE RUNNING, not at the next boot. Restores the policy afterwards."""
        from redis.asyncio import Redis
        from ab0t_quota.setup import _make_redis_revalidator

        r = Redis.from_url(f"redis://{REAL_ADDR}", decode_responses=False)
        app = FastAPI()
        app.state.quota_capabilities = {}
        fired = []

        class _Alerts:
            async def invariant_violated(self, name, detail):
                fired.append(name); return True

            async def invariant_restored(self, name):
                fired.append(f"restored:{name}"); return True

        revalidate = _make_redis_revalidator(app, r, {}, _Alerts())
        try:
            # D-80 made the eviction FACT a health signal, and facts are STICKY (that is the
            # point of them). Reset the counter-stats so this test starts from a clean server —
            # otherwise it inherits another test's real evictions and degrades for the right
            # reason at the wrong time.
            await r.config_resetstat()
            await r.config_set("maxmemory-policy", "noeviction")
            await revalidate()
            assert app.state.quota_capabilities["counter_eviction_policy"] == "noeviction"

            # ---- the 3am flip, on a LIVE server ----
            await r.config_set("maxmemory-policy", "allkeys-lru")
            await revalidate()

            caps = app.state.quota_capabilities
            assert "evict" in caps["counter_eviction_policy"].lower(), caps
            assert "counter_eviction_policy" in fired, "the flip must reach a human"

            app.state.quota_required_caps = {"counter_eviction_policy"}
            assert quota_health(app)["degraded"] == ["counter_eviction_policy"]
        finally:
            await r.config_set("maxmemory-policy", "noeviction")
            await r.aclose()


@pytest.mark.asyncio
@pytest.mark.skipif(not DDB_ENDPOINT, reason="AB0T_QUOTA_TEST_DDB_ENDPOINT not set — DDB-Local leg is operator-gated")
class TestRealDDBLocal:
    async def _client(self):
        import aioboto3
        session = aioboto3.Session(aws_access_key_id="x", aws_secret_access_key="x",
                                   region_name="us-east-1")
        cm = session.client("dynamodb", endpoint_url=DDB_ENDPOINT)
        return cm, await cm.__aenter__()

    async def test_a_REAL_ddb_table_is_verified_TTL_is_real_PITR_is_not_answerable(self):
        """DynamoDB Local answers DescribeTable and DescribeTimeToLive with REAL semantics,
        and CANNOT answer DescribeContinuousBackups (UnknownOperationException) — so this
        leg proves the table/GSI/TTL checks against a real control plane, and proves that
        PITR is precisely the thing only real AWS can confirm (it lands on the operator
        assertion path, ON THE RECORD, exactly as designed)."""
        cm, c = await self._client()
        table = "d76_preflight_probe"
        try:
            try:
                await c.create_table(
                    TableName=table,
                    KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
                    AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
                    BillingMode="PAY_PER_REQUEST")
            except Exception:
                pass  # already exists

            # TTL not yet enabled → WARN (never a refusal), and PITR unanswerable → refuse
            # without the assertion.
            _v, fatal, warn = await verify_ddb_table(c, table, ttl_attribute="ttl")
            assert fatal and "ddb_pitr_confirmed" in fatal, fatal

            _v2, fatal2, warn2 = await verify_ddb_table(
                c, table, ttl_attribute="ttl", pitr_confirmed=True)
            assert fatal2 is None, fatal2
            assert warn2 and "ttl" in warn2.lower(), "TTL is DISABLED on a fresh table → warn"

            # Enable TTL on the intended attribute → clean.
            await c.update_time_to_live(
                TableName=table,
                TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"})
            value, fatal3, warn3 = await verify_ddb_table(
                c, table, ttl_attribute="ttl", pitr_confirmed=True)
            assert fatal3 is None and warn3 is None, (fatal3, warn3)
            assert "ttl=ttl" in value

            # And the dangerous misconfiguration, against the REAL control plane: TTL on an
            # attribute we do not write ⇒ DynamoDB may delete rows we never marked ⇒ FATAL.
            _v4, fatal4, _w4 = await verify_ddb_table(
                c, table, ttl_attribute="expires_at", pitr_confirmed=True)
            assert fatal4 and "ttl" in fatal4.lower()
        finally:
            try:
                await c.delete_table(TableName=table)
            except Exception:
                pass
            await cm.__aexit__(None, None, None)
