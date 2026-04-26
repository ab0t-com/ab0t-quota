"""Response models for billing and payment proxy routes.

These match the exact response shapes from the billing (8002) and
payment (8005) services. All use extra="allow" so new upstream fields
pass through without breaking consumers.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


# =========================================================================
# Billing Service Models
# =========================================================================

class BillingBalanceResponse(BaseModel):
    balance: str = Field(default="0.00", description="Current balance")
    available_balance: str = Field(default="0.00", description="Available after reservations")
    currency: str = Field(default="usd", description="Currency code")
    model_config = {"extra": "allow"}


class BillingUsageSummaryResponse(BaseModel):
    total_cost: str = Field(default="0.00", description="Total cost this period")
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    model_config = {"extra": "allow"}


class BillingUsageRecord(BaseModel):
    record_id: str = Field(default="", description="Usage record ID")
    resource_type: str = Field(default="", description="compute, storage, etc.")
    cost: str = Field(default="0.00", description="Cost for this record")
    created_at: Optional[str] = None
    model_config = {"extra": "allow"}


class BillingUsageRecordsResponse(BaseModel):
    records: List[BillingUsageRecord] = Field(default_factory=list)
    count: int = 0
    has_more: bool = False
    model_config = {"extra": "allow"}


class BillingTransactionEntry(BaseModel):
    transaction_id: str = Field(..., description="Transaction ID")
    type: str = Field(..., description="credit, debit, reserve, commit, refund")
    amount: str = Field(..., description="Amount")
    description: Optional[str] = None
    created_at: Optional[str] = None
    model_config = {"extra": "allow"}


class BillingTransactionsResponse(BaseModel):
    transactions: List[BillingTransactionEntry] = Field(default_factory=list)
    count: int = 0
    has_more: bool = False
    model_config = {"extra": "allow"}


class TierChangeResponse(BaseModel):
    tier_id: str = Field(..., description="New tier ID")
    previous_tier_id: Optional[str] = Field(None, description="Previous tier ID")
    org_id: str = Field(default="", description="Organization ID")
    model_config = {"extra": "allow"}


class PromotionalCreditResponse(BaseModel):
    org_id: str = Field(..., description="Organization ID")
    old_balance: str = Field(default="0.00", description="Credit balance before")
    new_balance: str = Field(default="0.00", description="Credit balance after")
    available_balance: str = Field(default="0.00", description="Total available balance")
    payment_id: str = Field(default="", description="Idempotent reference (promo:{key})")
    model_config = {"extra": "allow"}


# =========================================================================
# Payment Service Models — Subscriptions
# =========================================================================

class SubscriptionItem(BaseModel):
    subscription_id: str = Field(..., description="Unique subscription ID")
    id: Optional[str] = Field(default=None, description="Alias")
    org_id: str = Field(..., description="Organization ID")
    plan_id: Optional[str] = None
    price_id: Optional[str] = None
    status: str = Field(..., description="active, canceled, past_due, etc.")
    amount: Optional[float] = None
    customer_email: Optional[str] = None
    current_period_start: str = Field(...)
    current_period_end: str = Field(...)
    cancel_at_period_end: bool = False
    canceled_at: Optional[str] = None
    ended_at: Optional[str] = None
    trial_end: Optional[str] = None
    next_billing_date: Optional[str] = None
    created_at: str = Field(...)
    updated_at: Optional[str] = None
    model_config = {"extra": "allow"}


class SubscriptionsResponse(BaseModel):
    subscriptions: List[SubscriptionItem] = Field(default_factory=list)
    total: int = 0
    has_more: bool = False
    model_config = {"extra": "allow"}


class CancelSubscriptionResponse(BaseModel):
    subscription_id: str
    status: str
    cancel_at_period_end: bool
    canceled_at: str
    message: str
    model_config = {"extra": "allow"}


# =========================================================================
# Payment Service Models — Invoices
# =========================================================================

class InvoiceItem(BaseModel):
    invoice_id: str = Field(...)
    invoice_number: str = Field(...)
    status: str = Field(...)
    subtotal: str = Field(...)
    amount_due: str = Field(...)
    amount_paid: str = Field(...)
    total_amount: str = Field(...)
    currency: str = Field(...)
    due_date: Optional[str] = None
    created_at: Optional[str] = None
    pdf_url: Optional[str] = None
    model_config = {"extra": "allow"}


class InvoicesResponse(BaseModel):
    invoices: List[InvoiceItem] = Field(default_factory=list)
    count: int = 0
    has_more: bool = False
    model_config = {"extra": "allow"}


# =========================================================================
# Payment Service Models — Payment Methods
# =========================================================================

class PaymentMethodItem(BaseModel):
    id: str = Field(...)
    type: str = Field(default="card")
    last4: str = ""
    brand: str = ""
    exp_month: Optional[int] = None
    exp_year: Optional[int] = None
    is_default: bool = False
    created_at: Optional[str] = None
    model_config = {"extra": "allow"}


class PaymentMethodsResponse(BaseModel):
    payment_methods: List[PaymentMethodItem] = Field(default_factory=list)
    model_config = {"extra": "allow"}


class PaymentMethodSetDefaultResponse(BaseModel):
    id: str
    is_default: bool
    message: str
    model_config = {"extra": "allow"}


class PaymentMethodDeleteResponse(BaseModel):
    status: str
    deleted: bool
    model_config = {"extra": "allow"}


# =========================================================================
# Payment Service Models — Plans
# =========================================================================

class PlanPrice(BaseModel):
    price_id: str = Field(...)
    amount: float = Field(default=0)
    currency: str = Field(default="usd")
    type: str = Field(default="recurring")
    interval: Optional[str] = None
    interval_count: int = 1
    display_text: Optional[str] = None
    model_config = {"extra": "allow"}


class PlanFeature(BaseModel):
    feature_key: str = Field(default="")
    type: str = Field(default="boolean")
    display_name: str = Field(default="")
    value: Optional[bool | int | float | str] = True
    model_config = {"extra": "allow"}


class PlanItem(BaseModel):
    plan_id: str = Field(...)
    name: str = Field(default="")
    description: Optional[str] = None
    features: List[PlanFeature] = Field(default_factory=list)
    prices: List[PlanPrice] = Field(default_factory=list)
    default_price: Optional[PlanPrice] = None
    trial_period_days: Optional[int] = None
    model_config = {"extra": "allow"}


class PlansResponse(BaseModel):
    plans: List[PlanItem] = Field(default_factory=list)
    count: int = 0
    model_config = {"extra": "allow"}


# =========================================================================
# Payment Service Models — Checkout & Portal
# =========================================================================

class CheckoutSessionResponse(BaseModel):
    id: str = Field(..., description="Stripe session ID")
    url: str = Field(..., description="Redirect URL for Stripe Checkout")
    expires_at: Optional[str] = None
    status: str = Field(..., description="open, complete, expired")
    verification_token: Optional[str] = None
    model_config = {"extra": "allow"}


class CheckoutInitResponse(BaseModel):
    session_token: str = Field(..., description="Anti-fraud session token")
    expires_at: str = Field(..., description="Token expiration (ISO 8601)")
    fingerprint: str = Field(..., description="Browser fingerprint hash")
    model_config = {"extra": "allow"}


class CheckoutVerifyResponse(BaseModel):
    session_id: Optional[str] = None
    status: str = Field(default="unknown", description="complete, paid, open, expired")
    payment_status: Optional[str] = None
    customer_email: Optional[str] = None
    amount_total: Optional[int] = None
    currency: Optional[str] = None
    mode: Optional[str] = None
    metadata: Optional[dict[str, str]] = None
    model_config = {"extra": "allow"}


class PortalSessionResponse(BaseModel):
    url: str = Field(..., description="Stripe Customer Portal URL")
    id: str = Field(..., description="Portal session ID")
    model_config = {"extra": "allow"}


class WebhookResponse(BaseModel):
    status: str = Field(default="ok")
    model_config = {"extra": "allow"}
