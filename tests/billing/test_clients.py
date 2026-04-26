"""Tests for billing module HTTP clients.

Uses httpx mock transport to test request construction, error handling,
and response parsing without hitting real services.
"""

import json

import httpx
import pytest
import pytest_asyncio

from ab0t_quota.billing.clients import (
    BillingServiceClient,
    BillingServiceError,
    PaymentServiceClient,
    PaymentServiceError,
)


def _mock_transport(responses: dict[str, tuple[int, dict]]):
    """Create a mock httpx transport that returns canned responses by URL path."""
    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for pattern, (status, body) in responses.items():
            if pattern in path:
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={"detail": f"No mock for {path}"})
    return httpx.MockTransport(handler)


@pytest.fixture
def payment_client():
    client = PaymentServiceClient("http://test:8005", "test_key")
    return client


@pytest.fixture
def billing_client():
    client = BillingServiceClient("http://test:8002", "test_key")
    return client


class TestPaymentClient:
    @pytest.mark.asyncio
    async def test_get_plans(self, payment_client):
        payment_client.client = httpx.AsyncClient(
            transport=_mock_transport({
                "/checkout/org-1/plans": (200, {
                    "plans": [
                        {"plan_id": "p1", "name": "Starter", "prices": []},
                        {"plan_id": "p2", "name": "Pro", "prices": []},
                    ],
                    "count": 2,
                }),
            })
        )
        result = await payment_client.get_plans("org-1")
        assert result.count == 2
        assert result.plans[0].name == "Starter"
        assert result.plans[1].plan_id == "p2"

    @pytest.mark.asyncio
    async def test_init_checkout(self, payment_client):
        payment_client.client = httpx.AsyncClient(
            transport=_mock_transport({
                "/checkout/init": (200, {
                    "session_token": "tok_123",
                    "expires_at": "2026-03-31T12:00:00Z",
                    "fingerprint": "fp_abc",
                }),
            })
        )
        result = await payment_client.init_checkout()
        assert result.session_token == "tok_123"
        assert result.fingerprint == "fp_abc"

    @pytest.mark.asyncio
    async def test_create_checkout_session(self, payment_client):
        payment_client.client = httpx.AsyncClient(
            transport=_mock_transport({
                "/checkout/org-1/plan/plan-1": (200, {
                    "id": "cs_test_123",
                    "url": "https://checkout.stripe.com/c/pay/cs_test_123",
                    "status": "open",
                }),
            })
        )
        result = await payment_client.create_checkout_session(
            "org-1", "plan-1",
            success_url="https://example.com/success",
            cancel_url="https://example.com/cancel",
        )
        assert result.id == "cs_test_123"
        assert result.status == "open"

    @pytest.mark.asyncio
    async def test_create_portal_session(self, payment_client):
        payment_client.client = httpx.AsyncClient(
            transport=_mock_transport({
                "/portal/org-1/session": (200, {
                    "url": "https://billing.stripe.com/p/session/test",
                    "id": "bps_123",
                }),
            })
        )
        result = await payment_client.create_portal_session("org-1", return_url="https://example.com")
        assert "stripe.com" in result.url

    @pytest.mark.asyncio
    async def test_get_subscriptions(self, payment_client):
        payment_client.client = httpx.AsyncClient(
            transport=_mock_transport({
                "/subscriptions/org-1": (200, {
                    "subscriptions": [{
                        "subscription_id": "sub_1",
                        "org_id": "org-1",
                        "status": "active",
                        "current_period_start": "2026-03-01",
                        "current_period_end": "2026-04-01",
                        "created_at": "2026-03-01",
                    }],
                    "total": 1,
                    "has_more": False,
                }),
            })
        )
        result = await payment_client.get_subscriptions("org-1")
        assert result.total == 1
        assert result.subscriptions[0].status == "active"

    @pytest.mark.asyncio
    async def test_get_payment_methods(self, payment_client):
        payment_client.client = httpx.AsyncClient(
            transport=_mock_transport({
                "/payment-methods/org-1": (200, {
                    "payment_methods": [{
                        "id": "pm_1", "type": "card", "last4": "4242",
                        "brand": "visa", "is_default": True,
                    }],
                }),
            })
        )
        result = await payment_client.get_payment_methods("org-1")
        assert result.payment_methods[0].last4 == "4242"
        assert result.payment_methods[0].is_default is True

    @pytest.mark.asyncio
    async def test_get_invoices(self, payment_client):
        payment_client.client = httpx.AsyncClient(
            transport=_mock_transport({
                "/invoices/org-1/": (200, {
                    "invoices": [{
                        "invoice_id": "inv_1",
                        "invoice_number": "INV-001",
                        "status": "paid",
                        "subtotal": "99.00",
                        "amount_due": "0.00",
                        "amount_paid": "99.00",
                        "total_amount": "99.00",
                        "currency": "usd",
                    }],
                    "count": 1,
                    "has_more": False,
                }),
            })
        )
        result = await payment_client.get_invoices("org-1")
        assert result.count == 1
        assert result.invoices[0].status == "paid"

    @pytest.mark.asyncio
    async def test_error_handling_503(self, payment_client):
        """Unreachable service raises PaymentServiceError with 503."""
        payment_client.client = httpx.AsyncClient(
            transport=_mock_transport({})  # no routes → connect error simulated below
        )
        # Force a connect error by using an invalid transport
        payment_client.base_url = "http://192.0.2.1:9999"  # non-routable IP
        payment_client.client = httpx.AsyncClient(timeout=0.1)
        with pytest.raises(PaymentServiceError) as exc_info:
            await payment_client.get_plans("org-1")
        assert exc_info.value.status_code in (503, 504)

    @pytest.mark.asyncio
    async def test_error_handling_400(self, payment_client):
        """Upstream 400 raises PaymentServiceError with detail."""
        payment_client.client = httpx.AsyncClient(
            transport=_mock_transport({
                "/checkout/org-1/plans": (400, {"detail": "Invalid org_id"}),
            })
        )
        with pytest.raises(PaymentServiceError) as exc_info:
            await payment_client.get_plans("org-1")
        assert exc_info.value.status_code == 400
        assert "Invalid org_id" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_api_key_header(self, payment_client):
        """Verify X-API-Key header is sent for opaque keys."""
        sent_headers = {}

        async def capture_handler(request: httpx.Request) -> httpx.Response:
            sent_headers.update(dict(request.headers))
            return httpx.Response(200, json={"plans": [], "count": 0})

        payment_client.client = httpx.AsyncClient(transport=httpx.MockTransport(capture_handler))
        await payment_client.get_plans("org-1")
        assert "x-api-key" in sent_headers
        assert sent_headers["x-api-key"] == "test_key"


class TestBillingClient:
    @pytest.mark.asyncio
    async def test_get_balance(self, billing_client):
        billing_client.client = httpx.AsyncClient(
            transport=_mock_transport({
                "/billing/org-1/balance": (200, {
                    "balance": "150.00",
                    "available_balance": "125.00",
                    "currency": "USD",
                }),
            })
        )
        result = await billing_client.get_balance("org-1")
        assert result.balance == "150.00"
        assert result.available_balance == "125.00"

    @pytest.mark.asyncio
    async def test_get_transactions(self, billing_client):
        billing_client.client = httpx.AsyncClient(
            transport=_mock_transport({
                "/billing/org-1/transactions/": (200, {
                    "transactions": [
                        {"transaction_id": "tx_1", "type": "credit", "amount": "50.00"},
                    ],
                    "count": 1,
                    "has_more": False,
                }),
            })
        )
        result = await billing_client.get_transactions("org-1")
        assert result.count == 1
        assert result.transactions[0].type == "credit"

    @pytest.mark.asyncio
    async def test_set_tier(self, billing_client):
        billing_client.client = httpx.AsyncClient(
            transport=_mock_transport({
                "/billing/org-1/tier": (200, {
                    "tier_id": "pro",
                    "previous_tier_id": "free",
                    "org_id": "org-1",
                }),
            })
        )
        result = await billing_client.set_tier("org-1", "pro")
        assert result.tier_id == "pro"
        assert result.previous_tier_id == "free"

    @pytest.mark.asyncio
    async def test_error_handling(self, billing_client):
        billing_client.client = httpx.AsyncClient(
            transport=_mock_transport({
                "/billing/org-1/balance": (500, {"detail": "Database error"}),
            })
        )
        with pytest.raises(BillingServiceError) as exc_info:
            await billing_client.get_balance("org-1")
        assert exc_info.value.status_code == 500
