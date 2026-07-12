"""Library gauge reconciler (P4.1/P4.2/P4.3) — the library's ONLY defence
against its own records being wrong.

Ticket 20260709_ab0t_quota_systemic_integrity_redesign.

WHY THIS EXISTS
---------------
QI-08: reconciliation was specced in the library (ARCHITECTURE.md) and never
built, so every consumer reinvented it (sandbox-platform needed three tickets
and one rejected implementation). This lifts the PROVEN pattern —
``sandbox-platform/app/quota.py::reconcile_org_gauges`` +
``app/database.py::count_live_quota_usage`` — into the library so no other
client rebuilds it.

D-28 promoted it from "nice to have" to load-bearing: the core invariant
``counter == Σ open activations`` is an INTERNAL-CONSISTENCY check. It compares a
cache (the counter) to a ledger (the activations). It cannot, even in principle,
detect the ledger diverging from REALITY — a lost row, a dropped write, a
resource that exists with nothing recorded. The ``observed_usage_provider`` seam
is the ONLY place this library touches the world; the reconciler is the mechanism
that acts on it.

THE LAW IT IMPLEMENTS — D-33 (SUPERSEDES D-10's wording)
--------------------------------------------------------
Three layers, each authoritative over exactly ONE thing:

  * ``observed_usage_provider``  -> EXISTENCE (what is actually live)
  * activation ledger            -> IDENTITY and COST
  * Redis counter                -> NOTHING; a cache of the level

Therefore:
  1. No provider configured  -> converge the counter to ``Σ open activations``
     (the zero-config self-heal). A handle-using client heals with no code.
  2. Provider configured AND it disagrees with the ledger about EXISTENCE ->
     that is a BUG, not drift. Converge the counter to the PROVIDER's observed
     set, flag the record for repair, and ALERT. NEVER silently reconcile it
     away — a silent reconcile here is internally consistent and factually wrong
     (the QB-01 inversion).
  3. Reality wins, but not instantly. Keep the recent-activity guard: never
     force-set a (org, resource) touched inside the guard window — the provider
     lags creation. Every force-set is AUDITED (before/after/which source won).
  4. Gauges only. Accumulators are NEVER reconciled.
  5. Fail-direction (D-31): if the provider is UNREACHABLE, do NOTHING and ALERT.
     Do NOT fall back to converging on the ledger — that is precisely the
     operation that erases reality when the record is the thing that's broken.

DEFAULTS ON (D-28 consequence 2)
--------------------------------
The re-raise in ``engine.acquire`` leaves an orphaned counter over-count on a
persist failure, healed ONLY by a running reconciler. A consumer with no
reconciler wiring holds that over-count indefinitely (fail-closed — it denies,
never grants, but it denies forever). A correctness feature a client must
remember to switch on is a correctness feature that is off. So ``ReconcileConfig``
defaults enabled, and ``setup_quota`` starts a background pass by default.

BOUNDARY (unchanged, binding)
-----------------------------
This module is GENERIC. Consumer-domain semantics (what "live" means, which
product rows count) enter ONLY via the ``observed_usage_provider`` callback —
their DB, their consuming-state semantics. They never enter this library.

REAL-BACKEND CAVEAT
-------------------
Verified only under ``fakeredis[lua]`` (lupa) + ``InMemoryActivationStore`` in
tests — NOT a real Redis ``EVAL`` nor real DynamoDB. See the phase-4 artifact's
not_verified section.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Iterable, Optional

from .counters.factory import create_counter
from .models.core import CounterType
from .models.requests import QuotaResetRequest

logger = logging.getLogger("ab0t_quota.reconcile")

# The shape the observed_usage_provider returns — identical to sandbox-platform's
# ``count_live_quota_usage``: {resource_key: {"total": float, "per_user": {uid: float}}}.
ObservedUsage = dict
ObservedUsageProvider = Callable[[str], "ObservedUsage | Awaitable[ObservedUsage]"]

# Recent-activity marker key. Same shape sandbox-platform sets on every gauge
# increment/decrement, so a consumer's existing touch keeps working after the
# P4.4 migration AND a handle-using consumer needs no touch at all (the ledger's
# opened_at is used as a second, zero-config signal).
_RECENT_KEY = "quota:reconcile:recent:{org_id}"

_ADMIN_USER = "system:library-reconciler"
_EPS = 1e-9


def _reconcile_enabled_env() -> bool:
    """Kill-switch, mirroring the consumer's proven POOL_QUOTA_RECONCILE_ENABLED."""
    v = os.getenv("AB0T_QUOTA_RECONCILE_ENABLED")
    if v is None:
        return True
    return v.strip().lower() not in ("off", "false", "0", "no")


@dataclass
class ReconcileConfig:
    """Every knob is a config option with a best-practice default."""
    enabled: bool = field(default_factory=_reconcile_enabled_env)
    interval_seconds: float = 300.0
    # D-10's `truth.source` (specced there, only `activations` was built until now):
    #   * "activations" — the activation LEDGER is authoritative; the provider (if
    #     any) is checked for existence and a disagreement is a BUG (D-33).
    #   * "provider" — the observed_usage_provider IS the sole truth. No ledger; the
    #     counter is force-set to the provider's observed set. This is the model a
    #     consumer on the legacy increment/decrement path uses (it never mints
    #     activations, so it has no ledger to compare against) — e.g. sandbox-platform,
    #     whose count_live_quota_usage reads its own product rows. No divergence alarm
    #     fires (there is no record claiming otherwise), and the activation-store
    #     durability gate (D-39) does not apply (the ledger is unused).
    # (The "counter" value is intentionally unimplemented — a counter can never be
    # authoritative over itself; D-10/D-33 make it a cache.)
    truth_source: str = "activations"
    # Skip an org whose (org, resource) was touched inside this window — the
    # provider lags creation. Matches sandbox's QUOTA_RECONCILE_ACTIVITY_GUARD_SECONDS.
    activity_guard_seconds: int = field(
        default_factory=lambda: max(
            int(os.getenv("QUOTA_RECONCILE_ACTIVITY_GUARD_SECONDS", "90") or 90), 0))
    # A drift storm must not become a self-inflicted DDB incident (FUTURE §3):
    # a bounded number of force-SETS and orgs per pass, plus optional pacing.
    max_force_sets_per_pass: int = 500
    max_orgs_per_pass: int = 5000
    pacing_sleep_seconds: float = 0.0
    # D-37: the BACKGROUND loop must REFUSE to run against a per-process
    # in-memory activation store. Deriving the target from a partial (per-replica)
    # ledger and force-setting a SHARED Redis counter UNDER-counts — the one
    # forbidden direction (D-31). Not-reconciling leaves a fail-CLOSED over-count
    # (D-27); reconciling from a partial view is strictly worse.
    refuse_in_memory_store: bool = True
    # D-39: the activation LEDGER is authoritative for identity+cost (D-33) — it
    # must be DURABLE, not an evictable cache. An in-memory store is per-process;
    # a Redis store with an allkeys-* eviction policy (or no persistence) can
    # silently drop OPEN rows → the reconciler sees fewer open → converges the
    # shared counter DOWN → under-count (the forbidden direction). So the
    # reconciler REFUSES to run against a non-durable ledger (guard the OPERATION,
    # not the scheduler: run_once() and the loop both enforce it, not only
    # start()). `force=True` on run_once is an explicit acknowledgement that the
    # caller accepts a partial view. Redis durability is machine-checked via
    # W-PY-A's check_redis_outbox_durability (reused, not reimplemented).
    require_durable_ledger: bool = True
    redis_durability_confirmed: bool = False
    # D-52: bound the observed_usage_provider. An async provider that hangs (a
    # slow/locked consumer DB query) would otherwise stall the whole pass and the
    # background loop indefinitely. On timeout, treat it as UNREACHABLE — do
    # nothing + alert (D-31); NEVER fall back to the ledger. 0/None disables.
    provider_timeout_seconds: float = 10.0
    # D-51: ABSENCE MEANS UNKNOWN, NEVER AN AFFIRMATIVE VALUE. A resource_key
    # MISSING from the provider result is "no observation" → skip + alert (fail
    # closed). An explicit `total: 0` is "observed zero" → converge. `converge`
    # restores the legacy absence=0 behaviour for a consumer who has PROVEN their
    # provider distinguishes "no data" from "zero".
    empty_provider: str = "skip_and_alert"   # skip_and_alert | converge
    # D-53: a provider whose per_user totals don't sum to its org total is a
    # CONSUMER bug. validate_and_alert: converge from what it gave AND alert (never
    # silently pick a side). trust: accept the numbers verbatim, no alert.
    per_user_sum: str = "validate_and_alert"  # validate_and_alert | trust
    # D2 (QB-03): the missed-decrement ALARM. An activation OPEN past this many
    # seconds with no release is surfaced as a money incident FROM the reconciler
    # pass (engine.stale_open_activations had no periodic caller, so the alarm for
    # the bug that shipped 3× never fired). Conservative default (7d) so a
    # legitimately long-lived resource doesn't false-alarm; 0 disables.
    stale_activation_seconds: float = field(
        default_factory=lambda: max(
            float(os.getenv("QUOTA_STALE_ACTIVATION_SECONDS", "604800") or 604800), 0.0))


@dataclass
class OrgReconcileResult:
    org_id: str
    skipped: Optional[str] = None            # "disabled" | "recent_activity" | "provider_unreachable"
    changes: dict = field(default_factory=dict)   # resource_key -> {"total": {from,to}, "source", ...}
    divergences: list = field(default_factory=list)  # resource_keys where provider != ledger (a BUG)
    force_sets: int = 0                       # audited counter mutations applied
    stale_open: int = 0                       # QB-03: OPEN activations past the stale threshold


@dataclass
class PassReconcileResult:
    orgs_reconciled: int = 0
    force_sets: int = 0
    divergences: int = 0
    skipped_recent: int = 0
    skipped_unreachable: int = 0
    backpressure: bool = False               # the per-pass budget stopped the pass early
    skipped: Optional[str] = None            # "disabled" for the whole pass


class _Budget:
    """A mutable per-pass force-set budget shared across orgs."""
    def __init__(self, limit: int):
        self.remaining = limit
        self.exhausted = False

    def take(self) -> bool:
        if self.remaining <= 0:
            self.exhausted = True
            return False
        self.remaining -= 1
        return True


class LibraryReconciler:
    """Reconciles GAUGE counters to their authoritative level (D-33).

    Args:
      engine: the QuotaEngine (for its redis, activation store, registry, the
        audited ``engine.reset``, and the alert manager).
      observed_usage_provider: OPTIONAL ``fn(org_id) -> {resource_key: {"total",
        "per_user"}}``. Its ABSENCE is the zero-config default (converge to the
        ledger). Sync or async. Consumer-owned — their DB, their semantics.
      drift_alerts: OPTIONAL DriftAlertManager. Defaults to a log-only one built
        from the engine's redis.
      config: OPTIONAL ReconcileConfig.
    """

    def __init__(
        self,
        engine,
        *,
        observed_usage_provider: Optional[ObservedUsageProvider] = None,
        drift_alerts=None,
        config: Optional[ReconcileConfig] = None,
        redis=None,
        activation_store=None,
        registry=None,
        gauge_resource_keys: Optional[Iterable[str]] = None,
        preflight=None,
    ):
        self._engine = engine
        self._provider = observed_usage_provider
        self._config = config or ReconcileConfig()
        if self._config.truth_source == "provider" and observed_usage_provider is None:
            # provider mode with no provider would converge every gauge to 0 (the
            # provider's "nothing") — a silent wipe. Refuse the misconfiguration loudly.
            raise ValueError(
                "ReconcileConfig.truth_source='provider' requires an "
                "observed_usage_provider — without it the reconciler would force "
                "every gauge to 0 (a silent wipe).")
        self._redis = redis if redis is not None else engine._redis
        self._store = (activation_store if activation_store is not None
                       else getattr(engine, "_activation_store", None))
        self._registry = registry if registry is not None else engine._registry
        if drift_alerts is None:
            from .alerts import DriftAlertManager
            drift_alerts = DriftAlertManager(redis=self._redis)
        self._alerts = drift_alerts
        self._gauge_keys = (list(gauge_resource_keys)
                            if gauge_resource_keys is not None
                            else self._discover_gauge_keys())
        self._task: Optional[asyncio.Task] = None
        self._refused_unsafe = False   # set by start() if the store is unsafe (D-37)
        # D-75 — "an assumption machine-checked once is an assumption trusted thereafter."
        # An optional async callable that RE-VERIFIES the library's infrastructure
        # invariants (Redis topology/eviction/scripting/version/headroom; the DDB tables)
        # once per pass. It RIDES THIS LOOP deliberately: a new worker is one more thing
        # that can be dead (D-50). It must never raise — a safe→unsafe transition is LOUD,
        # NOT FATAL (degrade health + alert), because a running service that suddenly
        # refuses is its own outage.
        self._preflight = preflight

    # ------------------------------------------------------------------ helpers

    def _discover_gauge_keys(self) -> list[str]:
        return [rd.resource_key for rd in self._registry.all()
                if rd.counter_type == CounterType.GAUGE]

    def _store_is_in_memory(self) -> bool:
        from .activations import InMemoryActivationStore
        return isinstance(self._store, InMemoryActivationStore)

    def unsafe_capability(self) -> Optional[str]:
        """SYNC fast-path: return the Capabilities OFF reason if the store is
        in-memory (D-37) — the one check start() can make without awaiting. A
        non-durable *Redis* ledger (D-39) is caught by the ASYNC durability gate
        in run_once()/the loop (it needs CONFIG GET). None if the sync check passes."""
        if self._config.refuse_in_memory_store and self._store_is_in_memory():
            return ("OFF — activation store is in-memory (per-process); unsafe "
                    "with a shared counter (would under-count, D-31/D-37)")
        return None

    async def ledger_durability(self) -> tuple[bool, str]:
        """Is the activation ledger DURABLE enough to reconcile a shared counter
        from (D-39)? The ledger is authoritative for identity+cost (D-33), so a
        store that can silently lose rows makes the reconciler under-count.
          * InMemoryActivationStore  → NOT durable (per-process).
          * RedisActivationStore     → machine-checked (reuses W-PY-A's
            check_redis_outbox_durability — persistence + non-evicting policy, or
            the on-the-record redis_durability_confirmed for ElastiCache).
          * DDBActivationStore       → durable.
          * any other (consumer-wired) store → trusted (they chose it deliberately).
        Returns (durable, reason)."""
        if self._config.truth_source == "provider":
            # provider mode does not read the ledger at all (the provider is the
            # sole truth), so its durability is irrelevant — the provider (the
            # consumer's product store) is the durable record. Never refuse here.
            return True, "provider-mode (activation ledger unused)"
        from .activations import (
            InMemoryActivationStore, RedisActivationStore, DDBActivationStore,
        )
        store = self._store
        if isinstance(store, InMemoryActivationStore):
            return False, "activation store is in-memory (per-process)"
        if isinstance(store, DDBActivationStore):
            return True, "DDB"
        if isinstance(store, RedisActivationStore):
            redis = getattr(store, "_redis", None)
            if redis is None:
                return False, "Redis activation store has no client"
            # REUSE, don't reimplement (D-35): a second durability check would be
            # the same duplication mistake in a new costume.
            from .billing.outbox import check_redis_outbox_durability
            durable, detail = await check_redis_outbox_durability(
                redis, confirmed=self._config.redis_durability_confirmed)
            return durable, f"Redis ({detail})"
        return True, f"custom store {type(store).__name__}"

    async def _call_provider(self, org_id: str) -> ObservedUsage:
        """Invoke the consumer's provider (sync or async). Raises on failure —
        the caller turns that into 'do nothing + alert' (D-31).

        D-52: an async provider is BOUNDED by provider_timeout_seconds; a timeout
        raises (asyncio.TimeoutError, a subclass of Exception) and is therefore
        handled by the same unreachable path — do nothing + alert. A SYNC provider
        that blocks cannot be bounded here (it blocks the loop before we await);
        that residual is noted in the artifact."""
        result = self._provider(org_id)
        if inspect.isawaitable(result):
            timeout = self._config.provider_timeout_seconds
            if timeout and timeout > 0:
                result = await asyncio.wait_for(result, timeout)
            else:
                result = await result
        return result or {}

    async def _is_recent_activity(self, org_id: str, opens: list) -> bool:
        """The recent-activity guard (D-33 §3). Two signals, either suffices:
          (a) the consumer's touch-key is live (parity with sandbox), OR
          (b) an open activation was opened inside the guard window (zero-config:
              a handle-using consumer needs no touch at all)."""
        try:
            if await self._redis.get(_RECENT_KEY.format(org_id=org_id)):
                return True
        except Exception:
            # The guard failing OPEN (treating as not-recent) would let us
            # force-set during in-flight traffic. Fail SAFE: treat a guard-read
            # error as recent (skip), never as clear.
            logger.warning("reconcile_guard_read_failed org=%s — treating as recent", org_id)
            return True
        window = self._config.activity_guard_seconds
        if window <= 0:
            return False
        now = datetime.now(timezone.utc)
        for a in opens:
            try:
                opened = datetime.fromisoformat(a.opened_at)
            except (ValueError, TypeError, AttributeError):
                continue
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            if (now - opened).total_seconds() <= window:
                return True
        return False

    @staticmethod
    def _ledger_totals(opens: list, resource_key: str) -> tuple[float, dict]:
        total = 0.0
        per_user: dict[str, float] = {}
        for a in opens:
            delta = float((a.spend or {}).get(resource_key, 0.0))
            if delta == 0.0:
                continue
            total += delta
            if a.user_id:
                per_user[a.user_id] = per_user.get(a.user_id, 0.0) + delta
        return total, per_user

    def _resolve_existence(
        self, *, ledger_total: float, provider_observed: Optional[float],
    ) -> tuple[float, str, bool]:
        """The precedence LAW for a GAUGE's EXISTENCE level (D-33). Returns
        (target_value, winning_source, is_divergence).

        THIS IS THE ONE HOME OF THE LAW (D-35). ``activations.resolve_gauge_level``
        used to encode a competing D-10 version; it has been reduced to a pure
        mechanism (Σ open activations) so that "which source wins" is decided here
        and nowhere else — two implementations of the law WAS the multi-source
        hazard this ticket exists to kill (FUTURE §1). Under D-33: when a provider
        is configured and disagrees with the ledger about existence, the provider
        WINS (it is authoritative for existence) and the disagreement is a BUG.
        Kept a separate method so the negative-control test can override it with
        the forbidden (converge-to-ledger) behaviour and prove the guard catches it.
        """
        if self._config.truth_source == "provider":
            # D-10 `truth.source=provider`: the provider IS the sole truth. There is
            # no ledger to diverge from, so we converge to the provider's observed
            # existence (0 when it reports nothing) and NEVER raise a divergence
            # alarm. This is the provider-authoritative model of a legacy-increment
            # consumer (sandbox-platform's reconcile_org_gauges, now library-owned).
            return (provider_observed if provider_observed is not None else 0.0), "provider", False
        if provider_observed is None:
            # D-33 §1: no provider -> the ledger is the best proxy for existence.
            return ledger_total, "activations", False
        if abs(provider_observed - ledger_total) <= _EPS:
            # provider and ledger AGREE — converge (to the shared value).
            return ledger_total, "activations", False
        # D-33 §2: they DISAGREE about existence. The provider is authoritative
        # for existence; the ledger lost or invented a row. Provider wins; BUG.
        return provider_observed, "provider", True

    async def _audited_force_set(
        self, org_id: str, resource_key: str, *, target_total: float,
        target_per_user: dict, source: str, reason: str,
    ) -> dict:
        """Force-set the org gauge AND its per-user partitions to the target
        (QI-06 — a repair tool that repairs half the state guarantees org/user
        divergence). Every mutation is audited (before/after/which source won).
        Mirrors sandbox-platform's reconcile_org_gauges."""
        rd = self._registry.require(resource_key)
        counter = create_counter(self._redis, org_id, rd)
        change: dict = {"source": source}

        current_total = await counter.get()
        if abs(current_total - target_total) > _EPS:
            # Org total goes through the AUDITED engine.reset (ADMIN_QUOTA_RESET
            # log line: admin_user_id, before, after, reason).
            await self._engine.reset(QuotaResetRequest(
                org_id=org_id, resource_key=resource_key,
                new_value=target_total, reason=reason, admin_user_id=_ADMIN_USER,
            ))
            change["total"] = {"from": current_total, "to": target_total}

        # Per-user partitions: sync to target, delete/zero the stale ones. No
        # audited engine primitive exists for per-user (QI-06), so each write
        # carries its own log line — exactly as the sandbox reference does.
        user_prefix = counter._user_key("")   # "quota:{org}:{rk}:gauge:user:"
        existing: set[str] = set()
        try:
            async for key in self._redis.scan_iter(match=f"{user_prefix}*", count=100):
                existing.add(key.decode() if isinstance(key, bytes) else str(key))
        except Exception as e:
            logger.warning("reconcile_user_scan_failed org=%s resource=%s error=%s",
                           org_id, resource_key, e)
        for uid, uval in (target_per_user or {}).items():
            ucurrent = await counter.get_user(uid)
            if abs(ucurrent - float(uval)) > _EPS:
                await counter.reset_user(uid, float(uval))
                logger.warning(
                    "reconcile_gauge_user_reconciled org=%s resource=%s user=%s "
                    "previous=%s new=%s source=%s reason=%s",
                    org_id, resource_key, uid, ucurrent, uval, source, reason,
                )
                change.setdefault("users", {})[uid] = {"from": ucurrent, "to": float(uval)}
            existing.discard(user_prefix + str(uid))
        for stale_key in existing:
            sraw = await self._redis.get(stale_key)
            scurrent = float(sraw) if sraw else 0.0
            if scurrent != 0.0:
                await self._redis.delete(stale_key)
                stale_uid = stale_key[len(user_prefix):]
                logger.warning(
                    "reconcile_gauge_stale_user_cleared org=%s resource=%s user=%s "
                    "previous=%s source=%s reason=%s",
                    org_id, resource_key, stale_uid, scurrent, source, reason,
                )
                change.setdefault("users", {})[stale_uid] = {"from": scurrent, "to": 0.0}
        return change

    # ------------------------------------------------------------------ per-org

    async def reconcile_org(
        self, org_id: str, *, reason: str = "periodic_gauge_reconcile",
        _budget: Optional[_Budget] = None,
    ) -> OrgReconcileResult:
        """Reconcile every GAUGE for one org to its authoritative level (D-33)."""
        res = OrgReconcileResult(org_id=org_id)
        if not self._config.enabled:
            res.skipped = "disabled"
            return res
        provider_mode = self._config.truth_source == "provider"
        if self._store is None and not provider_mode:
            logger.error("reconcile_no_activation_store org=%s — cannot enumerate the "
                         "ledger; skipping", org_id)
            res.skipped = "no_store"
            return res

        # provider mode has NO ledger — the provider is the sole truth; skip the
        # (unnecessary, possibly-failing) open-activation read entirely.
        opens = [] if provider_mode else await self._store.list_open(org_id)

        # Provider (D-33): configured => authoritative for existence; unreachable
        # => do NOTHING and alert (D-31); absent => converge to the ledger.
        provider_result: Optional[ObservedUsage] = None
        if self._provider is not None:
            try:
                provider_result = await self._call_provider(org_id)
            except Exception as e:
                logger.error("reconcile_provider_unreachable org=%s error=%s — doing "
                             "NOTHING (D-31); NOT converging to the ledger", org_id, e)
                await self._alerts.provider_unreachable(org_id, error=str(e))
                res.skipped = "provider_unreachable"
                return res

        recent = await self._is_recent_activity(org_id, opens)

        for rk in self._gauge_keys:
            ledger_total, ledger_per_user = self._ledger_totals(opens, rk)
            observed_total: Optional[float] = None
            observed_per_user: dict = {}
            if provider_result is not None:
                if rk in provider_result:
                    obs = provider_result.get(rk) or {}
                    observed_total = float(obs.get("total", 0.0))
                    observed_per_user = {
                        str(u): float(v) for u, v in (obs.get("per_user") or {}).items()
                    }
                    # D-53: a per_user set that doesn't sum to the org total is a
                    # CONSUMER bug — converge from what it gave, but make it visible.
                    if (self._config.per_user_sum == "validate_and_alert"
                            and observed_per_user
                            and abs(sum(observed_per_user.values()) - observed_total) > _EPS):
                        logger.error(
                            "reconcile_per_user_sum_mismatch org=%s resource=%s total=%s "
                            "sum_per_user=%s — the provider is inconsistent; converging from "
                            "its numbers and alerting (never silently picking a side).",
                            org_id, rk, observed_total, sum(observed_per_user.values()))
                        await self._alerts.per_user_sum_mismatch(
                            org_id, rk, total=observed_total,
                            per_user_sum=sum(observed_per_user.values()))
                elif self._config.empty_provider == "converge":
                    # D-51 opt-out: absence = observed zero (legacy). Only for a
                    # consumer who has PROVEN their provider omits ⇒ zero.
                    observed_total = 0.0
                else:
                    # D-51 DEFAULT skip_and_alert: a MISSING key is "no observation",
                    # not zero. Absence must not wipe the counter (that is the
                    # under-count/phantom-headroom direction, D-31). Skip + alert.
                    logger.warning(
                        "reconcile_missing_observation org=%s resource=%s — the provider "
                        "returned NO observation for this key (absence != zero, D-51); "
                        "leaving the counter untouched and alerting.", org_id, rk)
                    await self._alerts.missing_observation(org_id, rk)
                    res.skipped = res.skipped or "provider_incomplete"
                    continue

            target_total, source, divergence = self._resolve_existence(
                ledger_total=ledger_total, provider_observed=observed_total)
            target_per_user = observed_per_user if source == "provider" else ledger_per_user

            counter = create_counter(self._redis, org_id, self._registry.require(rk))
            current_total = await counter.get()
            needs_set = divergence or abs(current_total - target_total) > _EPS

            if not needs_set:
                # In sync — if a drift was previously flagged active, resolve it.
                await self._alerts.drift_resolved(org_id, rk, value=target_total)
                continue

            if recent:
                # Reality wins, but not instantly (D-33 §3): the provider lags a
                # just-created resource. Never force-set inside the guard window.
                res.skipped = "recent_activity"
                logger.info("reconcile_skip_recent org=%s resource=%s", org_id, rk)
                continue

            if _budget is not None and not _budget.take():
                logger.warning("reconcile_budget_exhausted org=%s resource=%s — "
                               "backpressure; deferring to next pass", org_id, rk)
                break

            if source == "provider":
                # D-33 §2: provider (existence) wins over a diverging ledger. The
                # provider gives EXISTENCE only (no activation identity/cost), so we
                # converge the COUNTER to it and audit it, but we do NOT fabricate or
                # delete ledger rows (D-35: the library must never mint money-bearing
                # identity from existence-only data). Audited force-set to the
                # provider's observed set + per-user.
                change = await self._audited_force_set(
                    org_id, rk, target_total=target_total,
                    target_per_user=observed_per_user, source=source, reason=reason)
            else:
                # source == "activations" (no provider, or provider AGREES): converge
                # to Σ open activations via the ONE mechanism (D-35). converge_gauge
                # sets the org counter + derives per-user + clears stale per-user keys.
                from .activations import converge_gauge
                await converge_gauge(
                    activation_store=self._store, org_id=org_id, resource_key=rk,
                    counter=counter, opens=opens)
                change = {"source": source,
                          "total": {"from": current_total, "to": target_total}}
            if change.get("total") or change.get("users"):
                res.changes[rk] = change
                res.force_sets += 1

            if divergence:
                res.divergences.append(rk)
                logger.error(
                    "reconcile_ledger_reality_divergence org=%s resource=%s provider=%s "
                    "ledger=%s — the RECORD is wrong (lost/invented a row). Counter set "
                    "to the provider's observed existence; the ledger needs repair "
                    "(identity/cost are the ledger's authority, not the provider's).",
                    org_id, rk, observed_total, ledger_total,
                )
                # D-36: name the real consequence. When the provider sees MORE live
                # than the ledger records, those resources have NO activation row →
                # their usage cannot be settled → un-billable (QB-01's signature). It
                # must read as a MONEY incident, not a counter nit.
                await self._alerts.divergence_detected(
                    org_id, rk, provider=(observed_total or 0.0), ledger=ledger_total)
            else:
                # A plain heal (over-count → Σ open). Distinct, rate-limited channel
                # so a heal never reads as a limit-incident and cannot alert-storm.
                await self._alerts.drift_detected(
                    org_id, rk, observed=(observed_total if observed_total is not None
                                          else target_total),
                    ledger=ledger_total, before=current_total, after=target_total,
                    source=source)

        # D2 (QB-03): the missed-decrement ALARM. `engine.stale_open_activations`
        # was implemented and never wired to a periodic caller, so the alarm for
        # the leak that shipped 3× in the reference consumer never fired. Provider
        # reconciliation heals the counter VALUE; this tells a HUMAN an activation
        # has been OPEN past any legitimate lifetime (likely a missed release —
        # a leaked slot that denies capacity and may accrue usage unbilled).
        if (not provider_mode and self._store is not None
                and self._config.stale_activation_seconds > 0 and opens):
            from .activations import stale_open_activations as _stale_filter
            stale = _stale_filter(opens, older_than_s=self._config.stale_activation_seconds)
            if stale:
                res.stale_open = len(stale)
                await self._alerts.stale_open_activations(
                    org_id, count=len(stale),
                    older_than_s=self._config.stale_activation_seconds)
        return res

    # ------------------------------------------------------------------ per-pass

    async def run_once(
        self, org_ids: Optional[Iterable[str]] = None,
        *, reason: str = "periodic_gauge_reconcile", force: bool = False,
    ) -> PassReconcileResult:
        """One reconciliation pass. Enumerates orgs from the LEDGER's open index
        (D-10/E3) when ``org_ids`` is not given — never from a counter snapshot, so
        a value the activations don't justify cannot enumerate itself back in.

        D-39: REFUSES to run against a non-durable ledger (in-memory, or an
        evictable/unconfirmed Redis) — guarding the OPERATION, not the scheduler,
        because a cron wired to run_once meets the identical hazard through a public
        API. ``force=True`` is an explicit acknowledgement that the caller accepts a
        partial view (e.g. a single-process test/tool)."""
        result = PassReconcileResult()
        if not self._config.enabled:
            result.skipped = "disabled"
            return result

        if self._config.require_durable_ledger and not force:
            durable, reason_txt = await self.ledger_durability()
            if not durable:
                logger.error(
                    "reconcile_refused_non_durable_ledger — %s. The activation ledger "
                    "is authoritative for identity+cost (D-33); reconciling a shared "
                    "counter from a store that can silently lose rows UNDER-counts "
                    "(D-31/D-39). Wire a durable store (DDB, or Redis with persistence "
                    "+ a non-evicting policy / redis_durability_confirmed), or pass "
                    "force=True to accept a partial view.", reason_txt)
                result.skipped = "ledger_not_durable"
                return result

        if org_ids is None:
            org_ids = await self._enumerate_orgs()

        budget = _Budget(self._config.max_force_sets_per_pass)
        for i, org_id in enumerate(org_ids):
            if i >= self._config.max_orgs_per_pass:
                result.backpressure = True
                logger.warning("reconcile_max_orgs_reached count=%d — deferring rest", i)
                break
            org_res = await self.reconcile_org(org_id, reason=reason, _budget=budget)
            result.orgs_reconciled += 1
            result.force_sets += org_res.force_sets
            result.divergences += len(org_res.divergences)
            if org_res.skipped == "recent_activity":
                result.skipped_recent += 1
            elif org_res.skipped == "provider_unreachable":
                result.skipped_unreachable += 1
            if budget.exhausted:
                result.backpressure = True
                logger.warning("reconcile_pass_budget_exhausted force_sets=%d — "
                               "deferring the rest of this drift storm to next pass",
                               result.force_sets)
                break
            if self._config.pacing_sleep_seconds > 0:
                await asyncio.sleep(self._config.pacing_sleep_seconds)
        return result

    async def _enumerate_orgs(self) -> list[str]:
        """Which orgs to reconcile this pass, when the caller passes none.

        * activations mode: orgs with >=1 OPEN activation, from the LEDGER's open
          index (NOT the counter snapshot — D-10/E3).
        * provider mode: there is no ledger, so the candidate population is every
          org that HAS a gauge counter — exactly the orgs that can be stuck-high.
          Enumerate them from the library's OWN gauge keyspace (`quota:{org}:{rk}:gauge`).
          This is generic (the library's own key format), not consumer-specific, and
          it is the population sandbox-platform's bespoke loop scanned by hand.
        """
        if self._config.truth_source == "provider":
            gauge_suffix = ":gauge"
            orgs: set[str] = set()
            try:
                async for key in self._redis.scan_iter(match="quota:*:gauge", count=200):
                    ks = key.decode() if isinstance(key, bytes) else str(key)
                    # quota:{org}:{resource_key}:gauge — org is a UUID and resource
                    # keys are dotted (no colons), so a 4-part split is unambiguous;
                    # per-user keys (…:gauge:user:{uid}) don't end in ':gauge'.
                    parts = ks.split(":")
                    if len(parts) == 4 and parts[0] == "quota" and ks.endswith(gauge_suffix):
                        if not self._gauge_keys or parts[2] in self._gauge_keys:
                            orgs.add(parts[1])
            except Exception as e:
                logger.error("reconcile_enumerate_provider_failed error=%s", e)
            return sorted(orgs)
        try:
            targets = await self._store.open_gauge_targets()
        except Exception as e:
            logger.error("reconcile_enumerate_failed error=%s", e)
            return []
        return sorted({org for (org, _rk) in targets})

    # ------------------------------------------------------------------ lifecycle

    def start(self, interval_seconds: Optional[float] = None) -> bool:
        """Start the background reconcile loop. Idempotent. Returns True if
        started, False if REFUSED (unsafe store, D-37) or disabled.

        D-37: refuse to AUTO-RUN against a per-process in-memory activation store.
        The background loop force-sets a SHARED Redis counter from Σ open
        activations; if the ledger is per-replica in-memory, each replica sees
        only its own activations and drives the shared counter DOWN to its
        partial view — an UNDER-count, the forbidden direction (D-31). Refusing
        leaves a fail-CLOSED over-count (D-27), which is strictly safer than
        reconciling from a partial view."""
        reason = self.unsafe_capability()
        if reason is not None:
            self._refused_unsafe = True
            logger.error(
                "quota reconciler REFUSING to start — %s. Wire a durable, SHARED "
                "activation store (RedisActivationStore/DDBActivationStore) into the "
                "engine; in-memory is dev-only. The over-count stays fail-closed until "
                "then (denies, never grants).", reason)
            return False
        if not self._config.enabled:
            return False
        if self._task is not None and not self._task.done():
            return True
        interval = interval_seconds if interval_seconds is not None else self._config.interval_seconds
        self._task = asyncio.create_task(self._loop(interval), name="ab0t_quota_reconciler")
        logger.info("quota reconciler started interval=%ss enabled=%s provider=%s",
                    interval, self._config.enabled, self._provider is not None)
        return True

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None

    def loop_liveness(self) -> tuple[bool, str]:
        """D-50: is the reconciler loop LIVE? Money-critical — a reconciler that
        refused/stopped leaves an orphaned counter over-count that never heals
        (D-28). Returns (healthy, detail).

        Healthy when disabled by config (a known state already in the `reconciler`
        capability), or not started here (manual run_once mode), or running.
        Unhealthy when it REFUSED an unsafe/non-durable ledger (D-37/D-39) or the
        loop task died — a startup log line alone is not enough; /quota/health must
        fail so a human sees it."""
        if not self._config.enabled:
            return True, "reconciler disabled by config"
        if self._refused_unsafe:
            return False, "reconciler refused/stopped — unsafe or non-durable activation ledger (D-37/D-39)"
        task = self._task
        if task is None:
            return True, "reconciler loop not started here (manual run_once)"
        if task.done():
            return False, "reconciler loop task is no longer running (died)"
        return True, "on"

    async def revalidate(self) -> None:
        """D-75 — one re-verification of the infrastructure invariants. Called by the loop
        each pass; exposed so an operator/cron can force one. Never raises."""
        if self._preflight is None:
            return
        try:
            await self._preflight()
        except Exception as e:  # a broken re-check must never kill the loop (D-50)
            logger.error("infrastructure re-verification failed (D-75): %s", e)

    async def _loop(self, interval: float) -> None:
        while True:
            # D-75: re-verify the world BEFORE reconciling against it. Runs every pass,
            # independent of whether the reconcile pass itself does any work.
            await self.revalidate()
            try:
                if self._refused_unsafe:
                    # D-39 + D-75: the ledger became non-durable (e.g. someone flipped the
                    # Redis to allkeys-* at runtime). STOP RECONCILING — converging a shared
                    # counter from a store that can silently lose rows UNDER-counts (D-31) —
                    # but DO NOT stop the loop. The loop is now the thing that will notice
                    # when the operator FIXES it: re-check durability cheaply each pass and
                    # RESUME. Killing the loop here (the old behaviour) meant a repaired
                    # Redis stayed degraded until someone restarted the process, and no
                    # `restored` alert could ever fire (D-26's resolve trail).
                    durable, why = await self.ledger_durability()
                    if not durable:
                        await asyncio.sleep(interval)
                        continue
                    self._refused_unsafe = False
                    logger.info("reconcile RESUMING — the activation ledger is durable again "
                                "(D-75): %s", why)

                res = await self.run_once()
                if res.skipped == "ledger_not_durable":
                    # D-39: the ledger is not durable. run_once already logged the ERROR.
                    # Mark refused so Capabilities/loop_liveness read OFF (a human sees a
                    # FAILING /quota/health, not a log line) — and keep the loop alive so
                    # the re-verification above can heal it without a restart (D-75).
                    self._refused_unsafe = True
                    logger.error("reconcile SUSPENDED — ledger not durable (D-39); "
                                 "wire a durable store or set redis_durability_confirmed. "
                                 "The loop stays alive and will RESUME automatically if the "
                                 "store becomes durable again.")
                    await asyncio.sleep(interval)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as e:  # a pass error must never kill the loop
                logger.error("reconcile_pass_failed error=%s", e)
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
