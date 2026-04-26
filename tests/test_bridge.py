"""Tests for the bridge-mode HTTP client (`ab0t_quota.bridge`).

Verifies the wire protocol matches docs/mesh-quota-api.md and the
fail-open behavior on network errors.
"""

from __future__ import annotations

import httpx
import pytest

from ab0t_quota.bridge import BridgeClient, BridgeContext, RemoteTierProvider


def _client_with_handler(handler) -> BridgeClient:
    """Build a BridgeClient pre-configured to use a mock transport."""
    transport = httpx.MockTransport(handler)
    bc = BridgeClient(
        base_url="https://billing.service.ab0t.com",
        api_key="ab0t_sk_test",
        service_name="svc-1",
    )
    # Replace the underlying client with one wired to the mock transport
    bc._client = httpx.AsyncClient(transport=transport, headers={"X-API-Key": "ab0t_sk_test"})
    return bc


# ---------------------------------------------------------------------------
# Wire protocol — URL, method, headers, params
# ---------------------------------------------------------------------------

class TestWireProtocol:
    @pytest.mark.asyncio
    async def test_check_uses_correct_url_and_method(self):
        captured = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, json={
                "decision": "allow", "resource_key": "thing.concurrent",
                "current": 0, "requested": 1, "limit": 5,
                "tier_id": "starter", "tier_display": "Starter",
                "severity": "info", "message": "ok",
            })

        client = _client_with_handler(handler)
        await client.check("org-1", "thing.concurrent")
        assert captured["method"] == "POST"
        assert "/billing/quota/svc-1/org-1/check/thing.concurrent" in captured["url"]
        assert captured["headers"]["x-api-key"] == "ab0t_sk_test"
        await client.close()

    @pytest.mark.asyncio
    async def test_check_passes_user_id_and_increment_as_query(self):
        captured = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"decision": "allow"})

        client = _client_with_handler(handler)
        await client.check("org-1", "thing.concurrent", user_id="alice", increment=2.5)
        assert "user_id=alice" in captured["url"]
        assert "increment=2.5" in captured["url"]
        await client.close()

    @pytest.mark.asyncio
    async def test_increment_url_and_response_parsing(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert "/billing/quota/svc-1/org-1/increment/thing.concurrent" in str(request.url)
            return httpx.Response(200, json={
                "resource_key": "thing.concurrent", "new_value": 4.0,
            })

        client = _client_with_handler(handler)
        new_val = await client.increment("org-1", "thing.concurrent")
        assert new_val == 4.0
        await client.close()

    @pytest.mark.asyncio
    async def test_increment_passes_idempotency_key(self):
        captured = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"new_value": 1.0})

        client = _client_with_handler(handler)
        await client.increment("org-1", "thing.concurrent", idempotency_key="op-42")
        assert "idempotency_key=op-42" in captured["url"]
        await client.close()

    @pytest.mark.asyncio
    async def test_decrement_url_and_response(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            assert "/billing/quota/svc-1/org-1/decrement/thing.concurrent" in str(request.url)
            return httpx.Response(200, json={
                "resource_key": "thing.concurrent", "new_value": 2.0,
            })

        client = _client_with_handler(handler)
        new_val = await client.decrement("org-1", "thing.concurrent")
        assert new_val == 2.0
        await client.close()

    @pytest.mark.asyncio
    async def test_check_bundle_endpoint(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            assert "/billing/quota/svc-1/org-1/check-bundle/default" in str(request.url)
            return httpx.Response(200, json={
                "allowed": True, "results": [], "denied_resources": [],
            })

        client = _client_with_handler(handler)
        result = await client.check_bundle("org-1", "default")
        assert result["allowed"] is True
        await client.close()

    @pytest.mark.asyncio
    async def test_usage_endpoint(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            assert "/billing/quota/svc-1/org-1/usage" in str(request.url)
            return httpx.Response(200, json={
                "org_id": "org-1", "tier_id": "starter",
                "tier_display": "Starter", "resources": [],
            })

        client = _client_with_handler(handler)
        out = await client.usage("org-1")
        assert out["org_id"] == "org-1"
        await client.close()

    @pytest.mark.asyncio
    async def test_get_tier_endpoint(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            assert "/billing/org-1/tier" in str(request.url)
            return httpx.Response(200, json={"tier_id": "pro"})

        client = _client_with_handler(handler)
        tier = await client.get_tier("org-1")
        assert tier == "pro"
        await client.close()


# ---------------------------------------------------------------------------
# Failure / fail-open behavior
# ---------------------------------------------------------------------------

class TestFailureBehavior:
    @pytest.mark.asyncio
    async def test_network_error_returns_fail_open_check_result(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client = _client_with_handler(handler)
        result = await client.check("org-1", "thing.concurrent")
        # Fail-open default: treat as allow with marker
        assert result["decision"] == "allow"
        assert result.get("_bridge_error") is True
        await client.close()

    @pytest.mark.asyncio
    async def test_404_no_catalog_published(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "No bridge-mode catalog published for service 'svc-1'"})

        client = _client_with_handler(handler)
        result = await client.check("org-1", "thing.concurrent")
        # Fail-open with marker so consumer can log and investigate
        assert result["_bridge_error"] is True
        assert result["_status"] == 404
        await client.close()

    @pytest.mark.asyncio
    async def test_increment_network_error_returns_zero(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timeout")

        client = _client_with_handler(handler)
        new_val = await client.increment("org-1", "thing.concurrent")
        assert new_val == 0.0  # fail-open
        await client.close()

    @pytest.mark.asyncio
    async def test_get_tier_falls_back_to_free_on_error(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("nope")

        client = _client_with_handler(handler)
        assert await client.get_tier("org-1") == "free"
        await client.close()


# ---------------------------------------------------------------------------
# RemoteTierProvider
# ---------------------------------------------------------------------------

class TestRemoteTierProvider:
    @pytest.mark.asyncio
    async def test_delegates_to_client(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"tier_id": "enterprise"})

        client = _client_with_handler(handler)
        provider = RemoteTierProvider(client)
        assert await provider.get_tier("org-1") == "enterprise"
        await client.close()


# ---------------------------------------------------------------------------
# BridgeContext (consumer-facing surface in bridge mode)
# ---------------------------------------------------------------------------

class TestBridgeContext:
    @pytest.mark.asyncio
    async def test_check_raises_429_on_deny(self):
        from fastapi import HTTPException

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "decision": "deny", "resource_key": "x.y",
                "current": 5, "requested": 1, "limit": 5,
                "tier_id": "free", "tier_display": "Free",
                "severity": "exceeded",
                "message": "over limit",
            })

        client = _client_with_handler(handler)
        ctx = BridgeContext(client)
        with pytest.raises(HTTPException) as exc:
            await ctx.check("org-1", "x.y")
        assert exc.value.status_code == 429
        await client.close()

    @pytest.mark.asyncio
    async def test_check_passes_through_on_allow(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"decision": "allow", "current": 0})

        client = _client_with_handler(handler)
        ctx = BridgeContext(client)
        result = await ctx.check("org-1", "x.y")
        assert result["decision"] == "allow"
        await client.close()

    @pytest.mark.asyncio
    async def test_no_upstream_surface_leaked(self):
        """BridgeContext exposes the same lean API as QuotaContext —
        no raw billing/payment client visible to consumers."""
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={})

        client = _client_with_handler(handler)
        ctx = BridgeContext(client)
        for forbidden in ("billing", "payment", "stripe", "sns"):
            attrs = [a for a in dir(ctx) if forbidden in a.lower() and not a.startswith("_")]
            assert not attrs, f"BridgeContext leaks {forbidden} surface: {attrs}"
        await client.close()
