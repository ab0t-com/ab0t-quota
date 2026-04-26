"""QuotaEngine — the core enforcement engine.

This is the single entry point services use for all quota operations:
check, increment, decrement, get_usage.
"""

from __future__ import annotations

import logging
from typing import Optional

from redis.asyncio import Redis

from .models.core import (
    ResourceDef, TierConfig, TierLimits, QuotaOverride,
    QuotaState, AlertSeverity, QuotaAlert, CounterType,
)
from .models.requests import (
    QuotaCheckRequest, QuotaIncrementRequest, QuotaDecrementRequest,
    QuotaBatchCheckRequest, QuotaResetRequest,
)
from .models.responses import (
    QuotaDecision, QuotaResult, QuotaBatchResult,
    QuotaUsageItem, QuotaUsageResponse,
)
from .alerts import AlertManager
from .counters.factory import create_counter
from .counters.rate import RateCounter
from .messages import MessageBuilder
from .registry import ResourceRegistry
from .providers import TierProvider
from .tiers import DEFAULT_TIERS

logger = logging.getLogger("ab0t_quota")


class QuotaEngine:
    """Core quota enforcement engine.

    Usage:
        engine = QuotaEngine(redis=redis, tier_provider=provider, registry=registry)

        # Pre-flight check
        result = await engine.check(QuotaCheckRequest(org_id="org-1", resource_key="sandbox.concurrent"))
        if result.denied:
            raise HTTPException(429, detail=result.to_api_error())

        # After successful provisioning
        await engine.increment(QuotaIncrementRequest(org_id="org-1", resource_key="sandbox.concurrent"))

        # On teardown
        await engine.decrement(QuotaDecrementRequest(org_id="org-1", resource_key="sandbox.concurrent"))
    """

    def __init__(
        self,
        redis: Redis,
        tier_provider: TierProvider,
        registry: ResourceRegistry,
        tiers: Optional[dict[str, TierConfig]] = None,
        override_loader: Optional[callable] = None,
        resource_bundles: Optional[dict[str, list[str]]] = None,
    ):
        self._redis = redis
        self._tier_provider = tier_provider
        self._registry = registry
        self._tiers = tiers or DEFAULT_TIERS
        self._override_loader = override_loader  # async fn(org_id, resource_key) → QuotaOverride | None
        self._alert_manager: Optional[AlertManager] = None
        # Bundle name → list[resource_key]: a named set of resources that
        # are checked / incremented / decremented together when the consumer
        # creates one "thing" of this kind. Generic — the library knows
        # nothing about what the bundles represent. Consumers declare whatever
        # bundle names make sense for their domain in `quota-config.json`:
        #
        #   "resource_bundles": {
        #     "<consumer-defined name>": ["<resource_key>", ...],
        #     ...
        #   }
        #
        # Examples (consumer-specific, never in the library):
        #   {"gpu_sandbox": ["sandbox.concurrent", "sandbox.gpu_instances"]}
        #   {"premium_contact": ["crm.contacts", "crm.premium_contacts"]}
        #   {"large_index": ["vector.indices", "vector.storage_gb"]}
        self._resource_bundles: dict[str, list[str]] = dict(resource_bundles or {})

    def set_resource_bundles(self, bundles: dict[str, list[str]]) -> None:
        """Replace the resource-bundle map. Used by setup_quota() to load
        bundles from `quota-config.json` after engine construction."""
        self._resource_bundles = dict(bundles or {})

    def bundle_resources(self, bundle_name: str) -> list[str]:
        """Return the resource_keys this bundle consumes. Empty list if
        the bundle is not declared (no checks happen — fail open at the
        dispatch layer; per-resource enforcement still applies if the
        consumer calls check() / increment() directly)."""
        return list(self._resource_bundles.get(bundle_name, []))

    # ------------------------------------------------------------------
    # Check
    # ------------------------------------------------------------------

    async def check(self, request: QuotaCheckRequest, **provider_kwargs) -> QuotaResult:
        """Check whether an org can consume a resource. Does NOT modify counters."""
        resource_def = self._registry.require(request.resource_key)
        tier_id = await self._tier_provider.get_tier(request.org_id, **provider_kwargs)
        tier = self._tiers.get(tier_id)
        if tier is None:
            tier = self._tiers.get("free", TierConfig(tier_id="free", display_name="Free"))

        tier_limits = tier.get_limit(request.resource_key)

        # Check for per-org override
        override = await self._load_override(request.org_id, request.resource_key)
        effective_limit = override.limit if override and not override.is_expired else tier_limits.limit
        has_override = override is not None and not override.is_expired

        # Get current org-level usage
        counter = create_counter(self._redis, request.org_id, resource_def)
        current = await counter.get()

        # Org-level check
        result = self._evaluate(
            resource_key=request.resource_key,
            current=current,
            requested=request.increment,
            limit=effective_limit,
            tier=tier,
            tier_limits=tier_limits,
            has_override=has_override,
            resource_def=resource_def,
            counter=counter,
        )

        if result.denied:
            result.denied_level = "org"
            if isinstance(counter, RateCounter):
                result.retry_after = await counter.seconds_until_slot()

        # Per-user sub-quota check (only for gauges, only when user_id provided).
        # Effective per-user limit = explicit per_user_limit, or derived from
        # tier.default_per_user_fraction when none is explicitly set.
        effective_per_user = tier.derive_per_user_limit(tier_limits)
        if (
            result.allowed
            and request.user_id
            and effective_per_user is not None
            and resource_def.counter_type == CounterType.GAUGE
        ):
            from .counters.gauge import GaugeCounter
            if isinstance(counter, GaugeCounter):
                user_current = await counter.get_user(request.user_id)
                user_after = user_current + request.increment
                if user_after > effective_per_user:
                    result = QuotaResult(
                        decision=QuotaDecision.DENY,
                        resource_key=request.resource_key,
                        current=current,
                        requested=request.increment,
                        limit=effective_limit,
                        tier_id=tier.tier_id,
                        tier_display=tier.display_name,
                        has_override=has_override,
                        upgrade_url=tier.upgrade_url,
                        severity=AlertSeverity.EXCEEDED,
                        message=(
                            f"You've used {user_current:.0f} of your personal "
                            f"{effective_per_user:.0f} {resource_def.unit} "
                            f"allowance. Ask your org admin to increase your limit "
                            f"or stop existing resources."
                        ),
                        user_id=request.user_id,
                        user_current=user_current,
                        user_limit=effective_per_user,
                        denied_level="user",
                    )
                else:
                    # Annotate the allowed result with user info
                    result.user_id = request.user_id
                    result.user_current = user_current
                    result.user_limit = effective_per_user

        # Fire alert on warning/critical/exceeded
        if self._alert_manager and result.severity in (
            AlertSeverity.WARNING, AlertSeverity.CRITICAL, AlertSeverity.EXCEEDED
        ):
            await self._alert_manager.maybe_alert(QuotaAlert(
                org_id=request.org_id,
                resource_key=request.resource_key,
                severity=result.severity,
                current=result.current,
                limit=result.limit or 0,
                utilization=result.utilization or 0,
                tier_id=result.tier_id,
                message=result.message,
            ))

        return result

    async def batch_check(self, request: QuotaBatchCheckRequest, **provider_kwargs) -> QuotaBatchResult:
        """Check multiple resources atomically. All must pass."""
        results = []
        for item in request.checks:
            single = QuotaCheckRequest(
                org_id=request.org_id,
                resource_key=item.resource_key,
                increment=item.increment,
                user_id=request.user_id,
                metadata=request.metadata,
            )
            results.append(await self.check(single, **provider_kwargs))

        denied = [r.resource_key for r in results if r.denied]
        warnings = [r.resource_key for r in results if r.warning]

        return QuotaBatchResult(
            allowed=len(denied) == 0,
            results=results,
            denied_resources=denied,
            warning_resources=warnings,
        )

    # ------------------------------------------------------------------
    # Increment / Decrement
    # ------------------------------------------------------------------

    async def increment(self, request: QuotaIncrementRequest) -> float:
        """Increment a counter after successful provisioning. Returns new value."""
        resource_def = self._registry.require(request.resource_key)
        counter = create_counter(self._redis, request.org_id, resource_def)
        # Per-user partition for gauges
        if request.user_id and resource_def.counter_type == CounterType.GAUGE:
            from .counters.gauge import GaugeCounter
            if isinstance(counter, GaugeCounter):
                return await counter.increment_user(request.user_id, request.delta, request.idempotency_key)
        return await counter.increment(request.delta, request.idempotency_key)

    async def decrement(self, request: QuotaDecrementRequest) -> float:
        """Decrement a GAUGE counter on resource release. Returns new value."""
        resource_def = self._registry.require(request.resource_key)
        if resource_def.counter_type != CounterType.GAUGE:
            raise TypeError(f"Cannot decrement {resource_def.counter_type.value} counter '{request.resource_key}'")
        counter = create_counter(self._redis, request.org_id, resource_def)
        # Per-user partition for gauges
        if request.user_id:
            from .counters.gauge import GaugeCounter
            if isinstance(counter, GaugeCounter):
                return await counter.decrement_user(request.user_id, request.delta, request.idempotency_key)
        return await counter.decrement(request.delta, request.idempotency_key)

    async def reset(self, request: QuotaResetRequest) -> None:
        """Admin: force-set a counter value."""
        resource_def = self._registry.require(request.resource_key)
        counter = create_counter(self._redis, request.org_id, resource_def)
        previous_value = await counter.get()
        logger.warning(
            "ADMIN_QUOTA_RESET admin_user_id=%s org_id=%s resource_key=%s previous_value=%s new_value=%s reason=%s",
            request.admin_user_id, request.org_id, request.resource_key,
            previous_value, request.new_value, request.reason,
        )
        await counter.reset(request.new_value)

    # ------------------------------------------------------------------
    # Resource-bundle helpers — declarative dispatch for "creating one thing"
    # ------------------------------------------------------------------

    async def check_for_bundle(
        self,
        org_id: str,
        bundle_name: str,
        user_id: Optional[str] = None,
        **provider_kwargs,
    ) -> QuotaBatchResult:
        """Pre-flight check for creating one of `bundle_name`.

        Looks up the bundle's declared resource_keys and batch-checks them
        all. Unknown bundle → trivially allowed (the library knows nothing
        about consumer-specific names; per-resource enforcement still
        applies when the consumer calls check() directly).
        """
        from .models.requests import QuotaCheckItem
        resource_keys = self._resource_bundles.get(bundle_name)
        if not resource_keys:
            return QuotaBatchResult(
                allowed=True, results=[], denied_resources=[], warning_resources=[],
            )
        return await self.batch_check(
            QuotaBatchCheckRequest(
                org_id=org_id,
                user_id=user_id,
                checks=[QuotaCheckItem(resource_key=rk) for rk in resource_keys],
            ),
            **provider_kwargs,
        )

    async def increment_for_bundle(
        self,
        org_id: str,
        bundle_name: str,
        user_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, float]:
        """Increment every counter the bundle consumes, after successful create.

        Returns {resource_key: new_value}. Per-resource idempotency keys
        are namespaced so a retry that already partially committed is safe.
        """
        out: dict[str, float] = {}
        for rk in self._resource_bundles.get(bundle_name, []):
            idem = f"{idempotency_key}:{rk}" if idempotency_key else None
            new_val = await self.increment(QuotaIncrementRequest(
                org_id=org_id, resource_key=rk, user_id=user_id, idempotency_key=idem,
            ))
            out[rk] = new_val
        return out

    async def decrement_for_bundle(
        self,
        org_id: str,
        bundle_name: str,
        user_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, float]:
        """Decrement every GAUGE counter the bundle consumes, on teardown.

        Non-gauge resources in the bundle (accumulators, rates) are silently
        skipped — they don't decrement.
        """
        out: dict[str, float] = {}
        for rk in self._resource_bundles.get(bundle_name, []):
            rd = self._registry.get(rk)
            if rd is None or rd.counter_type != CounterType.GAUGE:
                continue
            idem = f"{idempotency_key}:{rk}" if idempotency_key else None
            try:
                new_val = await self.decrement(QuotaDecrementRequest(
                    org_id=org_id, resource_key=rk, user_id=user_id, idempotency_key=idem,
                ))
                out[rk] = new_val
            except Exception as e:
                logger.warning(
                    "decrement_for_bundle_failed org=%s bundle=%s resource=%s error=%s",
                    org_id, bundle_name, rk, str(e),
                )
        return out

    # ------------------------------------------------------------------
    # Usage reporting
    # ------------------------------------------------------------------

    async def get_usage(self, org_id: str, **provider_kwargs) -> QuotaUsageResponse:
        """Get full usage report for an org across all registered resources."""
        tier_id = await self._tier_provider.get_tier(org_id, **provider_kwargs)
        tier = self._tiers.get(tier_id, self._tiers.get("free"))

        items = []
        for resource_def in self._registry.all():
            counter = create_counter(self._redis, org_id, resource_def)
            current = await counter.get()
            tier_limits = tier.get_limit(resource_def.resource_key)

            override = await self._load_override(org_id, resource_def.resource_key)
            effective_limit = override.limit if override and not override.is_expired else tier_limits.limit
            has_override = override is not None and not override.is_expired

            state = QuotaState(
                org_id=org_id,
                resource_key=resource_def.resource_key,
                current=current,
                limit=effective_limit,
                tier_id=tier_id,
                has_override=has_override,
            )
            items.append(QuotaUsageItem(
                resource_key=resource_def.resource_key,
                display_name=resource_def.display_name,
                unit=resource_def.unit,
                current=current,
                limit=effective_limit,
                utilization=state.utilization,
                severity=state.severity,
                has_override=has_override,
                counter_type=resource_def.counter_type.value,
            ))

        return QuotaUsageResponse(
            org_id=org_id,
            tier_id=tier_id,
            tier_display=tier.display_name,
            resources=items,
        )

    # ------------------------------------------------------------------
    # Tier cache management
    # ------------------------------------------------------------------

    def set_alert_manager(self, alert_manager: AlertManager) -> None:
        """Attach an alert manager for WARNING/CRITICAL notifications."""
        self._alert_manager = alert_manager

    async def invalidate_tier_cache(self, org_id: str) -> None:
        """Clear cached tier for an org. Call from payment webhooks after tier change."""
        if hasattr(self._tier_provider, "invalidate"):
            await self._tier_provider.invalidate(org_id)

    # ------------------------------------------------------------------
    # Feature gating
    # ------------------------------------------------------------------

    async def check_feature(self, org_id: str, feature_name: str, **provider_kwargs) -> bool:
        """Check if an org's tier includes a feature (e.g. 'gpu_access', 'sso')."""
        tier_id = await self._tier_provider.get_tier(org_id, **provider_kwargs)
        tier = self._tiers.get(tier_id, self._tiers.get("free"))
        return feature_name in tier.features

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _evaluate(
        self,
        resource_key: str,
        current: float,
        requested: float,
        limit: Optional[float],
        tier: TierConfig,
        tier_limits: TierLimits,
        has_override: bool,
        resource_def: ResourceDef,
        counter,
    ) -> QuotaResult:
        base = dict(
            resource_key=resource_key,
            current=current,
            requested=requested,
            limit=limit,
            tier_id=tier.tier_id,
            tier_display=tier.display_name,
            has_override=has_override,
            upgrade_url=tier.upgrade_url,
        )

        # Unlimited
        if limit is None:
            return QuotaResult(
                decision=QuotaDecision.UNLIMITED,
                severity=AlertSeverity.INFO,
                message=MessageBuilder.allow(resource_def, current, limit, current + requested),
                **base,
            )

        after = current + requested

        # Over limit — check burst allowance before hard deny
        if after > limit:
            burst = tier_limits.burst_allowance
            if burst and after <= limit + burst:
                return QuotaResult(
                    decision=QuotaDecision.ALLOW_WARNING,
                    severity=AlertSeverity.CRITICAL,
                    message=MessageBuilder.burst(resource_def, tier, current, limit, after),
                    **base,
                )

            # Hard deny
            retry_after = None
            return QuotaResult(
                decision=QuotaDecision.DENY,
                severity=AlertSeverity.EXCEEDED,
                message=MessageBuilder.deny(resource_def, tier, current, limit, requested),
                retry_after=retry_after,
                **base,
            )

        # Warning threshold
        utilization = after / limit if limit > 0 else 0
        if utilization >= tier_limits.critical_threshold:
            return QuotaResult(
                decision=QuotaDecision.ALLOW_WARNING,
                severity=AlertSeverity.CRITICAL,
                message=MessageBuilder.warning(resource_def, tier, current, limit, after),
                **base,
            )
        if utilization >= tier_limits.warning_threshold:
            return QuotaResult(
                decision=QuotaDecision.ALLOW_WARNING,
                severity=AlertSeverity.WARNING,
                message=MessageBuilder.warning(resource_def, tier, current, limit, after),
                **base,
            )

        # All clear
        return QuotaResult(
            decision=QuotaDecision.ALLOW,
            severity=AlertSeverity.INFO,
            message=MessageBuilder.allow(resource_def, current, limit, after),
            **base,
        )

    async def _load_override(self, org_id: str, resource_key: str) -> Optional[QuotaOverride]:
        if self._override_loader is None:
            return None
        try:
            return await self._override_loader(org_id, resource_key)
        except Exception as e:
            logger.error(
                "override_load_error org_id=%s resource_key=%s error=%s",
                org_id, resource_key, str(e),
            )
            return None
