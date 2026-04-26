"""Tests for the billing router factory.

Uses FastAPI TestClient to verify route generation, auth enforcement,
and request handling without hitting real services.
"""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from ab0t_quota.billing import create_billing_router


async def mock_auth_reader(request: Request):
    """Simulated auth dependency returning a user with org_id."""
    class User:
        org_id = "test-org-123"
        email = "test@example.com"
    return User()


async def mock_auth_admin(request: Request):
    return await mock_auth_reader(request)


def _create_app(**kwargs) -> FastAPI:
    """Create a test FastAPI app with the billing router mounted."""
    app = FastAPI()
    router = create_billing_router(**kwargs)
    app.include_router(router)
    return app


class TestRouterCreation:
    def test_minimal_config_creates_public_routes(self):
        """Without auth deps, only public routes are created."""
        app = _create_app(
            payment_url="http://test:8005",
            payment_api_key="key",
            billing_url="http://test:8002",
            billing_api_key="key",
            consumer_org_id="org-1",
        )
        paths = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/api/payments/plans" in paths
        assert "/api/payments/checkout/init" in paths
        assert "/api/webhooks/stripe" in paths
        assert "/checkout/success" in paths
        # Auth routes should NOT be present
        assert "/api/billing/balance" not in paths
        assert "/api/payments/subscriptions" not in paths

    def test_full_config_creates_all_routes(self):
        """With auth deps, all 20 routes are created."""
        app = _create_app(
            payment_url="http://test:8005",
            payment_api_key="key",
            billing_url="http://test:8002",
            billing_api_key="key",
            consumer_org_id="org-1",
            auth_reader=mock_auth_reader,
            auth_admin=mock_auth_admin,
        )
        paths = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/api/billing/balance" in paths
        assert "/api/billing/transactions" in paths
        assert "/api/payments/subscriptions" in paths
        assert "/api/payments/methods" in paths
        assert "/api/payments/portal" in paths
        assert "/api/payments/topup" in paths
        assert "/api/payments/checkout/{plan_id}" in paths

    def test_custom_prefix(self):
        """Prefix changes all route paths."""
        app = _create_app(
            payment_url="http://test:8005",
            payment_api_key="key",
            billing_url="http://test:8002",
            billing_api_key="key",
            consumer_org_id="org-1",
            prefix="/v2",
        )
        paths = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/v2/payments/plans" in paths
        assert "/v2/webhooks/stripe" in paths

    def test_missing_payment_url_raises(self):
        with pytest.raises(ValueError, match="payment_url"):
            create_billing_router(
                payment_url="",
                payment_api_key="key",
                billing_url="http://test:8002",
                billing_api_key="key",
                consumer_org_id="org-1",
            )

    def test_missing_consumer_org_raises(self):
        with pytest.raises(ValueError, match="consumer_org_id"):
            create_billing_router(
                payment_url="http://test:8005",
                payment_api_key="key",
                billing_url="http://test:8002",
                billing_api_key="key",
                consumer_org_id="",
            )


class TestPublicRoutes:
    @pytest.fixture
    def client(self):
        app = _create_app(
            payment_url="http://test:8005",
            payment_api_key="key",
            billing_url="http://test:8002",
            billing_api_key="key",
            consumer_org_id="org-1",
        )
        return TestClient(app)

    def test_checkout_success_page_route_registered(self):
        """Checkout success page route must be registered."""
        app = _create_app(
            payment_url="http://test:8005",
            payment_api_key="key",
            billing_url="http://test:8002",
            billing_api_key="key",
            consumer_org_id="org-1",
        )
        paths = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/checkout/success" in paths

    def test_webhook_requires_signature(self, client):
        """Webhook route must reject requests without Stripe-Signature."""
        response = client.post(
            "/api/webhooks/stripe",
            json={"type": "test"},
        )
        assert response.status_code == 400
        assert "Stripe-Signature" in response.json()["detail"]

    def test_checkout_complete_validates_input(self, client):
        """Checkout complete must reject missing session_id."""
        response = client.post(
            "/api/payments/checkout/complete",
            json={},
        )
        assert response.status_code == 422


class TestRouteOrdering:
    """Verify that static routes (init, complete, anonymous) are registered
    before the {plan_id} catch-all to prevent path conflicts."""

    @pytest.fixture
    def app(self):
        return _create_app(
            payment_url="http://test:8005",
            payment_api_key="key",
            billing_url="http://test:8002",
            billing_api_key="key",
            consumer_org_id="org-1",
            auth_reader=mock_auth_reader,
        )

    def test_init_not_caught_by_plan_id(self, app):
        paths = [r.path for r in app.routes if hasattr(r, "path")]
        init_idx = next(i for i, p in enumerate(paths) if "checkout/init" in p)
        plan_idx = next(i for i, p in enumerate(paths) if "{plan_id}" in p)
        assert init_idx < plan_idx, "checkout/init must be before {plan_id}"

    def test_complete_not_caught_by_plan_id(self, app):
        # Filter to only checkout routes to avoid noise from FastAPI defaults
        checkout_paths = [
            r.path for r in app.routes
            if hasattr(r, "path") and "checkout" in r.path
        ]
        complete_idx = next(i for i, p in enumerate(checkout_paths) if "complete" in p)
        plan_idx = next(
            (i for i, p in enumerate(checkout_paths) if "{plan_id}" in p and "anonymous" not in p),
            len(checkout_paths),  # if not found (no auth → no {plan_id} route)
        )
        assert complete_idx < plan_idx, "checkout/complete must be before {plan_id}"

    def test_anonymous_not_caught_by_plan_id(self, app):
        paths = [r.path for r in app.routes if hasattr(r, "path")]
        anon_idx = next(i for i, p in enumerate(paths) if "anonymous" in p)
        plan_idx = next(i for i, p in enumerate(paths) if "checkout/{plan_id}" in p and "anonymous" not in p)
        assert anon_idx < plan_idx, "anonymous/{plan_id} must be before {plan_id}"
