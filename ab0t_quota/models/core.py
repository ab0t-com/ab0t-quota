"""
Core domain models for the quota system.

These are the foundational types that every other module builds on.
They define what a resource is, how limits are structured, and how
quota state is tracked.
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal
from enum import Enum
from typing import Literal, Optional
from datetime import datetime, timezone
from pydantic import BaseModel, Field, model_validator

try:  # pragma: no cover - structlog optional at import time
    import structlog  # type: ignore
    _log = structlog.get_logger(__name__)
except Exception:  # pragma: no cover
    _log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class CounterType(str, Enum):
    """How a resource's usage is counted.

    GAUGE:       Current level, incremented and decremented explicitly.
                 Example: concurrent sandboxes, active CPU cores.

    RATE:        Sliding-window counter, auto-expires.
                 Example: API requests per hour.

    ACCUMULATOR: Monotonically increasing within a reset period.
                 Example: monthly spend in dollars.
    """
    GAUGE = "gauge"
    RATE = "rate"
    ACCUMULATOR = "accumulator"


class ResetPeriod(str, Enum):
    """Calendar-aligned reset schedule for ACCUMULATOR counters."""
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    NEVER = "never"


class AlertSeverity(str, Enum):
    """Quota usage alert levels."""
    INFO = "info"           # usage noted, no action needed
    WARNING = "warning"     # approaching limit (>= 80%)
    CRITICAL = "critical"   # at or near limit (>= 95%)
    EXCEEDED = "exceeded"   # over limit (blocked)


# ---------------------------------------------------------------------------
# Resource Definition
# ---------------------------------------------------------------------------

class ResourceDef(BaseModel):
    """Defines a countable resource within a service.

    Each service registers its own resources. The combination of
    (service, resource_key) is globally unique.

    Examples:
        ResourceDef(
            service="sandbox-platform",
            resource_key="sandbox.concurrent",
            display_name="Concurrent Sandboxes",
            counter_type=CounterType.GAUGE,
            unit="sandboxes",
        )
        ResourceDef(
            service="api-gateway",
            resource_key="api.requests_per_hour",
            display_name="API Requests / Hour",
            counter_type=CounterType.RATE,
            unit="requests",
            window_seconds=3600,
        )
        ResourceDef(
            service="sandbox-platform",
            resource_key="sandbox.monthly_cost",
            display_name="Monthly Compute Spend",
            counter_type=CounterType.ACCUMULATOR,
            unit="USD",
            reset_period=ResetPeriod.MONTHLY,
            precision=2,
        )
    """
    service: str = Field(
        ...,
        description="Owning service name (e.g. 'sandbox-platform', 'resource-service')",
    )
    resource_key: str = Field(
        ...,
        pattern=r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$",
        description="Dot-separated resource identifier (e.g. 'sandbox.concurrent')",
    )
    display_name: str = Field(
        ...,
        description="Human-readable name shown in dashboards and 429 responses",
    )
    description: Optional[str] = Field(
        default=None,
        description="Longer description for admin UIs and docs",
    )
    counter_type: CounterType = Field(
        ...,
        description="How usage is counted (gauge, rate, or accumulator)",
    )
    unit: str = Field(
        default="units",
        description="Unit label (e.g. 'sandboxes', 'requests', 'USD', 'cores')",
    )

    # Rate-specific
    window_seconds: Optional[int] = Field(
        default=None,
        ge=1,
        description="Sliding window size in seconds. Required when counter_type=RATE.",
    )

    # Accumulator-specific
    reset_period: Optional[ResetPeriod] = Field(
        default=None,
        description="When the accumulator resets. Required when counter_type=ACCUMULATOR.",
    )

    # Numeric precision (for cost-type accumulators)
    precision: int = Field(
        default=0,
        ge=0,
        description="Decimal places for the counter value (0=integer, 2=dollars.cents)",
    )

    @model_validator(mode="after")
    def _check_counter_type_requirements(self):
        if self.counter_type == CounterType.RATE and self.window_seconds is None:
            raise ValueError("window_seconds is required for RATE counters")
        if self.counter_type == CounterType.ACCUMULATOR and self.reset_period is None:
            raise ValueError("reset_period is required for ACCUMULATOR counters")
        return self

    @property
    def fully_qualified_key(self) -> str:
        """Globally unique key: service + resource_key."""
        return f"{self.service}:{self.resource_key}"


# ---------------------------------------------------------------------------
# Tier & Limits
# ---------------------------------------------------------------------------

class TierLimits(BaseModel):
    """Limits for a single resource within a tier.

    A None value means unlimited (no enforcement).
    """
    limit: Optional[float] = Field(
        default=None,
        description="Maximum allowed value. None = unlimited.",
    )
    warning_threshold: float = Field(
        default=0.80,
        ge=0.0,
        le=1.0,
        description="Fraction of limit at which WARNING alert fires (default 80%)",
    )
    critical_threshold: float = Field(
        default=0.95,
        ge=0.0,
        le=1.0,
        description="Fraction of limit at which CRITICAL alert fires (default 95%)",
    )
    burst_allowance: Optional[float] = Field(
        default=None,
        description="Temporary allowance above limit for RATE counters (soft cap).",
    )
    per_user_limit: Optional[float] = Field(
        default=None,
        description="Per-user sub-limit within the org. None = no per-user limit "
                    "(user can consume entire org quota). Only checked when user_id "
                    "is provided in the request.",
    )

    @property
    def is_unlimited(self) -> bool:
        return self.limit is None


# ---------------------------------------------------------------------------
# Billing relationship — schema (decided in ticket
# 20260516_paid_plan_balance_model_gap, context_03)
#
# The library defines the SCHEMA and library-wide DEFAULTS. Consumers
# declare per-tier policy in their own quota-config.json. The library
# never hardcodes a dollar amount, tier name, or consumer-specific rule.
# ---------------------------------------------------------------------------

class BillingModel(str, Enum):
    """How a tier relates to money. Default is `capacity_only` — safest
    possible: quota enforcement, no money side-effects from the
    subscription itself. Consumers opt into money flows by declaring a
    non-default `billing_model`.
    """
    CAPACITY_ONLY = "capacity_only"
    CONSUMPTION_ONLY = "consumption_only"
    SUBSCRIPTION_WITH_CREDITS = "subscription_with_credits"
    SUBSCRIPTION_UNLOCK_ONLY = "subscription_unlock_only"
    # Experimental — schema-advertised but runtime-incomplete. Rejected by
    # TierConfig validation unless AB0T_QUOTA_ALLOW_EXPERIMENTAL_BILLING_MODELS=true.
    # See TierConfig._validate_billing_config below.
    SUBSCRIPTION_WITH_OVERAGE = "subscription_with_overage"
    SEAT_BASED = "seat_based"
    METERED = "metered"


# Experimental billing models gated by AB0T_QUOTA_ALLOW_EXPERIMENTAL_BILLING_MODELS.
# Reason: the enum advertises them so the schema is forward-compatible, but
# no runtime path exists yet for overage metering, seat counting, or pure
# metered billing. A consumer who declares one of these without the opt-in
# would silently get a no-op tier and never know — that violates the drop-in
# promise. Fail loud at load time instead.
_EXPERIMENTAL_BILLING_MODELS: frozenset[BillingModel] = frozenset({
    BillingModel.SUBSCRIPTION_WITH_OVERAGE,
    BillingModel.SEAT_BASED,
    BillingModel.METERED,
})


class CreditTrigger(str, Enum):
    """When a `credit_grant` fires."""
    SIGNUP = "signup"
    SUBSCRIPTION_INVOICE_PAID = "subscription_invoice_paid"
    SCHEDULED_PERIOD_START = "scheduled_period_start"
    MANUAL = "manual"
    WEBHOOK_ADMIN = "webhook_admin"


class CreditLifecycle(str, Enum):
    """What happens to a credit_grant at the next period boundary."""
    PERSISTENT = "persistent"
    USE_IT_OR_LOSE_IT = "use_it_or_lose_it"
    ROLLOVER_UNLIMITED = "rollover_unlimited"
    ROLLOVER_CAPPED = "rollover_capped"


class CreditDestination(str, Enum):
    """Which ledger field receives the grant."""
    BALANCE = "balance"
    CREDIT_BALANCE = "credit_balance"
    SUBSCRIPTION_CREDIT = "subscription_credit"


class BillingPeriod(str, Enum):
    """Period over which a price or grant is denominated."""
    MONTH = "month"
    YEAR = "year"


class Price(BaseModel):
    """Recurring price for a paid tier. Library-agnostic to provider —
    consumer wires this to Stripe (or any other PSP) themselves.
    """
    amount_per_period: Decimal = Field(
        ...,
        gt=Decimal("0"),
        description="Amount charged per period (in `currency`). Must be > 0.",
    )
    currency: str = Field(
        default="USD",
        pattern=r"^[A-Z]{3}$",
        description="ISO-4217 currency code.",
    )
    period: BillingPeriod = Field(
        default=BillingPeriod.MONTH,
        description="Billing period.",
    )


class CreditGrant(BaseModel):
    """Per-tier declaration of when and how credit is granted to an org.

    Fields with library-level defaults track context_03 decisions D2 + D3.
    Consumers override per-tier; library never declares an amount.
    """
    trigger: CreditTrigger = Field(
        ...,
        description="When the grant fires. The library's webhook/event "
                    "handlers for each trigger value carry the grant out.",
    )
    amount_per_period: Decimal = Field(
        ...,
        gt=Decimal("0"),
        description="Money granted on each trigger fire. Consumer-declared; "
                    "library never hardcodes amounts.",
    )
    currency: str = Field(
        default="USD",
        pattern=r"^[A-Z]{3}$",
    )
    # D2 — library default. Matches consumer-SaaS norm (OpenAI/Vercel/
    # Cloudflare). Consumers can override per-tier.
    lifecycle: CreditLifecycle = Field(
        default=CreditLifecycle.USE_IT_OR_LOSE_IT,
        description="What happens to unused balance at each new grant. "
                    "Library default is USE_IT_OR_LOSE_IT (consumer-SaaS norm).",
    )
    rollover_max_periods: Optional[int] = Field(
        default=None,
        ge=1,
        description="Cap for ROLLOVER_CAPPED lifecycle: max periods' worth of "
                    "credit allowed to accumulate. Required only when "
                    "lifecycle == ROLLOVER_CAPPED.",
    )
    destination: CreditDestination = Field(
        default=CreditDestination.SUBSCRIPTION_CREDIT,
        description="Which ledger field receives the grant. SIGNUP-style "
                    "grants typically use CREDIT_BALANCE; subscription "
                    "grants typically use SUBSCRIPTION_CREDIT.",
    )
    reset_on_downgrade: bool = Field(
        default=True,
        description="When org moves to a strictly-lower tier, zero this "
                    "grant's destination field. Default true (downgrade is "
                    "voluntary, less surprising).",
    )
    # D3 — library default false. Tier upgrades preserve existing
    # subscription_credit; the next invoice adds on top.
    reset_on_upgrade: bool = Field(
        default=False,
        description="When org moves to a higher tier, zero this grant's "
                    "destination field. Default false (avoid surprise wipes "
                    "during voluntary upgrades).",
    )
    # v0.2.7 — controls how the lib's @idempotent default handler keys its
    # business dedup. Different consumers want different semantics:
    #   per_user_per_tier (default) — anti-farming, one credit per user per tier
    #   per_org_per_tier            — B2B "credit per company per tier"
    #   per_user_global             — strongest: one human, one credit, ever
    #   per_org_global              — one org, one credit, ever
    dedup: str = Field(
        default="per_user_per_tier",
        pattern=r"^(per_user_per_tier|per_org_per_tier|per_user_global|per_org_global)$",
        description="Business-dedup policy for the lib's default credit-grant handler. "
                    "See tickets/20260428_idempotency_replay_framework/TICKET.md.",
    )

    @model_validator(mode="after")
    def _validate_rollover_cap(self) -> "CreditGrant":
        if self.lifecycle == CreditLifecycle.ROLLOVER_CAPPED and self.rollover_max_periods is None:
            raise ValueError(
                "rollover_max_periods is required when lifecycle == 'rollover_capped'"
            )
        if self.lifecycle != CreditLifecycle.ROLLOVER_CAPPED and self.rollover_max_periods is not None:
            raise ValueError(
                "rollover_max_periods only applies to lifecycle == 'rollover_capped'"
            )
        return self


class TierConfig(BaseModel):
    """A named tier with limits for every resource it governs.

    The key in `limits` is a resource_key (e.g. 'sandbox.concurrent').
    Resources not listed inherit no limit (unlimited).
    """
    tier_id: str = Field(
        ...,
        pattern=r"^[a-z][a-z0-9_-]*$",
        description="Tier identifier (e.g. 'free', 'starter', 'pro', 'enterprise')",
    )
    display_name: str = Field(
        ...,
        description="Human-readable tier name (e.g. 'Starter Plan')",
    )
    description: Optional[str] = None
    sort_order: int = Field(
        default=0,
        description="Ordering for UI (0=lowest tier)",
    )
    limits: dict[str, TierLimits] = Field(
        default_factory=dict,
        description="Resource key → TierLimits mapping",
    )
    features: set[str] = Field(
        default_factory=set,
        description="Feature flags enabled on this tier (e.g. 'gpu_access', 'sso', 'audit_logs')",
    )
    upgrade_url: Optional[str] = Field(
        default=None,
        description="URL to upgrade from this tier (shown in 429 responses)",
    )
    default_per_user_fraction: Optional[float] = Field(
        default=None,
        gt=0.0,
        le=1.0,
        description="Default fraction of an org-level GAUGE limit that any "
                    "single user in the org may consume. Applied only when a "
                    "resource's TierLimits has no explicit per_user_limit. "
                    "Example: 0.5 means one user can take at most half the org "
                    "quota, preventing one team member from exhausting the org. "
                    "None disables the default.",
    )

    # -----------------------------------------------------------------------
    # Billing-relationship fields (see context_03_billing_model_decision.md
    # in ticket 20260516_paid_plan_balance_model_gap)
    #
    # D1: schema is the same shape for every consumer; choices are made by
    #     declarative per-tier config in the consumer's quota-config.json.
    # D5: library DEFINES schema + defaults; library NEVER hardcodes a
    #     consumer-specific amount or policy.
    # -----------------------------------------------------------------------

    billing_model: BillingModel = Field(
        default=BillingModel.CAPACITY_ONLY,
        description="How this tier relates to money. Default CAPACITY_ONLY: "
                    "limits only, no money side-effects from the tier itself. "
                    "Consumers opt into money flows by setting this to a "
                    "non-default value AND configuring the matching fields "
                    "(price, credit_grant).",
    )
    price: Optional[Price] = Field(
        default=None,
        description="Recurring price for the tier. Required for paid "
                    "subscription billing models; absent for free / "
                    "consumption-only / capacity-only.",
    )
    credit_grant: Optional[CreditGrant] = Field(
        default=None,
        description="Per-tier credit-grant policy. When set, the library "
                    "fires the configured trigger and writes the configured "
                    "amount/destination. When unset (default), no automatic "
                    "credit grants happen for this tier.",
    )

    # Back-compat alias for the legacy `initial_credit: <float>` shape on
    # free-tier configs. If `credit_grant` is unset and `initial_credit` is
    # set, the validator below synthesizes a CreditGrant from it.
    #
    # New configs should use `credit_grant` directly; this field exists
    # purely to avoid breaking existing consumer quota-config.json files.
    initial_credit: Optional[Decimal] = Field(
        default=None,
        gt=Decimal("0"),
        description="DEPRECATED back-compat shim. Use `credit_grant` instead. "
                    "When present without `credit_grant`, synthesizes a "
                    "signup-trigger persistent grant to credit_balance.",
    )

    def get_limit(self, resource_key: str) -> TierLimits:
        """Get limits for a resource, defaulting to unlimited if not defined."""
        return self.limits.get(resource_key, TierLimits())

    def derive_per_user_limit(self, tl: TierLimits) -> Optional[float]:
        """Resolve the effective per-user limit for a TierLimits entry.

        Precedence:
          1. Explicit `tl.per_user_limit` always wins.
          2. Otherwise, if the tier has `default_per_user_fraction` AND the
             org limit is finite (not None), derive ceil(limit * fraction).
          3. Otherwise None (no per-user enforcement).
        """
        if tl.per_user_limit is not None:
            return tl.per_user_limit
        if self.default_per_user_fraction is None or tl.limit is None:
            return None
        import math
        derived = math.ceil(tl.limit * self.default_per_user_fraction)
        # Never derive a per-user cap below 1 — would block all users
        return max(1.0, float(derived))

    # -----------------------------------------------------------------------
    # Cross-field validation for billing-relationship config.
    # Runs AFTER individual fields are validated. A misconfigured tier
    # fails LOAD time, not first-spend time.
    # -----------------------------------------------------------------------
    @model_validator(mode="after")
    def _validate_billing_config(self) -> "TierConfig":
        # Back-compat: synthesize a CreditGrant from the legacy
        # `initial_credit: <float>` shape on free-tier configs.
        # New configs should declare `credit_grant` directly.
        if self.initial_credit is not None and self.credit_grant is None:
            self.credit_grant = CreditGrant(
                trigger=CreditTrigger.SIGNUP,
                amount_per_period=self.initial_credit,
                currency="USD",
                lifecycle=CreditLifecycle.PERSISTENT,
                destination=CreditDestination.CREDIT_BALANCE,
                reset_on_downgrade=False,
                reset_on_upgrade=False,
            )

        # If both are set, the explicit credit_grant wins (back-compat
        # alias is only a default for the unset case). The `initial_credit`
        # value is kept on the model for audit but the synthesized grant
        # is not re-derived — explicit always wins over implicit.

        bm = self.billing_model

        # Experimental-model gate. These enum values exist for schema
        # forward-compat but have no runtime implementation yet. A consumer
        # configuring one would otherwise get a silent no-op tier — breaking
        # the drop-in promise that "configured behaviour actually happens".
        # Fail load by default; let advanced consumers opt in explicitly.
        if bm in _EXPERIMENTAL_BILLING_MODELS:
            opt_in = os.environ.get(
                "AB0T_QUOTA_ALLOW_EXPERIMENTAL_BILLING_MODELS", ""
            ).strip().lower() in ("1", "true", "yes", "on")
            if not opt_in:
                raise ValueError(
                    f"tier '{self.tier_id}': billing_model '{bm.value}' is "
                    f"experimental and not runtime-supported. Set env var "
                    f"AB0T_QUOTA_ALLOW_EXPERIMENTAL_BILLING_MODELS=true to "
                    f"opt in, or pick one of: capacity_only, consumption_only, "
                    f"subscription_with_credits, subscription_unlock_only."
                )
            try:
                _log.warning(
                    "experimental_billing_model_in_use",
                    tier_id=self.tier_id,
                    billing_model=bm.value,
                    note="runtime is incomplete; behaviour may change",
                )
            except TypeError:
                _log.warning(
                    "experimental billing_model '%s' on tier '%s' — runtime "
                    "incomplete; behaviour may change",
                    bm.value,
                    self.tier_id,
                )

        # subscription_with_credits — must declare a credit_grant whose
        # trigger is invoice-paid; price is required so we know what the
        # subscription costs (even if credit amount differs from price).
        if bm == BillingModel.SUBSCRIPTION_WITH_CREDITS:
            if self.price is None:
                raise ValueError(
                    f"tier '{self.tier_id}': billing_model 'subscription_with_credits' "
                    f"requires `price`"
                )
            if self.credit_grant is None:
                raise ValueError(
                    f"tier '{self.tier_id}': billing_model 'subscription_with_credits' "
                    f"requires `credit_grant`"
                )
            if self.credit_grant.trigger != CreditTrigger.SUBSCRIPTION_INVOICE_PAID:
                raise ValueError(
                    f"tier '{self.tier_id}': billing_model 'subscription_with_credits' "
                    f"requires credit_grant.trigger == 'subscription_invoice_paid'"
                )

        # subscription_unlock_only — paid tier with no auto-credit. Price
        # required; credit_grant must be absent (otherwise consumer should
        # use subscription_with_credits).
        elif bm == BillingModel.SUBSCRIPTION_UNLOCK_ONLY:
            if self.price is None:
                raise ValueError(
                    f"tier '{self.tier_id}': billing_model 'subscription_unlock_only' "
                    f"requires `price`"
                )
            if self.credit_grant is not None:
                raise ValueError(
                    f"tier '{self.tier_id}': billing_model 'subscription_unlock_only' "
                    f"must NOT have `credit_grant` — use 'subscription_with_credits' instead"
                )

        # consumption_only — no recurring subscription concept, so no
        # `price` (would be misleading). May still have a signup credit
        # grant (degenerate "free tier with initial credit" case).
        elif bm == BillingModel.CONSUMPTION_ONLY:
            if self.price is not None:
                raise ValueError(
                    f"tier '{self.tier_id}': billing_model 'consumption_only' "
                    f"must NOT have `price` — use 'subscription_unlock_only' "
                    f"or 'subscription_with_credits' for paid tiers"
                )
            if (self.credit_grant is not None
                    and self.credit_grant.trigger == CreditTrigger.SUBSCRIPTION_INVOICE_PAID):
                raise ValueError(
                    f"tier '{self.tier_id}': billing_model 'consumption_only' "
                    f"cannot use trigger 'subscription_invoice_paid' "
                    f"(no subscription invoices to trigger on)"
                )

        # capacity_only — pure quota tier. May have a price (e.g. paid
        # tier that just unlocks limits) OR be free. credit_grant on a
        # capacity_only tier is unusual but not forbidden — could be a
        # signup grant on a free capacity-only tier.

        return self


# ---------------------------------------------------------------------------
# Per-Org Overrides
# ---------------------------------------------------------------------------

class QuotaOverride(BaseModel):
    """Per-org override for a specific resource, superseding the tier limit.

    Used for enterprise customers with negotiated limits or temporary
    capacity increases.
    """
    org_id: str
    resource_key: str
    limit: Optional[float] = Field(
        description="Override limit. None = unlimited.",
    )
    reason: Optional[str] = Field(
        default=None,
        description="Why the override exists (e.g. 'Enterprise contract #1234')",
    )
    expires_at: Optional[datetime] = Field(
        default=None,
        description="When the override expires (None = permanent)",
    )
    created_by: Optional[str] = Field(
        default=None,
        description="User ID of the admin who created the override",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) > self.expires_at


# ---------------------------------------------------------------------------
# Quota State (read model)
# ---------------------------------------------------------------------------

class QuotaState(BaseModel):
    """Current usage state for a single resource within an org.

    This is a read model — populated by the engine from Redis counters.
    """
    org_id: str
    resource_key: str
    current: float = Field(
        default=0.0,
        description="Current usage value",
    )
    limit: Optional[float] = Field(
        default=None,
        description="Effective limit (tier limit or override, whichever applies)",
    )
    tier_id: str = Field(
        default="free",
    )
    has_override: bool = Field(
        default=False,
        description="Whether an org-specific override is active",
    )

    @property
    def utilization(self) -> Optional[float]:
        """Usage as fraction of limit (0.0–1.0+). None if unlimited."""
        if self.limit is None or self.limit == 0:
            return None
        return self.current / self.limit

    @property
    def remaining(self) -> Optional[float]:
        """How much headroom remains. None if unlimited. Can be negative."""
        if self.limit is None:
            return None
        return self.limit - self.current

    @property
    def severity(self) -> AlertSeverity:
        """Current alert severity based on utilization."""
        util = self.utilization
        if util is None:
            return AlertSeverity.INFO
        if util >= 1.0:
            return AlertSeverity.EXCEEDED
        if util >= 0.95:
            return AlertSeverity.CRITICAL
        if util >= 0.80:
            return AlertSeverity.WARNING
        return AlertSeverity.INFO


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

class QuotaAlert(BaseModel):
    """An alert generated when quota usage crosses a threshold."""
    org_id: str
    resource_key: str
    severity: AlertSeverity
    current: float
    limit: float
    utilization: float = Field(description="0.0–1.0+")
    tier_id: str
    message: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
