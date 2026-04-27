"""Response models for billing and payment proxy routes.

These match the exact response shapes from the billing (8002) and
payment (8005) services. All use extra="allow" so new upstream fields
pass through without breaking consumers.
"""

from __future__ import annotations

from typing import Any, List, Optional

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
    """Billing's actual /billing/usage/{org}/summary shape:
       {org_id, start_date, end_date, period, summary: {total_cost, ...},
        group_by}
    Surface the most-used fields at top-level too via model_post_init so
    legacy callers reading `.total_cost` directly keep working."""
    org_id: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    period: Optional[str] = None
    summary: Optional[dict] = None
    group_by: Optional[Any] = None
    # Convenience top-level fields (mirrored from summary{} below)
    total_cost: str = Field(default="0.00", description="Total cost this period")
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    model_config = {"extra": "allow"}

    def model_post_init(self, __context):
        if self.summary:
            tc = self.summary.get("total_cost")
            if tc is not None and self.total_cost == "0.00":
                self.total_cost = str(tc)
            if not self.period_start:
                self.period_start = self.summary.get("period_start") or self.start_date
            if not self.period_end:
                self.period_end = self.summary.get("period_end") or self.end_date


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
    """Billing transaction entry. Field naming matches billing's actual
    /billing/{org}/transactions response (id + debit/credit, not the
    earlier transaction_id + amount). All fields optional so future
    schema additions in billing flow through via extra='allow'."""
    id: Optional[str] = Field(default=None, description="Transaction id")
    type: Optional[str] = Field(default=None, description="balance, credit, debit, reserve, commit, refund")
    timestamp: Optional[str] = None
    description: Optional[str] = None
    debit: Optional[str] = None
    credit: Optional[str] = None
    balance: Optional[str] = None
    status: Optional[str] = None
    metadata: Optional[dict] = None
    # Backward-compat aliases for older billing schemas
    transaction_id: Optional[str] = None
    amount: Optional[str] = None
    created_at: Optional[str] = None
    model_config = {"extra": "allow"}


class BillingTransactionsResponse(BaseModel):
    """Billing's actual /billing/{org}/transactions shape:
       {transactions: [...], summary: {opening_balance, total_debits, ...}}
    NO count or has_more in the actual response — derive count from len()
    and surface summary as a structured dict."""
    transactions: List[BillingTransactionEntry] = Field(default_factory=list)
    summary: Optional[dict] = Field(default=None, description="Period summary (opening/closing balance, debits, credits)")
    # Convenience derived fields for legacy consumers
    count: int = 0
    has_more: bool = False
    model_config = {"extra": "allow"}

    def model_post_init(self, __context):
        if self.count == 0 and self.transactions:
            self.count = len(self.transactions)


class TierChangeResponse(BaseModel):
    """Billing returns this on PUT /billing/{org}/tier.
    Field names match billing's actual response: new_tier + previous_tier
    (NOT tier_id + previous_tier_id from earlier drafts of the contract).
    Provides backward-compat aliases for callers that read the old names."""
    org_id: str = Field(default="", description="Organization ID")
    new_tier: Optional[str] = Field(default=None, description="Newly-assigned tier id")
    previous_tier: Optional[str] = Field(default=None, description="Tier before this change")
    new_tier_display: Optional[str] = None
    changed_at: Optional[str] = None
    # Backward-compat aliases for callers that read the old names
    tier_id: Optional[str] = Field(default=None, description="DEPRECATED — use new_tier")
    previous_tier_id: Optional[str] = Field(default=None, description="DEPRECATED — use previous_tier")
    model_config = {"extra": "allow"}

    def model_post_init(self, __context):
        # Mirror the actual fields into the deprecated aliases so older
        # consumers that read .tier_id keep working.
        if self.tier_id is None and self.new_tier:
            self.tier_id = self.new_tier
        if self.previous_tier_id is None and self.previous_tier:
            self.previous_tier_id = self.previous_tier


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
