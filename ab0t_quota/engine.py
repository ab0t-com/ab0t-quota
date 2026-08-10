"""QuotaEngine — the core enforcement engine.

This is the single entry point services use for all quota operations:
check, increment, decrement, get_usage.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from redis.asyncio import Redis

from .models.core import (
    ResourceDef, TierConfig, TierLimits, QuotaOverride,
    QuotaState, AlertSeverity, QuotaAlert, CounterType, EnforcementConfig,
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
from .errors import QuotaConfigError
from .messages import MessageBuilder
from .registry import ResourceRegistry
from .providers import TierProvider

logger = logging.getLogger("ab0t_quota")


class InvalidSettlementCost(ValueError):
    """A settle() cost that is not a finite, non-negative decimal (D-47).

    NaN poisons a money accumulator irrecoverably (every subsequent read is NaN);
    inf is nonsensical; a negative cost is a refund wearing usage's clothes (refunds
    go through billing, never through settle()). settle() rejects them fail-closed —
    the activation stays unsettled (the drift alarm) rather than record poison."""


@dataclass
class AcquireResult:
    """Outcome of engine.acquire(). ``activation_id`` is the minted, retry-safe
    handle the caller carries to release()/settle()."""
    admitted: bool
    activation_id: Optional[str]
    denied_resource: Optional[str]
    reason: str
    values: dict = field(default_factory=dict)


# Atomic bundle check-and-spend (P2.2, QI-03). Checks ALL gauge limits (org and
# per-user) and, only if EVERY one passes, spends ALL of them — in ONE Lua op.
# KEYS = [idem, then per gauge: org, user, seq] (placeholders when no user);
# during dual-write (K-3) the whole key set is doubled with the secondary shape.
#   ARGV[1]=has_idem ARGV[2]=idem_ttl ARGV[3]=n ARGV[4]=dual ARGV[5]=v2p
#   per gauge i (base = 5 + (i-1)*4): has_user, delta, org_limit, user_limit
# Returns {admitted('1'/'0'), reason} reason='ok'|'dup'|1-based denied index.
from .counters.base import dual_lua

_ACQUIRE = dual_lua("1 + tonumber(ARGV[3])*3", 4, """
local n = tonumber(ARGV[3])
for i=1,n do
  local kb = 1 + (i-1)*3
  local ab = 5 + (i-1)*4
  seedv2(kb+1)
  if ARGV[ab+1] == '1' then seedv2(kb+2); seedv2(kb+3) end
end
if ARGV[1] == '1' then
  if idem_dup(1) then
    return {'1', 'dup'}
  end
end
for i=1,n do
  local kb = 1 + (i-1)*3
  local ab = 5 + (i-1)*4
  local has_user = ARGV[ab+1]
  local delta = tonumber(ARGV[ab+2])
  local org_limit = ARGV[ab+3]
  local user_limit = ARGV[ab+4]
  local ocur = redis.call('GET', KEYS[kb+1]); if not ocur then ocur = '0' end
  if org_limit ~= '' and (tonumber(ocur) + delta) > tonumber(org_limit) then
    return {'0', tostring(i)}
  end
  if has_user == '1' and user_limit ~= '' then
    local ucur = redis.call('GET', KEYS[kb+2]); if not ucur then ucur = '0' end
    if (tonumber(ucur) + delta) > tonumber(user_limit) then
      return {'0', tostring(i)}
    end
  end
end
if ARGV[1] == '1' then
  idem_claim(1)
end
for i=1,n do
  local kb = 1 + (i-1)*3
  local ab = 5 + (i-1)*4
  local has_user = ARGV[ab+1]
  local delta = tonumber(ARGV[ab+2])
  incrboth(kb+1, delta)
  if has_user == '1' then
    incrintboth(kb+3)
    incrboth(kb+2, delta)
  end
end
return {'1', 'ok'}
""")


class QuotaEngine:
    """Core quota enforcement engine.

    Usage:
        engine = QuotaEngine(redis=redis, tier_provider=provider, registry=registry,
                             tiers=load_tiers(config))  # tiers are POLICY: always explicit

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
        enforcement: Optional[EnforcementConfig | dict] = None,
        activation_store=None,
        activations_enabled: Optional[bool] = None,
        keyspace=None,
        observed_usage_provider: Optional[Callable[[str], Any]] = None,
        accumulator_usage_provider: Optional[Callable[[str], Any]] = None,
        drift_alerts=None,
        truth_provider_timeout_seconds: float = 10.0,
        read_repair_throttle_seconds: Optional[float] = None,
    ):
        self._redis = redis
        self._tier_provider = tier_provider
        self._registry = registry
        # --- Typed truth-source seams (ticket 20260810, robust self-correcting
        # quota; DESIGN_robust_quota.md) ------------------------------------------
        # The fast Redis counter is NEVER the authority — it is a cache of a
        # type-specific DERIVED truth. Two user-safety rules (recount-before-deny,
        # read-repair) recompute that truth from the resource's TYPE source before
        # ever harming a user. The engine dispatches on `counter_type`:
        #   * GAUGE       -> observed_usage_provider(org_id) -> {rk: {"total", "per_user"}}
        #                    (a LIVE count of what actually exists; the same seam the
        #                    reconciler uses — authoritative for EXISTENCE, D-33).
        #   * ACCUMULATOR -> accumulator_usage_provider(org_id) -> {rk: period_total}
        #                    (re-SUM of the durable event ledger for the CURRENT period;
        #                    NOT a live count — a meter's past is real and lives in the
        #                    ledger, DESIGN §2 Type B).
        #   * RATE        -> no truth source: the TTL'd window counter IS truth (self-
        #                    healing on window roll), so it is never recounted.
        # Both providers are OPTIONAL. Their ABSENCE is the zero-config default: the
        # engine behaves exactly as before (no recount, no read-repair) — so wiring a
        # provider is purely additive and can never change a consumer that has none.
        self._observed_usage_provider = observed_usage_provider
        self._accumulator_usage_provider = accumulator_usage_provider
        self._drift_alerts = drift_alerts
        self._truth_provider_timeout_seconds = truth_provider_timeout_seconds
        if read_repair_throttle_seconds is None:
            try:
                read_repair_throttle_seconds = float(
                    os.getenv("AB0T_QUOTA_READ_REPAIR_THROTTLE_SECONDS", "60") or 60)
            except ValueError:
                read_repair_throttle_seconds = 60.0
        self._read_repair_throttle_seconds = read_repair_throttle_seconds
        # K-1 (keyspace spec §3.2): the declared keyspace state — service scope,
        # version, dual flag. None = v1 single-shape, today's behaviour.
        from .keyspace import Keyspace
        self._keyspace = keyspace or Keyspace()
        # T-3/ENV-12: the tier catalog is POLICY and is never invented. An
        # explicit {} is a declaration (honoured); None/omitted is an error.
        if tiers is None:
            raise QuotaConfigError(
                name="tier catalog", config_key="tiers", code="QUOTA-CFG-004",
                state="QuotaEngine(tiers=None) — not supplied", env_names=(),
                remedy=("pass tiers=load_tiers(config). For tests/local dev only: "
                        "ab0t_quota.tiers.DEFAULT_TIERS may be passed explicitly."),
                docs_anchor="tiers",
            )
        self._tiers = tiers
        self._override_loader = override_loader  # async fn(org_id, resource_key) → QuotaOverride | None
        self._alert_manager: Optional[AlertManager] = None
        # --- Activation core (P2.1/P2.2, DECISIONS D-10) ---------------------
        # The atomic acquire()/release()/settle() API and the atomic-enforcing
        # increment path. `activations_enabled` is the documented rollback knob
        # (tasklist P2.2): default ON so the counter can never exceed the limit
        # under concurrency (QI-03); AB0T_QUOTA_ACTIVATIONS=off reverts increment
        # to the legacy pure-add behaviour for a consumer that needs the old
        # (racy) semantics during migration. See D-24.
        if activations_enabled is None:
            activations_enabled = os.getenv(
                "AB0T_QUOTA_ACTIVATIONS", "on",
            ).strip().lower() not in ("off", "false", "0", "no")
        self._activations_enabled = activations_enabled
        if activation_store is None:
            from .activations import InMemoryActivationStore
            activation_store = InMemoryActivationStore()
        self._activation_store = activation_store
        # Enforcement knobs (QP-01 / D-15). Coerce a plain dict (from config)
        # into the typed model. Mirrors the Go engine's Cfg.Enforcement.
        if isinstance(enforcement, dict):
            enforcement = EnforcementConfig(**enforcement)
        self._enforcement: EnforcementConfig = enforcement or EnforcementConfig()
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
        the bundle is not declared — callers must treat that per the
        enforcement unknown_bundle policy (default 'deny', loud — D-14/D-48),
        never as a silent allow."""
        return list(self._resource_bundles.get(bundle_name, []))

    # ------------------------------------------------------------------
    # Check
    # ------------------------------------------------------------------

    def _enforcement_short_circuit(
        self, request: QuotaCheckRequest, decision: QuotaDecision,
        *, reason: str, message: str, severity: AlertSeverity = AlertSeverity.INFO,
    ) -> QuotaResult:
        """Minimal result for a global enforcement decision (kill-switch /
        enforcement-disabled) taken before any tier/counter work (mirrors Go
        engine.go:49-64). Static message — no dynamic client strings."""
        return QuotaResult(
            decision=decision,
            resource_key=request.resource_key,
            current=0.0,
            requested=request.increment,
            limit=None,
            tier_id="",
            tier_display="",
            severity=severity,
            message=message,
            reason=reason,
        )

    async def check(self, request: QuotaCheckRequest, **provider_kwargs) -> QuotaResult:
        """Check whether an org can consume a resource. Does NOT modify counters."""
        # Enforcement knobs (QP-01 / D-15), mirroring the Go engine.
        # Global kill-switch — fail closed.
        if self._enforcement.global_kill_switch:
            return self._enforcement_short_circuit(
                request, QuotaDecision.DENY, reason="global_kill_switch",
                message="Quota enforcement halted by global kill switch.",
                severity=AlertSeverity.EXCEEDED,
            )
        # Enforcement disabled — allow everything without computing.
        if not self._enforcement.enabled:
            return self._enforcement_short_circuit(
                request, QuotaDecision.ALLOW, reason="enforcement_disabled",
                message="Quota enforcement is disabled.",
            )

        resource_def = self._registry.require(request.resource_key)
        tier_id = await self._tier_provider.get_tier(request.org_id, **provider_kwargs)
        tier = self._tiers.get(tier_id)
        if tier is None:
            # QP-02 / D-14: an unmapped tier id is a CONFIG ERROR — surface it
            # explicitly and alert. NEVER silently coerce to `free` (that denies
            # a paying org its capacity while hiding the bug). Mirrors Go's
            # `tier_not_in_config`.
            logger.error(
                "tier_not_in_config org=%s tier_id=%r resource=%s — check tier config",
                request.org_id, tier_id, request.resource_key,
            )
            if self._alert_manager:
                await self._alert_manager.maybe_alert(QuotaAlert(
                    org_id=request.org_id,
                    resource_key=request.resource_key,
                    severity=AlertSeverity.EXCEEDED,
                    current=0, limit=0, utilization=0,
                    tier_id=tier_id,
                    message="tier_not_in_config",
                ))
            return QuotaResult(
                decision=QuotaDecision.UNKNOWN_TIER,
                resource_key=request.resource_key,
                current=0.0, requested=request.increment, limit=None,
                tier_id=tier_id, tier_display=tier_id,
                severity=AlertSeverity.EXCEEDED,
                message="Your account tier is not configured. Please contact support.",
                reason="tier_not_in_config",
            )

        tier_limits = tier.get_limit(request.resource_key)

        # Check for per-org override
        override = await self._load_override(request.org_id, request.resource_key)
        effective_limit = override.limit if override and not override.is_expired else tier_limits.limit
        has_override = override is not None and not override.is_expired

        # Get current org-level usage
        counter = create_counter(self._redis, request.org_id, resource_def, keyspace=self._keyspace)
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

        # P1 — RECOUNT-BEFORE-DENY (ticket 20260810, the highest-impact rule).
        # Never block a user on a stale cached number. When the cache would DENY,
        # recompute the true usage from the resource's TYPE source of truth and only
        # deny if the truth is really at/over limit; otherwise repair the cache and
        # ALLOW. Fail-OPEN on a truth-source error (P1.2/D-31): allow + alert, never
        # block. RATE and consumers with no truth source configured are unaffected.
        if result.decision == QuotaDecision.DENY:
            result = await self._recount_before_deny(
                request, resource_def, result, counter, tier, tier_limits,
                effective_limit, has_override, effective_per_user)

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

        # Shadow mode — a would-be DENY becomes an ALLOW, logged not enforced
        # (D-15, mirror Go engine.go:164). Config errors (UNKNOWN_TIER) are NOT
        # shadowed — a mis-configured tier must still surface.
        if result.decision == QuotaDecision.DENY and self._enforcement.shadow_mode:
            logger.info(
                "shadow_would_deny org=%s resource=%s current=%s requested=%s limit=%s",
                request.org_id, request.resource_key,
                result.current, result.requested, result.limit,
            )
            result = result.model_copy(update={
                "decision": QuotaDecision.SHADOW_ALLOW,
                "reason": "shadow_would_deny",
            })

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

    async def _effective_limits(
        self, org_id: str, resource_key: str, resource_def, user_id: Optional[str],
        **provider_kwargs,
    ) -> tuple[Optional[float], Optional[float]]:
        """Resolve (org_limit_including_burst, per_user_limit) for a GAUGE, using
        the SAME tier/override/burst/per-user resolution as check(). None means
        unlimited / no-limit. If the tier is not in config, treat as unlimited
        here — check() owns the explicit UNKNOWN_TIER denial (D-14); increment must
        not silently deny on a tier lookup miss."""
        tier_id = await self._tier_provider.get_tier(org_id, **provider_kwargs)
        tier = self._tiers.get(tier_id)
        if tier is None:
            return None, None
        tier_limits = tier.get_limit(resource_key)
        override = await self._load_override(org_id, resource_key)
        base = override.limit if (override and not override.is_expired) else tier_limits.limit
        if base is None:
            org_limit = None
        else:
            org_limit = base + (tier_limits.burst_allowance or 0)
        per_user = None
        if user_id and resource_def.counter_type == CounterType.GAUGE:
            per_user = tier.derive_per_user_limit(tier_limits)
        return org_limit, per_user

    async def increment(self, request: QuotaIncrementRequest, **provider_kwargs) -> float:
        """Count usage after a resource actually exists. Returns the new value.

        D-24 (Option B) — the governing rule: **quota may refuse to authorise a
        FUTURE (at acquire(), before provisioning); it must never refuse to
        acknowledge a PRESENT.** increment() runs AFTER provisioning, so a refusal
        here would leave a resource existing-and-uncounted → phantom headroom
        (exactly the QG-06/QI-02 defect this ticket kills). Therefore:

          * DEFAULT `enforcement.legacy_increment='count_and_alert'`: GAUGES always
            count; when the new level crosses the limit an `over_limit_admitted`
            event fires so the over-admission is an OBSERVABLE fact (pillar 1) for
            the open-activation ledger + reconciler to catch. A one-time
            deprecation notice names acquire() as the enforcing replacement.
          * OPT-IN `enforcement.legacy_increment='enforce'`: atomic check-and-spend
            that refuses over-limit — ONLY safe for a consumer that has verified it
            increments BEFORE provisioning.

        ACCUMULATORS / RATES always record actual usage (cost past the cap must be
        recorded, not dropped). AB0T_QUOTA_ACTIVATIONS=off is the activation-PATH
        rollback (acquire persistence), NOT the lever for this behaviour.
        """
        resource_def = self._registry.require(request.resource_key)
        counter = create_counter(self._redis, request.org_id, resource_def, keyspace=self._keyspace)

        if resource_def.counter_type == CounterType.GAUGE:
            from .counters.gauge import GaugeCounter
            if isinstance(counter, GaugeCounter):
                if self._enforcement.legacy_increment == "enforce":
                    org_limit, per_user = await self._effective_limits(
                        request.org_id, request.resource_key, resource_def,
                        request.user_id, **provider_kwargs,
                    )
                    if request.user_id:
                        value, admitted = await counter.try_increment_user(
                            request.user_id, request.delta, org_limit, per_user,
                            request.idempotency_key,
                        )
                    else:
                        value, admitted = await counter.try_increment(
                            request.delta, org_limit, request.idempotency_key,
                        )
                    if not admitted:
                        logger.warning(
                            "gauge_admission_refused org=%s resource=%s user=%s "
                            "delta=%s limit=%s current=%s — legacy_increment=enforce; "
                            "create over limit NOT counted. Use acquire() to gate "
                            "BEFORE provisioning.",
                            request.org_id, request.resource_key, request.user_id,
                            request.delta, org_limit, value,
                        )
                    return value

                # DEFAULT (Option B): count at the fact, never refuse.
                self._warn_legacy_increment_once()
                if request.user_id:
                    value = await counter.increment_user(
                        request.user_id, request.delta, request.idempotency_key)
                else:
                    value = await counter.increment(request.delta, request.idempotency_key)
                await self._emit_over_limit_if_crossed(
                    request, resource_def, **provider_kwargs)
                return value

        # Non-gauge counters (accumulators / rates): always record.
        return await counter.increment(request.delta, request.idempotency_key)

    def _warn_legacy_increment_once(self) -> None:
        if getattr(self, "_legacy_increment_warned", False):
            return
        self._legacy_increment_warned = True
        logger.warning(
            "DEPRECATION: engine.increment()/increment_for_bundle() count at the fact "
            "and do NOT enforce limits (D-24 Option B) — a create past the limit is "
            "counted + alerted (over_limit_admitted), not blocked. Enforce BEFORE "
            "provisioning with engine.acquire(), which returns admitted + an "
            "activation_id. This notice fires once per engine.",
        )

    async def _emit_over_limit_if_crossed(
        self, request: QuotaIncrementRequest, resource_def, **provider_kwargs,
    ) -> None:
        """After a count-at-the-fact increment, if the new level is over the HARD
        limit (tier/override, burst excluded), emit `over_limit_admitted` — the
        observable-fact signal the reconciler/ledger exist to catch (pillar 1)."""
        tier_id = await self._tier_provider.get_tier(request.org_id, **provider_kwargs)
        tier = self._tiers.get(tier_id)
        if tier is None:
            return
        tl = tier.get_limit(request.resource_key)
        override = await self._load_override(request.org_id, request.resource_key)
        base = override.limit if (override and not override.is_expired) else tl.limit
        counter = create_counter(self._redis, request.org_id, resource_def, keyspace=self._keyspace)
        if base is not None:
            org_cur = await counter.get()
            if org_cur > base:
                await self._fire_over_limit(request.org_id, request.resource_key,
                                            "org", org_cur, base, tier_id)
        if request.user_id:
            from .counters.gauge import GaugeCounter
            if isinstance(counter, GaugeCounter):
                per_user = tier.derive_per_user_limit(tl)
                if per_user is not None:
                    u = await counter.get_user(request.user_id)
                    if u > per_user:
                        await self._fire_over_limit(request.org_id, request.resource_key,
                                                    "user", u, per_user, tier_id,
                                                    user_id=request.user_id)

    async def _fire_over_limit(self, org_id, resource_key, scope, level, limit,
                               tier_id, user_id=None) -> None:
        logger.warning(
            "over_limit_admitted org=%s resource=%s scope=%s user=%s level=%s limit=%s "
            "— counted at the fact (D-24 B); over-admission is now an OBSERVABLE fact. "
            "Gate with acquire() to prevent it.",
            org_id, resource_key, scope, user_id, level, limit,
        )
        if self._alert_manager:
            await self._alert_manager.maybe_alert(QuotaAlert(
                org_id=org_id, resource_key=resource_key,
                severity=AlertSeverity.EXCEEDED,
                current=level, limit=limit,
                utilization=(level / limit) if limit else 0,
                tier_id=tier_id, message="over_limit_admitted",
            ))

    async def decrement(self, request: QuotaDecrementRequest) -> float:
        """Decrement a GAUGE counter on resource release. Returns new value."""
        resource_def = self._registry.require(request.resource_key)
        if resource_def.counter_type != CounterType.GAUGE:
            raise TypeError(f"Cannot decrement {resource_def.counter_type.value} counter '{request.resource_key}'")
        counter = create_counter(self._redis, request.org_id, resource_def, keyspace=self._keyspace)
        # Per-user partition for gauges
        if request.user_id:
            from .counters.gauge import GaugeCounter
            if isinstance(counter, GaugeCounter):
                return await counter.decrement_user(request.user_id, request.delta, request.idempotency_key)
        return await counter.decrement(request.delta, request.idempotency_key)

    async def reset(self, request: QuotaResetRequest) -> None:
        """Admin: force-set a counter value."""
        resource_def = self._registry.require(request.resource_key)
        counter = create_counter(self._redis, request.org_id, resource_def, keyspace=self._keyspace)
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
            # QP-02 / D-14: an undeclared bundle name (usually a typo in
            # resource_bundles) must NOT silently disable enforcement. Always
            # loud; deny in enforce mode, allow+warn under shadow_mode.
            logger.warning(
                "unknown_bundle org=%s bundle=%r — not declared in resource_bundles; "
                "enforcement outcome=%s",
                org_id, bundle_name,
                "allow_warn" if (self._enforcement.shadow_mode
                                 or self._enforcement.unknown_bundle == "allow_warn"
                                 or not self._enforcement.enabled) else "deny",
            )
            allow = (
                not self._enforcement.enabled
                or self._enforcement.shadow_mode
                or self._enforcement.unknown_bundle == "allow_warn"
            )
            return QuotaBatchResult(
                allowed=allow,
                results=[],
                denied_resources=[] if allow else [bundle_name],
                warning_resources=[bundle_name] if allow else [],
            )
        return await self.batch_check(
            QuotaBatchCheckRequest(
                org_id=org_id,
                user_id=user_id,
                checks=[QuotaCheckItem(resource_key=rk) for rk in resource_keys],
            ),
            **provider_kwargs,
        )

    async def _gauge_specs(
        self, org_id: str, resource_keys: list[str], user_id: Optional[str],
        deltas: Optional[dict[str, float]] = None, **provider_kwargs,
    ) -> list[dict]:
        """Resolve the per-gauge (keys, limits, delta) specs for an atomic acquire.
        Non-gauge resources are skipped (they don't gate concurrency).

        W-T3/ET-04 (D-31): each delta is a MAGNITUDE, validated finite BEFORE
        the Lua. Pre-fix, acquire(deltas={rk: -5}) was ADMITTED and drove the
        gauge to -4.0 — the admission API erasing spend and breaching the
        QG-06 zero floor in one call; a NaN delta in a bundle would partially
        spend earlier gauges then burn the idem claim (scripts don't roll back)."""
        from .counters.base import finite_magnitude
        from .counters.gauge import GaugeCounter
        specs: list[dict] = []
        _seen: set[str] = set()
        for rk in resource_keys:
            # D-45: a bundle naming the same resource twice would spend it twice in
            # ONE acquire (counter past its limit; ledger records one unit) — the core
            # invariant broken in steady state. A bundle is a SET of resources; dedup
            # defensively (config load also REJECTS duplicates, config.py — belt & braces).
            if rk in _seen:
                logger.warning(
                    "resource_bundle_duplicate_key org=%s resource=%s — a bundle names "
                    "this resource more than once; counting it ONCE (D-45).", org_id, rk)
                continue
            _seen.add(rk)
            rd = self._registry.get(rk)
            if rd is None or rd.counter_type != CounterType.GAUGE:
                continue
            g = create_counter(self._redis, org_id, rd, keyspace=self._keyspace)
            if not isinstance(g, GaugeCounter):
                continue
            org_limit, per_user = await self._effective_limits(
                org_id, rk, rd, user_id, **provider_kwargs,
            )
            specs.append({
                "resource_key": rk, "gauge": g,
                "delta": finite_magnitude((deltas or {}).get(rk, 1.0)),
                "org_limit": org_limit,
                "user_limit": per_user if user_id else None,
                "has_user": bool(user_id),
            })
        return specs

    async def _atomic_bundle_spend(
        self, specs: list[dict], user_id: Optional[str], idem: Optional[str],
    ) -> tuple[bool, str]:
        """Run the one-Lua check-ALL-then-spend-ALL over the gauge specs.
        Returns (admitted, reason)."""
        if not specs:
            return True, "ok"
        from .counters.gauge import GaugeCounter
        ks = self._keyspace
        g0: GaugeCounter = specs[0]["gauge"]

        def _key_set(version=None):
            """One full [idem, per-gauge org/user/seq] set in the given shape
            (None = primary). Dual doubles the whole set (K-3)."""
            out = [ks.idem_key(g0._org_id, g0._resource_key, idem, version=version)]
            for s in specs:
                g: GaugeCounter = s["gauge"]
                org_key = ks.gauge_key(g._org_id, g._resource_key, version=version)
                out.append(org_key)
                if user_id:
                    out.append(ks.user_key(g._org_id, g._resource_key, user_id, version=version))
                    out.append(ks.seq_user_key(g._org_id, g._resource_key, user_id, version=version))
                else:
                    out.append(org_key)   # placeholder (has_user='0' skips it)
                    out.append(org_key)
            return out

        keys: list = _key_set()
        if ks.secondary_version:
            keys += _key_set(version=ks.secondary_version)
        argv: list = ["1" if idem else "0", 86400, len(specs),
                      "1" if ks.secondary_version else "0",
                      "1" if ks.primary_is_v2 else "0"]
        for s in specs:
            argv.extend([
                "1" if s["has_user"] else "0",
                repr(float(s["delta"])),
                GaugeCounter._fmt_limit(s["org_limit"]),
                GaugeCounter._fmt_limit(s["user_limit"]),
            ])
        res = await self._redis.eval(_ACQUIRE, len(keys), *keys, *argv)
        admitted = res[0] in (b"1", "1", 1)
        reason = res[1].decode() if isinstance(res[1], bytes) else str(res[1])
        return admitted, reason

    async def acquire(
        self,
        org_id: str,
        bundle_name: Optional[str] = None,
        *,
        resource_key: Optional[str] = None,
        user_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        **provider_kwargs,
    ):
        """Atomically check ALL of a bundle's (or a single resource's) gauge limits
        and, only if every one passes, spend them all — in ONE Lua op — then mint an
        `activation_id` and persist an OPEN activation record (P2.2, DECISIONS D-10).

        This is the fully retry-safe create path (kills QI-03 TOCTOU + the
        fake-atomic batch_check, and QI-05 by minting identity). Returns an
        ``AcquireResult`` carrying ``admitted``, the ``activation_id`` (when
        admitted), the first denied resource, and per-resource new values.

        Non-gauge resources in a bundle (accumulators/rates) are NOT gated here —
        they record actual usage via settle()/increment(), not admission.
        """
        from .activations import Activation, mint_activation_id, ActivationState
        # Enforcement preamble (QP-01/QP-02, DECISIONS D-14/D-15/D-31). The atomic
        # admission gate MUST honor the same knobs check()/check_for_bundle() do.
        # acquire() previously bypassed them, silently ADMITTING (1) when an operator
        # flipped the global kill switch and (2) when a bundle name / resource_key was
        # an undeclared typo. Both are the forbidden fail-OPEN direction (D-31): a
        # config that means "deny" must never be silently widened to "allow".
        if bundle_name is not None:
            resource_keys = self._resource_bundles.get(bundle_name)
            label = bundle_name
            unknown = resource_keys is None
        elif resource_key is not None:
            resource_keys = [resource_key]
            label = resource_key
            unknown = self._registry.get(resource_key) is None
        else:
            raise ValueError("acquire requires bundle_name or resource_key")

        # --- Enforcement preamble (D-48) --------------------------------------
        # The atomic admission gate MUST honor EVERY enforcement knob check() does.
        # acquire() was written after D-14/D-15 and inherited NEITHER, silently
        # ADMITTING under the kill switch, an unknown bundle, an unknown tier, and
        # ignoring shadow_mode/enabled. Each is the forbidden fail-OPEN direction
        # (D-31): a config that means "deny"/"allow" silently produced the opposite.
        # This block is verified IDENTICAL to check()/check_for_bundle() by the
        # enforcement-contract matrix (tests/test_enforcement_contract_matrix_*).
        #
        # global kill switch — fail closed (mirrors check(), Go engine.go:49-64).
        if self._enforcement.global_kill_switch:
            logger.warning(
                "acquire_denied org=%s bundle=%s reason=global_kill_switch",
                org_id, label,
            )
            return AcquireResult(admitted=False, activation_id=None,
                                 denied_resource=label, reason="global_kill_switch",
                                 values={})
        enforce = self._enforcement.enabled
        # Unknown bundle / unregistered resource — a config typo must NOT silently
        # disable enforcement (QP-02/D-14). Deny in enforce mode; allow+warn under
        # shadow_mode / unknown_bundle='allow_warn' / enforcement disabled. Always loud.
        if unknown:
            allow = (
                not enforce
                or self._enforcement.shadow_mode
                or self._enforcement.unknown_bundle == "allow_warn"
            )
            logger.warning(
                "unknown_bundle org=%s bundle=%r — not declared/registered; acquire "
                "enforcement outcome=%s", org_id, label,
                "allow_warn" if allow else "deny",
            )
            if not allow:
                return AcquireResult(admitted=False, activation_id=None,
                                     denied_resource=label, reason="unknown_bundle",
                                     values={})
            resource_keys = resource_keys or []
        # Unknown tier — explicit deny + alert, NOT shadowed (a config error must
        # surface; mirrors check()'s UNKNOWN_TIER, D-14). Never silent-coerce to free.
        # Skipped only when enforcement is disabled (which allows without computing).
        if enforce:
            tier_id = await self._tier_provider.get_tier(org_id, **provider_kwargs)
            if self._tiers.get(tier_id) is None:
                logger.error(
                    "tier_not_in_config org=%s tier_id=%r bundle=%s — acquire denied",
                    org_id, tier_id, label,
                )
                if self._alert_manager:
                    await self._alert_manager.maybe_alert(QuotaAlert(
                        org_id=org_id, resource_key=label,
                        severity=AlertSeverity.EXCEEDED,
                        current=0, limit=0, utilization=0, tier_id=tier_id,
                        message="tier_not_in_config",
                    ))
                return AcquireResult(admitted=False, activation_id=None,
                                     denied_resource=label, reason="tier_not_in_config",
                                     values={})

        specs = await self._gauge_specs(org_id, resource_keys, user_id, **provider_kwargs)
        # Enforcement disabled — allow everything without computing (D-15): drop all
        # limits so the atomic spend still records usage + mints the activation
        # (counting is not enforcing), but never denies.
        if not enforce:
            for s in specs:
                s["org_limit"] = None
                s["user_limit"] = None

        admitted, reason = await self._atomic_bundle_spend(specs, user_id, idempotency_key)

        # P1 — RECOUNT-BEFORE-DENY on the atomic gauge path (ticket 20260810). A
        # denial here rests on the CACHED gauge; before returning it, verify against
        # the live provider. If the cache was stale-high (drift), repair the gauges to
        # the observed truth and RETRY the spend once. Fail-OPEN on a truth-source
        # error (D-31): retry with limits dropped so a provider outage never blocks a
        # user. Only active when an observed_usage_provider is wired (else unchanged).
        if (not admitted and enforce and not self._enforcement.shadow_mode
                and self._observed_usage_provider is not None and specs):
            admitted, reason = await self._acquire_recount_before_deny(
                org_id, specs, user_id, idempotency_key, denied_reason=reason)

        # Shadow mode — a would-be DENY becomes an ALLOW: spent + logged, not blocked
        # (D-15, mirror check()/Go engine.go:164). The first (limited) spend claimed
        # NOTHING on denial (the Lua sets the idem key only after every check passes),
        # so re-running with limits dropped is replay-safe and spends exactly once.
        if not admitted and self._enforcement.shadow_mode:
            logger.info(
                "shadow_would_deny org=%s bundle=%s user=%s", org_id, label, user_id,
            )
            for s in specs:
                s["org_limit"] = None
                s["user_limit"] = None
            admitted, reason = await self._atomic_bundle_spend(
                specs, user_id, idempotency_key)
            reason = "shadow_would_deny"

        if not admitted:
            denied_rk = None
            try:
                denied_rk = specs[int(reason) - 1]["resource_key"]
            except (ValueError, IndexError):
                denied_rk = label
            logger.info(
                "acquire_denied org=%s bundle=%s user=%s denied_resource=%s",
                org_id, label, user_id, denied_rk,
            )
            return AcquireResult(admitted=False, activation_id=None,
                                 denied_resource=denied_rk, reason=reason, values={})

        values = {s["resource_key"]: await s["gauge"].get() for s in specs}
        activation_id = None
        if self._activations_enabled and reason != "dup":
            activation_id = mint_activation_id()
            spend = {s["resource_key"]: float(s["delta"]) for s in specs}
            try:
                await self._activation_store.put_open(Activation(
                    activation_id=activation_id, org_id=org_id, user_id=user_id,
                    resource_key=label, spend=spend,
                    state=ActivationState.OPEN.value,
                ))
            except Exception:
                # FAIL-CLOSED (D-27). The counter is already spent (over-count vs the
                # ledger). We must NOT report this acquire as admitted: if we did and
                # the caller provisioned, the ledger would be MISSING the row, and the
                # reconciler (converge_gauge) would later drive the counter DOWN to
                # Σ open activations — BELOW the live resource. That is under-count /
                # phantom headroom, the one forbidden direction (QI-02/QG-06 class).
                # Re-raise instead: the caller does not provision; the orphaned spend
                # is an OVER-count that converge heals to Σ open. Over-count only ever
                # DENIES capacity (annoying, safe), never grants it. The invariant the
                # ledger guarantees — acquire succeeds ⟹ activation persisted — holds.
                logger.error(
                    "acquire_persist_failed org=%s bundle=%s — FAILING the acquire "
                    "(fail-closed, D-27); the orphaned counter spend will heal to "
                    "Σ open activations. Caller MUST NOT provision.",
                    org_id, label,
                )
                raise
        return AcquireResult(admitted=True, activation_id=activation_id,
                             denied_resource=None, reason=reason, values=values)

    async def release(self, activation_id: str) -> bool:
        """Release an activation: idempotently mark it RELEASED and return its spent
        gauges to zero. Keyed ONLY on the minted `activation_id` — no caller-composed
        key, no TTL horizon — so a reused resource-id can never collide (QI-05.1) and
        a replayed release is a no-op. Returns True if THIS call performed the release,
        False if it was already released / unknown."""
        # Ordering is LOAD-BEARING and fail-closed (D-27): mark RELEASED (ledger
        # drops) BEFORE decrementing the counter, so a crash in the window leaves an
        # OVER-count (counter >= Σ open), never an under-count. Inverting this
        # (decrement-then-mark) is a fail-OPEN under-count — guarded by the negative
        # control in tests/test_crash_fail_closed_20260710.py (inverting it goes RED).
        row = await self._activation_store.mark_released(activation_id)
        if row is None:
            return False  # already released or unknown — replay-safe by construction
        from .counters.gauge import GaugeCounter
        for rk, delta in (row.spend or {}).items():
            rd = self._registry.get(rk)
            if rd is None or rd.counter_type != CounterType.GAUGE:
                continue
            g = create_counter(self._redis, row.org_id, rd, keyspace=self._keyspace)
            if not isinstance(g, GaugeCounter):
                continue
            # Idempotent by the activation state transition above (this line only
            # runs once per activation), so a plain decrement is safe here.
            if row.user_id:
                await g.decrement_user(row.user_id, float(delta),
                                       idempotency_key=f"release:{activation_id}")
            else:
                await g.decrement(float(delta), idempotency_key=f"release:{activation_id}")
        return True

    @staticmethod
    def _validate_settlement_cost(cost) -> str:
        """D-47: a settlement cost must be a FINITE, NON-NEGATIVE decimal. Reject
        NaN/inf/negative fail-closed with a typed error BEFORE touching the ledger —
        never let poison into a money accumulator. Returns the normalized string."""
        from decimal import Decimal, InvalidOperation
        s = str(cost).strip()
        try:
            d = Decimal(s)
        except (InvalidOperation, ValueError):
            raise InvalidSettlementCost("settlement cost is not a valid decimal")
        if not d.is_finite():
            raise InvalidSettlementCost("settlement cost must be finite")
        if d < 0:
            raise InvalidSettlementCost("settlement cost must be non-negative")
        return s

    async def settle(self, activation_id: str, cost: str) -> bool:
        """Record an activation's final cost, idempotent on `activation_id` (QB-02).
        Returns True if THIS call recorded it, False if already settled / unknown.

        D-47: rejects a non-finite / negative cost (raises InvalidSettlementCost)
        before any ledger write — fail-closed, so poison never lands.
        D-46: a re-settle with a DIFFERENT cost keeps first-wins idempotence but emits
        a loud `settle_conflict` alert carrying BOTH values — a caller that computed two
        different costs for one activation has a money bug, never silently discarded."""
        cost_str = self._validate_settlement_cost(cost)
        row = await self._activation_store.mark_settled(activation_id, cost_str)
        if row is not None:
            return True
        # Already settled or unknown. Detect a CONFLICTING re-settle (D-46): same
        # activation, already SETTLED, but a DIFFERENT cost than this call carries.
        from .activations import ActivationState
        existing = await self._activation_store.get(activation_id)
        if (existing is not None
                and existing.state == ActivationState.SETTLED.value
                and existing.cost is not None
                and existing.cost != cost_str):
            logger.warning(
                "settle_conflict activation_id=%s org=%s settled_cost=%s new_cost=%s "
                "— caller computed two different costs for one activation; keeping the "
                "first (idempotent), NOT overwriting. Investigate the caller (D-46).",
                activation_id, existing.org_id, existing.cost, cost_str,
            )
            if self._alert_manager:
                await self._alert_manager.maybe_alert(QuotaAlert(
                    org_id=existing.org_id, resource_key=existing.resource_key,
                    severity=AlertSeverity.EXCEEDED,
                    current=0, limit=0, utilization=0,
                    tier_id="", message="settle_conflict",
                ))
        return False

    async def open_activations(self, org_id: str, *, limit: int = 100):
        """List an org's OPEN activations (the drift alarm — QB-03)."""
        return await self._activation_store.list_open(org_id, limit=limit)

    async def stale_open_activations(self, org_id: str, *, older_than_s: float, limit: int = 500):
        """OPEN activations older than `older_than_s` — a missed-decrement (QB-03)
        made an OBSERVABLE fact instead of invisible drift. Fires the alert manager
        (if wired) once per stale activation-set so operators SEE the leak. Returns
        the stale activations."""
        from .activations import stale_open_activations as _filter
        opens = await self._activation_store.list_open(org_id, limit=limit)
        stale = _filter(opens, older_than_s=older_than_s)
        if stale:
            logger.warning(
                "stale_open_activations org=%s count=%d older_than_s=%s — likely "
                "missed release(s); investigate for drift (QB-03).",
                org_id, len(stale), older_than_s,
            )
        return stale

    async def _mark_gauge_activity(self, org_id: str) -> None:
        """Recent-activity marker for the reconciler's guard (D-33 §3). Set on
        every gauge bundle mutation so the periodic reconciler skips an org with
        in-flight lifecycle traffic instead of racing a just-applied delta against
        a lagging provider/GSI read. The library owns BOTH halves of the guard now:
        this SET, and the READ in reconcile.LibraryReconciler._is_recent_activity —
        so a consumer that deletes its bespoke marker keeps the guarantee. Window =
        QUOTA_RECONCILE_ACTIVITY_GUARD_SECONDS (default 90), matching the reconciler.
        Best-effort: a marker failure must never break the counter op."""
        try:
            window = max(int(os.getenv("QUOTA_RECONCILE_ACTIVITY_GUARD_SECONDS", "90") or 90), 0)
        except ValueError:
            window = 90
        if window <= 0:
            return
        try:
            ks = self._keyspace
            await self._redis.set(ks.recent_key(org_id), "1", ex=window)
            if ks.secondary_version:  # dual: both shapes (keyspace spec §7 #5)
                await self._redis.set(
                    ks.recent_key(org_id, version=ks.secondary_version), "1", ex=window)
        except Exception:
            pass

    async def increment_for_bundle(
        self,
        org_id: str,
        bundle_name: str,
        user_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, float]:
        """Increment every counter the bundle consumes, after successful create.

        D-24 (Option B): by DEFAULT each resource is counted at the fact via
        increment() (which fires over_limit_admitted on crossing) — a bundle create
        past the limit is COUNTED + alerted, never silently refused. The opt-in
        `enforcement.legacy_increment='enforce'` runs the ONE-Lua atomic
        check-ALL-then-spend-ALL instead (all-or-nothing, refuses over-limit) — only
        safe for a consumer that increments BEFORE provisioning. The fully
        retry-safe gate is acquire() (returns an activation_id + admitted).
        Returns {resource_key: new_value}.
        """
        out: dict[str, float] = {}
        resource_keys = self._resource_bundles.get(bundle_name)
        if resource_keys is None:
            # D-14/D-48: an undeclared bundle name (a typo) must NOT silently count
            # NOTHING — that leaves the just-provisioned resources uncounted (phantom
            # headroom). increment counts at the fact (D-24) so this cannot DENY, but
            # it must be LOUD, never silent.
            logger.warning(
                "unknown_bundle org=%s bundle=%r — not declared in resource_bundles; "
                "increment_for_bundle counted NOTHING (a typo leaves provisioned "
                "resources uncounted). Fix the bundle name.", org_id, bundle_name)
            resource_keys = []
        await self._mark_gauge_activity(org_id)

        if self._enforcement.legacy_increment == "enforce":
            specs = await self._gauge_specs(org_id, resource_keys, user_id)
            admitted, _reason = await self._atomic_bundle_spend(specs, user_id, idempotency_key)
            if not admitted:
                logger.warning(
                    "increment_for_bundle_refused org=%s bundle=%s user=%s — "
                    "legacy_increment=enforce; bundle over limit NOT counted. "
                    "Use acquire() to gate BEFORE provisioning.",
                    org_id, bundle_name, user_id,
                )
            for s in specs:
                out[s["resource_key"]] = await s["gauge"].get()
            for rk in resource_keys:  # non-gauges always record
                rd = self._registry.get(rk)
                if rd is None or rd.counter_type == CounterType.GAUGE:
                    continue
                idem = f"{idempotency_key}:{rk}" if idempotency_key else None
                out[rk] = await self.increment(QuotaIncrementRequest(
                    org_id=org_id, resource_key=rk, user_id=user_id, idempotency_key=idem,
                ))
            return out

        # DEFAULT (Option B): count each resource at the fact; increment() alerts.
        for rk in resource_keys:
            idem = f"{idempotency_key}:{rk}" if idempotency_key else None
            out[rk] = await self.increment(QuotaIncrementRequest(
                org_id=org_id, resource_key=rk, user_id=user_id, idempotency_key=idem,
            ))
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
        await self._mark_gauge_activity(org_id)
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
                # A failed release leaves the gauge NOT decremented → OVER-count
                # (fail-closed: denies capacity, never grants — safe). But it is DRIFT,
                # not a no-op: surface it loudly so the reconciler/operator sees it,
                # rather than letting it vanish. (converge_gauge heals it to Σ open.)
                logger.error(
                    "gauge_release_drift org=%s bundle=%s resource=%s error=%s — "
                    "release did NOT decrement; counter now OVER-counts (fail-closed, "
                    "denies capacity). Will heal on next reconcile (converge_gauge).",
                    org_id, bundle_name, rk, str(e),
                )
        return out

    # ------------------------------------------------------------------
    # Usage reporting
    # ------------------------------------------------------------------

    async def get_usage(self, org_id: str, **provider_kwargs) -> QuotaUsageResponse:
        """Get full usage report for an org across all registered resources."""
        # P2.2 — READ-REPAIR (ticket 20260810). A plain usage read opportunistically
        # recomputes from truth and repairs a drifted cache, so the number a user sees
        # is honest AND self-healing. Throttled per-org and best-effort — it never
        # breaks the read, and is a no-op for a consumer with no truth source wired.
        await self._maybe_read_repair(org_id)
        tier_id = await self._tier_provider.get_tier(org_id, **provider_kwargs)
        tier = self._tiers.get(tier_id) or self._lowest_tier()

        items = []
        for resource_def in self._registry.all():
            counter = create_counter(self._redis, org_id, resource_def, keyspace=self._keyspace)
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
                warning_threshold=tier_limits.warning_threshold,
                critical_threshold=tier_limits.critical_threshold,
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
    # Typed truth-sources: recount-before-deny, read-repair, reconcile_org
    # (ticket 20260810 — DESIGN_robust_quota.md §0 the universal principle:
    #  the fast counter is a cache of a type-specific derived truth; staleness
    #  must never harm the user.)
    # ------------------------------------------------------------------

    _EPS = 1e-9

    async def _call_truth_provider(self, provider, org_id: str) -> dict:
        """Invoke a (sync or async) truth provider, bounded by
        ``truth_provider_timeout_seconds`` for async (D-52: a hung provider must
        not stall the caller). Raises on failure — the caller decides fail-open."""
        result = provider(org_id)
        if inspect.isawaitable(result):
            t = self._truth_provider_timeout_seconds
            if t and t > 0:
                result = await asyncio.wait_for(result, t)
            else:
                result = await result
        return result or {}

    def _drift_alert_manager(self):
        """The drift metric/alert channel. If the consumer wired one it is reused;
        otherwise a log-only ``DriftAlertManager`` is lazily built from the engine's
        Redis (so ``quota.drift_detected`` still fires with zero extra config)."""
        if self._drift_alerts is None:
            from .alerts import DriftAlertManager
            self._drift_alerts = DriftAlertManager(redis=self._redis)
        return self._drift_alerts

    @staticmethod
    def _extract_truth_from_obs(obs: Optional[dict], resource_def) -> Optional[tuple[float, dict]]:
        """Pull ``(total, per_user)`` for a resource from a provider result dict, or
        None if the key is ABSENT (absence != zero, D-51 — a missing key is 'no
        observation', never an affirmative zero that could wipe the counter)."""
        rk = resource_def.resource_key
        if obs is None or rk not in obs:
            return None
        entry = obs.get(rk)
        if resource_def.counter_type == CounterType.GAUGE:
            entry = entry or {}
            total = float(entry.get("total", 0.0))
            per_user = {str(u): float(v) for u, v in (entry.get("per_user") or {}).items()}
            return total, per_user
        # ACCUMULATOR: accept {rk: number} or {rk: {"total": number}}.
        if isinstance(entry, dict):
            return float(entry.get("total", 0.0)), {}
        return float(entry), {}

    def _truth_provider_for(self, resource_def):
        """The truth provider for a resource's TYPE, or None. GAUGE→live provider,
        ACCUMULATOR→ledger provider, RATE→None (the window counter is truth)."""
        ct = resource_def.counter_type
        if ct == CounterType.GAUGE:
            return self._observed_usage_provider
        if ct == CounterType.ACCUMULATOR:
            return self._accumulator_usage_provider
        return None

    async def _recompute_true_usage(self, org_id: str, resource_def) -> Optional[tuple[float, dict]]:
        """Recompute a single resource's TRUE usage from its type source of truth.
        Returns ``(total, per_user)``; None when no source is configured OR the
        provider returned no observation for this key. Raises on a provider error
        (the caller fails open)."""
        provider = self._truth_provider_for(resource_def)
        if provider is None:
            return None
        obs = await self._call_truth_provider(provider, org_id)
        return self._extract_truth_from_obs(obs, resource_def)

    async def _repair_counter_to_truth(
        self, org_id: str, resource_def, counter, true_total: float, true_per_user: dict,
    ) -> float:
        """Force-set the cached counter to the type's truth. GAUGE also syncs its
        per-user partitions to the observed set and CLEARS stale ones (QI-06 — a
        repair that fixes half the state guarantees org/user divergence). Returns the
        previous org value."""
        before = await counter.get()
        await counter.reset(true_total)
        if (resource_def.counter_type == CounterType.GAUGE
                and hasattr(counter, "reset_user") and hasattr(counter, "_user_key")):
            prefix = counter._user_key("")
            existing: set[str] = set()
            try:
                async for k in self._redis.scan_iter(match=f"{prefix}*", count=100):
                    existing.add(k.decode() if isinstance(k, bytes) else str(k))
            except Exception:
                pass
            for uid, uval in (true_per_user or {}).items():
                await counter.reset_user(uid, float(uval))
                existing.discard(prefix + str(uid))
            for stale in existing:
                try:
                    raw = await self._redis.get(stale)
                    if raw and float(raw) != 0.0:
                        await self._redis.delete(stale)
                except Exception:
                    pass
        return before

    async def _recount_before_deny(
        self, request: QuotaCheckRequest, resource_def, denied_result: QuotaResult,
        counter, tier: TierConfig, tier_limits: TierLimits, effective_limit,
        has_override: bool, effective_per_user,
    ) -> QuotaResult:
        """P1 — the recount-before-deny mechanism. Given a would-be DENY, recompute
        the true usage from the resource's TYPE truth source; repair a stale cache and
        re-decide. Fail-OPEN on a truth-source error (never block a user on an
        unverifiable cache). Returns the (possibly flipped) result."""
        ct = resource_def.counter_type
        if ct == CounterType.RATE:
            return denied_result  # the TTL'd window counter is already truth
        provider = self._truth_provider_for(resource_def)
        if provider is None:
            return denied_result  # no truth source wired — keep the cache-based deny
        org_id, rk = request.org_id, request.resource_key
        try:
            truth = await self._recompute_true_usage(org_id, resource_def)
        except Exception as e:
            # P1.2 — the truth source is unreachable. The correct fail direction is
            # TYPE-AWARE (not uniform), because the two counter types drift in OPPOSITE
            # directions:
            #
            #   * GAUGE — the cache drifts HIGH from lost decrements (the cd790b95 "5/0"
            #     class), so a stale cache must NEVER block a user. Fail-OPEN: allow +
            #     alert. Any over-admission is bounded and healed by the reconciler.
            #
            #   * ACCUMULATOR — the cache IS the durable ledger-backed running total: a
            #     meter/cost counter is monotonic within its period and has NO
            #     lost-decrement drift, so "cache says over limit" means the customer
            #     genuinely accrued that spend. Failing OPEN here would let spend blow
            #     past the cap during a ledger-source outage (overspend / revenue risk).
            #     So do NOT fail-open: KEEP the cache-based deny (fail-to-last-known for
            #     money — the cache is the best available estimate). A genuinely-wrong
            #     meter cache is corrected by reconcile_org / read-repair, NEVER by
            #     fail-open. Still alert (distinct reason) so the outage is visible.
            try:
                await self._drift_alert_manager().provider_unreachable(org_id, error=str(e))
            except Exception:
                pass
            if ct == CounterType.ACCUMULATOR:
                logger.error(
                    "recount_source_unavailable_kept_deny org=%s resource=%s error=%s — "
                    "KEEPING the deny (money-safety, fail-to-last-known): an accumulator "
                    "cache is the durable ledger sum with no lost-decrement drift, so "
                    "allowing on a source outage risks overspend past the cap.",
                    org_id, rk, e,
                )
                return denied_result.model_copy(update={
                    "reason": "recount_source_unavailable_kept_deny",
                })
            logger.error(
                "recount_before_deny_truth_unreachable org=%s resource=%s error=%s — "
                "ALLOWING (fail-open on drift, D-31); a gauge's stale cache must never "
                "block a user, and an IO error is not evidence of real usage.",
                org_id, rk, e,
            )
            return denied_result.model_copy(update={
                "decision": QuotaDecision.ALLOW,
                "severity": AlertSeverity.INFO,
                "reason": "recount_fail_open",
                "message": MessageBuilder.allow(
                    resource_def, denied_result.current, effective_limit,
                    denied_result.current + request.increment),
            })
        if truth is None:
            # Provider reachable but gave NO observation for this key (absence !=
            # zero, D-51). We cannot prove headroom; keep the deny.
            return denied_result
        true_total, true_per_user = truth
        cached = await counter.get()
        drifted = abs(true_total - cached) > self._EPS
        if drifted:
            await self._repair_counter_to_truth(
                org_id, resource_def, counter, true_total, true_per_user)
            try:
                await self._drift_alert_manager().drift_detected(
                    org_id, rk, observed=true_total, ledger=cached,
                    before=cached, after=true_total,
                    source="provider" if ct == CounterType.GAUGE else "ledger",
                    resource_type=ct.value, tier_id=tier.tier_id)
            except Exception:
                pass
        # Re-decide the admission with the TRUE numbers.
        repaired = self._evaluate(
            resource_key=rk, current=true_total, requested=request.increment,
            limit=effective_limit, tier=tier, tier_limits=tier_limits,
            has_override=has_override, resource_def=resource_def, counter=counter)
        # Re-apply the per-user gauge sub-check with the TRUE per-user level.
        if (repaired.allowed and request.user_id and effective_per_user is not None
                and ct == CounterType.GAUGE):
            user_current = float((true_per_user or {}).get(request.user_id, 0.0))
            if user_current + request.increment > effective_per_user:
                return denied_result.model_copy(update={
                    "current": true_total,
                    "user_current": user_current,
                    "user_limit": effective_per_user,
                    "denied_level": "user",
                    "reason": "recount_user_over_limit",
                })
            repaired.user_id = request.user_id
            repaired.user_current = user_current
            repaired.user_limit = effective_per_user
        if repaired.allowed and drifted:
            repaired.reason = "recount_repaired_allow"
        elif repaired.denied:
            repaired.denied_level = "org"
        return repaired

    async def _acquire_recount_before_deny(
        self, org_id: str, specs: list[dict], user_id: Optional[str],
        idem: Optional[str], *, denied_reason: str,
    ) -> tuple[bool, str]:
        """P1 for acquire()'s atomic gauge spend. Verify a denial against the live
        provider; repair any drifted gauge and RETRY the spend once. Fail-OPEN on a
        truth-source error: retry with limits dropped (never block on an unverifiable
        cache). Returns ``(admitted, reason)``."""
        try:
            obs = await self._call_truth_provider(self._observed_usage_provider, org_id)
        except Exception as e:
            logger.error(
                "acquire_recount_truth_unreachable org=%s error=%s — retrying with "
                "limits dropped (fail-open on drift, D-31); a stale cache must never "
                "block a user.", org_id, e,
            )
            try:
                await self._drift_alert_manager().provider_unreachable(org_id, error=str(e))
            except Exception:
                pass
            dropped = [{**s, "org_limit": None, "user_limit": None} for s in specs]
            return await self._atomic_bundle_spend(dropped, user_id, idem)
        repaired_any = False
        for s in specs:
            rk = s["resource_key"]
            counter = s["gauge"]
            rd = self._registry.get(rk)
            if rd is None:
                continue
            truth = self._extract_truth_from_obs(obs, rd)
            if truth is None:
                continue  # absence != zero — can't verify this gauge; leave it
            true_total, per_user = truth
            cached = await counter.get()
            if abs(true_total - cached) > self._EPS:
                await self._repair_counter_to_truth(org_id, rd, counter, true_total, per_user)
                repaired_any = True
                try:
                    await self._drift_alert_manager().drift_detected(
                        org_id, rk, observed=true_total, ledger=cached,
                        before=cached, after=true_total, source="provider",
                        resource_type="gauge")
                except Exception:
                    pass
        if not repaired_any:
            return False, denied_reason  # the cache matched reality — a real deny
        return await self._atomic_bundle_spend(specs, user_id, idem)

    async def reconcile_org(
        self, org_id: str, resource_key: Optional[str] = None,
    ) -> dict:
        """P2 — recompute each resource from its TYPE source of truth and force-set
        the cached counter to it. Powers a user-facing 'Recalculate usage' button:
        idempotent, safe to call anytime, and it ONLY ever sets the counter to the
        derived truth (never an arbitrary value).

          * GAUGE       -> observed_usage_provider (live existence count)
          * ACCUMULATOR -> accumulator_usage_provider (re-sum of the durable period ledger)
          * RATE        -> skipped (the TTL'd window counter is self-healing truth)

        Emits ``quota.drift_detected`` on every repair and ``quota.drift_resolved``
        when already in sync. Returns a structured before->after per resource::

            {"org_id": ..., "resources": {resource_key: {
                "counter_type": "gauge"|"accumulator"|"rate",
                "before": float|None, "after": float|None,
                "changed": bool, "source": "provider"|"ledger"|None,
                "status": "repaired"|"in_sync"|"skipped_rate"|"no_truth_source"
                          |"no_observation"|"truth_unavailable"}}}
        """
        if resource_key is not None:
            resource_defs = [self._registry.require(resource_key)]
        else:
            resource_defs = list(self._registry.all())

        # Fetch each provider AT MOST ONCE per pass (a provider returns the whole
        # org in one call), then dispatch per resource from the cached result.
        need_gauge = (self._observed_usage_provider is not None
                      and any(rd.counter_type == CounterType.GAUGE for rd in resource_defs))
        need_acc = (self._accumulator_usage_provider is not None
                    and any(rd.counter_type == CounterType.ACCUMULATOR for rd in resource_defs))
        gauge_obs = acc_obs = None
        gauge_err = acc_err = None
        if need_gauge:
            try:
                gauge_obs = await self._call_truth_provider(self._observed_usage_provider, org_id)
            except Exception as e:
                gauge_err = e
                logger.error("reconcile_org_gauge_provider_unreachable org=%s error=%s", org_id, e)
                try:
                    await self._drift_alert_manager().provider_unreachable(org_id, error=str(e))
                except Exception:
                    pass
        if need_acc:
            try:
                acc_obs = await self._call_truth_provider(self._accumulator_usage_provider, org_id)
            except Exception as e:
                acc_err = e
                logger.error("reconcile_org_acc_provider_unreachable org=%s error=%s", org_id, e)
                try:
                    await self._drift_alert_manager().provider_unreachable(org_id, error=str(e))
                except Exception:
                    pass

        out: dict = {"org_id": org_id, "resources": {}}
        for rd in resource_defs:
            rk = rd.resource_key
            ct = rd.counter_type
            entry = {"counter_type": ct.value, "before": None, "after": None,
                     "changed": False, "source": None, "status": None}
            if ct == CounterType.RATE:
                entry["status"] = "skipped_rate"
                out["resources"][rk] = entry
                continue
            provider = self._truth_provider_for(rd)
            if provider is None:
                entry["status"] = "no_truth_source"
                out["resources"][rk] = entry
                continue
            counter = create_counter(self._redis, org_id, rd, keyspace=self._keyspace)
            before = await counter.get()
            entry["before"] = before
            obs = gauge_obs if ct == CounterType.GAUGE else acc_obs
            err = gauge_err if ct == CounterType.GAUGE else acc_err
            if err is not None:
                entry["after"] = before
                entry["status"] = "truth_unavailable"
                out["resources"][rk] = entry
                continue
            truth = self._extract_truth_from_obs(obs, rd)
            if truth is None:
                entry["after"] = before
                entry["status"] = "no_observation"
                out["resources"][rk] = entry
                continue
            true_total, true_per_user = truth
            source = "provider" if ct == CounterType.GAUGE else "ledger"
            entry["source"] = source
            entry["after"] = true_total
            if abs(true_total - before) > self._EPS:
                await self._repair_counter_to_truth(org_id, rd, counter, true_total, true_per_user)
                entry["changed"] = True
                entry["status"] = "repaired"
                try:
                    await self._drift_alert_manager().drift_detected(
                        org_id, rk, observed=true_total, ledger=before,
                        before=before, after=true_total, source=source,
                        resource_type=ct.value)
                except Exception:
                    pass
            else:
                entry["status"] = "in_sync"
                try:
                    await self._drift_alert_manager().drift_resolved(org_id, rk, value=true_total)
                except Exception:
                    pass
            out["resources"][rk] = entry
        return out

    async def _maybe_read_repair(self, org_id: str) -> None:
        """P2.2 — throttled, best-effort read-repair on the usage getter. A plain
        read opportunistically recomputes from truth and repairs the cache. No-op
        when no truth source is wired; throttled per-org so a hot dashboard cannot
        hammer the provider; never raises (a repair failure must not break a read)."""
        if (self._observed_usage_provider is None
                and self._accumulator_usage_provider is None):
            return
        throttle = self._read_repair_throttle_seconds
        if throttle and throttle > 0:
            try:
                key = self._keyspace.readrepair_key(org_id)
                ok = await self._redis.set(key, "1", ex=int(throttle), nx=True)
                if not ok:
                    return  # another read repaired inside the throttle window
            except Exception:
                pass
        try:
            await self.reconcile_org(org_id)
        except Exception as e:
            logger.warning("read_repair_failed org=%s error=%s — read continues", org_id, e)

    # ------------------------------------------------------------------
    # Tier cache management
    # ------------------------------------------------------------------

    def set_alert_manager(self, alert_manager: AlertManager) -> None:
        """Attach an alert manager for WARNING/CRITICAL notifications."""
        self._alert_manager = alert_manager

    def set_drift_alerts(self, drift_alerts) -> None:
        """Attach the DriftAlertManager used for ``quota.drift_detected`` metrics +
        drift alerts emitted by recount-before-deny / reconcile_org / read-repair.
        setup_quota shares the SAME manager the reconciler uses so the engine's
        repairs reach the configured sinks (webhook + scrapeable Redis counters),
        not only the lazily-built log-only default (ticket 20260810, Phase 3)."""
        self._drift_alerts = drift_alerts

    def set_truth_providers(
        self, *, observed_usage_provider=None, accumulator_usage_provider=None,
    ) -> None:
        """Attach/replace the typed truth-source seams after construction (used by
        setup_quota once the consumer callbacks are resolved). None leaves the
        existing provider unchanged is NOT assumed — pass explicitly."""
        self._observed_usage_provider = observed_usage_provider
        self._accumulator_usage_provider = accumulator_usage_provider

    async def invalidate_tier_cache(self, org_id: str) -> None:
        """Clear cached tier for an org. Call from payment webhooks after tier change."""
        if hasattr(self._tier_provider, "invalidate"):
            await self._tier_provider.invalidate(org_id)

    # ------------------------------------------------------------------
    # Feature gating
    # ------------------------------------------------------------------

    def _lowest_tier(self) -> TierConfig:
        """The consumer's OWN lowest tier, by sort_order.

        D-CK-5: the fallback used to be `self._tiers.get("free")` — a tier
        NAME in library logic, which returns None (and then AttributeErrors)
        for any consumer whose entry plan is called something else. The
        catalog is non-empty by construction (QUOTA-CFG-004 refuses
        tiers=None); an explicitly EMPTY catalog is a declaration and raises.
        """
        try:
            return min(self._tiers.values(), key=lambda t: (t.sort_order, t.tier_id))
        except ValueError:
            raise QuotaConfigError(
                name="tier catalog", config_key="tiers", code="QUOTA-CFG-004",
                state="empty catalog — no tier to fall back to",
                env_names=(),
                remedy="declare at least one tier in quota-config.json `tiers[]`",
                docs_anchor="tiers",
            )

    async def check_feature(self, org_id: str, feature_name: str, **provider_kwargs) -> bool:
        """Check if an org's tier includes a feature (e.g. 'gpu_access', 'sso')."""
        tier_id = await self._tier_provider.get_tier(org_id, **provider_kwargs)
        tier = self._tiers.get(tier_id) or self._lowest_tier()
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
                message=MessageBuilder.deny(resource_def, tier, current, limit, requested,
                                            tiers=self._tiers),
                retry_after=retry_after,
                **base,
            )

        # Warning threshold
        utilization = after / limit if limit > 0 else 0
        if utilization >= tier_limits.critical_threshold:
            return QuotaResult(
                decision=QuotaDecision.ALLOW_WARNING,
                severity=AlertSeverity.CRITICAL,
                message=MessageBuilder.warning(
                    resource_def, tier, current, limit, after,
                    warning_threshold=tier_limits.warning_threshold,
                    critical_threshold=tier_limits.critical_threshold),
                **base,
            )
        if utilization >= tier_limits.warning_threshold:
            return QuotaResult(
                decision=QuotaDecision.ALLOW_WARNING,
                severity=AlertSeverity.WARNING,
                message=MessageBuilder.warning(
                    resource_def, tier, current, limit, after,
                    warning_threshold=tier_limits.warning_threshold,
                    critical_threshold=tier_limits.critical_threshold),
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
            # FAIL-CLOSED (FE2 / D-27 shape): an override can RESTRICT a limit BELOW
            # the tier (an org deliberately capped). Swallowing a load IO error and
            # returning None falls back to the HIGHER base limit — the restriction
            # evaporates and the org OVER-admits. An IO error must never become a
            # permission / a wider limit. Re-raise: the admission decision fails
            # closed (deny) rather than silently widening. (A reporting caller like
            # get_usage will surface the error rather than show a wrong limit.)
            logger.error(
                "override_load_error org_id=%s resource_key=%s error=%s — FAILING "
                "CLOSED (an override may restrict below tier; an IO error must not "
                "silently widen the limit).",
                org_id, resource_key, str(e),
            )
            raise
