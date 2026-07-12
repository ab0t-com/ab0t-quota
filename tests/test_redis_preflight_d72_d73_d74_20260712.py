"""D-72 / D-73 / D-74 — the Redis assumptions the library never machine-checked.

D-71 closed TOPOLOGY. Three assumptions remained, and the ORDER matters:

  * **D-72 (the urgent one) — the COUNTER keyspace is never checked for EVICTION.**
    `check_redis_outbox_durability` (D-32) guards the OUTBOX. The COUNTER — the thing
    the library exists to protect — runs on the same Redis with NO check. Under
    `allkeys-lru`, Redis evicts a live gauge key under memory pressure; the counter
    then reads ZERO for a resource that is still running ⇒ **under-count ⇒ phantom
    headroom ⇒ over-admission** — D-31's forbidden direction. And unlike D-71 it does
    not announce itself: **it fails silently, at runtime, as free quota, behind a green
    health check.** A loud refusal is a support ticket; this is unbilled revenue.
    The counter is not a cache of convenience — it IS the admission gate.

  * **D-73 — scripting capability.** Every counter op is `EVAL`. A Redis with scripting
    disabled/renamed (some managed offerings) fails at the FIRST acquire, not at boot —
    the exact D-71 shape, one primitive over. A boot-time `SCRIPT LOAD` of the REAL
    `_ACQUIRE` source is definitive, and warms the script cache as a bonus.

  * **D-74 — no Redis version floor is asserted anywhere.**

House law being applied (D-32/D-49/D-51/D-71): a DEFINITIVE negative is a hard,
unoverridable refusal; an ABSENT signal (CONFIG unavailable — ElastiCache disables it)
needs an EXPLICIT operator assertion on the record; absence is never health.

Real-Redis legs (`AB0T_QUOTA_TEST_*_ADDR`) are operator-gated and skipped in CI. After
D-71 — where the emulator agreed with a wrong model and only a real server caught it —
every gate here was ALSO exercised against real redis:7 containers (see the artifact).

Ticket: 20260709_ab0t_quota_systemic_integrity_redesign (D-72, D-73, D-74)
"""
import json
import os

import pytest
import pytest_asyncio
import fakeredis.aioredis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch

from ab0t_quota.redis_preflight import (
    CounterEvictionError,
    RedisVersionError,
    ScriptingUnsupportedError,
    check_redis_counter_eviction,
    check_redis_script_capability,
    check_redis_version,
    evaluate_eviction,
    evaluate_version,
    counter_eviction_error,
    scripting_error,
)
from ab0t_quota.setup import quota_health, _register_capability_routes


# ---------------------------------------------------------------------------
# fakes — CONFIG GET is what a real Redis answers; ElastiCache disables it
# ---------------------------------------------------------------------------

class _ConfigRedis(fakeredis.aioredis.FakeRedis):
    """fakeredis + a controllable CONFIG GET / INFO (it supports NEITHER natively —
    which is itself the 'CONFIG unavailable' case)."""

    _policy = "noeviction"
    _appendonly = "yes"
    _save = ""
    _version = "7.2.4"
    _config_available = True
    _info_available = True

    async def config_get(self, key):
        if not self._config_available:
            raise Exception("ERR unknown command 'config|get'")
        return {key: {"maxmemory-policy": self._policy,
                      "appendonly": self._appendonly,
                      "save": self._save}.get(key, "")}

    async def info(self, section=None, **kw):
        if not self._info_available:
            raise Exception("ERR unknown command 'info'")
        return {"redis_version": self._version}


def _redis(**kw):
    r = _ConfigRedis()
    for k, v in kw.items():
        setattr(r, f"_{k}", v)
    return r


# ===========================================================================
# D-72 — the COUNTER may not live on an evicting Redis
# ===========================================================================

@pytest.mark.asyncio
class TestCounterEvictionCheck:
    @pytest.mark.parametrize("policy", ["allkeys-lru", "allkeys-lfu", "allkeys-random"])
    async def test_evicting_policy_is_REJECTED(self, policy):
        """An evicted gauge is a live resource the counter has forgotten — under-count,
        phantom headroom, over-admission. The forbidden direction (D-31)."""
        ok, reason = await check_redis_counter_eviction(_redis(policy=policy))
        assert ok is False
        assert "evict" in reason.lower()

    @pytest.mark.parametrize("policy", ["noeviction", "volatile-lru", ""])
    async def test_non_evicting_policy_is_ACCEPTED(self, policy):
        """[control] the guard is not a blanket reject — a correct Redis passes.
        (volatile-* only evicts keys with a TTL; gauges carry none.)"""
        ok, reason = await check_redis_counter_eviction(_redis(policy=policy))
        assert ok is True, reason

    async def test_counter_check_does_NOT_require_persistence(self):
        """[boundary] The counter's fatal property is EVICTION, not persistence: a
        restart-lost counter heals (the reconciler converges it to Σ open activations,
        D-28); an EVICTED counter under load silently under-counts while the process
        keeps serving. So `appendonly=no` alone must NOT block startup — over-refusing
        trains operators to ignore the guard (the D-49 false-503 lesson)."""
        ok, _ = await check_redis_counter_eviction(_redis(policy="noeviction", appendonly="no"))
        assert ok is True

    async def test_CONFIG_unavailable_requires_the_operator_assertion(self):
        """ElastiCache disables CONFIG. Unverified ⇒ NOT safe (D-51). The explicit
        on-the-record assertion — never a silent assumption — is what unblocks it."""
        unconfirmed, reason = await check_redis_counter_eviction(_redis(config_available=False))
        assert unconfirmed is False
        assert "redis_durability_confirmed" in reason
        confirmed, _ = await check_redis_counter_eviction(
            _redis(config_available=False), confirmed=True)
        assert confirmed is True

    async def test_an_assertion_does_NOT_override_a_READ_allkeys_policy(self):
        """D-32's law, restated: the assertion rescues an ABSENT signal. A Redis that
        SAYS allkeys-lru IS evicting — asserting otherwise does not change what it does."""
        ok, _ = evaluate_eviction("allkeys-lru", unavailable=False, confirmed=True)
        assert ok is False

    async def test_error_names_cause_and_remedy(self):
        msg = str(counter_eviction_error("maxmemory-policy=allkeys-lru"))
        for token in ("evict", "under-count", "noeviction", "counter"):
            assert token in msg.lower(), f"the refusal must name {token!r}: {msg}"


# ===========================================================================
# D-73 — scripting capability (every counter op is EVAL)
# ===========================================================================

class _NoScriptRedis(fakeredis.aioredis.FakeRedis):
    async def script_load(self, script):
        raise Exception("ERR unknown command 'script'")


@pytest.mark.asyncio
class TestScriptCapability:
    async def test_real_ACQUIRE_source_loads_on_a_scripting_redis(self):
        """[control] the check loads the REAL _ACQUIRE source — not `return 1`. A probe
        that proves a toy script runs proves nothing about ours."""
        ok, reason = await check_redis_script_capability(fakeredis.aioredis.FakeRedis())
        assert ok is True, reason
        assert "sha" in reason.lower() or "loaded" in reason.lower()

    async def test_scripting_disabled_is_REJECTED_at_boot_not_at_first_acquire(self):
        ok, reason = await check_redis_script_capability(_NoScriptRedis())
        assert ok is False
        assert "script" in reason.lower()

    async def test_error_names_cause_and_remedy(self):
        msg = str(scripting_error("SCRIPT LOAD failed"))
        for token in ("eval", "script", "acquire"):
            assert token in msg.lower(), f"the refusal must name {token!r}: {msg}"


# ===========================================================================
# D-74 — version floor
# ===========================================================================

@pytest.mark.asyncio
class TestVersionFloor:
    @pytest.mark.parametrize("version,ok", [
        ("7.2.4", True), ("6.0.0", True), ("6.2.14", True),
        ("5.0.14", False), ("4.0.9", False),
    ])
    async def test_floor_is_enforced(self, version, ok):
        status, _ = evaluate_version(version, floor=(6, 0, 0))
        assert (status == "ok") is ok

    async def test_unreadable_version_is_UNKNOWN_never_assumed_ok(self):
        """Absence is not a value (D-51). It degrades the probe; it does not — see the
        artifact — refuse startup, a DELIBERATE deviation stated in the open."""
        status, _ = await check_redis_version(_redis(info_available=False))
        assert status == "unknown"

    async def test_below_floor_refuses(self):
        status, detail = await check_redis_version(_redis(version="5.0.14"))
        assert status == "below_floor"
        assert "5.0.14" in detail


# ===========================================================================
# setup_quota REFUSES TO START — the gates, wired (the point)
# ===========================================================================

BASE_CONFIG = {
    "storage": {"redis_url": "redis://test/0", "persistence_enabled": False,
                "redis_cluster_confirmed_disabled": True},
    "tier_provider": {"type": "static", "default_tier": "starter"},
    "alerts": {"enabled": False},
    "enforcement": {"enabled": True},
    "resources": [{"service": "t", "resource_key": "thing.concurrent",
                   "display_name": "T", "counter_type": "gauge", "unit": "t"}],
    "tiers": [{"tier_id": "starter", "display_name": "S", "sort_order": 1,
               "limits": {"thing.concurrent": 5}, "features": []}],
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


def _write(path, **storage):
    cfg = json.loads(json.dumps(BASE_CONFIG))
    cfg["storage"].update(storage)
    path.write_text(json.dumps(cfg))


def _boot(redis_obj):
    from ab0t_quota import setup_quota
    app = FastAPI()
    with patch("redis.asyncio.Redis.from_url", side_effect=lambda *a, **k: redis_obj):
        setup_quota(app, enable_paid=False)
        return TestClient(app)


class TestSetupGates:
    def test_allkeys_lru_REFUSES_TO_START(self, config_file, monkeypatch):
        """The D-72 defect, at the boundary that matters: today the library boots onto
        an evicting Redis and silently over-admits when a gauge is evicted."""
        monkeypatch.delenv("AB0T_QUOTA_REDIS_DURABILITY_CONFIRMED", raising=False)
        with pytest.raises(CounterEvictionError) as exc:
            with _boot(_redis(policy="allkeys-lru")):
                pass
        assert "evict" in str(exc.value).lower()

    def test_CONFIG_unavailable_REFUSES_without_the_operator_assertion(
            self, config_file, monkeypatch):
        monkeypatch.delenv("AB0T_QUOTA_REDIS_DURABILITY_CONFIRMED", raising=False)
        _write(config_file)  # no redis_durability_confirmed
        with pytest.raises(CounterEvictionError) as exc:
            with _boot(_redis(config_available=False)):
                pass
        assert "redis_durability_confirmed" in str(exc.value)

    def test_operator_assertion_allows_start_and_is_ON_THE_RECORD(
            self, config_file, monkeypatch):
        monkeypatch.delenv("AB0T_QUOTA_REDIS_DURABILITY_CONFIRMED", raising=False)
        _write(config_file, redis_durability_confirmed=True)
        with _boot(_redis(config_available=False)) as client:
            caps = client.get("/quota/capabilities").json()
        assert "assert" in caps["counter_eviction_policy"].lower()

    def test_scripting_disabled_REFUSES_TO_START(self, config_file):
        class _R(_ConfigRedis):
            async def script_load(self, script):
                raise Exception("ERR unknown command 'script'")
        with pytest.raises(ScriptingUnsupportedError):
            with _boot(_R()):
                pass

    def test_below_floor_version_REFUSES_TO_START(self, config_file):
        with pytest.raises(RedisVersionError):
            with _boot(_redis(version="5.0.14")):
                pass

    def test_a_correct_redis_STARTS_and_reports_every_gate(self, config_file):
        """[control] noeviction + scripting + 7.x ⇒ start, with every verdict readable."""
        with _boot(_redis(policy="noeviction", version="7.2.4")) as client:
            caps = client.get("/quota/capabilities").json()
        assert caps["counter_eviction_policy"].startswith("noeviction")
        assert caps["redis_scripting"].startswith("on")
        assert caps["redis_version"].startswith("7.2.4")


# ===========================================================================
# the gates have a SINK — /quota/health (D-40 / D-49 / D-51)
# ===========================================================================

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
    "redis_topology": "single-node",
    "counter_eviction_policy": "noeviction",
    "redis_scripting": "on (EVAL verified)",
}


class TestHealthSink:
    def test_healthy_control(self):
        assert quota_health(_app_with(HEALTHY))["status"] == "ok"

    @pytest.mark.parametrize("value", ["allkeys-lru", "unknown", "", "EVICTING (allkeys-lru)"])
    def test_evicting_or_unknown_counter_policy_degrades_and_503s(self, value):
        caps = dict(HEALTHY, counter_eviction_policy=value)
        client = TestClient(_app_with(caps))
        r = client.get("/quota/health")
        assert r.status_code == 503, f"{value!r} must FAIL the probe"
        assert r.json()["degraded"] == ["counter_eviction_policy"]

    def test_ABSENT_counter_policy_degrades_when_setup_declared_it_required(self):
        caps = {k: v for k, v in HEALTHY.items() if k != "counter_eviction_policy"}
        h = quota_health(_app_with(caps, required=list(HEALTHY) + ["counter_eviction_policy"]))
        assert h["degraded"] == ["counter_eviction_policy"]

    def test_scripting_off_degrades(self):
        h = quota_health(_app_with(dict(HEALTHY, redis_scripting="unknown")))
        assert h["degraded"] == ["redis_scripting"]

    def test_health_reports_only_KEYS_never_values(self):
        caps = dict(HEALTHY, counter_eviction_policy="allkeys-lru on redis-prod-7:6379")
        body = TestClient(_app_with(caps)).get("/quota/health").json()
        assert body["degraded"] == ["counter_eviction_policy"]
        assert "redis-prod-7" not in str(body)


# ===========================================================================
# cross-runtime contract (D-43) — the structural conformance item
# ===========================================================================

CONF = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "conformance", "scenarios.json")


class TestStructuralConformance:
    def test_python_satisfies_the_declared_structural_item(self):
        """D-43: ONE data file, two runtimes. These are SETUP-level contracts (no engine
        exists yet when they run), so they are registered as a structural conformance item
        and each runtime asserts its OWN refusal against the SAME declared substrings.
        Go's twin: TestGoSatisfiesDeclaredStructuralItem_ST_PREFLIGHT_1."""
        with open(CONF) as f:
            doc = json.load(f)
        item = {i["id"]: i for i in doc.get("structural_conformance", [])}.get("ST-PREFLIGHT-1")
        assert item is not None, "the counter preflight must be declared in scenarios.json"
        assert set(item["runtimes"]) == {"python", "go"}

        evict_msg = str(counter_eviction_error("maxmemory-policy=allkeys-lru")).lower()
        for token in item["eviction_error_must_contain"]:
            assert token.lower() in evict_msg, f"declared token {token!r} missing from the Python refusal"
        script_msg = str(scripting_error("SCRIPT LOAD failed")).lower()
        for token in item["scripting_error_must_contain"]:
            assert token.lower() in script_msg, f"declared token {token!r} missing from the Python refusal"

        from ab0t_quota.redis_preflight import EVICTING_POLICIES, REDIS_VERSION_FLOOR
        assert sorted(item["evicting_policies"]) == sorted(EVICTING_POLICIES)
        assert item["version_floor"] == ".".join(str(x) for x in REDIS_VERSION_FLOOR)
        assert item["config_key"] == "storage.redis_durability_confirmed"


# ===========================================================================
# REAL Redis (operator-gated; recipes in the artifact)
# ===========================================================================

EVICT_ADDR = os.getenv("AB0T_QUOTA_TEST_EVICT_ADDR")      # redis:7 --maxmemory-policy allkeys-lru
SAFE_ADDR = os.getenv("AB0T_QUOTA_TEST_REAL_ADDR")        # redis:7 --maxmemory-policy noeviction
NOSCRIPT_ADDR = os.getenv("AB0T_QUOTA_TEST_NOSCRIPT_ADDR")  # redis:7 --rename-command SCRIPT ""


def _real(addr):
    from redis.asyncio import Redis
    return Redis.from_url(f"redis://{addr}", decode_responses=False)


@pytest.mark.asyncio
@pytest.mark.skipif(not EVICT_ADDR, reason="AB0T_QUOTA_TEST_EVICT_ADDR not set — real-Redis leg is operator-gated")
class TestRealEvictingRedis:
    async def test_a_REAL_allkeys_lru_redis_is_REFUSED(self):
        r = _real(EVICT_ADDR)
        try:
            ok, reason = await check_redis_counter_eviction(r)
            assert ok is False, f"a REAL allkeys-lru Redis reported safe: {reason}"
            assert "allkeys-lru" in reason
        finally:
            await r.aclose()


@pytest.mark.asyncio
@pytest.mark.skipif(not SAFE_ADDR, reason="AB0T_QUOTA_TEST_REAL_ADDR not set — real-Redis leg is operator-gated")
class TestRealSafeRedis:
    async def test_a_REAL_noeviction_redis_is_ACCEPTED(self):
        """[control] against a real, correctly-configured redis:7 every gate must PASS —
        a guard that refuses everything has told you nothing."""
        r = _real(SAFE_ADDR)
        try:
            ok, reason = await check_redis_counter_eviction(r)
            assert ok is True, reason
            script_ok, script_reason = await check_redis_script_capability(r)
            assert script_ok is True, script_reason
            status, detail = await check_redis_version(r)
            assert status == "ok", detail
        finally:
            await r.aclose()

    async def test_the_version_floor_can_actually_REFUSE_a_real_server(self):
        """[negative control on D-74] The same REAL 7.x server, judged against an
        impossible floor, must be refused — proving the floor is load-bearing and not
        a comparison that always passes."""
        r = _real(SAFE_ADDR)
        try:
            status, _ = await check_redis_version(r, floor=(99, 0, 0))
            assert status == "below_floor"
        finally:
            await r.aclose()


@pytest.mark.asyncio
@pytest.mark.skipif(not NOSCRIPT_ADDR,
                    reason="AB0T_QUOTA_TEST_NOSCRIPT_ADDR not set — real-Redis leg is operator-gated")
class TestRealScriptingDisabledRedis:
    async def test_a_REAL_redis_with_SCRIPT_renamed_away_is_REFUSED(self):
        """A managed Redis that disables scripting: today the library boots and dies at
        the first acquire. It must refuse at boot."""
        r = _real(NOSCRIPT_ADDR)
        try:
            ok, reason = await check_redis_script_capability(r)
            assert ok is False, f"a REAL scripting-disabled Redis reported OK: {reason}"
        finally:
            await r.aclose()
