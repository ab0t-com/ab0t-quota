"""Tests for the tier-catalog auto-publish that runs in setup_quota lifespan.

The library POSTs the consumer's loaded tiers to the central tier catalog
on startup so cross-service admin views reflect the consumer's actual
limits, not library defaults. Best-effort — failure does not block startup.
"""

from __future__ import annotations

import os

import httpx
import pytest

from ab0t_quota.models.core import (
    CounterType, ResetPeriod, ResourceDef, TierConfig, TierLimits,
)
from ab0t_quota.registry import ResourceRegistry
from ab0t_quota.setup import (
    _publish_tier_catalog,
    _resolve_service_name,
)


@pytest.fixture
def tiers():
    return {
        "starter": TierConfig(
            tier_id="starter", display_name="Starter",
            sort_order=1,
            features={"basic", "api"},
            upgrade_url="/upgrade",
            default_per_user_fraction=0.5,
            limits={
                "thing.concurrent": TierLimits(
                    limit=5, warning_threshold=0.8, critical_threshold=0.95,
                ),
                "thing.cost": TierLimits(limit=100.0),
            },
        ),
        "pro": TierConfig(
            tier_id="pro", display_name="Pro",
            sort_order=2,
            features={"basic", "api", "premium"},
            limits={"thing.concurrent": TierLimits(limit=25, per_user_limit=10)},
        ),
    }


@pytest.fixture
def fake_billing(monkeypatch):
    """Patch httpx.AsyncClient with a mock transport. Returns the captured request."""
    captured: dict = {}

    async def fake_handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["headers"] = dict(request.headers)
        try:
            captured["body"] = httpx._content.encode_request(request.read()).decode()  # type: ignore
        except Exception:
            captured["body"] = request.read().decode("utf-8", errors="replace")
        return httpx.Response(200, json={"status": "ok"})

    transport = httpx.MockTransport(fake_handler)

    class FakeAsyncClient(httpx.AsyncClient):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    monkeypatch.setenv("AB0T_MESH_API_KEY", "ab0t_sk_test_xxx")
    return captured


# ---------------------------------------------------------------------------
# _resolve_service_name
# ---------------------------------------------------------------------------

class TestResolveServiceName:
    def test_env_var_wins(self, monkeypatch):
        monkeypatch.setenv("AB0T_SERVICE_NAME", "from-env")
        registry = ResourceRegistry()
        assert _resolve_service_name({"service_name": "from-config"}, registry) == "from-env"

    def test_falls_back_to_config(self, monkeypatch):
        monkeypatch.delenv("AB0T_SERVICE_NAME", raising=False)
        registry = ResourceRegistry()
        assert _resolve_service_name({"service_name": "from-config"}, registry) == "from-config"

    def test_falls_back_to_first_resource(self, monkeypatch):
        monkeypatch.delenv("AB0T_SERVICE_NAME", raising=False)
        registry = ResourceRegistry()
        registry.register(ResourceDef(
            service="my-mesh-service", resource_key="thing.concurrent",
            display_name="X", counter_type=CounterType.GAUGE, unit="x",
        ))
        assert _resolve_service_name({}, registry) == "my-mesh-service"

    def test_returns_none_when_nothing_configured(self, monkeypatch):
        monkeypatch.delenv("AB0T_SERVICE_NAME", raising=False)
        registry = ResourceRegistry()
        assert _resolve_service_name({}, registry) is None


# ---------------------------------------------------------------------------
# _publish_tier_catalog
# ---------------------------------------------------------------------------

class TestPublishTierCatalog:
    @pytest.mark.asyncio
    async def test_publishes_full_tier_catalog(self, fake_billing, tiers):
        ok = await _publish_tier_catalog("svc-1", tiers)
        assert ok is True
        assert fake_billing["method"] == "PUT"
        assert "/billing/tier-catalog/svc-1" in fake_billing["url"]
        # Auth headers
        assert fake_billing["headers"]["x-api-key"] == "ab0t_sk_test_xxx"
        assert fake_billing["headers"]["x-service-name"] == "svc-1"

    @pytest.mark.asyncio
    async def test_payload_includes_all_tier_fields(self, fake_billing, tiers):
        await _publish_tier_catalog("svc-1", tiers)
        import json
        body = json.loads(fake_billing["body"])
        # Two tiers
        tier_ids = {t["tier_id"] for t in body["tiers"]}
        assert tier_ids == {"starter", "pro"}
        # Starter tier preserves all the relevant fields
        starter = next(t for t in body["tiers"] if t["tier_id"] == "starter")
        assert starter["display_name"] == "Starter"
        assert starter["sort_order"] == 1
        assert "basic" in starter["features"]
        assert starter["upgrade_url"] == "/upgrade"
        assert starter["default_per_user_fraction"] == 0.5
        assert starter["limits"]["thing.concurrent"]["limit"] == 5
        assert starter["limits"]["thing.concurrent"]["warning_threshold"] == 0.8
        assert starter["limits"]["thing.cost"]["limit"] == 100.0
        # Pro preserves explicit per_user_limit
        pro = next(t for t in body["tiers"] if t["tier_id"] == "pro")
        assert pro["limits"]["thing.concurrent"]["per_user_limit"] == 10

    @pytest.mark.asyncio
    async def test_skips_when_no_mesh_key(self, monkeypatch, tiers):
        monkeypatch.delenv("AB0T_MESH_API_KEY", raising=False)
        ok = await _publish_tier_catalog("svc-1", tiers)
        assert ok is False  # no key → skip

    @pytest.mark.asyncio
    async def test_does_not_raise_on_5xx(self, monkeypatch, tiers):
        async def boom(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": "service unavailable"})

        class FakeClient(httpx.AsyncClient):
            def __init__(self, *a, **kw):
                kw["transport"] = httpx.MockTransport(boom)
                super().__init__(*a, **kw)

        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
        monkeypatch.setenv("AB0T_MESH_API_KEY", "ab0t_sk_test")
        # Should return False, not raise — best-effort
        ok = await _publish_tier_catalog("svc-1", tiers)
        assert ok is False

    @pytest.mark.asyncio
    async def test_includes_resources_and_bundles_when_passed(self, fake_billing, tiers):
        """Bridge mode: catalog publish includes resource defs + bundles
        so billing can run a server-side engine for this service."""
        registry = ResourceRegistry()
        registry.register(
            ResourceDef(
                service="svc-1", resource_key="thing.concurrent",
                display_name="Concurrent Things",
                counter_type=CounterType.GAUGE, unit="things",
            ),
            ResourceDef(
                service="svc-1", resource_key="thing.cost",
                display_name="Monthly Cost",
                counter_type=CounterType.ACCUMULATOR, unit="USD",
                reset_period=ResetPeriod.MONTHLY, precision=2,
            ),
        )
        bundles = {"default": ["thing.concurrent"], "with_cost": ["thing.concurrent", "thing.cost"]}
        await _publish_tier_catalog("svc-1", tiers, registry=registry, bundles=bundles)
        import json
        body = json.loads(fake_billing["body"])
        # Resources serialized with all metadata billing's engine needs
        assert "resources" in body
        rks = {r["resource_key"] for r in body["resources"]}
        assert rks == {"thing.concurrent", "thing.cost"}
        cost = next(r for r in body["resources"] if r["resource_key"] == "thing.cost")
        assert cost["counter_type"] == "accumulator"
        assert cost["reset_period"] == "monthly"
        assert cost["precision"] == 2
        # Bundles preserved
        assert body["resource_bundles"] == bundles

    @pytest.mark.asyncio
    async def test_no_registry_no_resources_field(self, fake_billing, tiers):
        """When called without registry+bundles (legacy), payload has tiers only."""
        await _publish_tier_catalog("svc-1", tiers)
        import json
        body = json.loads(fake_billing["body"])
        assert "tiers" in body
        assert "resources" not in body
        assert "resource_bundles" not in body

    @pytest.mark.asyncio
    async def test_does_not_raise_on_connection_error(self, monkeypatch, tiers):
        async def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        class FakeClient(httpx.AsyncClient):
            def __init__(self, *a, **kw):
                kw["transport"] = httpx.MockTransport(boom)
                super().__init__(*a, **kw)

        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
        monkeypatch.setenv("AB0T_MESH_API_KEY", "ab0t_sk_test")
        ok = await _publish_tier_catalog("svc-1", tiers)
        assert ok is False  # never blocks startup
