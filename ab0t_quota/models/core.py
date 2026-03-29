"""
Core domain models for the quota system.

These are the foundational types that every other module builds on.
They define what a resource is, how limits are structured, and how
quota state is tracked.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field, model_validator


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

    def get_limit(self, resource_key: str) -> TierLimits:
        """Get limits for a resource, defaulting to unlimited if not defined."""
        return self.limits.get(resource_key, TierLimits())


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
        default_factory=datetime.utcnow,
    )

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.utcnow() > self.expires_at


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
    timestamp: datetime = Field(default_factory=datetime.utcnow)
