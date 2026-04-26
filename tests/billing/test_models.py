"""Tests for billing module Pydantic models.

Validates that all response models correctly parse upstream service
responses, handle missing fields gracefully, and pass through unknown
fields via extra='allow'.
"""

import pytest

from ab0t_quota.billing.models import (
    BillingBalanceResponse,
    BillingTransactionEntry,
    BillingTransactionsResponse,
    BillingUsageRecord,
    BillingUsageRecordsResponse,
    BillingUsageSummaryResponse,
    CancelSubscriptionResponse,
    CheckoutInitResponse,
    CheckoutSessionResponse,
    CheckoutVerifyResponse,
    InvoiceItem,
    InvoicesResponse,
    PaymentMethodDeleteResponse,
    PaymentMethodItem,
    PaymentMethodSetDefaultResponse,
    PaymentMethodsResponse,
    PlanFeature,
    PlanItem,
    PlanPrice,
    PlansResponse,
    PortalSessionResponse,
    SubscriptionItem,
    SubscriptionsResponse,
    TierChangeResponse,
    WebhookResponse,
)


class TestBillingModels:
    def test_balance_defaults(self):
        b = BillingBalanceResponse()
        assert b.balance == "0.00"
        assert b.currency == "usd"

    def test_balance_from_upstream(self):
        b = BillingBalanceResponse.model_validate({
            "balance": "125.50",
            "available_balance": "100.00",
            "currency": "USD",
            "reserved_balance": "25.50",  # extra field — should pass through
        })
        assert b.balance == "125.50"
        assert b.available_balance == "100.00"

    def test_transaction_entry(self):
        t = BillingTransactionEntry.model_validate({
            "transaction_id": "tx_123",
            "type": "credit",
            "amount": "50.00",
            "description": "Top-up",
            "created_at": "2026-03-30T12:00:00Z",
        })
        assert t.transaction_id == "tx_123"
        assert t.type == "credit"

    def test_transactions_response_empty(self):
        r = BillingTransactionsResponse()
        assert r.transactions == []
        assert r.count == 0
        assert r.has_more is False

    def test_tier_change(self):
        t = TierChangeResponse.model_validate({
            "tier_id": "pro",
            "previous_tier_id": "free",
            "org_id": "org-1",
        })
        assert t.tier_id == "pro"
        assert t.previous_tier_id == "free"


class TestPaymentModels:
    def test_subscription_item(self):
        s = SubscriptionItem.model_validate({
            "subscription_id": "sub_123",
            "org_id": "org-1",
            "status": "active",
            "current_period_start": "2026-03-01T00:00:00Z",
            "current_period_end": "2026-04-01T00:00:00Z",
            "created_at": "2026-03-01T00:00:00Z",
        })
        assert s.subscription_id == "sub_123"
        assert s.status == "active"
        assert s.cancel_at_period_end is False

    def test_subscriptions_response(self):
        r = SubscriptionsResponse.model_validate({
            "subscriptions": [],
            "total": 0,
            "has_more": False,
        })
        assert len(r.subscriptions) == 0

    def test_invoice_item(self):
        i = InvoiceItem.model_validate({
            "invoice_id": "inv_123",
            "invoice_number": "INV-001",
            "status": "paid",
            "subtotal": "99.00",
            "amount_due": "0.00",
            "amount_paid": "99.00",
            "total_amount": "99.00",
            "currency": "usd",
        })
        assert i.invoice_id == "inv_123"
        assert i.status == "paid"
        assert i.pdf_url is None

    def test_payment_method_item(self):
        m = PaymentMethodItem.model_validate({
            "id": "pm_123",
            "type": "card",
            "last4": "4242",
            "brand": "visa",
            "exp_month": 12,
            "exp_year": 2028,
            "is_default": True,
        })
        assert m.last4 == "4242"
        assert m.is_default is True

    def test_payment_methods_response_empty(self):
        r = PaymentMethodsResponse()
        assert r.payment_methods == []


class TestPlanModels:
    def test_plan_price(self):
        p = PlanPrice.model_validate({
            "price_id": "price_123",
            "amount": 29.0,
            "currency": "usd",
            "type": "recurring",
            "interval": "month",
        })
        assert p.amount == 29.0
        assert p.interval == "month"

    def test_plan_feature(self):
        f = PlanFeature.model_validate({
            "feature_key": "gpu_access",
            "type": "boolean",
            "display_name": "GPU Access",
            "value": True,
        })
        assert f.feature_key == "gpu_access"

    def test_plan_item(self):
        p = PlanItem.model_validate({
            "plan_id": "plan_123",
            "name": "Starter",
            "description": "For small teams",
            "features": [
                {"feature_key": "basic", "type": "boolean", "display_name": "Basic", "value": True},
            ],
            "prices": [
                {"price_id": "price_1", "amount": 29, "currency": "usd", "type": "recurring", "interval": "month"},
            ],
        })
        assert p.name == "Starter"
        assert len(p.features) == 1
        assert len(p.prices) == 1
        assert p.prices[0].amount == 29

    def test_plans_response(self):
        r = PlansResponse.model_validate({
            "plans": [
                {"plan_id": "p1", "name": "Free"},
                {"plan_id": "p2", "name": "Pro"},
            ],
            "count": 2,
        })
        assert r.count == 2
        assert r.plans[0].name == "Free"
        assert r.plans[1].plan_id == "p2"


class TestCheckoutModels:
    def test_checkout_session(self):
        s = CheckoutSessionResponse.model_validate({
            "id": "cs_test_123",
            "url": "https://checkout.stripe.com/c/pay/cs_test_123",
            "status": "open",
            "expires_at": "2026-03-31T12:00:00Z",
        })
        assert s.id == "cs_test_123"
        assert "stripe.com" in s.url

    def test_checkout_init(self):
        i = CheckoutInitResponse.model_validate({
            "session_token": "eyJ...",
            "expires_at": "2026-03-31T12:30:00Z",
            "fingerprint": "abc123",
        })
        assert i.fingerprint == "abc123"

    def test_checkout_verify(self):
        v = CheckoutVerifyResponse.model_validate({
            "session_id": "cs_test_123",
            "status": "complete",
            "payment_status": "paid",
            "customer_email": "user@example.com",
            "metadata": {"org_id": "org-1", "plan_id": "plan-1"},
        })
        assert v.status == "complete"
        assert v.metadata["plan_id"] == "plan-1"

    def test_checkout_verify_minimal(self):
        v = CheckoutVerifyResponse.model_validate({"status": "open"})
        assert v.status == "open"
        assert v.customer_email is None

    def test_portal_session(self):
        p = PortalSessionResponse.model_validate({
            "url": "https://billing.stripe.com/p/session/test_123",
            "id": "bps_123",
        })
        assert "stripe.com" in p.url

    def test_webhook_response(self):
        w = WebhookResponse.model_validate({"status": "ok", "event_id": "evt_123"})
        assert w.status == "ok"


class TestExtraFieldsPassThrough:
    """All models use extra='allow' — unknown fields must not be rejected."""

    def test_balance_extra(self):
        b = BillingBalanceResponse.model_validate({
            "balance": "0", "future_field": "works",
        })
        assert b.balance == "0"

    def test_subscription_extra(self):
        s = SubscriptionItem.model_validate({
            "subscription_id": "s1", "org_id": "o1", "status": "active",
            "current_period_start": "x", "current_period_end": "y",
            "created_at": "z",
            "pause_collection": {"behavior": "void"},
            "custom_field": 42,
        })
        assert s.subscription_id == "s1"

    def test_plan_item_extra(self):
        p = PlanItem.model_validate({
            "plan_id": "p1",
            "stripe_product_id": "prod_xyz",
        })
        assert p.plan_id == "p1"
