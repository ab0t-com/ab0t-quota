"""
Request models — inputs to the QuotaEngine and QuotaGuard.

These are the contracts that calling code uses to ask the engine questions.
They are NOT HTTP request bodies (though services may expose thin API
wrappers around them).
"""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field


class QuotaCheckRequest(BaseModel):
    """Check whether an org can consume a resource.

    The engine evaluates this WITHOUT changing any counter.
    Use this as a pre-flight check before provisioning.
    """
    org_id: str = Field(..., description="Organization to check")
    resource_key: str = Field(
        ...,
        description="Resource to check (e.g. 'sandbox.concurrent')",
    )
    increment: float = Field(
        default=1.0,
        gt=0,
        description="How much the org wants to consume (e.g. 1 sandbox, 4 CPU cores, 0.52 USD)",
    )
    user_id: Optional[str] = Field(
        default=None,
        description="Requesting user (for per-user sub-limits if applicable)",
    )
    metadata: Optional[dict] = Field(
        default=None,
        description="Arbitrary context passed through to alerts (e.g. instance_type, tool_id)",
    )


class QuotaBatchCheckRequest(BaseModel):
    """Check multiple resources atomically.

    All checks must pass for the batch to be allowed.
    Example: creating a GPU sandbox checks both sandbox.concurrent AND
    sandbox.gpu_instances in one call.
    """
    org_id: str
    user_id: Optional[str] = None
    checks: list[QuotaCheckItem] = Field(
        ...,
        min_length=1,
        description="List of resource checks to evaluate together",
    )
    metadata: Optional[dict] = None


class QuotaCheckItem(BaseModel):
    """A single item within a batch check."""
    resource_key: str
    increment: float = Field(default=1.0, gt=0)


class QuotaIncrementRequest(BaseModel):
    """Increment a counter after successful provisioning.

    Call this AFTER the resource is actually created, not before.
    For gauges: delta is added to current value.
    For accumulators: delta is added to the period total.
    For rates: a timestamped event is recorded in the sliding window.
    """
    org_id: str
    resource_key: str
    delta: float = Field(
        default=1.0,
        description="Amount to add (positive). For rates, this is event count.",
    )
    user_id: Optional[str] = None
    idempotency_key: Optional[str] = Field(
        default=None,
        description="Prevent double-counting on retries. If set, the same key "
                    "within 24h is a no-op.",
    )
    metadata: Optional[dict] = None


class QuotaDecrementRequest(BaseModel):
    """Decrement a GAUGE counter when a resource is released.

    Only valid for GAUGE counters (e.g. sandbox terminated → decrement
    sandbox.concurrent). Accumulators and rates cannot be decremented.
    """
    org_id: str
    resource_key: str
    delta: float = Field(
        default=1.0,
        gt=0,
        description="Amount to subtract (positive number, will be negated internally).",
    )
    user_id: Optional[str] = None
    idempotency_key: Optional[str] = None


class QuotaOverrideRequest(BaseModel):
    """Set or remove a per-org limit override.

    Platform admins use this to give enterprise customers custom limits
    that differ from their tier.
    """
    org_id: str
    resource_key: str
    limit: Optional[float] = Field(
        description="New limit. None = unlimited. Omit the field entirely to "
                    "remove the override and revert to tier default.",
    )
    reason: Optional[str] = Field(
        default=None,
        description="Audit trail: why this override exists",
    )
    expires_at: Optional[str] = Field(
        default=None,
        description="ISO 8601 expiry. None = permanent.",
    )


class QuotaResetRequest(BaseModel):
    """Manually reset a counter (admin operation).

    Primarily for GAUGE counters that have drifted due to bugs
    (e.g. sandbox terminated but counter not decremented).
    """
    org_id: str
    resource_key: str
    new_value: float = Field(
        default=0.0,
        ge=0,
        description="Value to reset the counter to",
    )
    reason: str = Field(
        ...,
        description="Required audit reason for manual reset",
    )
    admin_user_id: str = Field(
        ...,
        description="ID of the admin performing the reset (audit trail)",
    )
