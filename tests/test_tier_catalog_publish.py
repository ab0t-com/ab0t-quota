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


class TestCatalogOmitsBillingPolicy:
    """Lock in `context_03_billing_model_decision.md` D5: billing-policy fields
    (billing_model, price, credit_grant, initial_credit) MUST NOT appear in
    the published catalog payload, even on tiers that have them set.

    Rationale:
      - The catalog is for cross-service admin views + bridge-mode engine.
        Neither needs billing policy.
      - Publishing billing policy would put billing-service in the
        policy-resolution path, recreating the boundary violation T8/T9 fixed.
      - Consumer billing policy stays consumer-local; it's resolved against
        the local TierConfig via `_build_default_credit_grant_handler`.

    If a future contributor adds these fields to the catalog payload, this
    test will fail and they must justify the change against D5.
    """

    @pytest.fixture
    def tiers_with_billing_policy(self):
        """Tiers that exercise EVERY billing-policy field — none should leak."""
        from decimal import Decimal
        from ab0t_quota.models.core import (
            BillingModel, BillingPeriod, CreditDestination, CreditGrant,
            CreditLifecycle, CreditTrigger, Price, TierConfig, TierLimits,
        )
        return {
            "free": TierConfig(
                tier_id="free", display_name="Free",
                # Back-compat shim: free tier with legacy initial_credit
                initial_credit=Decimal("5.00"),
                limits={"thing.concurrent": TierLimits(limit=1)},
            ),
            "starter": TierConfig(
                tier_id="starter", display_name="Starter",
                billing_model=BillingModel.SUBSCRIPTION_WITH_CREDITS,
                price=Price(
                    amount_per_period=Decimal("20.00"),
                    currency="USD",
                    period=BillingPeriod.MONTH,
                ),
                credit_grant=CreditGrant(
                    trigger=CreditTrigger.SUBSCRIPTION_INVOICE_PAID,
                    amount_per_period=Decimal("25.00"),
                    currency="USD",
                    lifecycle=CreditLifecycle.USE_IT_OR_LOSE_IT,
                    destination=CreditDestination.SUBSCRIPTION_CREDIT,
                ),
                limits={"thing.concurrent": TierLimits(limit=10)},
            ),
            "pro": TierConfig(
                tier_id="pro", display_name="Pro",
                billing_model=BillingModel.SUBSCRIPTION_UNLOCK_ONLY,
                price=Price(amount_per_period=Decimal("100")),
                limits={"thing.concurrent": TierLimits(limit=100)},
            ),
        }

    @pytest.mark.asyncio
    async def test_billing_policy_fields_never_in_payload(
        self, fake_billing, tiers_with_billing_policy
    ):
        await _publish_tier_catalog("svc-1", tiers_with_billing_policy)
        import json
        body = json.loads(fake_billing["body"])

        # The top-level payload must not contain any billing-policy keys.
        FORBIDDEN_TOP = {"prices", "credit_grants", "billing_models"}
        for k in FORBIDDEN_TOP:
            assert k not in body, (
                f"D5 violation: catalog payload exposed top-level '{k}'. "
                f"See context_03_billing_model_decision.md."
            )

        # No tier entry may carry billing-policy fields.
        FORBIDDEN_PER_TIER = {
            "billing_model", "price", "credit_grant", "initial_credit",
        }
        for tier in body["tiers"]:
            leaked = FORBIDDEN_PER_TIER & set(tier.keys())
            assert not leaked, (
                f"D5 violation: tier '{tier['tier_id']}' exposed billing-policy "
                f"fields {leaked} in catalog payload. These must stay "
                f"consumer-local. See context_03_billing_model_decision.md."
            )

    @pytest.mark.asyncio
    async def test_only_quota_policy_fields_published(
        self, fake_billing, tiers_with_billing_policy
    ):
        """Inverse of the forbidden check — payload SHOULD include the
        quota-policy fields the catalog actually exists for."""
        await _publish_tier_catalog("svc-1", tiers_with_billing_policy)
        import json
        body = json.loads(fake_billing["body"])
        starter = next(t for t in body["tiers"] if t["tier_id"] == "starter")
        # Quota-policy fields the catalog IS for
        assert "tier_id" in starter
        assert "display_name" in starter
        assert "limits" in starter
        assert "features" in starter

    @pytest.mark.asyncio
    async def test_payload_keys_are_an_allowlist(
        self, fake_billing, tiers_with_billing_policy
    ):
        """Defence in depth: assert each tier dict only contains the
        allow-listed quota-policy keys. If someone adds a new key (billing
        or otherwise), this test forces them to update the allowlist AND
        check against D5.
        """
        await _publish_tier_catalog("svc-1", tiers_with_billing_policy)
        import json
        body = json.loads(fake_billing["body"])
        ALLOWED = {
            "tier_id", "display_name", "description", "sort_order",
            "features", "upgrade_url", "default_per_user_fraction", "limits",
        }
        for tier in body["tiers"]:
            extra = set(tier.keys()) - ALLOWED
            assert not extra, (
                f"tier '{tier['tier_id']}' carries unexpected keys {extra}. "
                f"If adding a new quota-policy field, extend ALLOWED here "
                f"AND verify it does not constitute billing policy per D5."
            )
