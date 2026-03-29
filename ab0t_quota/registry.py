"""Resource registry — services register their countable resources here.

Each service calls `registry.register()` at startup with its ResourceDefs.
The engine uses the registry to look up counter types and metadata.
"""

from __future__ import annotations

from typing import Optional
from .models.core import ResourceDef, CounterType, ResetPeriod


class ResourceRegistry:
    """In-process registry of all known resource definitions."""

    def __init__(self):
        self._resources: dict[str, ResourceDef] = {}

    def register(self, *defs: ResourceDef) -> None:
        for d in defs:
            self._resources[d.resource_key] = d

    def get(self, resource_key: str) -> Optional[ResourceDef]:
        return self._resources.get(resource_key)

    def require(self, resource_key: str) -> ResourceDef:
        r = self.get(resource_key)
        if r is None:
            raise KeyError(f"Unknown resource: {resource_key}. Did you call registry.register()?")
        return r

    def all(self) -> list[ResourceDef]:
        return list(self._resources.values())

    def keys(self) -> list[str]:
        return list(self._resources.keys())


# ---------------------------------------------------------------------------
# Pre-built resource definitions for each service
# ---------------------------------------------------------------------------

SANDBOX_RESOURCES = [
    ResourceDef(
        service="sandbox-platform",
        resource_key="sandbox.concurrent",
        display_name="Concurrent Sandboxes",
        counter_type=CounterType.GAUGE,
        unit="sandboxes",
    ),
    ResourceDef(
        service="sandbox-platform",
        resource_key="sandbox.monthly_cost",
        display_name="Monthly Compute Spend",
        counter_type=CounterType.ACCUMULATOR,
        unit="USD",
        reset_period=ResetPeriod.MONTHLY,
        precision=2,
    ),
    ResourceDef(
        service="sandbox-platform",
        resource_key="sandbox.gpu_instances",
        display_name="Concurrent GPU Instances",
        counter_type=CounterType.GAUGE,
        unit="instances",
    ),
    ResourceDef(
        service="sandbox-platform",
        resource_key="sandbox.browser_sessions",
        display_name="Concurrent Browser Sessions",
        counter_type=CounterType.GAUGE,
        unit="sessions",
    ),
    ResourceDef(
        service="sandbox-platform",
        resource_key="sandbox.desktop_sessions",
        display_name="Concurrent Desktop Sessions",
        counter_type=CounterType.GAUGE,
        unit="sessions",
    ),
]

RESOURCE_SERVICE_RESOURCES = [
    ResourceDef(
        service="resource-service",
        resource_key="resource.cpu_cores",
        display_name="Total CPU Cores",
        counter_type=CounterType.GAUGE,
        unit="cores",
    ),
    ResourceDef(
        service="resource-service",
        resource_key="resource.allocations",
        display_name="Active Allocations",
        counter_type=CounterType.GAUGE,
        unit="allocations",
    ),
    ResourceDef(
        service="resource-service",
        resource_key="resource.monthly_cost",
        display_name="Monthly Resource Spend",
        counter_type=CounterType.ACCUMULATOR,
        unit="USD",
        reset_period=ResetPeriod.MONTHLY,
        precision=2,
    ),
]

AUTH_RESOURCES = [
    ResourceDef(
        service="auth-service",
        resource_key="auth.users_per_org",
        display_name="Members per Organization",
        counter_type=CounterType.GAUGE,
        unit="users",
    ),
    ResourceDef(
        service="auth-service",
        resource_key="auth.teams_per_org",
        display_name="Teams per Organization",
        counter_type=CounterType.GAUGE,
        unit="teams",
    ),
    ResourceDef(
        service="auth-service",
        resource_key="auth.api_keys_per_org",
        display_name="API Keys per Organization",
        counter_type=CounterType.GAUGE,
        unit="keys",
    ),
]

API_GATEWAY_RESOURCES = [
    ResourceDef(
        service="api-gateway",
        resource_key="api.requests_per_hour",
        display_name="API Requests / Hour",
        counter_type=CounterType.RATE,
        unit="requests",
        window_seconds=3600,
    ),
]
