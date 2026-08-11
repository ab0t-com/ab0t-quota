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
    QuotaMetric,
)
from .providers import TierProvider, JWTTierProvider, AuthServiceTierProvider, StaticTierProvider, TierFetchError
from .alerts import (
    AlertManager, AlertDispatcher, LogAlertDispatcher, WebhookAlertDispatcher,
    MetricDispatcher, LogMetricDispatcher, RedisCounterMetricDispatcher,
)
from .config import load_config, load_tiers, load_resources, load_resource_bundles
from .errors import QuotaConfigError
from .messages import MessageBuilder, Templates
from .persistence import QuotaStore
from .setup import setup_quota, QuotaContext
from .bridge import BridgeClient, BridgeContext, RemoteTierProvider
from .caches import CachedBridgeClient, TTLCache
from .activations import (
    Activation, ActivationState, ActivationStore, InMemoryActivationStore,
    RedisActivationStore, DDBActivationStore, mint_activation_id,
    resolve_gauge_level, converge_gauge, stale_open_activations,
)
from .reconcile import LibraryReconciler, ReconcileConfig
from .alerts import DriftAlertManager

__version__ = "0.6.6"

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
    "QuotaMetric",
    # Providers
    "TierProvider",
    "JWTTierProvider",
    "AuthServiceTierProvider",
    "StaticTierProvider",
    "TierFetchError",
    # Alerts
    "AlertManager",
    "AlertDispatcher",
    "LogAlertDispatcher",
    "WebhookAlertDispatcher",
    "DriftAlertManager",
    # Metrics (F9/P4.1)
    "MetricDispatcher",
    "LogMetricDispatcher",
    "RedisCounterMetricDispatcher",
    # Reconciler (P4)
    "LibraryReconciler",
    "ReconcileConfig",
    # Persistence
    "QuotaStore",
    # Config
    "load_config",
    "load_tiers",
    "QuotaConfigError",
    "load_resources",
    "load_resource_bundles",
    # Messages
    "MessageBuilder",
    "Templates",
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
