"""Tests for TTLCache and CachedBridgeClient."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from ab0t_quota.bridge import BridgeClient
from ab0t_quota.caches import CachedBridgeClient, TTLCache


# ---------------------------------------------------------------------------
# TTLCache
# ---------------------------------------------------------------------------

class TestTTLCache:
    @pytest.mark.asyncio
    async def test_set_and_get(self):
        cache = TTLCache(ttl_seconds=10.0)
        await cache.set("k", "v")
        assert await cache.get("k") == "v"

    @pytest.mark.asyncio
    async def test_miss_returns_none(self):
        cache = TTLCache(ttl_seconds=10.0)
        assert await cache.get("nope") is None

    @pytest.mark.asyncio
    async def test_ttl_expiration(self):
        cache = TTLCache(ttl_seconds=0.05)
        await cache.set("k", "v")
        assert await cache.get("k") == "v"
        await asyncio.sleep(0.07)
        assert await cache.get("k") is None

    @pytest.mark.asyncio
    async def test_invalidate(self):
        cache = TTLCache(ttl_seconds=10.0)
        await cache.set("k", "v")
        await cache.invalidate("k")
        assert await cache.get("k") is None

    @pytest.mark.asyncio
    async def test_clear(self):
        cache = TTLCache(ttl_seconds=10.0)
        await cache.set("a", 1)
        await cache.set("b", 2)
        await cache.clear()
        assert await cache.get("a") is None
        assert await cache.get("b") is None

    @pytest.mark.asyncio
    async def test_max_entries_evicts_oldest(self):
        cache = TTLCache(ttl_seconds=10.0, max_entries=3)
        await cache.set("a", 1)
        await cache.set("b", 2)
        await cache.set("c", 3)
        await cache.set("d", 4)  # should evict "a"
        assert await cache.get("a") is None
        assert await cache.get("d") == 4

    @pytest.mark.asyncio
    async def test_get_or_fetch_caches_result(self):
        cache = TTLCache(ttl_seconds=10.0)
        calls = {"n": 0}

        async def fetch():
            calls["n"] += 1
            return "fetched"

        v1 = await cache.get_or_fetch("k", fetch)
        v2 = await cache.get_or_fetch("k", fetch)
        assert v1 == v2 == "fetched"
        assert calls["n"] == 1  # only one fetch despite two get_or_fetch calls


# ---------------------------------------------------------------------------
# CachedBridgeClient — fixtures
# ---------------------------------------------------------------------------

def _client_with_handler(handler) -> BridgeClient:
    transport = httpx.MockTransport(handler)
    bc = BridgeClient(
        base_url="https://billing.service.ab0t.com",
        api_key="ab0t_sk_test",
        service_name="svc-1",
    )
    bc._client = httpx.AsyncClient(transport=transport, headers={"X-API-Key": "ab0t_sk_test"})
    return bc


# ---------------------------------------------------------------------------
# Tier cache
# ---------------------------------------------------------------------------

class TestTierCaching:
    @pytest.mark.asyncio
    async def test_tier_cached_across_calls(self):
        calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json={"tier_id": "starter"})

        wrapped = CachedBridgeClient(_client_with_handler(handler), tier_ttl_seconds=10.0)
        for _ in range(5):
            assert await wrapped.get_tier("org-1") == "starter"
        assert calls["n"] == 1  # one HTTP call for 5 reads
        await wrapped.close()

    @pytest.mark.asyncio
    async def test_tier_invalidate_forces_refetch(self):
        calls = {"n": 0}
        responses = ["starter", "pro"]

        async def handler(request: httpx.Request) -> httpx.Response:
            tier = responses[min(calls["n"], len(responses) - 1)]
            calls["n"] += 1
            return httpx.Response(200, json={"tier_id": tier})

        wrapped = CachedBridgeClient(_client_with_handler(handler), tier_ttl_seconds=10.0)
        assert await wrapped.get_tier("org-1") == "starter"
        await wrapped.invalidate_tier("org-1")
        assert await wrapped.get_tier("org-1") == "pro"
        assert calls["n"] == 2
        await wrapped.close()

    @pytest.mark.asyncio
    async def test_tier_ttl_expires(self):
        calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json={"tier_id": "starter"})

        wrapped = CachedBridgeClient(_client_with_handler(handler), tier_ttl_seconds=0.05)
        await wrapped.get_tier("org-1")
        await asyncio.sleep(0.07)
        await wrapped.get_tier("org-1")
        assert calls["n"] == 2
        await wrapped.close()


# ---------------------------------------------------------------------------
# Decision cache
# ---------------------------------------------------------------------------

class TestDecisionCaching:
    @pytest.mark.asyncio
    async def test_allow_cached_within_ttl(self):
        calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json={
                "decision": "allow", "current": 0, "limit": 5,
                "resource_key": "x.y",
            })

        wrapped = CachedBridgeClient(_client_with_handler(handler), decision_ttl_seconds=10.0)
        for _ in range(5):
            r = await wrapped.check("org-1", "x.y")
            assert r["decision"] == "allow"
        assert calls["n"] == 1  # only one HTTP call
        await wrapped.close()

    @pytest.mark.asyncio
    async def test_deny_NOT_cached(self):
        """Denials must always be fresh — never cache a 'deny' result."""
        calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json={
                "decision": "deny", "current": 5, "limit": 5,
                "resource_key": "x.y",
            })

        wrapped = CachedBridgeClient(_client_with_handler(handler), decision_ttl_seconds=10.0)
        for _ in range(3):
            await wrapped.check("org-1", "x.y")
        assert calls["n"] == 3  # every deny round-trips
        await wrapped.close()

    @pytest.mark.asyncio
    async def test_per_user_keyed_separately(self):
        """alice and bob shouldn't share the same cached decision."""
        seen_users = []

        async def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            seen_users.append("alice" if "alice" in url else "bob")
            return httpx.Response(200, json={
                "decision": "allow", "current": 0, "limit": 5,
                "resource_key": "x.y",
            })

        wrapped = CachedBridgeClient(_client_with_handler(handler), decision_ttl_seconds=10.0)
        await wrapped.check("org-1", "x.y", user_id="alice")
        await wrapped.check("org-1", "x.y", user_id="bob")
        await wrapped.check("org-1", "x.y", user_id="alice")  # cached
        await wrapped.check("org-1", "x.y", user_id="bob")    # cached
        assert seen_users == ["alice", "bob"]
        await wrapped.close()

    @pytest.mark.asyncio
    async def test_bridge_error_NOT_cached(self):
        """Bridge errors are fail-open with markers — must not pollute cache."""
        calls = {"n": 0}
        states = ["error", "ok"]

        async def handler(request: httpx.Request) -> httpx.Response:
            n = calls["n"]
            calls["n"] += 1
            if n == 0:
                raise httpx.ConnectError("boom")
            return httpx.Response(200, json={
                "decision": "allow", "current": 0, "limit": 5,
                "resource_key": "x.y",
            })

        wrapped = CachedBridgeClient(_client_with_handler(handler), decision_ttl_seconds=10.0)
        # First call: bridge error, fail-open with marker — NOT cached
        r1 = await wrapped.check("org-1", "x.y")
        assert r1.get("_bridge_error") is True
        # Second call: actually hits the network and gets a real allow
        r2 = await wrapped.check("org-1", "x.y")
        assert r2["decision"] == "allow"
        assert "_bridge_error" not in r2
        await wrapped.close()


# ---------------------------------------------------------------------------
# Pass-through ops
# ---------------------------------------------------------------------------

class TestPassThrough:
    @pytest.mark.asyncio
    async def test_increment_is_never_cached(self):
        """Counter writes must hit the wire every time."""
        calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json={"new_value": float(calls["n"])})

        wrapped = CachedBridgeClient(_client_with_handler(handler))
        for i in range(3):
            new_val = await wrapped.increment("org-1", "x.y")
            assert new_val == float(i + 1)
        assert calls["n"] == 3
        await wrapped.close()

    @pytest.mark.asyncio
    async def test_usage_is_never_cached(self):
        calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json={"org_id": "org-1", "tier_id": "free", "tier_display": "Free", "resources": []})

        wrapped = CachedBridgeClient(_client_with_handler(handler))
        for _ in range(3):
            await wrapped.usage("org-1")
        assert calls["n"] == 3
        await wrapped.close()
