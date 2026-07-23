"""T-10 (GATE-03, pack 20260721): the providers the quickstart recommends must
pass the shipped gates under a DOCUMENTED assertion — or stop being
recommended. Both halves are pinned here:

  * behavioral: capability profiles of the recommended managed providers
    (CONFIG disabled — the library says so itself, redis_preflight.py §D-72
    header) pass `verify_redis_invariants` WITH the documented assertion and
    fail WITHOUT it (the flag is load-bearing, not decorative);
  * doc-lint: docs/quickstart.md, which recommends those providers, actually
    documents the assertion path (it used to promise a 2-minute setup and
    never mention a single `*_confirmed` flag — a false claim).
"""
from __future__ import annotations

from pathlib import Path

import pytest

QUICKSTART = Path(__file__).resolve().parents[1] / "docs" / "quickstart.md"


class ProviderRedis:
    """A managed-Redis capability profile: INFO-rich, optionally CONFIG-less,
    scripting on, single-node, healthy persistence, no evictions."""

    def __init__(self, *, config_available: bool, version: str):
        self._config_available = config_available
        self._version = version

    async def ping(self):
        # GT T1 grew verify_redis_invariants with a leading reachability probe;
        # the profile fake mirrors the interface. No assertion changed.
        return True

    async def config_get(self, key):
        if not self._config_available:
            raise Exception("ERR unknown command 'CONFIG'")
        return {"maxmemory-policy": {"maxmemory-policy": "noeviction"},
                "appendonly": {"appendonly": "yes"},
                "save": {"save": "3600 1"}}[key]

    async def info(self, section=None):
        return {
            "cluster": {"cluster_enabled": "0"},
            "server": {"redis_version": self._version},
            "memory": {"maxmemory": 0, "used_memory": 10_000},
            "stats": {"evicted_keys": 0},
            "persistence": {"aof_enabled": "1", "aof_last_write_status": "ok",
                            "rdb_last_bgsave_status": "ok",
                            "aof_last_bgrewrite_status": "ok"},
        }.get(section, {})

    async def script_load(self, src):
        return "a" * 40


# provider -> (profile, the documented assertion its profile requires)
PROVIDERS = {
    "elasticache": (dict(config_available=False, version="7.1.0"),
                    {"storage": {"redis_durability_confirmed": True}}),
    "upstash": (dict(config_available=False, version="6.2.6"),
                {"storage": {"redis_durability_confirmed": True}}),
    "self-hosted": (dict(config_available=True, version="7.2.0"), {}),
}


@pytest.fixture
def no_suitewide_assertions(monkeypatch):
    """The suite-wide conftest assertion envs would stand in for the config
    flag; clear them HERE (per-test) so the documented config path is what
    is actually proven."""
    monkeypatch.delenv("AB0T_QUOTA_REDIS_DURABILITY_CONFIRMED", raising=False)
    monkeypatch.delenv("AB0T_QUOTA_REDIS_CLUSTER_CONFIRMED_DISABLED", raising=False)
    monkeypatch.delenv("AB0T_QUOTA_REDIS_SCRIPTING_CONFIRMED", raising=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(PROVIDERS))
async def test_recommended_provider_passes_under_documented_assertion(
        name, no_suitewide_assertions):
    from ab0t_quota.redis_preflight import verify_redis_invariants
    profile, assertion_cfg = PROVIDERS[name]
    caps, unsafe = await verify_redis_invariants(
        ProviderRedis(**profile), assertion_cfg, outbox_on_redis=False)
    assert unsafe == [], \
        f"{name} with its documented assertion still refused: {unsafe}"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["elasticache", "upstash"])
async def test_config_less_provider_refuses_without_the_assertion(
        name, no_suitewide_assertions):
    """Control: the documented flag is load-bearing — omit it and the gate
    refuses (unknown fails closed), which is exactly what the quickstart must
    warn about."""
    from ab0t_quota.redis_preflight import verify_redis_invariants
    profile, _ = PROVIDERS[name]
    caps, unsafe = await verify_redis_invariants(
        ProviderRedis(**profile), {}, outbox_on_redis=False)
    assert any(k == "counter_eviction_policy" for k, _ in unsafe), \
        f"{name} without the assertion should refuse on the CONFIG-less eviction check"


def test_quickstart_documents_the_assertion_path():
    """Doc half of GATE-03: the page that recommends Upstash/ElastiCache must
    document the machine-checks and the on-the-record assertion those
    providers need — recommending infrastructure our own gates refuse,
    without the flag, is a false 2-minute claim."""
    text = QUICKSTART.read_text()
    assert "redis_durability_confirmed" in text, \
        "quickstart recommends CONFIG-less providers but never names the required assertion"
    assert "machine-check" in text.lower() or "preflight" in text.lower(), \
        "quickstart must say the library machine-checks the Redis at boot"


def test_quickstart_recommendation_points_at_the_provider_note():
    """The recommendation line itself must flag that managed providers need
    the assertion — not bury it 150 lines later with no pointer."""
    line = next(l for l in QUICKSTART.read_text().splitlines()
                if "Upstash" in l and "ElastiCache" in l)
    assert "confirmed" in line or "assertion" in line or "see" in line.lower(), \
        f"the provider recommendation carries no pointer to the assertion note: {line!r}"
