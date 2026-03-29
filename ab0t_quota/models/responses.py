"""
Response models — outputs from the QuotaEngine.

QuotaResult is the primary return type. It is designed to be directly
serializable into a 429 response body so services don't need to
format their own error payloads.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional
from datetime import datetime, timezone
from pydantic import BaseModel, Field, computed_field

from .core import AlertSeverity, TierLimits


class QuotaDecision(str, Enum):
    """Outcome of a quota check."""
    ALLOW = "allow"             # under limit, proceed
    ALLOW_WARNING = "allow_warning"  # under limit but approaching — proceed with caution
    DENY = "deny"               # over limit, reject the request
    UNLIMITED = "unlimited"     # no limit configured for this resource


class QuotaResult(BaseModel):
    """Result of a single quota check.

    Designed to be returned directly as a 429 response body when denied,
    or logged when allowed with warnings.

    Usage:
        result = await engine.check(request)
        if result.denied:
            raise HTTPException(status_code=429, detail=result.to_api_error())
        if result.warning:
            logger.warning("quota_warning", **result.model_dump())
    """
    decision: QuotaDecision
    resource_key: str
    current: float = Field(description="Current usage before this request")
    requested: float = Field(description="Amount requested")
    limit: Optional[float] = Field(description="Effective limit (None = unlimited)")
    tier_id: str
    tier_display: str = Field(description="Human-readable tier name")
    has_override: bool = Field(default=False)
    severity: AlertSeverity = AlertSeverity.INFO
    message: str = Field(description="Human-readable explanation")
    upgrade_url: Optional[str] = Field(default=None)
    retry_after: Optional[int] = Field(
        default=None,
        description="Seconds to wait before retrying (for RATE limits)",
    )
    # Per-user sub-quota fields (populated when user_id was provided)
    user_id: Optional[str] = Field(default=None, description="User checked (if per-user)")
    user_current: Optional[float] = Field(default=None, description="User's current usage")
    user_limit: Optional[float] = Field(default=None, description="User's per-user limit")
    denied_level: Optional[str] = Field(
        default=None,
        description="Which level caused denial: 'org' or 'user' (None if allowed)",
    )

    @computed_field
    @property
    def remaining(self) -> Optional[float]:
        """How much headroom remains after this request would be applied."""
        if self.limit is None:
            return None
        return self.limit - self.current - self.requested

    @computed_field
    @property
    def utilization(self) -> Optional[float]:
        """Current usage as fraction of limit (before this request)."""
        if self.limit is None or self.limit == 0:
            return None
        return round(self.current / self.limit, 4)

    @property
    def allowed(self) -> bool:
        return self.decision in (QuotaDecision.ALLOW, QuotaDecision.ALLOW_WARNING, QuotaDecision.UNLIMITED)

    @property
    def denied(self) -> bool:
        return self.decision == QuotaDecision.DENY

    @property
    def warning(self) -> bool:
        return self.decision == QuotaDecision.ALLOW_WARNING

    def to_api_error(self) -> dict:
        """Format for HTTP 429 response body. Consistent across all services."""
        return {
            "error": "quota_exceeded",
            "resource": self.resource_key,
            "current": self.current,
            "requested": self.requested,
            "limit": self.limit,
            "remaining": self.remaining,
            "tier": self.tier_id,
            "tier_display": self.tier_display,
            "upgrade_url": self.upgrade_url,
            "retry_after": self.retry_after,
            "message": self.message,
        }


class QuotaBatchResult(BaseModel):
    """Result of a batch check (multiple resources at once).

    The batch is denied if ANY individual check is denied.
    """
    allowed: bool = Field(description="True only if ALL checks passed")
    results: list[QuotaResult]
    denied_resources: list[str] = Field(
        default_factory=list,
        description="Resource keys that were denied (empty if all allowed)",
    )
    warning_resources: list[str] = Field(
        default_factory=list,
        description="Resource keys with warnings (approaching limit)",
    )

    @property
    def first_denial(self) -> Optional[QuotaResult]:
        """The first denied result, for use in error responses."""
        for r in self.results:
            if r.denied:
                return r
        return None


# ---------------------------------------------------------------------------
# Usage / Dashboard responses
# ---------------------------------------------------------------------------

class QuotaUsageItem(BaseModel):
    """Usage for a single resource — used in dashboard views."""
    resource_key: str
    display_name: str
    unit: str
    current: float
    limit: Optional[float]
    utilization: Optional[float] = Field(description="0.0–1.0+, None if unlimited")
    severity: AlertSeverity
    has_override: bool = False
    counter_type: str  # gauge | rate | accumulator


class QuotaUsageResponse(BaseModel):
    """Full usage report for an org — all resources with limits."""
    org_id: str
    tier_id: str
    tier_display: str
    resources: list[QuotaUsageItem]
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @computed_field
    @property
    def warnings_count(self) -> int:
        return sum(1 for r in self.resources if r.severity in (AlertSeverity.WARNING, AlertSeverity.CRITICAL))

    @computed_field
    @property
    def exceeded_count(self) -> int:
        return sum(1 for r in self.resources if r.severity == AlertSeverity.EXCEEDED)


class QuotaLimitsResponse(BaseModel):
    """All limits for a tier — returned by /quotas/tiers endpoint."""
    tier_id: str
    tier_display: str
    description: Optional[str]
    sort_order: int
    features: set[str]
    limits: dict[str, TierLimitDetail]
    upgrade_url: Optional[str] = None


class TierLimitDetail(BaseModel):
    """Detailed limit info for display in tier comparison UIs."""
    display_name: str
    unit: str
    limit: Optional[float] = Field(description="None = unlimited")
    limit_display: str = Field(description="Formatted string (e.g. '5 sandboxes', 'Unlimited')")


class QuotaTierResponse(BaseModel):
    """List of all tiers with limits — for pricing pages."""
    tiers: list[QuotaLimitsResponse]


class QuotaAlertResponse(BaseModel):
    """Active alerts for an org."""
    org_id: str
    alerts: list[QuotaAlertItem]
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class QuotaAlertItem(BaseModel):
    """Individual alert for display."""
    resource_key: str
    display_name: str
    severity: AlertSeverity
    current: float
    limit: float
    utilization: float
    message: str
    timestamp: datetime
