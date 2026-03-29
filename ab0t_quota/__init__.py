"""
ab0t-quota — Shared quota, rate-limit, and tier enforcement for ab0t platform services.

Usage:
    from ab0t_quota import QuotaEngine, QuotaGuard, QuotaResult
    from ab0t_quota.models import TierConfig, ResourceDef, QuotaCheckRequest
    from ab0t_quota.tiers import DEFAULT_TIERS
"""

from .engine import QuotaEngine
from .middleware import QuotaGuard
from .models.responses import QuotaResult, QuotaUsageResponse, QuotaLimitsResponse
from .models.requests import QuotaCheckRequest, QuotaIncrementRequest, QuotaDecrementRequest, QuotaBatchCheckRequest
from .models.core import (
    ResourceDef,
    CounterType,
    TierConfig,
    TierLimits,
    QuotaOverride,
)
from .providers import TierProvider, JWTTierProvider, AuthServiceTierProvider, StaticTierProvider
from .alerts import AlertManager, AlertDispatcher, LogAlertDispatcher, WebhookAlertDispatcher
from .config import load_config, load_tiers, load_resources
from .messages import MessageBuilder
from .persistence import QuotaStore

__version__ = "0.1.0"

__all__ = [
    # Engine & middleware
    "QuotaEngine",
    "QuotaGuard",
    # Responses
    "QuotaResult",
    "QuotaUsageResponse",
    "QuotaLimitsResponse",
    # Requests
    "QuotaCheckRequest",
    "QuotaIncrementRequest",
    "QuotaDecrementRequest",
    "QuotaBatchCheckRequest",
    # Core models
    "ResourceDef",
    "CounterType",
    "TierConfig",
    "TierLimits",
    "QuotaOverride",
    # Providers
    "TierProvider",
    "JWTTierProvider",
    "AuthServiceTierProvider",
    "StaticTierProvider",
    # Alerts
    "AlertManager",
    "AlertDispatcher",
    "LogAlertDispatcher",
    "WebhookAlertDispatcher",
    # Persistence
    "QuotaStore",
    # Config
    "load_config",
    "load_tiers",
    "load_resources",
    # Messages
    "MessageBuilder",
]
