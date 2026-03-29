"""Default tier definitions for the ab0t platform.

Services import DEFAULT_TIERS as a starting point. These can be overridden
per-deployment via the TierRegistry or by loading from a config endpoint.
"""

from .models.core import TierConfig, TierLimits

DEFAULT_TIERS: dict[str, TierConfig] = {
    "free": TierConfig(
        tier_id="free",
        display_name="Free",
        description="For experimentation and evaluation",
        sort_order=0,
        features={"basic_sandboxes", "browser_sessions"},
        upgrade_url="/billing/upgrade",
        limits={
            # Sandbox platform
            "sandbox.concurrent":       TierLimits(limit=1),
            "sandbox.monthly_cost":     TierLimits(limit=10.00),
            "sandbox.gpu_instances":    TierLimits(limit=0),
            "sandbox.browser_sessions": TierLimits(limit=2),
            "sandbox.desktop_sessions": TierLimits(limit=0),
            # Resource service
            "resource.cpu_cores":       TierLimits(limit=4),
            "resource.allocations":     TierLimits(limit=3),
            # Auth
            "auth.users_per_org":       TierLimits(limit=5),
            "auth.teams_per_org":       TierLimits(limit=2),
            "auth.api_keys_per_org":    TierLimits(limit=10),
            # API rate limits
            "api.requests_per_hour":    TierLimits(limit=1000),
        },
    ),
    "starter": TierConfig(
        tier_id="starter",
        display_name="Starter",
        description="For small teams getting started",
        sort_order=1,
        features={"basic_sandboxes", "browser_sessions", "desktop_sessions", "api_access"},
        upgrade_url="/billing/upgrade",
        limits={
            "sandbox.concurrent":       TierLimits(limit=5),
            "sandbox.monthly_cost":     TierLimits(limit=100.00),
            "sandbox.gpu_instances":    TierLimits(limit=1),
            "sandbox.browser_sessions": TierLimits(limit=10),
            "sandbox.desktop_sessions": TierLimits(limit=5),
            "resource.cpu_cores":       TierLimits(limit=32),
            "resource.allocations":     TierLimits(limit=20),
            "auth.users_per_org":       TierLimits(limit=25),
            "auth.teams_per_org":       TierLimits(limit=10),
            "auth.api_keys_per_org":    TierLimits(limit=50),
            "api.requests_per_hour":    TierLimits(limit=10_000),
        },
    ),
    "pro": TierConfig(
        tier_id="pro",
        display_name="Pro",
        description="For growing teams with production workloads",
        sort_order=2,
        features={
            "basic_sandboxes", "browser_sessions", "desktop_sessions",
            "api_access", "gpu_access", "audit_logs", "priority_support",
        },
        upgrade_url="/billing/upgrade",
        limits={
            "sandbox.concurrent":       TierLimits(limit=25),
            "sandbox.monthly_cost":     TierLimits(limit=1000.00),
            "sandbox.gpu_instances":    TierLimits(limit=5),
            "sandbox.browser_sessions": TierLimits(limit=50),
            "sandbox.desktop_sessions": TierLimits(limit=25),
            "resource.cpu_cores":       TierLimits(limit=256),
            "resource.allocations":     TierLimits(limit=100),
            "auth.users_per_org":       TierLimits(limit=100),
            "auth.teams_per_org":       TierLimits(limit=50),
            "auth.api_keys_per_org":    TierLimits(limit=200),
            "api.requests_per_hour":    TierLimits(limit=50_000),
        },
    ),
    "enterprise": TierConfig(
        tier_id="enterprise",
        display_name="Enterprise",
        description="Custom limits, SLA, dedicated support",
        sort_order=3,
        features={
            "basic_sandboxes", "browser_sessions", "desktop_sessions",
            "api_access", "gpu_access", "audit_logs", "priority_support",
            "sso", "custom_roles", "sla", "dedicated_support",
        },
        limits={
            "sandbox.concurrent":       TierLimits(limit=None),  # unlimited
            "sandbox.monthly_cost":     TierLimits(limit=None),
            "sandbox.gpu_instances":    TierLimits(limit=50),
            "sandbox.browser_sessions": TierLimits(limit=None),
            "sandbox.desktop_sessions": TierLimits(limit=None),
            "resource.cpu_cores":       TierLimits(limit=1000),
            "resource.allocations":     TierLimits(limit=500),
            "auth.users_per_org":       TierLimits(limit=10_000),
            "auth.teams_per_org":       TierLimits(limit=500),
            "auth.api_keys_per_org":    TierLimits(limit=1000),
            "api.requests_per_hour":    TierLimits(limit=100_000),
        },
    ),
}
