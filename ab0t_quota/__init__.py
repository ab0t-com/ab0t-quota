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
from .config import load_config, load_tiers, load_resources, load_resource_bundles
from .messages import MessageBuilder
from .persistence import QuotaStore
from .setup import setup_quota, QuotaContext
from .bridge import BridgeClient, BridgeContext, RemoteTierProvider
from .caches import CachedBridgeClient, TTLCache

__version__ = "0.2.1"

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
    "load_resource_bundles",
    # Messages
    "MessageBuilder",
    # Drop-in setup
    "setup_quota",
    "QuotaContext",
    # Bridge mode (third-party HTTP-only deployments)
    "BridgeClient",
    "BridgeContext",
    "RemoteTierProvider",
    "CachedBridgeClient",
    "TTLCache",
]
