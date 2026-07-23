"""One-line drop-in setup for ab0t-quota.

Consumers add a single line to their FastAPI app and get the engine,
rate-limiting middleware, /api/quotas/* endpoints, snapshot worker,
heartbeat monitor, lifecycle emitter (with cost auto-recording), and
the optional paid-tier surface (pricing/checkout/portal/invoices/webhook).

Usage::

    from fastapi import FastAPI
    from ab0t_quota import setup_quota

    app = FastAPI()
    setup_quota(app)        # one line. Done.

If you also have your own lifespan, pass it before calling setup_quota::

    @asynccontextmanager
    async def my_lifespan(app):
        # my own setup
        yield
        # my own teardown

    app = FastAPI(lifespan=my_lifespan)
    setup_quota(app)        # composes its async init around yours

The consumer never imports any other mesh service client and never sets
URL env vars for upstream services. They only need:

  * `AB0T_MESH_API_KEY` — single mesh credential
  * `quota-config.json`  — tier definitions, resources, bundles, pricing
  * `AB0T_CONSUMER_ORG_ID` (only when enable_paid=True)

Internally the library resolves mesh URLs from defaults; ops can override
via `AB0T_MESH_<SERVICE>_URL` for local dev — those overrides are NOT part
of the consumer-facing API.

DEPLOYMENT MODES
----------------
This file currently supports the engine-local deployment mode: the
QuotaEngine runs in-process and reads/writes Redis directly. This is
the right default for mesh services co-located with shared-redis +
shared-dynamodb.

Future modes (see dev/ARCHITECTURE_LEARNINGS_20260425.md):
  * byo_redis — third party brings their own managed Redis
  * bridge    — pure HTTPS client; engine runs in billing only

Mode selection will be a config setting (`engine_mode`) in a future
release. Today, only the local mode is wired.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, FastAPI, HTTPException, Request
from redis.asyncio import Redis

from .alerts import AlertManager, LogAlertDispatcher
from .config import (
    load_config,
    load_enforcement,
    load_resource_bundles,
    load_resources,
    load_tiers,
)
from .engine import QuotaEngine
from .middleware import QuotaGuard
from .resolve import (
    Requirement,
    check_deprecated_generic_env,
    offline_mode,
    resolve_ddb_endpoint,
    resolve_ddb_region,
    resolve_dependencies,
    resolve_dependency,
    strip_url_password,
)
from .models.requests import QuotaCheckRequest
from .persistence import QuotaStore
from .providers import (
    AuthServiceTierProvider,
    JWTTierProvider,
    StaticTierProvider,
    TierProvider,
)
from .registry import ResourceRegistry

logger = logging.getLogger("ab0t_quota.setup")


# ---------------------------------------------------------------------------
# Internal mesh URL resolver — never exposed to consumers
# ---------------------------------------------------------------------------

# Production defaults. Library-internal — the consumer's code, config, and
# environment never reference these. Override via AB0T_MESH_<SERVICE>_URL
# for local dev only.
_MESH_DEFAULTS: dict[str, str] = {
    "billing": "https://billing.service.ab0t.com",
    "payment": "https://payment.service.ab0t.com",
}


def _mesh_url(service: str) -> str:
    """Resolve the URL for a mesh service. Library-internal."""
    env_override = os.getenv(f"AB0T_MESH_{service.upper()}_URL")
    if env_override:
        return env_override
    if service not in _MESH_DEFAULTS:
        raise KeyError(f"Unknown mesh service: {service}")
    return _MESH_DEFAULTS[service]


# ---------------------------------------------------------------------------
# Public surface returned to the consumer
# ---------------------------------------------------------------------------

class QuotaContext:
    """Live quota handle. Exposes ONLY quota-engine surface — never
    upstream mesh service clients. Consumer code never touches billing
    or payment objects directly.

    Available on `app.state.quota` after setup_quota() runs."""

    def __init__(
        self,
        engine: QuotaEngine,
        registry: ResourceRegistry,
        redis: Redis,
        store: Optional[QuotaStore],
    ):
        self._engine = engine
        self._registry = registry
        self._redis = redis
        self._store = store

    @property
    def engine(self) -> QuotaEngine:
        """Underlying engine, for advanced uses (custom checks, get_usage, etc)."""
        return self._engine

    async def check(self, org_id: str, resource_key: str, **kwargs):
        """Pre-flight check; raises 429 if denied."""
        result = await self._engine.check(
            QuotaCheckRequest(org_id=org_id, resource_key=resource_key, **kwargs),
        )
        if result.denied:
            raise HTTPException(status_code=429, detail=result.to_api_error())
        return result

    async def check_bundle(self, org_id: str, bundle: str, user_id: Optional[str] = None):
        """Pre-flight bundle check; raises 429 with the first denial."""
        result = await self._engine.check_for_bundle(org_id, bundle, user_id=user_id)
        if not result.allowed:
            denial = result.first_denial
            raise HTTPException(
                status_code=429,
                detail=denial.to_api_error() if denial else {"error": "quota_exceeded"},
            )
        return result

    async def increment_bundle(self, org_id: str, bundle: str, user_id: Optional[str] = None, idempotency_key: Optional[str] = None):
        return await self._engine.increment_for_bundle(
            org_id, bundle, user_id=user_id, idempotency_key=idempotency_key,
        )

    async def decrement_bundle(self, org_id: str, bundle: str, user_id: Optional[str] = None, idempotency_key: Optional[str] = None):
        return await self._engine.decrement_for_bundle(
            org_id, bundle, user_id=user_id, idempotency_key=idempotency_key,
        )

    async def usage(self, org_id: str):
        return await self._engine.get_usage(org_id)

    async def feature(self, org_id: str, feature_name: str) -> bool:
        return await self._engine.check_feature(org_id, feature_name)


# ---------------------------------------------------------------------------
# setup_quota — the one-liner
# ---------------------------------------------------------------------------

def setup_quota(
    app: FastAPI,
    *,
    mode: Optional[str] = None,
    config_path: Optional[str] = None,
    org_extractor: Optional[Callable[[Request], Awaitable[Optional[str]]]] = None,
    auth_dependency: Optional[Any] = None,
    rate_limit_resource: str = "api.requests_per_hour",
    enable_rate_limit: bool = True,
    enable_quota_api: bool = True,
    enable_paid: bool = True,
    api_prefix: str = "/api/quotas",
    on_ready: Optional[Callable[["QuotaContext"], Any]] = None,
    # P4.2 — the observed_usage_provider seam. OPTIONAL callback
    #   fn(org_id) -> {resource_key: {"total": float, "per_user": {uid: float}}}
    # that reports what is ACTUALLY live (the consumer's product rows). It is the
    # ONLY defence against the ledger diverging from reality (D-28/D-33): the
    # provider is authoritative for EXISTENCE. Its ABSENCE is the zero-config
    # default — the reconciler still runs and converges the counter to
    # Σ open activations (a handle-using client self-heals with no code). Sync or
    # async. Consumer-owned: their DB, their consuming-state semantics.
    observed_usage_provider: Optional[Callable[[str], Any]] = None,
    # D-37 — the activation store the engine (acquire/release) and the reconciler
    # share. DEFAULT is a durable, SHARED RedisActivationStore (self-provisioned
    # from the same Redis as the counter). In-memory is per-process and unsafe
    # with a shared counter (the reconciler would converge to a partial view and
    # UNDER-count, D-31) — so it is dev-only and must be injected explicitly here.
    activation_store: Optional[Any] = None,
    # Paid-tier surface forwarded to create_billing_router(...)
    paid_auth_reader: Optional[Any] = None,
    paid_auth_admin: Optional[Any] = None,
    paid_auth_url: Optional[str] = None,
    paid_auth_org_slug: Optional[str] = None,
    paid_checkout_store: Optional[Any] = None,
    paid_templates_dir: Optional[str] = None,
    paid_route_prefix: str = "/api",
) -> None:
    """Wire ab0t-quota into a FastAPI app in one synchronous call.

    Mounts middleware and quota routes immediately (must happen before app
    starts), and composes an async lifespan onto the app for engine
    initialization, snapshot worker, and clean teardown. After this call:

      - Routes /api/quotas/{usage,tiers,check/{key},check-bundle/{name}} are mounted
      - QuotaGuard rate-limit middleware is mounted
      - When the app starts: engine warms up, store init, seed_redis, snapshot worker
      - When the app stops: workers cancel, connections close
      - The QuotaContext is available on `app.state.quota` for route handlers

    Args:
        app: The FastAPI app.
        config_path: Path to quota-config.json. Defaults to env / cwd / /etc/ab0t.
        org_extractor: Async callable that extracts org_id from the Request.
            Defaults to `request.state.user.org_id`.
        auth_dependency: FastAPI Depends() for authenticated /usage and /check endpoints.
        rate_limit_resource: Resource key the QuotaGuard middleware enforces.
        enable_rate_limit: Mount QuotaGuard middleware. Default True.
        enable_quota_api: Mount /api/quotas/* endpoints. Default True.
        enable_paid: Wire LifecycleEmitter cost auto-record + paid-tier routes.
            Requires AB0T_MESH_API_KEY and AB0T_CONSUMER_ORG_ID. Default True.
        api_prefix: URL prefix for the quota API. Default /api/quotas.
    """
    config = load_config(config_path)
    storage = config.get("storage", {})
    enforcement = config.get("enforcement", {})

    if not enforcement.get("enabled", True):
        logger.warning("quota enforcement disabled in config")

    # D-10: loud startup call-out for deprecated generic env names left behind
    # by the 0.7 harvest removal (presence-only; runs for every mode).
    check_deprecated_generic_env(config)

    # ----- Mode selection ---------------------------------------------------
    # Resolution order: explicit kwarg → config.engine_mode → "local"
    resolved_mode = (mode or config.get("engine_mode") or "local").lower()
    if resolved_mode not in ("local", "byo_redis", "bridge"):
        logger.warning("unknown engine_mode %r — falling back to 'local'", resolved_mode)
        resolved_mode = "local"

    if resolved_mode == "bridge":
        # K-9/D-KS-8: bridge mode does not consume the keyspace state (its
        # counters live server-side) — a declared state must refuse, not no-op.
        _st = config.get("storage") or {}
        if _st.get("keyspace_version") == 2 or _st.get("keyspace_dual_write") is True:
            from .errors import QuotaConfigError
            raise QuotaConfigError(
                name="counter keyspace version", config_key="storage.keyspace_version",
                code="QUOTA-CFG-006",
                state="storage.keyspace_version/keyspace_dual_write declared in "
                      "bridge mode",
                env_names=(),
                remedy="bridge mode does not consume the keyspace declaration — "
                       "counters live in the billing service's Redis; remove the "
                       "keys (the server side owns its keyspace migration)",
                docs_anchor="keyspace",
            )
        return _setup_quota_bridge(
            app, config=config, org_extractor=org_extractor,
            auth_dependency=auth_dependency, enable_quota_api=enable_quota_api,
            api_prefix=api_prefix, on_ready=on_ready,
        )
    # Both "local" and "byo_redis" use the same code path; the only
    # difference is which Redis URL the consumer provides.

    # === SYNC PHASE — must happen before app starts ===========================

    # 1. Build everything that doesn't need network: Redis client (lazy
    #    connection), registry, tier definitions, bundles. Engine itself
    #    is constructed later so handlers can capture it via app.state.quota.

    # DECLARED, NOT DISCOVERED (pack 20260721, T-1/T-2): every dependency is
    # resolved ONCE, from declared sources only (config / namespaced env),
    # before any client object exists. No generic env var, no invented default.
    plan = resolve_dependencies(config, mode=resolved_mode)
    app.state.quota_resolution_plan = plan
    logger.info("%s", plan.provenance_block())

    # T-5 (ENV-07/08/11): the outbound inventory — every target this process
    # may contact, stated BEFORE any of them is contacted — and the offline
    # ("contact nobody") switch that suppresses them all.
    offline = offline_mode(config)
    if offline:
        logger.warning(
            "AB0T_QUOTA OFFLINE MODE — contacting nobody: tier-catalog publish, "
            "auth auto-subscribe, webhook alerts, paid-tier client wiring and DDB "
            "self-provisioning (state store, activation ledger, outbox) are OFF "
            "for this process")
    logger.info(
        "OUTBOUND TARGETS this process may contact:%s\n"
        "  billing  %s   (tier-catalog PUT, credit grants)\n"
        "  payment  %s   (checkout proxy)\n"
        "  (offline mode: AB0T_QUOTA_OFFLINE=true suppresses all of the above)",
        " [OFFLINE — all suppressed]" if offline else "",
        _mesh_url("billing"), _mesh_url("payment"))

    redis_url = plan["redis_url"].value
    pw_row = plan["redis_password"]
    redis_kwargs: dict = {"decode_responses": False}
    if pw_row.declared:
        # D-5(a): a separately-declared redis_password BEATS a URL-embedded one
        # (both runtimes). redis-py's from_url merges URL options OVER kwargs
        # (E-03 — the old comment here claimed the reverse), so implement the
        # precedence explicitly: strip the URL password, pass the declared field.
        redis_url, url_pw = strip_url_password(redis_url)
        if url_pw is not None and url_pw != pw_row.value:
            logger.warning(
                "redis password declared in TWO places and they differ: %s wins; "
                "the URL-embedded password is dropped. Remove the stale copy "
                "(ENV-02's assembled-pair hazard).", pw_row.source)
        redis_kwargs["password"] = pw_row.value
    redis = Redis.from_url(redis_url, **redis_kwargs)

    registry = ResourceRegistry()
    for r in load_resources(config):
        registry.register(r)

    tiers = load_tiers(config)
    bundles = load_resource_bundles(config)
    provider = _build_tier_provider(config, redis)

    # K-9 (keyspace spec §3.2): the declared keyspace state, from the resolved
    # rows + the declared service identity. Default (1,false) = pre-K-1 bytes.
    from .keyspace import Keyspace, KeyspaceScopeError
    _ksv = int(plan["keyspace_version"].value)
    _ksd = bool(plan["keyspace_dual_write"].value)
    _ks_service = _resolve_service_name(config, registry)
    try:
        keyspace = Keyspace(service=_ks_service, version=_ksv, dual_write=_ksd)
    except KeyspaceScopeError:
        if _ksv == 1 and not _ksd:
            # v1 keys carry no scope: an odd service_name must not break a
            # drop-in upgrade. Loud, and the marker boot-guard is scope-less.
            logger.warning(
                "service_name %r cannot be a keyspace scope (charset guard) — "
                "keeping the unscoped v1 keyspace; fix it before any v2 migration",
                _ks_service)
            keyspace = Keyspace()
        else:
            raise
    app.state.quota_keyspace = keyspace

    # Engine without an override loader yet — set after store init in the lifespan
    # D-37/D-39: the activation LEDGER is authoritative for identity+cost (D-33) —
    # it must be DURABLE and SHARED. An in-memory default is per-process (partial
    # view → under-count). And Redis is a CACHE (D-39): under an allkeys-* eviction
    # policy it silently drops OPEN rows → the reconciler under-counts. So this is a
    # PROVISIONAL store for the sync phase only; the async lifespan resolves a
    # DURABLE store (DDB self-provisioned, else Redis under the machine-checked
    # durability conditions) and the reconciler REFUSES a non-durable ledger.
    from .activations import RedisActivationStore
    resolved_activation_store = activation_store or RedisActivationStore(redis)

    engine = QuotaEngine(
        redis=redis,
        tier_provider=provider,
        registry=registry,
        tiers=tiers,
        resource_bundles=bundles,
        # QP-01 / D-15: carry the documented enforcement knobs from config into
        # the engine so enabled / shadow_mode / global_kill_switch / unknown_bundle
        # actually behave (previously read at :226 and only logged).
        enforcement=load_enforcement(config),
        activation_store=resolved_activation_store,
        keyspace=keyspace,  # K-9: counters/reconciler/recent-guard inherit it
    )

    # Alert manager: log dispatcher always; webhook if configured
    dispatchers = [LogAlertDispatcher()]
    alerts_cfg = config.get("alerts", {})
    if alerts_cfg.get("webhook_url") and not offline:
        try:
            from .alerts import WebhookAlertDispatcher
            dispatchers.append(WebhookAlertDispatcher(url=alerts_cfg["webhook_url"]))
        except Exception as e:
            logger.warning("webhook alert dispatcher init failed: %s", e)
    engine.set_alert_manager(AlertManager(
        redis=redis,
        dispatchers=dispatchers,
        cooldown_seconds=alerts_cfg.get("cooldown_seconds", 3600),
    ))

    # 2. Mount routes
    if enable_quota_api:
        _mount_quota_routes(app, engine, api_prefix, org_extractor, auth_dependency)

    # 3. Mount middleware (must happen before app starts — that's why
    #    setup_quota is synchronous)
    if enable_rate_limit and registry.get(rate_limit_resource):
        app.add_middleware(
            QuotaGuard,
            engine=engine,
            resource_key=rate_limit_resource,
            org_extractor=org_extractor,
        )
        logger.info("quota rate-limit middleware mounted on %s", rate_limit_resource)
    elif enable_rate_limit:
        logger.info("rate-limit resource %s not registered; middleware skipped",
                    rate_limit_resource)

    # 4. Wire paid-tier surface (lifecycle emitter, heartbeat monitor, billing router)
    #    Routes are mounted now; the heartbeat monitor task is started in the lifespan.
    paid_state = (
        _wire_paid_tier_sync(
            app, engine, redis, config,
            auth_reader=paid_auth_reader,
            auth_admin=paid_auth_admin,
            auth_url=paid_auth_url,
            auth_org_slug=paid_auth_org_slug,
            checkout_store=paid_checkout_store,
            templates_dir=paid_templates_dir,
            route_prefix=paid_route_prefix,
            tiers=tiers,  # T0b — pass already-loaded TierConfig dict to the billing router
        ) if enable_paid and not offline else None
    )
    if enable_paid and offline:
        logger.warning("offline mode: paid-tier surface NOT wired (no mesh clients constructed)")

    # === ASYNC PHASE — composed into the app's lifespan =======================

    existing_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def composed_lifespan(_app: FastAPI):
        # GT T2 (the incident's fix) — REACHABILITY FIRST. The first gate to
        # touch Redis owns the diagnosis; unreachable/unauthenticated must own
        # it as itself, never as a topology/eviction/scripting verdict.
        await _gate_redis_reachable(_app, redis, config, plan)

        # D-71 — machine-check the Redis TOPOLOGY before ANYTHING touches the
        # counter. The counter's Lua scripts are multi-key and CROSSSLOT on a
        # Redis Cluster (D-23, observed at the server); our prod is single-node,
        # so only a CLIENT would ever hit it — which is exactly why a LIBRARY may
        # not assume it. Refuse loudly at startup instead of breaking silently at
        # the first acquire. Same shape as D-32's durability machine-check.
        await _gate_redis_topology(_app, redis, config)

        # D-72/D-73/D-74 — the rest of the Redis preflight, for the SAME reason and in
        # order of how quietly they fail. D-72 is the URGENT one: an `allkeys-*` Redis
        # evicts a LIVE gauge, the counter reads zero for a resource that is still
        # running, and the library silently over-admits — D-31's forbidden direction,
        # at runtime, behind a green health check. (D-71 at least refuses loudly.)
        await _gate_redis_counter_store(_app, redis, config)

        # K-9 (spec §3.3): keyspace boot guards — QUOTA-CFG-011 (version
        # regression against a completed migration) and QUOTA-CFG-012
        # (brownfield v2 over live v1 keys). Typed refusals, deliberately
        # UNWRAPPED (fatal): both worlds silently zero/orphan counters.
        from .keyspace_migration import check_boot_keyspace
        _ks_marker = await check_boot_keyspace(redis, keyspace)
        _ks_phase = (_ks_marker or {}).get("phase", "none")
        _app.state.quota_keyspace_state = {
            "service": keyspace.service, "version": keyspace.version,
            "dual_write": keyspace.dual_write,
            "migration_phase": _ks_phase, "marker": _ks_marker,
        }

        # Inner: existing user lifespan (if any), wrapped by our async init/teardown
        store: Optional[QuotaStore] = None
        # T-6/D-3: table-creation policy, decided ONCE here and passed to every
        # self-provision call site. Default false — nothing is created in a
        # consumer's cloud without the explicit opt-in (ENV-04).
        auto_create = bool(storage.get("auto_create_tables", False))
        # Gate-C re-gate (T-5): offline gates the state store too — this was the
        # third, ungated DDB self-provision path (describe_table + CreateTable).
        if storage.get("persistence_enabled", True) and offline:
            logger.warning("offline mode: state-store DDB persistence init SKIPPED — "
                           "no session constructed, no seed/sync, no CreateTable")
        if storage.get("persistence_enabled", True) and not offline:
            try:
                # Region is SDK-kind: declared config wins; otherwise region=None
                # defers to boto3's own documented chain (AWS_REGION/profile/IMDS)
                # — never a hardcoded us-east-1 (ENV-04). Endpoint is declared-or-
                # unset, allowlist-validated at resolve time (ENV-14).
                store = QuotaStore(
                    table_name=storage.get("dynamodb_table", "ab0t_quota_state"),
                    region=plan["dynamodb_region"].value,
                    endpoint_url=plan["dynamodb_endpoint"].value,
                )
                await store.initialize(create=auto_create)
            except Exception as e:
                logger.warning("quota persistence init failed (non-fatal): %s", e)
                store = None

        # Now that store is ready, set the override loader on the engine
        if store is not None:
            async def load_override(org_id: str, resource_key: str):
                try:
                    return await store.get_override(org_id, resource_key)
                except Exception as e:
                    logger.warning("override_load_failed org=%s resource=%s error=%s",
                                   org_id, resource_key, e)
                    return None
            engine._override_loader = load_override

            # Seed Redis from DynamoDB if requested
            try:
                restored = await store.seed_redis(redis, registry,
                                                  keyspace=keyspace)
                if restored:
                    logger.info("seeded %d quota counters from DynamoDB", restored)
            except Exception as e:
                logger.warning("seed_redis failed (non-fatal): %s", e)

            # Start snapshot worker
            interval = int(storage.get("persistence_sync_interval_seconds", 300))
            store.start_sync_worker(redis, registry, interval_seconds=interval)

        # Start the outbox drain worker if a LifecycleEmitter was wired (QB-01).
        # Without this the outbox retains failed money events but never retries
        # them — a table, not a fix. Library-owned background task; stopped in
        # teardown. Kill-switch: AB0T_QUOTA_OUTBOX_DRAIN_ENABLED=false.
        drain_emitter = getattr(_app.state, "quota_emitter", None)
        outbox_cfg = config.get("outbox", {}) or {}
        outbox_ddb_close = None
        if drain_emitter is not None and outbox_cfg.get("enabled", True):
            # D-32: resolve a DURABLE outbox by default (self-provision DDB /
            # Redis self-check / refuse-to-bill), set capabilities, and only then
            # start the drain worker — never onto an ephemeral store.
            outbox_ddb_close = await _resolve_outbox_durability(
                _app, drain_emitter, redis=redis, config=config,
                storage=storage, enable_paid=enable_paid,
            )
            if not drain_emitter._billing_disabled:
                drain_emitter.start_drain_worker(
                    interval_seconds=float(outbox_cfg.get("drain_interval_seconds", 30)),
                    max_per_pass=int(outbox_cfg.get("max_per_pass", 100)),
                )

        # D-82 — the handler ledger PROVISIONS and is PREFLIGHTED, exactly like the outbox and
        # the activation store. It used to ASSUME its table existed: a client who wired it hit
        # ResourceNotFoundException at their FIRST auth webhook, in production. It stayed
        # invisible for the most instructive reason (D-78): the only thing that had ever
        # exercised it was a fake — and a fake never notices, because a fake creates nothing.
        # GATE-06/T-17 — DELIBERATE ASYMMETRY, on the record: this block is UNWRAPPED
        # (fatal) while the activation path below degrades. The ledger runs only when the
        # consumer EXPLICITLY wired it (opt-in via app.state.quota_handler_ledger + a ddb
        # client); a wired-but-broken ledger silently degrading is exactly the D-82 incident
        # again. The activation store may degrade because it has a machine-checked Redis
        # fallback; the ledger has none.
        _ledger = getattr(_app.state, "quota_handler_ledger", None)
        if (_ledger is not None and hasattr(_ledger, "ensure_table")
                and getattr(_ledger, "ddb", None) is not None):
            await _ledger.ensure_table(create=auto_create)
            await _preflight_ddb_store(
                _app, config, _ledger, cap_key="ddb_handler_ledger",
                table=_ledger.table, ttl_attr="ttl",
                required_gsis=("gsi1", "gsi2"))  # query_by_user / query_by_status (ENV-16)

        # Start heartbeat monitor if paid-tier wired one up
        heartbeat_task = None
        if paid_state and paid_state.get("heartbeat_monitor"):
            import asyncio
            heartbeat_task = asyncio.create_task(
                paid_state["heartbeat_monitor"].start(),
                name="ab0t_quota_heartbeat",
            )

        # D-39: resolve a DURABLE activation ledger (DDB self-provisioned, else
        # Redis under the machine-check) and set it on the engine BEFORE the
        # reconciler reads it. The ledger is authoritative for identity+cost (D-33)
        # and must not live in an evictable cache.
        act_ddb_close = None
        act_durable, act_detail = False, "unknown"
        try:
            act_ddb_close = await _provision_activation_store(
                _app, engine, redis=redis, config=config, storage=storage,
                injected=activation_store,
            )
        except Exception as e:
            logger.error("activation ledger provisioning failed (non-fatal): %s", e)

        # Start the library gauge reconciler (P4). Defaults ON (D-28): the
        # re-raise in acquire leaves an orphaned counter over-count on persist
        # failure, healed ONLY by a running reconciler — a correctness feature a
        # client must remember to switch on is a correctness feature that is off.
        # With no observed_usage_provider it converges to Σ open activations
        # (zero-config self-heal); with one it makes the provider authoritative
        # for existence (D-33). Kill-switch: reconcile.enabled / AB0T_QUOTA_RECONCILE_ENABLED.
        reconciler = None
        from .reconcile import LibraryReconciler, ReconcileConfig
        reconcile_cfg = config.get("reconcile", {}) or {}
        act_cfg = config.get("activations", {}) or {}
        rc = ReconcileConfig()
        if "enabled" in reconcile_cfg:
            rc.enabled = bool(reconcile_cfg["enabled"])
        if "interval_seconds" in reconcile_cfg:
            rc.interval_seconds = float(reconcile_cfg["interval_seconds"])
        if "max_force_sets_per_pass" in reconcile_cfg:
            rc.max_force_sets_per_pass = int(reconcile_cfg["max_force_sets_per_pass"])
        # D-10 `truth.source`: "activations" (default) or "provider" (the counter is
        # force-set to the observed_usage_provider; no activation ledger). A
        # legacy-increment consumer with a product-truth query uses "provider".
        if reconcile_cfg.get("truth_source") in ("activations", "provider"):
            rc.truth_source = reconcile_cfg["truth_source"]
        # D-39: the on-the-record operator assertion for ElastiCache (CONFIG GET
        # unavailable). The reconciler machine-checks Redis durability otherwise.
        rc.redis_durability_confirmed = bool(act_cfg.get("redis_durability_confirmed", False))
        # Populate the Capabilities `reconciler` field (owned by this lane) with
        # the REAL status — on / off(config) / OFF(unsafe or non-durable store).
        caps = dict(getattr(_app.state, "quota_capabilities", {}) or {})
        # D-40: route the reconciler's drift/divergence alerts to the CONFIGURED
        # dispatchers (incl. the webhook). `divergence_detected` (live resources
        # with NO activation row → un-settleable usage) is QB-01's signature and
        # must reach a HUMAN, not terminate in a log-only default dispatcher.
        drift_alerts = _build_drift_alert_manager(redis, config)

        # D-75 — the guards' own caveat: every infrastructure check we own (D-32, D-71,
        # D-72, D-73, D-76) verified the world ONCE, at boot, and then trusted it forever.
        # A `CONFIG SET maxmemory-policy allkeys-lru` at 3am is invisible to all of them,
        # and the counter becomes silently evictable → over-admission behind a green probe.
        # The re-check RIDES THE RECONCILER LOOP (never a new worker — one more loop is one
        # more thing that can be dead, D-50), and a runtime transition is LOUD, NOT FATAL.
        #
        # NOTE (framed, not hidden): if the reconciler is OFF, nothing re-verifies — but a
        # reconciler that is off ALREADY degrades /quota/health (D-49), so the deployment is
        # not silently trusting a stale verdict; it is loudly degraded for a broader reason.
        revalidate = _make_redis_revalidator(_app, redis, config, drift_alerts)
        ddb_targets = dict(getattr(_app.state, "quota_ddb_preflight_targets", {}) or {})
        if ddb_targets:
            _prior = revalidate

            async def revalidate():  # noqa: F811 - compose Redis + DDB re-verification
                await _prior()
                from .ddb_preflight import pitr_confirmed_from, verify_ddb_tables
                caps_now = dict(getattr(_app.state, "quota_capabilities", {}) or {})
                try:
                    client = next(iter(ddb_targets.values()))[0]
                    tables = {k: (t, a) for k, (_c, t, a, _g) in ddb_targets.items()}
                    ddb_caps, ddb_unsafe = await verify_ddb_tables(
                        client, tables, pitr_confirmed=pitr_confirmed_from(config))
                except Exception as e:
                    logger.error("DDB re-verification failed (D-75/D-76): %s", e)
                    return
                caps_now.update(ddb_caps)
                _app.state.quota_capabilities = caps_now
                for key, detail in ddb_unsafe:
                    logger.error("DDB INVARIANT VIOLATED AT RUNTIME (D-75/D-76) — %s: %s",
                                 key, detail)
                    try:
                        await drift_alerts.invariant_violated(key, detail)
                    except Exception as e:
                        logger.error("invariant alert dispatch failed: %s", e)

        _app.state.quota_revalidate = revalidate

        if rc.enabled:
            try:
                reconciler = LibraryReconciler(
                    engine, observed_usage_provider=observed_usage_provider,
                    config=rc, drift_alerts=drift_alerts, preflight=revalidate,
                )
                # D-37/D-39: the reconciler REFUSES a non-durable ledger (in-memory,
                # or an evictable/unconfirmed Redis). `ledger_durability()` is the
                # single source of that judgement (reused, not duplicated in setup).
                act_durable, act_detail = await reconciler.ledger_durability()
                _app.state.quota_reconciler = reconciler
                if act_durable:
                    started = reconciler.start()
                    caps["reconciler"] = (
                        ("on(provider)" if observed_usage_provider is not None
                         else "on(ledger)") if started else "off(refused)")
                else:
                    caps["reconciler"] = f"OFF — activation store not durable ({act_detail})"
                    logger.error("quota reconciler NOT started — activation ledger not "
                                 "durable (D-39): %s. Wire DDB, or Redis with persistence "
                                 "+ a non-evicting policy / redis_durability_confirmed.",
                                 act_detail)
            except Exception as e:
                logger.error("quota reconciler start failed (non-fatal): %s", e)
                reconciler = None
                caps["reconciler"] = "OFF (start failed)"
        else:
            logger.warning("quota reconciler DISABLED by config — an orphaned "
                           "over-count from an acquire persist-failure will NOT heal")
            caps["reconciler"] = "off(config)"
        caps["activation_store"] = f"{act_detail} ({'durable' if act_durable else 'NOT durable'})"
        _app.state.quota_capabilities = caps

        # Publish QuotaContext on app.state for route handlers to use
        ctx = QuotaContext(engine=engine, registry=registry, redis=redis, store=store)
        _app.state.quota = ctx

        # Auto-publish tier catalog to billing so cross-service admin views
        # (`/billing/{org}/tier/limits`) reflect the consumer's actual limits
        # instead of library defaults. Best-effort.
        service_name = _resolve_service_name(config, registry)
        if service_name and enable_paid and not offline:
            await _publish_tier_catalog(
                service_name, tiers, registry=registry, bundles=bundles,
            )

        # Fire on_ready callback so the consumer's wiring code can stash a
        # reference to the engine (useful when their helper functions don't
        # have request access to read app.state.quota at call time).
        if on_ready is not None:
            try:
                result = on_ready(ctx)
                if hasattr(result, "__await__"):
                    await result
            except Exception as e:
                logger.warning("on_ready callback failed: %s", e)

        logger.info(
            "quota setup complete: %d resources, %d tiers, %d bundles, paid=%s",
            len(registry.all()), len(tiers), len(bundles), enable_paid,
        )

        # Capabilities / WhyOff snapshot (P6.2) — ONE startup line answering "is my
        # integrity actually wired?". Every silent-degradation site should surface
        # here (Python parity with the Go Capabilities snapshot). This single line
        # would have made several of this ticket's findings visible on day one.
        try:
            _emit_capabilities_snapshot(_app, engine, config, enable_paid=enable_paid)
        except Exception as e:
            logger.warning("capabilities snapshot failed: %s", e)

        # D-66 — DERIVE the required money-loop set from config in ONE place (not
        # appended at wiring sites). This is the contract the probe checks; the
        # loops' wiring only reports whether it was satisfied. Enumerating what
        # SHOULD run here, and what DOES run via quota_loop_liveness, makes any gap
        # a health failure rather than a silence.
        try:
            _app.state.quota_required_loops = required_money_loops(config, enable_paid=enable_paid)
        except Exception as e:
            logger.warning("required-loop derivation failed: %s", e)

        # D-71 (same law, applied to CAPABILITIES) — declare which capability keys
        # this deployment MUST report affirmatively. A snapshot that is missing one
        # of them is a service whose integrity wiring did not finish, and the probe
        # can only degrade on entries it knows to expect (D-49/D-51: absence is not
        # a value). `redis_topology` is required whenever the counter is on Redis.
        try:
            _app.state.quota_required_caps = set(_ALL_CRITICAL_CAPS)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("required-capability derivation failed: %s", e)

        # D-40 — give the snapshot a CONSUMER. Emitting it is the easy half.
        try:
            _register_capability_routes(_app)
        except Exception as e:
            logger.warning("capabilities routes not registered: %s", e)

        # Auto-subscribe the auth-event webhook with auth, idempotently.
        # Reads env directly. Only fires if handlers are registered.
        # Best-effort: failures log a warning, never block startup.
        try:
            from . import auth_events as _ae
            if _ae.registered_event_types() and not offline:
                async def _do_subscribe():
                    try:
                        await _ae.subscribe_on_startup()
                    except Exception as e:
                        logger.warning("auth-event auto-subscribe failed: %s", e)
                asyncio.create_task(_do_subscribe())
        except Exception as e:
            logger.warning("auth-event auto-subscribe scheduling failed: %s", e)

        try:
            if existing_lifespan is not None:
                async with existing_lifespan(_app):
                    yield
            else:
                yield
        finally:
            # Teardown: stop the outbox drain worker, stop heartbeat, close store
            # (also stops snapshot worker), close Redis.
            if reconciler is not None:
                try:
                    await reconciler.stop()
                except Exception as e:
                    logger.warning("quota reconciler stop failed: %s", e)
            if act_ddb_close is not None:
                try:
                    await act_ddb_close()
                except Exception as e:
                    logger.warning("activation ledger ddb client close failed: %s", e)
            if drain_emitter is not None:
                try:
                    await drain_emitter.stop_drain_worker()
                except Exception as e:
                    logger.warning("outbox drain worker stop failed: %s", e)
            if outbox_ddb_close is not None:
                try:
                    await outbox_ddb_close()
                except Exception as e:
                    logger.warning("outbox ddb client close failed: %s", e)
            if heartbeat_task is not None:
                if paid_state and paid_state.get("heartbeat_monitor"):
                    paid_state["heartbeat_monitor"].stop()
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except Exception:
                    pass
            if store is not None:
                try:
                    await store.close()
                except Exception as e:
                    logger.warning("quota store close failed: %s", e)
            try:
                await redis.aclose()
            except Exception as e:
                logger.warning("quota redis close failed: %s", e)

    app.router.lifespan_context = composed_lifespan


# ---------------------------------------------------------------------------
# Internal builders
# ---------------------------------------------------------------------------

def resolve_bridge_identity(config) -> tuple:
    """Bridge mode's REQUIRED identity — ONE resolver spec shared by boot
    (`_setup_quota_bridge`) and preflight (T-12 §2.5: never two chains).
    Returns the two Resolved rows; raises QuotaConfigError 007/008."""
    mesh_key_row = resolve_dependency(
        config, name="mesh API key (bridge mode)", config_key="(env-only)",
        env=("AB0T_MESH_API_KEY",), requirement=Requirement.REQUIRED, secret=True,
        code="QUOTA-CFG-007",
        previously=lambda: ("versions before 0.7 booted anyway; every quota op then "
                            "FAILED CLOSED (deny, 429) at request time"),
        remedy="export AB0T_MESH_API_KEY — bridge mode cannot enforce without it.",
        docs_anchor="bridge",
    )
    service_row = resolve_dependency(
        config, name="service name (bridge mode)", config_key="service_name",
        env=("AB0T_SERVICE_NAME",), requirement=Requirement.REQUIRED,
        code="QUOTA-CFG-008",
        previously=lambda: ("versions before 0.7 booted anyway; every quota op then "
                            "FAILED CLOSED (deny, 429) at request time"),
        remedy="set service_name in quota-config.json (or export AB0T_SERVICE_NAME).",
        docs_anchor="bridge",
    )
    return mesh_key_row, service_row


def _setup_quota_bridge(
    app: FastAPI,
    *,
    config: dict,
    org_extractor: Optional[Callable[[Request], Awaitable[Optional[str]]]],
    auth_dependency: Optional[Any],
    enable_quota_api: bool,
    api_prefix: str,
    on_ready: Optional[Callable],
) -> None:
    """Bridge-mode wiring: thin HTTPS client, no Redis, no DynamoDB, no engine.

    Every quota op is a round-trip to billing's mesh quota API.
    Tier resolution and allow-decisions are cached in-memory to amortize
    cost. See docs/mesh-quota-api.md for the wire protocol.

    Required env: AB0T_MESH_API_KEY. Service identity from config or
    AB0T_SERVICE_NAME env var.
    """
    from .bridge import BridgeClient, BridgeContext
    from .caches import CachedBridgeClient

    # T-20 (ENV-01 class, bridge branch): bridge mode HARD-REQUIRES its mesh
    # identity. Booting without it shipped a service whose every quota op
    # failed closed (deny, 429) at request time — an outage with extra steps.
    mesh_key_row, service_row = resolve_bridge_identity(config)
    mesh_key = mesh_key_row.value
    service_name = service_row.value

    tier_cfg = config.get("tier_provider", {})
    cache_cfg = config.get("bridge_cache", {})
    tier_ttl = float(cache_cfg.get("tier_ttl_seconds", tier_cfg.get("cache_ttl_seconds", 60)))
    decision_ttl = float(cache_cfg.get("decision_ttl_seconds", 1.0))

    raw_client = BridgeClient(
        base_url=_mesh_url("billing"),
        api_key=mesh_key,
        service_name=service_name,
    )
    client = CachedBridgeClient(
        client=raw_client,
        tier_ttl_seconds=tier_ttl,
        decision_ttl_seconds=decision_ttl,
    )
    bridge_ctx = BridgeContext(client)

    # Mount /api/quotas/* endpoints — same shape as engine-local mode
    if enable_quota_api:
        _mount_bridge_routes(app, client, api_prefix, org_extractor, auth_dependency)

    # Compose lifespan for clean shutdown of the HTTP client
    existing_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def composed_lifespan(_app: FastAPI):
        _app.state.quota = bridge_ctx
        if on_ready is not None:
            try:
                result = on_ready(bridge_ctx)
                if hasattr(result, "__await__"):
                    await result
            except Exception as e:
                logger.warning("on_ready callback failed: %s", e)
        logger.info(
            "quota setup complete (bridge): service=%s tier_ttl=%ds decision_ttl=%ds",
            service_name, tier_ttl, decision_ttl,
        )
        try:
            if existing_lifespan is not None:
                async with existing_lifespan(_app):
                    yield
            else:
                yield
        finally:
            try:
                await client.close()
            except Exception as e:
                logger.warning("bridge client close failed: %s", e)

    app.router.lifespan_context = composed_lifespan


def _mount_bridge_routes(
    app: FastAPI,
    client,  # CachedBridgeClient
    prefix: str,
    org_extractor: Optional[Callable[[Request], Awaitable[Optional[str]]]],
    auth_dependency: Optional[Any],
) -> None:
    """/api/quotas/* routes that delegate to the bridge client.

    Same path shape as engine-local mode so consumer routes / dashboards
    don't change between modes.
    """
    router = APIRouter()

    async def _default_extract(request: Request) -> Optional[str]:
        user = getattr(request.state, "user", None)
        return getattr(user, "org_id", None) if user else None

    extract = org_extractor or _default_extract
    deps = [auth_dependency] if auth_dependency else []

    @router.get("/usage", tags=["quota"], dependencies=deps)
    async def get_usage(request: Request):
        org_id = await extract(request)
        if not org_id:
            raise HTTPException(status_code=401, detail="Unable to resolve org_id")
        return await client.usage(org_id)

    @router.get("/check/{resource_key}", tags=["quota"], dependencies=deps)
    async def check_resource(resource_key: str, request: Request):
        org_id = await extract(request)
        if not org_id:
            raise HTTPException(status_code=401, detail="Unable to resolve org_id")
        return await client.check(org_id, resource_key)

    @router.get("/check-bundle/{bundle_name}", tags=["quota"], dependencies=deps)
    async def check_bundle(bundle_name: str, request: Request):
        org_id = await extract(request)
        if not org_id:
            raise HTTPException(status_code=401, detail="Unable to resolve org_id")
        return await client.check_bundle(org_id, bundle_name)

    # Note: /tiers is intentionally NOT mounted in bridge mode. Tier catalog
    # is owned by billing in bridge deployments; consumers can fetch it via
    # GET /billing/{org}/tier/limits?service=<name> directly if needed.

    app.include_router(router, prefix=prefix)


async def _publish_tier_catalog(
    service_name: str,
    tiers: dict,
    registry: Optional["ResourceRegistry"] = None,
    bundles: Optional[dict] = None,
    timeout: float = 5.0,
) -> bool:
    """Best-effort PUT of the consumer's full quota catalog to billing.

    Includes everything billing needs to run a server-side engine for this
    service in bridge mode: tiers, resource definitions (counter types,
    units, windows, reset periods), and resource bundles.

    Cross-service admin views (`/billing/{org}/tier/limits?service=...`)
    use the tier slice. Bridge-mode clients hit billing's per-service
    engine which is built from the resource + bundle slices.

    Best-effort — failure does not block startup. Re-published every
    startup because the catalog is operator-owned config, not user-mutated
    state. Library-internal — consumers never call this directly.
    """
    import httpx
    # Catalog publish targets billing — use billing-scoped key if set
    mesh_key = (
        os.getenv("AB0T_MESH_BILLING_API_KEY", "")
        or os.getenv("AB0T_MESH_API_KEY", "")
    )
    if not mesh_key:
        logger.debug("catalog publish skipped: no billing mesh API key set")
        return False

    # DECISION (locked, do not relitigate):
    #   The catalog publishes quota policy (tier_id, limits, features,
    #   resource bundles) only. It deliberately OMITS billing-policy fields:
    #   `billing_model`, `price`, `credit_grant`, `initial_credit`.
    #
    #   Rationale — see `context_03_billing_model_decision.md` D5 in ticket
    #   20260516_paid_plan_balance_model_gap:
    #     * D5: library DEFINES the schema + defaults; library NEVER hardcodes
    #       a consumer-specific amount or policy.
    #     * Billing-policy lives in the consumer's `quota-config.json` and is
    #       resolved consumer-side. Publishing it to billing would (a) leak
    #       consumer pricing into a multi-tenant central store and (b) put
    #       billing-service in the policy-resolution path, recreating the
    #       service-boundary violation T8/T9 fixed.
    #
    #   The catalog exists for:
    #     1. Admin views: `/billing/{org}/tier/limits?service=...` shows tier
    #        names + limits across services. No money concerns.
    #     2. Bridge mode: billing runs a per-service engine for consumers who
    #        opted out of in-process enforcement. Engine needs resources +
    #        bundles + limits — NOT prices or credit-grant policy.
    #
    #   Regression test: tests/test_tier_catalog_publish.py
    #     ::TestCatalogOmitsBillingPolicy
    payload: dict = {
        "tiers": [
            {
                "tier_id": tier.tier_id,
                "display_name": tier.display_name,
                "description": tier.description,
                "sort_order": tier.sort_order,
                "features": sorted(tier.features),
                "upgrade_url": tier.upgrade_url,
                "default_per_user_fraction": tier.default_per_user_fraction,
                "limits": {
                    rk: {
                        "limit": tl.limit,
                        "warning_threshold": tl.warning_threshold,
                        "critical_threshold": tl.critical_threshold,
                        "per_user_limit": tl.per_user_limit,
                        "burst_allowance": tl.burst_allowance,
                    }
                    for rk, tl in tier.limits.items()
                },
            }
            for tier in tiers.values()
        ],
    }

    # Optional: include resources and bundles so billing can run a real
    # engine for this service in bridge mode.
    if registry is not None:
        payload["resources"] = [
            {
                "service": rd.service,
                "resource_key": rd.resource_key,
                "display_name": rd.display_name,
                "description": rd.description,
                "counter_type": rd.counter_type.value,
                "unit": rd.unit,
                "window_seconds": rd.window_seconds,
                "reset_period": rd.reset_period.value if rd.reset_period else None,
                "precision": rd.precision,
            }
            for rd in registry.all()
        ]
    if bundles:
        payload["resource_bundles"] = dict(bundles)

    url = f"{_mesh_url('billing')}/billing/tier-catalog/{service_name}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.put(
                url,
                json=payload,
                headers={"X-API-Key": mesh_key, "X-Service-Name": service_name},
            )
            if 200 <= resp.status_code < 300:
                logger.info(
                    "catalog published service=%s tiers=%d resources=%d bundles=%d",
                    service_name, len(payload["tiers"]),
                    len(payload.get("resources", [])),
                    len(payload.get("resource_bundles", {})),
                )
                return True
            logger.warning(
                "catalog publish failed service=%s status=%d body=%s",
                service_name, resp.status_code, resp.text[:200],
            )
            return False
    except Exception as e:
        # Best-effort. Catalog is for admin views + bridge mode; library
        # engine-local enforcement still works regardless.
        logger.warning("catalog publish error service=%s error=%s",
                       service_name, e)
        return False


def _resolve_service_name(config: dict, registry: ResourceRegistry) -> Optional[str]:
    """Derive the consumer's service name for catalog publish, in order:
       1. AB0T_SERVICE_NAME env var
       2. config["service_name"]
       3. First registered resource's `service` field
       4. None — publish is skipped
    """
    name = os.getenv("AB0T_SERVICE_NAME") or config.get("service_name")
    if name:
        return name
    resources = registry.all()
    if resources:
        return resources[0].service
    return None


def _build_tier_provider(config: dict, redis: Redis) -> TierProvider:
    """Pick a tier provider based on config. Mesh-billing is the default;
    consumers don't choose URLs."""
    tier_cfg = config.get("tier_provider", {})
    provider_type = tier_cfg.get("type", "mesh")

    if provider_type in ("mesh", "billing"):
        billing_url = _mesh_url("billing")
        # Tier reads hit billing — billing-scoped key, fall back to unified
        mesh_key = (
            os.getenv("AB0T_MESH_BILLING_API_KEY", "")
            or os.getenv("AB0T_MESH_API_KEY", "")
        )

        async def fetch_tier(org_id: str) -> str:
            import httpx
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    resp = await client.get(
                        f"{billing_url}/billing/{org_id}/tier",
                        headers={"X-API-Key": mesh_key},
                    )
                    if resp.status_code == 200:
                        return resp.json().get("tier_id", "free")
            except Exception as e:
                logger.warning("mesh tier fetch failed org=%s error=%s", org_id, e)
            return tier_cfg.get("default_tier", "free")

        return AuthServiceTierProvider(
            fetch_fn=fetch_tier,
            redis=redis,
            cache_ttl=int(tier_cfg.get("cache_ttl_seconds", 60)),
            default_tier=tier_cfg.get("default_tier", "free"),
        )

    if provider_type == "jwt":
        return JWTTierProvider(
            claim_key=tier_cfg.get("jwt_claim_key", "org_tier"),
            default_tier=tier_cfg.get("default_tier", "free"),
        )

    return StaticTierProvider(default_tier=tier_cfg.get("default_tier", "free"))


def _build_drift_alert_manager(redis, config):
    """D-40 — build the reconciler's DriftAlertManager wired to the CONFIGURED
    dispatchers (log + webhook), so `divergence_detected` (a money incident,
    QB-01's signature) reaches a human instead of a log-only default.

    Config: `alerts.drift_webhook_url` (falls back to `alerts.webhook_url`),
    `alerts.drift_cooldown_seconds` (falls back to `alerts.cooldown_seconds`)."""
    from .alerts import DriftAlertManager, LogAlertDispatcher
    alerts_cfg = config.get("alerts", {}) or {}
    dispatchers = [LogAlertDispatcher()]
    webhook = alerts_cfg.get("drift_webhook_url") or alerts_cfg.get("webhook_url")
    if webhook:
        try:
            from .alerts import WebhookAlertDispatcher
            dispatchers.append(WebhookAlertDispatcher(url=webhook))
        except Exception as e:
            logger.warning("drift webhook dispatcher init failed: %s", e)
    return DriftAlertManager(
        redis=redis,
        dispatchers=dispatchers,
        cooldown_seconds=int(alerts_cfg.get("drift_cooldown_seconds",
                                            alerts_cfg.get("cooldown_seconds", 600))),
    )


def _emit_capabilities_snapshot(app, engine, config, *, enable_paid: bool) -> dict:
    """P6.2 — build + log the one-line integrity capabilities snapshot, and stash
    it on app.state.quota_capabilities. Answers "is my integrity actually wired?"
    for every degradation-prone seam. Best-effort reads; unknown-but-owned-elsewhere
    seams (activations = W-PY-B, reconciler = W-PY-C) are marked, never guessed.

    Values already set by the outbox resolver (`billing`, `outbox`) are preserved.
    """
    import os as _os
    caps = dict(getattr(app.state, "quota_capabilities", {}) or {})

    # Enforcement knobs (this lane).
    enf = getattr(engine, "_enforcement", None)
    if enf is not None:
        caps["enforcement"] = (
            f"enabled={getattr(enf, 'enabled', '?')},"
            f"shadow={getattr(enf, 'shadow_mode', '?')},"
            f"kill={getattr(enf, 'global_kill_switch', '?')}"
        )

    # Handler-ledger backend (a known silent-degradation site: DDB→Redis→memory).
    ledger = getattr(app.state, "quota_handler_ledger", None)
    caps["ledger_store"] = type(ledger).__name__ if ledger is not None else "none"

    # Outbox / billing default when the resolver didn't run (e.g. enable_paid off).
    caps.setdefault("outbox", "none" if not enable_paid else "unknown")
    caps.setdefault("billing", "OFF (paid disabled)" if not enable_paid else "unknown")

    # K-9: the active counter key shape + migration phase, from the one boot
    # read (app.state.quota_keyspace_state) — operators must be able to SEE
    # which shape a live process reads/writes.
    ksstate = getattr(app.state, "quota_keyspace_state", None)
    if ksstate is not None:
        caps["keyspace"] = (
            f"v{ksstate['version']}"
            + ("+dual" if ksstate["dual_write"] else "")
            + f"(phase={ksstate['migration_phase']})")
    else:
        ksobj = getattr(app.state, "quota_keyspace", None)
        if ksobj is not None:
            caps.setdefault("keyspace", f"v{ksobj.version}"
                            + ("+dual" if ksobj.dual_write else ""))

    # D-40: activation mode is a real, readable value — never `unknown(owned:…)`.
    # The durable-store detail is reported separately as `activation_store` by the
    # reconciler block (D-39); this field is the on/off rollback toggle.
    caps.setdefault("activations", _os.getenv("AB0T_QUOTA_ACTIVATIONS", "on"))
    if "reconciler" not in caps:
        caps["reconciler"] = (
            "on" if getattr(app.state, "quota_reconciler", None) is not None
            else "off (not started)"
        )

    app.state.quota_capabilities = caps
    line = " ".join(f"{k}={caps[k]}" for k in sorted(caps))
    logger.info("AB0T_QUOTA CAPABILITIES — %s", line)
    return caps


# D-40 — the snapshot must be CONSUMED, not merely emitted. Eight times in this
# library a mechanism was mistaken for the guarantee it owed; twice the mechanism
# was an event nobody received. A capabilities line that nothing reads is the same
# defect wearing ceremony. These two seams give it a reader.

#: Capability keys whose degradation is a MONEY problem, not a cosmetic one.
_MONEY_CRITICAL_CAPS = ("billing", "reconciler")

#: D-71/D-72/D-73 — capability keys whose degradation is an INFRASTRUCTURE problem:
#: the library's assumptions about the client's Redis, machine-checked at startup.
#:   redis_topology          (D-71) — multi-key Lua CROSSSLOTs on a cluster (D-23)
#:   counter_eviction_policy (D-72) — an allkeys-* Redis EVICTS a live gauge: the
#:                                    counter reads zero for a running resource and the
#:                                    library over-admits. THE quiet one: D-71 refuses
#:                                    at boot; this one leaks quota at runtime.
#:   redis_scripting         (D-73) — every counter op is EVAL
#: Startup refuses on all three — these entries make the verdict READABLE afterwards,
#: so a partially-wired or hand-built snapshot cannot report a cheerful 200 over an
#: environment the counter cannot safely run on.
#: `redis_version` (D-74) is recorded but NOT probe-critical: an unreadable version is
#: not the same class of hazard as an unreadable eviction policy (stated in the open —
#: see information_redis_preflight_20260712.md).
#:   counter_evictions_observed (D-80) — the FACT, not the policy: a server that has ALREADY
#:                                    evicted keys passes every policy check we own, while an
#:                                    evicted gauge sits UNDER-counted in the counter.
_INFRA_CRITICAL_CAPS = ("redis_reachable", "redis_topology",
                        "counter_eviction_policy", "redis_scripting",
                        "counter_evictions_observed")

#: Every capability the probe judges. Ordered: money first, then infrastructure.
_ALL_CRITICAL_CAPS = _MONEY_CRITICAL_CAPS + _INFRA_CRITICAL_CAPS

#: D-50 — periodic loops whose DEATH is a money problem. A dead drain silently
#: re-opens QB-01; a dead/refused reconciler leaves an orphaned over-count that
#: never heals (D-28); a dead stale-lease sweeper strands credit grants forever
#: (QC-02). These must fail /quota/health, not just log at startup.
#: NOTE (framed as D-54): this is the *universe* of money-critical loop NAMES.
#: `quota_health` derives the REQUIRED subset per-deployment from
#: `app.state.quota_required_loops` (what SHOULD run given config) so a loop that
#: was never wired still fails the probe — "absence of a loop is a level below
#: absence of a value" (see the artifact).
_MONEY_CRITICAL_LOOPS = ("outbox_drain", "reconciler_loop", "stale_lease_sweeper",
                         "heartbeat_monitor", "preflight_reverification")


def required_money_loops(config: dict, *, enable_paid: bool) -> set:
    """D-66 — the money-critical loops that MUST run, DERIVED from the declared
    config + capability schema in ONE place. Wiring SATISFIES this contract; it
    does NOT define it.

    A required-set assembled by appends at wiring sites can only list what someone
    remembered to register — add a loop, forget the append, and the probe is blind:
    the same defect class this ticket chased a dozen times, relocated one level up
    (nobody wired the loop → nobody registered the loop as required). Deriving it
    closes that: a loop the config asks for but nobody wired is required-but-absent,
    and `quota_health` degrades on it precisely because this set was derived.

      - enable_paid            ⇒ outbox_drain      (else usage is silently un-billed, QB-01)
      - reconciler enabled     ⇒ reconciler_loop   (else an orphaned over-count never heals, D-28)
      - auth-event webhook set ⇒ stale_lease_sweeper (else crashed handlers strand grants, QC-02)
    """
    import os as _os
    from .reconcile import _reconcile_enabled_env
    required: set = set()
    if enable_paid:
        required.add("outbox_drain")
    rcfg = config.get("reconcile", {}) or {}
    # Mirror setup's effective-enabled: config value if declared, else the env
    # default (so an operator kill-switch is respected and doesn't demand a loop
    # they deliberately turned off).
    reconcile_enabled = bool(rcfg["enabled"]) if "enabled" in rcfg else _reconcile_enabled_env()
    if reconcile_enabled:
        required.add("reconciler_loop")
    if _os.getenv("AB0T_AUTH_WEBHOOK_SECRET", ""):
        required.add("stale_lease_sweeper")
    # D-67: the heartbeat monitor is DEPRECATED and OFF by default. If a consumer
    # opts in (heartbeat.enabled), it becomes required — so a configured-but-UNFED
    # monitor degrades the probe ("you turned it on and never fed it").
    if (config.get("heartbeat", {}) or {}).get("enabled", False):
        required.add("heartbeat_monitor")
    # D-79 — the D-75 re-verification RIDES the reconciler loop, which a client can switch
    # off. A guarantee the client can switch off SILENTLY is not a guarantee. So DERIVE the
    # requirement from config (D-66's law: wiring SATISFIES the contract, it does not DEFINE
    # it): if the counter lives on Redis, that Redis's invariants MUST be re-verified — and a
    # required-but-absent loop degrades /quota/health. Turning the reconciler off is now a
    # visible, on-the-record degradation, not a quiet loss of the guarantee.
    # One resolution path (T-1/ENV-09): the same resolver the connection was
    # built from — OPTIONAL here so tier-less/store-less configs can still ask.
    # No generic env is consulted; an undeclared store creates no requirement.
    row = resolve_dependency(
        config, name="Redis counter store URL", config_key="storage.redis_url",
        env=("QUOTA_REDIS_URL",), requirement=Requirement.OPTIONAL,
    )
    if row.declared:
        required.add("preflight_reverification")
    return required


def quota_loop_liveness(app) -> dict:
    """D-50 — inspect the LIVE background workers on app.state and report each
    loop's liveness. This crosses the scheduler→human boundary: a worker that is
    dead or permanently backing off is invisible to a caps snapshot taken at
    startup, so it is read from the worker objects at call time.

    Returns ``{loop_name: {"healthy": bool, "detail": str}}`` for every worker
    that is present (absent workers are simply omitted — an app that never wired
    them, e.g. the capability unit tests, gets an empty dict and is judged on caps).
    """
    out: dict = {}
    emitter = getattr(app.state, "quota_emitter", None)
    if emitter is not None and hasattr(emitter, "drain_worker_liveness"):
        try:
            healthy, detail = emitter.drain_worker_liveness()
            out["outbox_drain"] = {"healthy": bool(healthy), "detail": str(detail)}
        except Exception as e:  # a broken liveness probe must never crash /health
            out["outbox_drain"] = {"healthy": False, "detail": f"liveness probe error: {e!r}"}
    reconciler = getattr(app.state, "quota_reconciler", None)
    if reconciler is not None and hasattr(reconciler, "loop_liveness"):
        try:
            healthy, detail = reconciler.loop_liveness()
            out["reconciler_loop"] = {"healthy": bool(healthy), "detail": str(detail)}
        except Exception as e:
            out["reconciler_loop"] = {"healthy": False, "detail": f"liveness probe error: {e!r}"}
    sweeper = getattr(app.state, "quota_stale_lease_sweeper", None)
    if sweeper is not None and hasattr(sweeper, "loop_liveness"):
        try:
            healthy, detail = sweeper.loop_liveness()
            out["stale_lease_sweeper"] = {"healthy": bool(healthy), "detail": str(detail)}
        except Exception as e:
            out["stale_lease_sweeper"] = {"healthy": False, "detail": f"liveness probe error: {e!r}"}
    # D-79 — the re-verification is CARGO on the reconciler loop. Liveness of the CARRIER is
    # not delivery of the CARGO: a reconciler that runs with no preflight wired onto it looks
    # healthy from the outside and re-verifies nothing. Both must hold.
    # Reported ONLY when the deployment's config DERIVED the requirement (D-79/D-66) — a
    # caller who never declared it (a unit test, an in-memory counter) is not judged on a loop
    # its configuration never asked for. A false 503 trains operators to ignore the probe (D-49).
    _required = getattr(app.state, "quota_required_loops", None) or set()
    rec = getattr(app.state, "quota_reconciler", None)
    if rec is not None and "preflight_reverification" in _required:
        carrier_ok, carrier_detail = (True, "running")
        if hasattr(rec, "loop_liveness"):
            try:
                carrier_ok, carrier_detail = rec.loop_liveness()
            except Exception as e:
                carrier_ok, carrier_detail = False, f"liveness probe error: {e!r}"
        cargo = getattr(rec, "_preflight", None) is not None
        out["preflight_reverification"] = {
            "healthy": bool(carrier_ok and cargo),
            "detail": ("re-verifying every reconcile pass" if (carrier_ok and cargo)
                       else "no preflight wired onto the reconciler loop" if carrier_ok
                       else f"carrier loop unhealthy: {carrier_detail}"),
        }

    heartbeat = getattr(app.state, "quota_heartbeat_monitor", None)
    if heartbeat is not None and hasattr(heartbeat, "monitor_liveness"):
        try:
            healthy, detail = heartbeat.monitor_liveness()
            out["heartbeat_monitor"] = {"healthy": bool(healthy), "detail": str(detail)}
        except Exception as e:
            out["heartbeat_monitor"] = {"healthy": False, "detail": f"liveness probe error: {e!r}"}
    return out


def quota_health(app) -> dict:
    """D-40 — money-aware health. Returns `degraded` when a money-critical
    capability is OFF, so a probe can FAIL rather than report a cheerful 200 over
    a service that is silently not billing (QB-01) or not healing drift (D-28).

    Consumers with their own /health should fold this in::

        h = quota_health(app)
        if h["status"] != "ok": ...  # degrade YOUR health check too

    The response names only capability KEYS, never their values — diagnostics live
    on /quota/capabilities.
    """
    caps = dict(getattr(app.state, "quota_capabilities", {}) or {})

    def _is_on(k) -> bool:
        # D-49 — "the absence of a positive signal is not health." A money-critical
        # capability is healthy ONLY when the snapshot AFFIRMATIVELY says it is on.
        # Missing (partial/absent snapshot), empty, `off*`, `unknown*`, or any
        # unparseable/transitional value ⇒ NOT proven on ⇒ degraded, 503. This is
        # D-31 applied to observability: over-reporting `degraded` is the safe
        # direction; a false `healthy` is the forbidden one. A predicate that is red
        # only on an explicit `off` is still green over a service that never finished
        # wiring integrity — the mechanism-vs-guarantee pattern, in the probe built
        # to end it. Normalized-prefix match so every real shipped on-value passes:
        # `ON (outbox=ddb)`, `on(provider)`, `on(ledger)`, `on`.
        s = str(caps.get(k, "") or "").strip().lower()
        return s.startswith("on")

    def _cap_ok(k) -> bool:
        # The infra caps have their own affirmative vocabularies — an "on" prefix would
        # be meaningless for a topology or an eviction policy. Everything else uses
        # D-49's on-only predicate. In every case ABSENCE is not health (D-51).
        if k == "redis_topology":                       # D-71
            from .topology import topology_ok
            return topology_ok(caps.get(k))
        if k == "counter_eviction_policy":              # D-72
            from .redis_preflight import EVICTING_POLICIES
            s = str(caps.get(k, "") or "").strip().lower()
            if not s or s.startswith("unknown") or s.startswith("evicting"):
                return False
            if any(p in s for p in EVICTING_POLICIES):  # never bless an evicting policy
                return False
            return True
        if k == "redis_scripting":                      # D-73
            return str(caps.get(k, "") or "").strip().lower().startswith("on")
        if k == "counter_evictions_observed":           # D-80
            from .redis_preflight import eviction_facts_ok
            return eviction_facts_ok(caps.get(k))
        if k == "redis_persist_status":                 # D-81
            from .redis_preflight import persist_facts_ok
            return persist_facts_ok(caps.get(k))
        if k == "memory_headroom":                      # D-77
            # Degrade only on a READ low-headroom. An unreadable memory statistic is not a
            # hazard the way an unreadable eviction policy is (the D-74 deviation, ratified,
            # applied here) — `unknown`/`unbounded` do not degrade.
            return not str(caps.get(k, "") or "").strip().lower().startswith("low_headroom")
        if k.startswith("ddb_"):                        # D-76
            return str(caps.get(k, "") or "").strip().upper().startswith("ACTIVE")
        return _is_on(k)

    # D-71: which capabilities MUST report affirmatively is DECLARED by setup_quota
    # (app.state.quota_required_caps), mirroring the D-66 required-loops derivation:
    # a real deployment always records `redis_topology`, so its ABSENCE degrades. A
    # caller that never declared the set (unit tests, a hand-built snapshot, a bridge
    # consumer with no Redis of its own) is judged on the money caps plus whatever
    # infra caps its snapshot actually carries — never on a key it was never asked for.
    required_caps = getattr(app.state, "quota_required_caps", None)
    if required_caps is None:
        required_caps = set(_MONEY_CRITICAL_CAPS) | {k for k in _INFRA_CRITICAL_CAPS if k in caps}

    # D-76/D-77 — capabilities that only EXIST when the deployment uses them (a DDB store; a
    # Redis that reports its memory). They are judged whenever present: their verdict is a
    # real signal, and a service whose ledger table went unsafe must not report 200.
    dynamic = [k for k in caps if k.startswith("ddb_")] + [
        k for k in ("memory_headroom", "redis_persist_status") if k in caps]

    degraded = [k for k in list(_ALL_CRITICAL_CAPS) + dynamic
                if (k in required_caps or k in dynamic) and not _cap_ok(k)]

    # D-50: a money-critical background LOOP that is dead / permanently backing off
    # must fail the probe too — a dead drain is QB-01 behind a 200. Read live from
    # the worker objects (absent workers omitted → backward compatible).
    loops = quota_loop_liveness(app)
    # D-54: derive the REQUIRED loops from the DECLARED CONTRACT (what SHOULD run
    # given config), set on app.state.quota_required_loops by setup_quota. A
    # required loop that is ABSENT from live liveness (never wired) is a guarantee
    # with NO capability entry — and a probe can only degrade on entries it knows.
    # "Absence of a loop is a level below absence of a value" — so a required loop
    # that is missing OR unhealthy degrades. When quota_required_loops is unset
    # (e.g. a consumer that didn't declare it, or the unit tests), fall back to the
    # D-50 behaviour: degrade only a PRESENT-but-unhealthy loop.
    required = getattr(app.state, "quota_required_loops", None)
    if required is not None:
        degraded += [name for name in _MONEY_CRITICAL_LOOPS if name in required
                     and (name not in loops or not loops[name]["healthy"])]
    else:
        degraded += [name for name in _MONEY_CRITICAL_LOOPS
                     if name in loops and not loops[name]["healthy"]]

    return {"status": "degraded" if degraded else "ok", "degraded": degraded}


def _register_capability_routes(app) -> None:
    """D-40 — mount `/quota/capabilities` (full snapshot) and `/quota/health`
    (money-aware, 503 when degraded). Namespaced so the library never squats on a
    consumer's own `/health`."""
    try:
        from fastapi.responses import JSONResponse
    except Exception:  # pragma: no cover - FastAPI always present in practice
        return

    async def _capabilities():
        snap = dict(getattr(app.state, "quota_capabilities", {}) or {})
        # D-50: surface live loop liveness alongside the startup snapshot, so a
        # worker that died AFTER boot is visible to a human, not just in the logs.
        snap["loops"] = quota_loop_liveness(app)
        return snap

    async def _health():
        h = quota_health(app)
        # 503 so an orchestrator's probe actually fails. A health check that is
        # always green is an event with no sink and a 200 attached.
        return JSONResponse(h, status_code=200 if h["status"] == "ok" else 503)

    existing = {getattr(r, "path", None) for r in getattr(app, "routes", [])}
    if "/quota/capabilities" not in existing:
        app.add_api_route("/quota/capabilities", _capabilities, methods=["GET"])
    if "/quota/health" not in existing:
        app.add_api_route("/quota/health", _health, methods=["GET"])


def _plan_ddb_values(app, config):
    """(region, endpoint) from the setup ResolutionPlan; direct callers (tests,
    tools) without a plan resolve the same specs — one resolution path, never
    an ambient read (T-1). region None defers to the AWS SDK's own chain."""
    plan = getattr(getattr(app, "state", None), "quota_resolution_plan", None)
    if plan is not None and "dynamodb_region" in plan:
        return plan["dynamodb_region"].value, plan["dynamodb_endpoint"].value
    return resolve_ddb_region(config).value, resolve_ddb_endpoint(config).value


async def _provision_activation_store(app, engine, *, redis, config, storage, injected):
    """D-39 — resolve a DURABLE activation ledger and set it on the engine. The
    ledger is authoritative for IDENTITY and COST (D-33); it must not live in an
    evictable cache. PREFER DDB (self-provisioned from standard config, or the
    `app.state.ddb_client` override), else fall back to Redis (which the reconciler
    then machine-checks for durability). An injected store is RESPECTED as-is.

    Returns an async close callable for a self-provisioned DDB client, or None.
    Durability itself is decided by the reconciler (single source: its
    `ledger_durability()`), so this only PROVISIONS — it does not duplicate the
    durability check (that reuse discipline is D-35)."""
    import os as _os
    if injected is not None:
        engine._activation_store = injected
        return None  # respect the consumer's choice; the reconciler judges durability

    act_cfg = config.get("activations", {}) or {}
    store_pref = (act_cfg.get("store") or "ddb").lower()
    table = act_cfg.get("ddb_table", "ab0t_quota_activations")

    # (1) Prefer DDB — a real durable store for identity+cost.
    if store_pref != "redis" and offline_mode(config):
        logger.warning("offline mode: activation-ledger DDB self-provision SKIPPED — using Redis under the durability machine-check")
    if store_pref != "redis" and not offline_mode(config):
        from .activations import DDBActivationStore, connect_ddb_activation_store
        ddb_close = None
        try:
            override = getattr(app.state, "ddb_client", None)
            if override is not None:
                store = DDBActivationStore(override, table_name=table)
            else:
                region, endpoint = _plan_ddb_values(app, config)
                store, ddb_close = await connect_ddb_activation_store(
                    region=region, endpoint_url=endpoint, table_name=table)
            # T-6/D-3 call-site policy: creation only under the explicit opt-in.
            await store.ensure_table(
                create=bool((config.get("storage") or {}).get("auto_create_tables", False)))
            # D-76: the ledger is authoritative for identity + cost (D-33) and lives in a
            # table nobody ever checked. Verify it (ACTIVE, GSIs ACTIVE, TTL on the attribute
            # we actually write, PITR on) and REFUSE to start if it is unsafe. Registered for
            # D-75 re-verification too — a table's config can change under us.
            await _preflight_ddb_store(app, config, store, cap_key="ddb_activations",
                                       table=table, ttl_attr="ttl",
                                       required_gsis=("GSI1",))  # open-rows scan
            engine._activation_store = store
            logger.info("activation ledger on DDB (durable) table=%s", table)
            return ddb_close
        except Exception as e:
            logger.warning("activation ledger DDB provision failed (%s) — falling back "
                           "to Redis under the durability machine-check (D-39)", e)
            if ddb_close is not None:
                try:
                    await ddb_close()
                except Exception:
                    pass

    # (2) Redis fallback — kept, but the reconciler will REFUSE it unless it passes
    # the durability machine-check (persistence + non-evicting policy, or the
    # on-the-record redis_durability_confirmed).
    from .activations import RedisActivationStore
    engine._activation_store = RedisActivationStore(redis)
    return None


async def _gate_redis_reachable(app, redis, config, plan) -> None:
    """GT T1/T2 — PING, classified, BEFORE any other gate. The capability is
    written before the refusal so /quota/capabilities and /quota/health show
    the cause. D-2: bounded retry (storage.connect_retry_seconds, default 30s,
    0 = fail immediately) absorbs transient boot blips — for the UNREACHABLE
    kind only; auth/ACL failures do not heal by waiting."""
    import asyncio as _aio
    import time as _t

    from .redis_preflight import (
        PROBE_FAILED_CAP, REACHABLE_OK, check_redis_reachable, reachability_error,
    )
    from .resolve import redact_url

    storage_cfg = (config or {}).get("storage", {}) or {}
    budget = float(storage_cfg.get("connect_retry_seconds", 30))
    deadline = _t.monotonic() + max(0.0, budget)
    delay = 0.5
    while True:
        ok, kind, detail = await check_redis_reachable(redis, timeout=5.0)
        if ok:
            break
        if kind == "unreachable" and _t.monotonic() + delay <= deadline:
            logger.warning("declared Redis unreachable — retrying within the D-2 "
                           "budget (%.0fs): %s", budget, detail)
            await _aio.sleep(delay)
            delay = min(delay * 2, 5.0)
            continue
        caps = dict(getattr(app.state, "quota_capabilities", {}) or {})
        caps["redis_reachable"] = f"{PROBE_FAILED_CAP} [{kind}: {detail}]"
        app.state.quota_capabilities = caps
        row = plan["redis_url"]
        err = reachability_error(kind, detail,
                                 url_display=redact_url(row.value or ""),
                                 source=row.source)
        logger.error("DECLARED REDIS UNREACHABLE/UNAUTHENTICATED (GT T1) — "
                     "refusing to start: %s", err)
        raise err
    caps = dict(getattr(app.state, "quota_capabilities", {}) or {})
    caps["redis_reachable"] = REACHABLE_OK
    app.state.quota_capabilities = caps
    logger.info("redis reachability verified (GT T1): PING ok")


async def _gate_redis_topology(app, redis, config) -> None:
    """D-71 — the startup TOPOLOGY gate, the twin of D-32's durability gate.

    `CLUSTER INFO` → cluster_enabled. A clustered Redis CROSSSLOTs every multi-key
    counter script (D-23, observed at a real cluster), so the library REFUSES TO
    START and says why. An unverifiable topology (managed Redis that disables
    CLUSTER INFO) is UNKNOWN, and unknown fails closed — unless the operator puts
    an assertion on the record (`storage.redis_cluster_confirmed_disabled: true`).
    An operator assertion NEVER overrides a positive cluster_enabled:1, exactly as
    `redis_durability_confirmed` never overrides an `allkeys-*` eviction policy.

    The verdict is written to Capabilities BEFORE any refusal, so a consumer that
    catches the error (or an operator reading /quota/capabilities) sees the cause.
    """
    from .topology import (
        SINGLE_NODE, capability_value, check_redis_cluster_topology,
        confirmed_disabled_from, topology_error,
    )
    topo, detail = await check_redis_cluster_topology(
        redis, confirmed_disabled=confirmed_disabled_from(config))

    caps = dict(getattr(app.state, "quota_capabilities", {}) or {})
    caps["redis_topology"] = capability_value(topo, detail)
    app.state.quota_capabilities = caps

    if topo == SINGLE_NODE:
        logger.info("redis topology verified (D-71): %s", detail)
        return
    from .topology import PROBE_FAILED
    if topo == PROBE_FAILED:
        # GT T4: a failed probe refuses as REACHABILITY (never a topology verdict)
        logger.error("REDIS PROBE FAILED (reachability/credentials) — refusing "
                     "to start: %s", detail)
    else:
        logger.error("REDIS TOPOLOGY UNSUPPORTED/UNVERIFIED (D-71) — refusing to start: %s", detail)
    raise topology_error(topo, detail)


def _make_redis_revalidator(app, redis, config, alerts):
    """D-75 — **"An assumption machine-checked once is an assumption trusted thereafter."**

    Returns an async callable that RE-RUNS the whole Redis preflight (topology, eviction,
    scripting, version, headroom) and reports the truth about the world *now*. It rides the
    reconciler loop — never its own worker, because every loop we add is one more thing that
    can be dead (D-50).

    A safe→unsafe transition at runtime is **LOUD, NOT FATAL**:
      * Capabilities are updated (so `/quota/capabilities` tells the truth),
      * `/quota/health` degrades **immediately** (the caps ARE the probe's input),
      * a money-incident alert fires — with a paired `restored` when it heals (D-26),
      * and the process **keeps serving**. A running service that suddenly refuses is its
        own outage; the operator decides whether to drain.

    Boot refuses; runtime degrades. Same judgement, different consequence.
    """
    from .redis_preflight import verify_redis_invariants

    async def _revalidate() -> None:
        try:
            # D-81: severity depends on WHERE the outbox lives — a failing AOF is money loss
            # only if the outbox is on THIS Redis (the counter heals; money does not).
            outbox_on_redis = bool(getattr(app.state, "quota_outbox_on_redis", False))
            caps_update, unsafe = await verify_redis_invariants(
                redis, config, outbox_on_redis=outbox_on_redis)
        except Exception as e:  # a broken re-check must never kill the loop (D-50)
            logger.error("redis re-verification failed (D-75): %s", e)
            return

        caps = dict(getattr(app.state, "quota_capabilities", {}) or {})
        previously_unsafe = set(getattr(app.state, "quota_unsafe_invariants", set()) or set())
        now_unsafe = {k for k, _ in unsafe}

        caps.update(caps_update)
        app.state.quota_capabilities = caps
        app.state.quota_unsafe_invariants = now_unsafe

        # D-80: an OBSERVED eviction means the counter itself is now untrustworthy — an evicted
        # gauge reads LOW. Mark it, so the reconcile pass that follows this re-check IN THE SAME
        # LOOP TICK converges it back to Σ open activations (D-28/D-33). The convergence is
        # STRUCTURAL — the re-check runs at the top of the reconcile pass — not a callback
        # somebody has to remember to wire (which is how half the defects in this ticket began).
        if "counter_evictions_observed" in now_unsafe:
            app.state.quota_counter_untrusted = True
        elif getattr(app.state, "quota_counter_untrusted", False):
            app.state.quota_counter_untrusted = False

        for key, detail in unsafe:
            if key not in previously_unsafe:
                logger.error(
                    "REDIS INVARIANT VIOLATED AT RUNTIME (D-75) — %s: %s. The library verified "
                    "this at startup and it has CHANGED underneath us. Health is degraded; the "
                    "service keeps serving (a sudden refusal is its own outage) — drain or fix.",
                    key, detail)
            if alerts is not None:
                try:
                    await alerts.invariant_violated(key, detail)
                except Exception as e:
                    logger.error("invariant alert dispatch failed: %s", e)

        for key in previously_unsafe - now_unsafe:
            logger.info("redis invariant RESTORED (D-75) — %s", key)
            if alerts is not None:
                try:
                    await alerts.invariant_restored(key)
                except Exception as e:
                    logger.error("invariant restore-alert dispatch failed: %s", e)

    return _revalidate


def _ddb_client_of(store):
    """The DDB client a provisioned store talks to (`DDBOutboxStore.ddb` /
    `DDBActivationStore._ddb`). None for a store the consumer injected themselves — the
    D-76 gate verifies the DynamoDB tables THIS LIBRARY provisions and writes to; a custom
    store the consumer wired is theirs to guarantee (and we say so, rather than guessing at
    a control plane we were never given)."""
    return getattr(store, "ddb", None) or getattr(store, "_ddb", None)


async def _preflight_ddb_store(app, config, store, *, cap_key: str, table: str,
                               ttl_attr: str, required_gsis: tuple = ()) -> None:
    """D-76 boot gate + D-75 registration for one provisioned DDB store."""
    client = _ddb_client_of(store)
    if client is None:
        caps = dict(getattr(app.state, "quota_capabilities", {}) or {})
        caps[cap_key] = "ACTIVE (custom store — no DDB control plane to verify)"
        app.state.quota_capabilities = caps
        logger.info("DDB preflight skipped for %s: the store exposes no DynamoDB client "
                    "(consumer-injected store)", cap_key)
        return
    _register_ddb_preflight_target(app, cap_key, client, table, ttl_attr, required_gsis)
    await _gate_ddb_stores(app, config,
                           clients={cap_key: (client, table, ttl_attr, required_gsis)})


def _register_ddb_preflight_target(app, cap_key: str, client, table: str, ttl_attr: str, required_gsis: tuple = ()) -> None:
    """D-75/D-76 — remember every DDB table we depend on, so the periodic re-verification
    (riding the reconciler loop) can re-check them too. A boot-time check that is never
    repeated is exactly the caveat D-75 exists to close."""
    targets = dict(getattr(app.state, "quota_ddb_preflight_targets", {}) or {})
    targets[cap_key] = (client, table, ttl_attr, required_gsis)
    app.state.quota_ddb_preflight_targets = targets


async def _gate_ddb_stores(app, config, *, clients) -> None:
    """D-76 — the DynamoDB preflight, at boot. `clients` maps capability_key →
    (client, table, ttl_attribute). Refuses on a FATAL finding (missing/inactive table, a
    backfilling GSI, a TTL pointed at an attribute we do not write, or an unverified backup
    posture on a money store); WARNS on a disabled TTL (nothing is lost — refusing there
    would be the D-49 false-503 mistake)."""
    if not clients:
        return
    from .ddb_preflight import DDBPreflightError, pitr_confirmed_from, verify_ddb_table

    caps = dict(getattr(app.state, "quota_capabilities", {}) or {})
    confirmed = pitr_confirmed_from(config)
    for cap_key, (client, table, ttl_attr, required_gsis) in clients.items():
        value, fatal, warn = await verify_ddb_table(
            client, table, ttl_attribute=ttl_attr, pitr_confirmed=confirmed,
            required_gsis=tuple(required_gsis))
        caps[cap_key] = value
        app.state.quota_capabilities = caps
        if warn:
            logger.warning("DDB preflight WARNING (%s): %s", cap_key, warn)
        if fatal:
            logger.error("DDB STORE UNSAFE (D-76) — refusing to start: %s: %s", cap_key, fatal)
            raise DDBPreflightError(cap_key, fatal)
        logger.info("DDB preflight verified (D-76): %s = %s", cap_key, value)


async def _gate_redis_counter_store(app, redis, config) -> None:
    """D-72/D-73/D-74 — the counter's own infrastructure preflight.

    **D-72 (eviction).** `check_redis_outbox_durability` (D-32) guarded the OUTBOX; the
    COUNTER — the thing the library exists to protect — ran on the same Redis with no
    check at all. Under `allkeys-*`, Redis evicts a live gauge under memory pressure,
    the counter reads ZERO for a running resource, and admission silently over-grants:
    under-count → phantom headroom → over-admission (D-31's forbidden direction). This
    is *worse* than D-71: D-71 refuses loudly at boot; this fails silently at runtime,
    as free quota, behind a green probe. Hard startup ERROR. `CONFIG` unavailable →
    the explicit on-the-record `storage.redis_durability_confirmed` assertion (D-32's
    shape); an assertion never overrides a policy the server actually reported.

    **D-73 (scripting).** Every counter op is EVAL. `SCRIPT LOAD` the REAL `_ACQUIRE`
    at boot — so a Redis that cannot run our Lua is a startup refusal, not a first-
    acquire outage. (It also warms the script cache.)

    **D-74 (version floor).** Below the floor → refuse. Unreadable (INFO disabled) →
    `unknown`, which DEGRADES the probe but does not refuse: see the artifact — a
    deliberate, stated deviation, because a version we cannot read is not the same kind
    of hazard as an eviction policy we cannot read.

    Every verdict is written to Capabilities BEFORE any refusal, so the cause is
    readable on /quota/capabilities and fails /quota/health (D-40/D-49/D-51).
    """
    from .redis_preflight import (
        check_redis_counter_eviction, check_redis_script_capability, check_redis_version,
        counter_eviction_error, durability_confirmed_from, scripting_confirmed_from,
        scripting_error, version_error,
    )
    caps = dict(getattr(app.state, "quota_capabilities", {}) or {})

    # --- D-72: eviction (the urgent one) ---
    confirmed = durability_confirmed_from(config)
    ok, detail = await check_redis_counter_eviction(redis, confirmed=confirmed)
    caps["counter_eviction_policy"] = detail if ok else f"EVICTING/UNVERIFIED ({detail})"
    app.state.quota_capabilities = caps
    if not ok:
        logger.error("COUNTER STORE UNSAFE (D-72) — refusing to start: %s", detail)
        raise counter_eviction_error(detail)
    logger.info("counter eviction policy verified (D-72): %s", detail)

    # --- D-73: scripting (T-9 hatch: assertion covers the unrunnable-probe
    # case only; an observed Lua compile rejection is never overridable) ---
    script_ok, script_detail = await check_redis_script_capability(
        redis, confirmed=scripting_confirmed_from(config))
    caps["redis_scripting"] = script_detail if script_ok else f"OFF ({script_detail})"
    app.state.quota_capabilities = caps
    if not script_ok:
        logger.error("REDIS CANNOT RUN THE COUNTER'S LUA (D-73) — refusing to start: %s",
                     script_detail)
        raise scripting_error(script_detail)

    # --- D-74: version floor ---
    status, ver_detail = await check_redis_version(redis)
    caps["redis_version"] = ver_detail if status == "ok" else f"{status} ({ver_detail})"
    app.state.quota_capabilities = caps
    if status == "below_floor":
        logger.error("REDIS BELOW SUPPORTED VERSION FLOOR (D-74) — refusing to start: %s",
                     ver_detail)
        raise version_error(ver_detail)
    if status == "unknown":
        logger.warning("Redis version could not be verified (D-74): %s", ver_detail)

    # --- D-77: memory headroom. `noeviction` fails CLOSED at the cliff (writes OOM →
    # acquire raises → admission denies) — the SAFE direction, but the service DIES. So this
    # never refuses; it DEGRADES on the way there, so the cliff is visible before 3am.
    from .redis_preflight import check_memory_headroom
    mem_status, mem_detail = await check_memory_headroom(redis)
    caps["memory_headroom"] = (mem_detail if mem_status == "ok"
                               else f"{mem_status} ({mem_detail})" if mem_status == "low_headroom"
                               else mem_status)
    app.state.quota_capabilities = caps
    if mem_status == "low_headroom":
        logger.error("REDIS MEMORY HEADROOM LOW (D-77): %s", mem_detail)


async def _resolve_outbox_durability(app, emitter, *, redis, config, storage, enable_paid):
    """D-32/D-34 — make the lifecycle outbox durable BY DEFAULT, and REFUSE TO
    START a paid service that cannot durably bill (D-34 Option A). Returns an
    async close callable for a self-provisioned DDB client, or None. Sets
    app.state.quota_capabilities['billing'] / ['outbox'].

    Order: (1) self-provision DDB from config (bounded retry+backoff — a transient
    blip must not stop a deploy, an ABSENCE must) unless outbox.store=redis;
    (2) if DDB absent, run the Redis durability self-check; (3) gate: durable →
    billing ON; not durable + paid + !allow_ephemeral → RAISE (fail startup);
    not durable + allow_ephemeral → start with billing DISABLED (dev)."""
    import asyncio as _asyncio
    import os as _os
    outbox_cfg = config.get("outbox", {}) or {}
    store_pref = (outbox_cfg.get("store") or "ddb").lower()
    allow_ephemeral = bool(outbox_cfg.get("allow_ephemeral", False))
    ddb_table = outbox_cfg.get("ddb_table", "ab0t_quota_outbox")
    retry_cfg = outbox_cfg.get("provision_retry", {}) or {}
    attempts = max(1, int(retry_cfg.get("attempts", 3)))
    base_delay = float(retry_cfg.get("initial_seconds", 0.5))
    max_delay = float(retry_cfg.get("max_seconds", 5.0))
    ddb_close = None
    durable = False
    detail = "none"

    from .billing.outbox import (
        DDBOutboxStore, RedisOutboxStore, connect_ddb_outbox_store,
        check_redis_outbox_durability,
    )

    # (1) Self-provision DDB by default, with BOUNDED RETRY + BACKOFF. A transient
    # DDB blip is retried; an ABSENCE (retries exhausted) falls through — and, for
    # a paid service, ultimately stops startup. app.state.ddb_client is an OPTIONAL
    # override, never a precondition.
    if store_pref != "redis" and offline_mode(config):
        logger.warning("offline mode: outbox DDB self-provision SKIPPED — "
                       "falling to the Redis outbox under the durability machine-check")
    if store_pref != "redis" and not offline_mode(config):
        for attempt in range(1, attempts + 1):
            try:
                override = getattr(app.state, "ddb_client", None)
                if override is not None:
                    store = DDBOutboxStore(override, table_name=ddb_table)
                else:
                    region, endpoint = _plan_ddb_values(app, config)
                    store, ddb_close = await connect_ddb_outbox_store(
                        region=region, endpoint_url=endpoint, table_name=ddb_table,
                    )
                # T-6/D-3 call-site policy: creation only under the explicit opt-in.
                await store.ensure_table(
                    create=bool((storage or {}).get("auto_create_tables", False)))
                # D-76: the outbox holds MONEY events nothing can reconstruct. Same gate.
                await _preflight_ddb_store(app, config, store, cap_key="ddb_outbox",
                                           table=ddb_table, ttl_attr="ttl",
                                           required_gsis=("gsi_status",))  # drain scan
                emitter.set_outbox_store(store)
                durable, detail = True, "DDB"
                break
            except Exception as e:
                logger.warning("outbox DDB provision attempt %d/%d failed: %s", attempt, attempts, e)
                if ddb_close is not None:
                    try:
                        await ddb_close()
                    except Exception:
                        pass
                    ddb_close = None
                if attempt < attempts:
                    await _asyncio.sleep(min(base_delay * (2 ** (attempt - 1)), max_delay))
        if not durable:
            logger.error("outbox DDB unavailable after %d attempts — treating as ABSENT", attempts)

    # (2) Redis durability self-check (CHECK 2 as a machine check).
    if not durable and redis is not None:
        # D-81: the outbox is landing on THIS Redis — so a persistence FAILURE here is money
        # nobody can reconstruct, not a counter that heals. Record it for the runtime re-check.
        app.state.quota_outbox_on_redis = True
        emitter.set_outbox_store(RedisOutboxStore(redis))
        confirmed = bool(outbox_cfg.get("redis_durability_confirmed", False))
        durable, detail = await check_redis_outbox_durability(redis, confirmed=confirmed)
        if durable:
            logger.info("outbox on Redis; durability verified: %s", detail)
        else:
            logger.error("OUTBOX REDIS NOT DURABLE (OPERATOR CHECK 2): %s", detail)

    # (3) Gate.
    caps = dict(getattr(app.state, "quota_capabilities", {}) or {})
    if durable:
        caps["outbox"] = detail
        caps["billing"] = f"ON (outbox={detail})"
        app.state.quota_capabilities = caps
        logger.info("quota capabilities: billing=%s", caps["billing"])
        return ddb_close

    caps["outbox"] = f"NON-DURABLE ({detail})"
    if not enable_paid:
        # Not a paid service — billing was never on; record and continue.
        caps["billing"] = f"OFF — no durable outbox ({detail})"
        app.state.quota_capabilities = caps
        logger.warning("no durable outbox (%s); enable_paid=False so billing was never on", detail)
        return ddb_close
    if allow_ephemeral:
        # Explicit on-the-record dev escape: start, but billing is DISABLED.
        emitter.disable_billing(f"no durable outbox ({detail}); allow_ephemeral=true")
        caps["billing"] = f"OFF — no durable outbox ({detail}, allow_ephemeral=true)"
        app.state.quota_capabilities = caps
        logger.warning("paid service starting with billing DISABLED (allow_ephemeral=true, DEV): %s", detail)
        return ddb_close

    # D-34 Option A: a paid service that cannot durably bill must NOT start.
    # Serving billable work for free is the leak, not availability — QB-01 through
    # a different door.
    caps["billing"] = f"OFF — no durable outbox ({detail})"
    app.state.quota_capabilities = caps
    raise RuntimeError(
        f"enable_paid=True but NO durable outbox store is available ({detail}). A paid service that "
        f"cannot durably record billing must not start (D-34): serving billable workloads for free "
        f"is the leak, not availability. Fix: make DDB reachable (outbox.store=ddb + DYNAMODB "
        f"endpoint/creds, or app.state.ddb_client; on a FRESH environment also pre-create the "
        f"outbox table or opt in with storage.auto_create_tables=true, T-6), or provide a durable "
        f"Redis (persistence + a non-evicting policy, or outbox.redis_durability_confirmed=true), "
        f"or set outbox.allow_ephemeral=true to start with billing DISABLED (dev only)."
    )


def _wire_paid_tier_sync(
    app: FastAPI,
    engine: QuotaEngine,
    redis: Redis,
    config: dict,
    *,
    auth_reader: Optional[Any] = None,
    auth_admin: Optional[Any] = None,
    auth_url: Optional[str] = None,
    auth_org_slug: Optional[str] = None,
    checkout_store: Optional[Any] = None,
    templates_dir: Optional[str] = None,
    route_prefix: str = "/api",
    tiers: Optional[dict] = None,
) -> Optional[dict]:
    """Mount the paid-tier surface synchronously: lifecycle emitter, billing
    proxy router. Returns state needed by the lifespan (heartbeat monitor)."""
    # Mesh credentials. Today the ab0t mesh issues separate API keys per
    # upstream service (billing has its own scope set, payment has its own).
    # Allow per-upstream override; fall back to AB0T_MESH_API_KEY for the
    # future unified-mesh-credential case.
    mesh_key = os.getenv("AB0T_MESH_API_KEY", "")
    billing_api_key = os.getenv("AB0T_MESH_BILLING_API_KEY", "") or mesh_key
    payment_api_key = os.getenv("AB0T_MESH_PAYMENT_API_KEY", "") or mesh_key
    consumer_org_id = os.getenv("AB0T_CONSUMER_ORG_ID", "")
    state: dict = {}

    if not (billing_api_key or payment_api_key):
        logger.warning("enable_paid=True but no mesh API key set "
                       "(AB0T_MESH_API_KEY or AB0T_MESH_{BILLING,PAYMENT}_API_KEY); "
                       "skipping paid-tier wiring")
        return state

    # LifecycleEmitter bound to engine for cost auto-recording. The
    # consumer can opt out of auto-record by setting cost_resource_key
    # to null in config (some consumers track cost out-of-band).
    cost_resource_key = config.get("billing_integration", {}).get("cost_resource_key")
    if cost_resource_key:
        from .billing.lifecycle import LifecycleEmitter
        # Outbox knobs (QB-01 / D-9 / D-12). Default horizon 900s — the
        # deployed reservation window is unverified (prod file says 1440min,
        # dev/template say 30min), so default to the worst case until an
        # operator confirms. Past-horizon events are voided + alerted, never
        # silently dropped.
        outbox_cfg = config.get("outbox", {}) or {}
        # Provisional store only. The AUTHORITATIVE durable store is resolved
        # asynchronously in the lifespan (_resolve_outbox_durability, D-32):
        # self-provision DDB by default, else run the Redis durability self-check,
        # else refuse to bill. A Redis client is available synchronously so the
        # emitter is never store-less; the lifespan upgrades/validates it.
        from .billing.outbox import RedisOutboxStore
        provisional_store = (
            RedisOutboxStore(redis)
            if (outbox_cfg.get("enabled", True) and redis is not None) else None
        )
        # --- D-12: the settlement fallback gets a REAL billing client --------
        # Billing's durable settlement path (POST /billing/{org_id}/settle) closes
        # the revenue-loss hole — but ONLY if something calls it. Without this
        # client the emitter keeps voiding-and-alerting past the horizon and the
        # money is still gone: a mechanism with no caller (the parent's D-64).
        # No billing key → None → the emitter voids exactly as it did before.
        # Ticket: billing/output/tickets/20260712_revenue_chain_integrity (D-12).
        settlement_client = None
        if billing_api_key:
            from .billing.clients import BillingServiceClient
            settlement_client = BillingServiceClient(
                base_url=_mesh_url("billing"), api_key=billing_api_key,
            )
        else:
            logger.error(
                "D-12: NO billing API key — the lifecycle outbox has NO settlement "
                "fallback. A money event that misses its reservation window will be "
                "VOIDED and the revenue LOST. Set AB0T_MESH_BILLING_API_KEY."
            )

        emitter = LifecycleEmitter(
            engine=engine,
            cost_resource_key=cost_resource_key,
            outbox_enabled=outbox_cfg.get("enabled", True),
            outbox_max_retry_horizon_s=float(outbox_cfg.get("max_retry_horizon_s", 900.0)),
            outbox_past_horizon=outbox_cfg.get("past_horizon", "void_and_alert"),
            outbox_store=provisional_store,
            settlement_client=settlement_client,
        )
        app.state.quota_emitter = emitter

        # Heartbeat monitor (DEPRECATED — D-67 / QB-04). Superseded by the
        # observed_usage_provider seam + reconciler (D-33): provider-PULLED existence
        # truth beats consumer-PUSHED heartbeats (a crashed process can't heartbeat).
        # DISABLED BY DEFAULT so no deployment carries an unfed, un-feedable dormant
        # loop that creates false confidence. Opt-in via heartbeat.enabled; when on it
        # is a REQUIRED, visible loop (D-66) whose UNFED state degrades /quota/health.
        if (config.get("heartbeat", {}) or {}).get("enabled", False):
            try:
                from .billing.heartbeat import HeartbeatMonitor
                _hb = HeartbeatMonitor(redis=redis, emitter=emitter)
                state["heartbeat_monitor"] = _hb
                app.state.quota_heartbeat_monitor = _hb   # surfaced for liveness/health
                logger.warning("heartbeat monitor ENABLED (DEPRECATED, D-67) — feed it via "
                               "monitor.record(); an UNFED monitor degrades /quota/health. "
                               "Prefer observed_usage_provider.")
            except Exception as e:
                logger.warning("heartbeat monitor init failed: %s", e)
        else:
            logger.info("heartbeat monitor disabled (default; DEPRECATED/superseded by "
                        "observed_usage_provider — D-67)")

    # Billing/payment proxy router
    if not consumer_org_id:
        logger.warning("enable_paid=True but AB0T_CONSUMER_ORG_ID not set; "
                       "billing router not mounted")
        return state

    try:
        from .billing import create_billing_router
        router = create_billing_router(
            payment_url=_mesh_url("payment"),
            payment_api_key=payment_api_key,
            billing_url=_mesh_url("billing"),
            billing_api_key=billing_api_key,
            consumer_org_id=consumer_org_id,
            auth_reader=auth_reader,
            auth_admin=auth_admin,
            auth_url=auth_url,
            auth_org_slug=auth_org_slug,
            quota_config_path=os.getenv("QUOTA_CONFIG_PATH"),
            checkout_store=checkout_store,
            templates_dir=templates_dir,
            prefix=route_prefix,
            # T0b — thread the already-loaded tier_registry through so the
            # Stripe webhook proxy can use it for paid-invoice event
            # dispatch (T2/T3 — both invoice.paid and invoice.payment_succeeded).
            # Same instance as the engine's tier dict; startup validation
            # and runtime policy stay in sync.
            tier_registry=tiers,
        )
        app.include_router(router)
        logger.info("paid-tier proxy router mounted at prefix=%s", route_prefix)
    except Exception as e:
        logger.warning("paid-tier router mount failed: %s", e)

    # Auth-event webhook receiver — generic infrastructure, the lib auto-
    # registers a default signup-credit handler so consumers get drop-in
    # signup grants with zero custom code (T11 in ticket
    # 20260516_auto_credit_invoice_paid_wiring).
    #
    # Resolution order for the auth.user.registered event:
    #   1. If a consumer has already registered their own handler (via
    #      @on_auth_event / register_handler from their app code), the
    #      consumer's handler runs. The default factory still runs too —
    #      idempotency at grant_initial_credit_for_user's Redis flag +
    #      billing-side idempotency_key dedupes so only one grant lands.
    #   2. Otherwise, the lib's default handler runs — reads tier_registry
    #      for each user's tier, applies the configured credit_grant
    #      (with trigger=signup) per the new schema. Legacy `initial_credit`
    #      back-compat already covered by grant_initial_credit_for_user.
    #
    # The consumer never has to write a custom signup-credit handler. They
    # only declare credit_grant in quota-config.json.
    webhook_secret = os.getenv("AB0T_AUTH_WEBHOOK_SECRET", "")
    if webhook_secret:
        try:
            from . import auth_events as _ae
            from .handler_ledger import auto_select_store

            # Auto-select ledger backend: DDB > Redis > InMemory.
            # Wires observability + idempotency for @idempotent handlers.
            ddb_for_ledger = getattr(app.state, "ddb_client", None)
            ledger_store = auto_select_store(redis=redis, ddb_client=ddb_for_ledger)
            app.state.quota_handler_ledger = ledger_store

            # D-66: the stale-lease sweeper's REQUIREMENT is DERIVED from config
            # (auth-event webhook set ⇒ required) in ONE place — required_money_loops,
            # set on the lifespan — NOT appended here. Wiring only SATISFIES the
            # contract; a wiring failure below therefore cannot un-declare it, and a
            # future loop cannot be forgotten at a wiring site.

            # D1 / D-50 / QC-02 — start the library-owned stale-lease sweeper so a
            # handler that crashed mid-delivery (its in_progress row stranded, auth
            # already 200'd) is RECLAIMED + re-driven. `drain_stale_leases` had no
            # scheduler — a disconnected guarantee — so credit grants stayed stranded
            # forever, invisible to `events --status failed`. Money-critical.
            try:
                from .handler_ledger import StaleLeaseSweeper
                _sweep_cfg = config.get("stale_lease", {}) or {}

                async def _sweep_redispatch(row):
                    await _ae.redispatch_stale_row(row, ledger_store)

                _sweeper = StaleLeaseSweeper(
                    ledger_store, _sweep_redispatch,
                    interval_seconds=float(_sweep_cfg.get("interval_seconds", 300)),
                    limit=int(_sweep_cfg.get("limit", 100)),
                )
                app.state.quota_stale_lease_sweeper = _sweeper
                _prev_ls = app.router.lifespan_context

                @asynccontextmanager
                async def _sweeper_lifespan(_app, _prev_ls=_prev_ls, _sweeper=_sweeper):
                    _sweeper.start()
                    try:
                        if _prev_ls is not None:
                            async with _prev_ls(_app):
                                yield
                        else:
                            yield
                    finally:
                        await _sweeper.stop()

                app.router.lifespan_context = _sweeper_lifespan
            except Exception as e:
                logger.warning("stale-lease sweeper wiring failed: %s", e)

            app.include_router(
                _ae.make_router(webhook_secret=webhook_secret, ledger_store=ledger_store),
                prefix=route_prefix + "/quotas",
            )
            logger.info("auth-event webhook mounted at %s/quotas/_webhooks/auth (ledger=%s)",
                        route_prefix, type(ledger_store).__name__)

            # T11 — auto-register the default signup-credit handler.
            # Build the legacy initial_credits dict from tiers (back-compat
            # path); also pass tier_registry so the new credit_grant schema
            # takes precedence when configured.
            legacy_initial_credits: dict[str, float] = {}
            for tier_id, tier_cfg in tiers.items():
                ic = getattr(tier_cfg, "initial_credit", None)
                if ic is not None and ic > 0:
                    legacy_initial_credits[tier_id] = float(ic)

            billing_url = _mesh_url("billing")
            billing_api_key = (
                os.getenv("AB0T_MESH_BILLING_API_KEY", "")
                or os.getenv("AB0T_MESH_API_KEY", "")
            )
            mesh_api_key = os.getenv("AB0T_MESH_API_KEY", "") or billing_api_key
            # T-1/ENV-08: declared/namespaced only — the generic AUTH_SERVICE_URL
            # read is gone. Unset ⇒ the PIN path is off (logged in the plan).
            auth_url_for_pin = resolve_dependency(
                config, name="auth service URL (tier-pinning)", config_key="auth.url",
                env=("AB0T_AUTH_AUTH_URL",), requirement=Requirement.OPTIONAL,
            ).value or ""

            default_handler = _ae._build_default_credit_grant_handler(
                initial_credits=legacy_initial_credits,
                tier_provider=provider,
                redis=redis,
                billing_url=billing_url,
                billing_api_key=billing_api_key,
                auth_url=auth_url_for_pin,
                mesh_api_key=mesh_api_key,
                pin_store=None,  # consumer can pass their own pin_store via @on_auth_event if needed
                tier_registry=tiers,
            )

            # Sentinel attribute so we can detect/un-register the lib-owned
            # handler later if a consumer wants to fully replace it.
            setattr(default_handler, "_ab0t_quota_default", True)

            already_registered = _ae.registered_event_types()
            if "auth.user.registered" in already_registered:
                # Consumer registered their own handler too — both run; the
                # Redis flag + billing idempotency_key make this safe.
                logger.info(
                    "auth-event: consumer also has a handler on auth.user.registered; "
                    "both will run, lib dedups via Redis flag + billing idempotency_key"
                )

            _ae.register_handler("auth.user.registered", default_handler)
            logger.info(
                "auth-event: default signup-credit handler auto-registered "
                "(%d legacy initial_credits + tier_registry with %d tiers)",
                len(legacy_initial_credits), len(tiers),
            )
        except Exception as e:
            logger.warning("auth-event webhook setup failed: %s", e)
    else:
        logger.info("AB0T_AUTH_WEBHOOK_SECRET not set — auth-event webhook disabled "
                    "(initial credits will not auto-grant on registration)")

    return state


def _mount_quota_routes(
    app: FastAPI,
    engine: QuotaEngine,
    prefix: str,
    org_extractor: Optional[Callable[[Request], Awaitable[Optional[str]]]],
    auth_dependency: Optional[Any],
) -> None:
    """Mount /usage, /tiers, /check/{key}, /check-bundle/{name}."""
    router = APIRouter()

    async def _default_extract(request: Request) -> Optional[str]:
        user = getattr(request.state, "user", None)
        return getattr(user, "org_id", None) if user else None

    extract = org_extractor or _default_extract
    deps = [auth_dependency] if auth_dependency else []

    @router.get("/usage", tags=["quota"], dependencies=deps)
    async def get_usage(request: Request):
        org_id = await extract(request)
        if not org_id:
            raise HTTPException(status_code=401, detail="Unable to resolve org_id")
        usage = await engine.get_usage(org_id)
        return usage.model_dump()

    @router.get("/tiers", tags=["quota"])
    async def get_tiers():
        """Returns the engine's loaded tier config — the consumer's actual limits."""
        out = []
        for tier in sorted(engine._tiers.values(), key=lambda t: t.sort_order):
            limits = {}
            for key, tl in tier.limits.items():
                limits[key] = {
                    "limit": tl.limit,
                    "limit_display": "Unlimited" if tl.limit is None else f"{tl.limit:g}",
                }
            out.append({
                "tier_id": tier.tier_id,
                "display_name": tier.display_name,
                "description": tier.description,
                "features": list(tier.features),
                "limits": limits,
                "upgrade_url": tier.upgrade_url,
            })
        return {"tiers": out}

    @router.get("/check/{resource_key}", tags=["quota"], dependencies=deps)
    async def check_resource(resource_key: str, request: Request):
        org_id = await extract(request)
        if not org_id:
            raise HTTPException(status_code=401, detail="Unable to resolve org_id")
        result = await engine.check(QuotaCheckRequest(org_id=org_id, resource_key=resource_key))
        return result.model_dump()

    @router.get("/check-bundle/{bundle_name}", tags=["quota"], dependencies=deps)
    async def check_bundle(bundle_name: str, request: Request):
        org_id = await extract(request)
        if not org_id:
            raise HTTPException(status_code=401, detail="Unable to resolve org_id")
        result = await engine.check_for_bundle(org_id, bundle_name)
        return result.model_dump()

    app.include_router(router, prefix=prefix)
