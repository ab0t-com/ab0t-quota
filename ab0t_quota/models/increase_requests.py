"""
Models for self-service quota increase requests.

Users submit these when they hit a limit and need more capacity.
Platform admins review and approve/deny via the global admin dashboard.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional
from datetime import datetime, timezone
from pydantic import BaseModel, Field


class IncreaseRequestStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"     # auto-expired after review_ttl


class QuotaIncreaseRequest(BaseModel):
    """User's request to increase a quota limit."""
    org_id: str
    resource_key: str
    user_id: str = Field(description="User who submitted the request")
    current_limit: float = Field(description="Current effective limit at time of request")
    requested_limit: float = Field(
        gt=0,
        description="Desired new limit",
    )
    justification: str = Field(
        min_length=10,
        max_length=1000,
        description="Why the increase is needed (shown to admin reviewer)",
    )
    current_usage: Optional[float] = Field(
        default=None,
        description="Usage at time of request (for admin context)",
    )
    tier_id: Optional[str] = Field(
        default=None,
        description="Org's tier at time of request",
    )


class QuotaIncreaseRecord(BaseModel):
    """Stored record of an increase request (includes admin response)."""
    request_id: str = Field(description="Unique ID (UUID)")
    org_id: str
    resource_key: str
    user_id: str
    current_limit: float
    requested_limit: float
    justification: str
    current_usage: Optional[float] = None
    tier_id: Optional[str] = None
    status: IncreaseRequestStatus = IncreaseRequestStatus.PENDING
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    reviewed_at: Optional[datetime] = None
    reviewed_by: Optional[str] = Field(default=None, description="Admin user_id who reviewed")
    admin_note: Optional[str] = Field(default=None, description="Admin's response note")
    approved_limit: Optional[float] = Field(
        default=None,
        description="Actual limit granted (may differ from requested)",
    )


class QuotaIncreaseReview(BaseModel):
    """Admin's review of an increase request."""
    request_id: str
    action: IncreaseRequestStatus = Field(description="'approved' or 'denied'")
    approved_limit: Optional[float] = Field(
        default=None,
        description="Limit to grant (required if approved, can differ from requested)",
    )
    admin_note: Optional[str] = Field(
        default=None,
        max_length=500,
        description="Note to the user explaining the decision",
    )
    override_expires_at: Optional[datetime] = Field(
        default=None,
        description="If approved, when the override expires (None = permanent)",
    )
