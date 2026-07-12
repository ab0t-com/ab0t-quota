"""D-71 — the Redis TOPOLOGY guard. A library may not ASSUME the client's infra.

The atomic counter's Lua scripts are multi-key (`_INCR`, `_DECR`, `_ACQUIRE` — see
W-RR's `information_real_redis_conformance_20260711.md` §4, where every one of them
was observed returning **CROSSSLOT** against a real clustered Redis). Our own prod
is single-node, so *we* never hit it. But this is a **library**: a mesh client on a
clustered Redis writes one `quota-config.json` (the drop-in promise), boots, and the
counter primitive fails outright at the first `acquire` — with no startup signal
explaining why. That is a pillar-2 violation.

We already machine-check Redis **durability** at boot (D-32: `CONFIG GET appendonly`
/ `maxmemory-policy`, refuse if unsafe, allow an on-the-record operator assertion
when `CONFIG` is disabled). We never machine-checked **topology**. Same shape, same
fix — that is what these tests pin:

  1. `cluster_enabled:1`            → REFUSE TO START, loudly, naming cause + remedy.
  2. `CLUSTER INFO` unavailable     → UNKNOWN → refuse, unless the operator asserts
                                       `storage.redis_cluster_confirmed_disabled`.
  3. `cluster_enabled:0`            → single-node → start (the control: not a
                                       blanket reject).
  4. Capabilities carry `redis_topology`, and a bad/unknown topology FAILS
     `/quota/health` (D-40: an event with no sink is not observability; D-49/D-51:
     the absence of a positive signal is not health).

Real-cluster leg: the tests marked `real_cluster` run against an actual
cluster-enabled Redis when `AB0T_QUOTA_TEST_CLUSTER_ADDR` is set (recipe in the
artifact — throwaway container, isolated port, torn down). They are skipped
otherwise, so CI never depends on Docker.

Ticket: 20260709_ab0t_quota_systemic_integrity_redesign (D-71, D-23, D-32, D-43)
"""
import contextlib
import json
import os

import pytest
import pytest_asyncio
import fakeredis.aioredis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch

from ab0t_quota.topology import (
    CLUSTER,
    SINGLE_NODE,
    UNKNOWN,
    ClusterTopologyError,
    check_redis_cluster_topology,
    evaluate_topology,
    parse_cluster_enabled,
    topology_error,
)
from ab0t_quota.setup import quota_health, _register_capability_routes


# ---------------------------------------------------------------------------
# fakes — the three Redis answers a real deployment can give
# ---------------------------------------------------------------------------

# NOTE — these fakes model what REAL redis:7 servers do (verified against throwaway
# containers, see the artifact). The obvious model is WRONG, and only a real server
# says so:
#   * a NON-clustered redis:7 ERRORS on `CLUSTER INFO`
#     ("ERR This instance has cluster support disabled");
#   * a CLUSTER-enabled node ANSWERS `CLUSTER INFO`, but its payload has NO
#     `cluster_enabled` field at all.
# So `INFO cluster` — which answers cluster_enabled:0|1 on BOTH — is the primary probe.
# A guard built on `CLUSTER INFO` alone would refuse every correct single-node client.

CLUSTER_INFO_CLUSTERED = (
    "cluster_state:ok\r\ncluster_slots_assigned:16384\r\ncluster_known_nodes:1\r\n"
)
NOT_CLUSTER_ERR = "ERR This instance has cluster support disabled"


class _FakeTopologyRedis(fakeredis.aioredis.FakeRedis):
    """fakeredis + controllable topology probes."""

    _info_cluster = None          # dict | None (None ⇒ INFO cluster raises/omits)
    _cluster_info = None          # str | Exception | None

    async def info(self, section=None, **kwargs):
        if section == "cluster":
            if self._info_cluster is None:
                raise Exception("ERR INFO section unavailable")
            return dict(self._info_cluster)
        return await super().info(section, **kwargs)

    async def execute_command(self, *args, **kwargs):
        if args and str(args[0]).upper() == "CLUSTER":
            ci = self._cluster_info
            if isinstance(ci, Exception):
                raise ci
            if ci is None:
                raise Exception("ERR unknown command 'cluster'")
            return ci.encode()
        return await super().execute_command(*args, **kwargs)


def _clustered_redis():
    """A real cluster node: INFO cluster ⇒ cluster_enabled:1."""
    r = _FakeTopologyRedis()
    r._info_cluster = {"cluster_enabled": 1}
    r._cluster_info = CLUSTER_INFO_CLUSTERED
    return r


def _single_node_redis():
    """A real single-node redis:7: INFO cluster ⇒ 0; CLUSTER INFO ERRORS."""
    r = _FakeTopologyRedis()
    r._info_cluster = {"cluster_enabled": 0}
    r._cluster_info = Exception(NOT_CLUSTER_ERR)
    return r


def _trimmed_info_clustered_redis():
    """A managed Redis with a trimmed INFO, but CLUSTER INFO answers ⇒ cluster mode."""
    r = _FakeTopologyRedis()
    r._info_cluster = None
    r._cluster_info = CLUSTER_INFO_CLUSTERED
    return r


def _trimmed_info_single_node_redis():
    """Trimmed INFO, and CLUSTER INFO says cluster support is disabled ⇒ single-node."""
    r = _FakeTopologyRedis()
    r._info_cluster = None
    r._cluster_info = Exception(NOT_CLUSTER_ERR)
    return r


def _no_probe_redis():
    """Neither probe answers (an emulator / a proxy) ⇒ UNKNOWN."""
    r = _FakeTopologyRedis()
    r._info_cluster = None
    r._cluster_info = None
    return r


# ---------------------------------------------------------------------------
# 1. the pure decision (mirrors billing/outbox.py's durability split)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestEvaluateTopology:
    async def test_cluster_enabled_true_is_CLUSTER(self):
        topo, detail = evaluate_topology(True, confirmed_disabled=False, probe="INFO cluster")
        assert topo == CLUSTER
        assert "cluster_enabled:1" in detail

    async def test_cluster_enabled_false_is_single_node(self):
        """[control] the guard is not a blanket reject — a correct Redis passes."""
        topo, _ = evaluate_topology(False, confirmed_disabled=False, probe="INFO cluster")
        assert topo == SINGLE_NODE

    async def test_unverifiable_without_assertion_is_UNKNOWN(self):
        topo, detail = evaluate_topology(None, confirmed_disabled=False)
        assert topo == UNKNOWN
        assert "redis_cluster_confirmed_disabled" in detail

    async def test_unverifiable_with_operator_assertion_is_single_node_on_the_record(self):
        """D-32's shape: when the server cannot tell us, an EXPLICIT operator
        assertion — never a silent assumption — is what unblocks startup."""
        topo, detail = evaluate_topology(None, confirmed_disabled=True)
        assert topo == SINGLE_NODE
        assert "assert" in detail.lower()

    async def test_an_operator_assertion_does_NOT_override_a_POSITIVE_cluster_signal(self):
        """D-71 x D-32: the assertion rescues an ABSENT signal. It must never override
        a DEFINITIVE negative — exactly as `redis_durability_confirmed` cannot override
        an `allkeys-*` eviction policy. A cluster that says it is a cluster IS one;
        CROSSSLOT does not care what the operator asserted."""
        topo, _ = evaluate_topology(True, confirmed_disabled=True)
        assert topo == CLUSTER

    async def test_parse_cluster_enabled_handles_text_and_dict(self):
        assert parse_cluster_enabled("# Cluster\r\ncluster_enabled:1\r\n") is True
        assert parse_cluster_enabled({"cluster_enabled": 0}) is False
        assert parse_cluster_enabled("nonsense") is None      # absence is not a value
        assert parse_cluster_enabled(None) is None


# ---------------------------------------------------------------------------
# 2. the live probe against a Redis — INFO cluster first, CLUSTER INFO fallback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestProbeAgainstRedis:
    async def test_clustered_redis_detected(self):
        topo, _ = await check_redis_cluster_topology(_clustered_redis())
        assert topo == CLUSTER

    async def test_single_node_redis_detected_even_though_CLUSTER_INFO_errors(self):
        """The real-server trap: a correct single-node redis:7 ERRORS on CLUSTER INFO.
        A CLUSTER-INFO-only guard would refuse it — a blanket reject of the happy path."""
        topo, detail = await check_redis_cluster_topology(_single_node_redis())
        assert topo == SINGLE_NODE, detail

    async def test_trimmed_INFO_falls_back_to_CLUSTER_INFO_both_ways(self):
        """A managed Redis may trim INFO. Answering CLUSTER INFO at all ⇒ cluster mode;
        its 'cluster support disabled' error ⇒ single-node."""
        topo_c, _ = await check_redis_cluster_topology(_trimmed_info_clustered_redis())
        assert topo_c == CLUSTER
        topo_s, _ = await check_redis_cluster_topology(_trimmed_info_single_node_redis())
        assert topo_s == SINGLE_NODE

    async def test_no_usable_probe_is_unknown_then_assertable(self):
        topo, _ = await check_redis_cluster_topology(_no_probe_redis())
        assert topo == UNKNOWN
        topo2, _ = await check_redis_cluster_topology(_no_probe_redis(), confirmed_disabled=True)
        assert topo2 == SINGLE_NODE


# ---------------------------------------------------------------------------
# 3. the client-facing error names the cause AND the remedy
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestErrorMessage:
    async def test_cluster_error_names_cause_and_remedy(self):
        err = topology_error(CLUSTER, "CLUSTER INFO reports cluster_enabled:1")
        assert isinstance(err, ClusterTopologyError)
        msg = str(err)
        for token in ("CROSSSLOT", "multi-key", "single-node", "cluster_enabled:1", "roadmap"):
            assert token in msg, f"the refusal must name {token!r}: {msg}"

    async def test_unknown_error_names_the_operator_assertion(self):
        msg = str(topology_error(UNKNOWN, "CLUSTER INFO unavailable"))
        assert "redis_cluster_confirmed_disabled" in msg
        assert "CROSSSLOT" in msg


# ---------------------------------------------------------------------------
# 4. setup_quota REFUSES TO START — the whole point (D-71.2)
# ---------------------------------------------------------------------------

BASE_CONFIG = {
    "storage": {"redis_url": "redis://test/0", "persistence_enabled": False},
    "tier_provider": {"type": "static", "default_tier": "starter"},
    "alerts": {"enabled": False},
    "enforcement": {"enabled": True},
    "resources": [{
        "service": "test-svc", "resource_key": "thing.concurrent",
        "display_name": "Things", "counter_type": "gauge", "unit": "things",
    }],
    "tiers": [{
        "tier_id": "starter", "display_name": "Starter", "sort_order": 1,
        "limits": {"thing.concurrent": 5}, "features": [],
    }],
}


@pytest_asyncio.fixture
async def config_file(tmp_path):
    p = tmp_path / "quota-config.json"
    p.write_text(json.dumps(BASE_CONFIG))
    old = os.environ.get("QUOTA_CONFIG_PATH")
    os.environ["QUOTA_CONFIG_PATH"] = str(p)
    yield p
    if old is None:
        os.environ.pop("QUOTA_CONFIG_PATH", None)
    else:
        os.environ["QUOTA_CONFIG_PATH"] = old


def _write_config(path, **storage_overrides):
    cfg = json.loads(json.dumps(BASE_CONFIG))
    cfg["storage"].update(storage_overrides)
    path.write_text(json.dumps(cfg))


@contextlib.contextmanager
def _boot(redis_obj):
    """Boot a real FastAPI app through setup_quota's lifespan against `redis_obj`.
    The lifespan is where the topology check runs — so a refusal surfaces here."""
    from ab0t_quota import setup_quota

    app = FastAPI()
    with patch("redis.asyncio.Redis.from_url", side_effect=lambda *a, **k: redis_obj):
        setup_quota(app, enable_paid=False)
        with TestClient(app) as client:
            yield app, client


class TestSetupRefusesToStart:
    def test_clustered_redis_REFUSES_TO_START(self, config_file):
        """The defect this ticket closes: today the library boots happily onto a
        cluster and the counter primitive fails at the first acquire. It must
        refuse at STARTUP, with the reason."""
        # The env assertion (set for the emulator suite in conftest) must NOT
        # rescue a POSITIVE cluster signal.
        with pytest.raises(ClusterTopologyError) as exc:
            with _boot(_clustered_redis()):
                pass
        msg = str(exc.value)
        assert "CROSSSLOT" in msg
        assert "single-node" in msg

    def test_unverifiable_topology_REFUSES_without_the_operator_assertion(
            self, config_file, monkeypatch):
        """CLUSTER INFO unavailable + no assertion => unknown => refuse (D-71.3).
        Unknown is not safe; unknown is unknown."""
        monkeypatch.delenv("AB0T_QUOTA_REDIS_CLUSTER_CONFIRMED_DISABLED", raising=False)
        _write_config(config_file)  # no redis_cluster_confirmed_disabled
        with pytest.raises(ClusterTopologyError) as exc:
            with _boot(_no_probe_redis()):
                pass
        assert "redis_cluster_confirmed_disabled" in str(exc.value)

    def test_operator_assertion_in_config_allows_start_and_is_ON_THE_RECORD(
            self, config_file, monkeypatch):
        """The ElastiCache-shaped escape (D-32's shape): an explicit assertion in
        quota-config.json starts the service AND is recorded in Capabilities."""
        monkeypatch.delenv("AB0T_QUOTA_REDIS_CLUSTER_CONFIRMED_DISABLED", raising=False)
        _write_config(config_file, redis_cluster_confirmed_disabled=True)
        with _boot(_no_probe_redis()) as (app, client):
            caps = client.get("/quota/capabilities").json()
        assert caps["redis_topology"].startswith("single-node")
        assert "assert" in caps["redis_topology"].lower(), (
            "an operator assertion must be visible in the snapshot, not silent")

    def test_single_node_redis_STARTS_and_reports_the_topology(self, config_file):
        """[control] the guard does not block a correct deployment."""
        with _boot(_single_node_redis()) as (app, client):
            caps = client.get("/quota/capabilities").json()
        assert caps["redis_topology"] == SINGLE_NODE


# ---------------------------------------------------------------------------
# 5. the guard has a SINK — /quota/health (D-40 / D-49 / D-51)
# ---------------------------------------------------------------------------

def _app_with(caps: dict, *, required=None) -> FastAPI:
    app = FastAPI()
    app.state.quota_capabilities = dict(caps)
    if required is not None:
        app.state.quota_required_caps = set(required)
    _register_capability_routes(app)
    return app


HEALTHY = {
    "billing": "on (outbox=ddb)",
    "reconciler": "on(provider)",
    "redis_topology": SINGLE_NODE,
}


class TestTopologyFailsTheHealthProbe:
    def test_single_node_is_healthy(self):
        assert quota_health(_app_with(HEALTHY))["status"] == "ok"

    def test_CLUSTER_topology_degrades_and_503s(self):
        client = TestClient(_app_with(dict(HEALTHY, redis_topology=CLUSTER)))
        r = client.get("/quota/health")
        assert r.status_code == 503, "a cluster topology must FAIL the probe, not log a line"
        assert r.json()["degraded"] == ["redis_topology"]

    def test_unknown_topology_degrades(self):
        h = quota_health(_app_with(dict(HEALTHY, redis_topology=UNKNOWN)))
        assert h["status"] == "degraded"
        assert h["degraded"] == ["redis_topology"]

    def test_ABSENT_topology_degrades_when_setup_declared_it_required(self):
        """D-49/D-51 — absence of a positive signal is not health. A snapshot that
        never got a topology verdict (setup crashed mid-way) must degrade."""
        caps = {"billing": "on (outbox=ddb)", "reconciler": "on(provider)"}
        h = quota_health(_app_with(caps, required=["billing", "reconciler", "redis_topology"]))
        assert h["status"] == "degraded"
        assert h["degraded"] == ["redis_topology"]

    def test_health_reports_only_KEYS_never_values(self):
        """House rule: no dynamic strings to a client. The probe body names the
        capability KEY; the diagnosis lives on /quota/capabilities."""
        caps = dict(HEALTHY, redis_topology="CLUSTER (unsupported) - node redis-prod-7:6379")
        body = TestClient(_app_with(caps)).get("/quota/health").json()
        assert body["degraded"] == ["redis_topology"]
        assert "redis-prod-7" not in str(body)


# ---------------------------------------------------------------------------
# 6. cross-runtime contract (D-43) — the structural conformance item
# ---------------------------------------------------------------------------

CONF = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "conformance", "scenarios.json")


class TestStructuralConformance:
    def test_python_satisfies_the_declared_structural_item(self):
        """D-43: ONE data file, two runtimes. The topology guard is a SETUP-level
        contract (not an engine scenario), so it is registered as a structural
        conformance item — and each runtime asserts its own error against the
        SAME declared substrings. Go's twin test reads this identical file."""
        with open(CONF) as f:
            doc = json.load(f)
        items = {i["id"]: i for i in doc.get("structural_conformance", [])}
        item = items.get("ST-TOPOLOGY-1")
        assert item is not None, "the topology guard must be declared in scenarios.json"
        assert set(item["runtimes"]) == {"python", "go"}

        cluster_msg = str(topology_error(CLUSTER, "CLUSTER INFO reports cluster_enabled:1"))
        for token in item["cluster_error_must_contain"]:
            assert token in cluster_msg, f"declared token {token!r} missing from the Python refusal"

        unknown_msg = str(topology_error(UNKNOWN, "CLUSTER INFO unavailable"))
        for token in item["unknown_error_must_contain"]:
            assert token in unknown_msg, f"declared token {token!r} missing from the Python refusal"

        assert item["capability_key"] == "redis_topology"
        assert item["config_key"] == "storage.redis_cluster_confirmed_disabled"


# ---------------------------------------------------------------------------
# 7. REAL cluster (operator-gated; the recipe is in the artifact)
# ---------------------------------------------------------------------------

CLUSTER_ADDR = os.getenv("AB0T_QUOTA_TEST_CLUSTER_ADDR")
REAL_ADDR = os.getenv("REAL_REDIS_ADDR")


@pytest.mark.skipif(not CLUSTER_ADDR,
                    reason="AB0T_QUOTA_TEST_CLUSTER_ADDR not set — real-cluster leg is operator-gated")
@pytest.mark.asyncio
class TestRealClusterRefusal:
    async def test_a_REAL_clustered_redis_is_detected_and_refused(self):
        from redis.asyncio import Redis
        r = Redis.from_url(f"redis://{CLUSTER_ADDR}", decode_responses=False)
        try:
            topo, detail = await check_redis_cluster_topology(r)
            assert topo == CLUSTER, f"a real cluster-enabled Redis reported {topo!r} ({detail})"
            assert "CROSSSLOT" in str(topology_error(topo, detail))
        finally:
            await r.aclose()


@pytest.mark.skipif(not REAL_ADDR,
                    reason="REAL_REDIS_ADDR not set — real single-node leg is operator-gated")
@pytest.mark.asyncio
class TestRealSingleNodeAccepted:
    async def test_a_REAL_single_node_redis_is_accepted(self):
        """[control] against a real, non-clustered redis:7 the guard must PASS —
        otherwise it is a blanket reject that would break every honest client."""
        from redis.asyncio import Redis
        r = Redis.from_url(f"redis://{REAL_ADDR}", decode_responses=False)
        try:
            topo, detail = await check_redis_cluster_topology(r)
            assert topo == SINGLE_NODE, f"real single-node Redis reported {topo!r} ({detail})"
        finally:
            await r.aclose()
