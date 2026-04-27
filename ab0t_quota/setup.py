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
    load_resource_bundles,
    load_resources,
    load_tiers,
)
from .engine import QuotaEngine
from .middleware import QuotaGuard
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

    # ----- Mode selection ---------------------------------------------------
    # Resolution order: explicit kwarg → config.engine_mode → "local"
    resolved_mode = (mode or config.get("engine_mode") or "local").lower()
    if resolved_mode not in ("local", "byo_redis", "bridge"):
        logger.warning("unknown engine_mode %r — falling back to 'local'", resolved_mode)
        resolved_mode = "local"

    if resolved_mode == "bridge":
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

    redis_url = (
        storage.get("redis_url")
        or os.getenv("QUOTA_REDIS_URL")
        or os.getenv("REDIS_URL", "redis://localhost:6379/0")
    )
    redis = Redis.from_url(redis_url, decode_responses=False)

    registry = ResourceRegistry()
    for r in load_resources(config):
        registry.register(r)

    tiers = load_tiers(config)
    bundles = load_resource_bundles(config)
    provider = _build_tier_provider(config, redis)

    # Engine without an override loader yet — set after store init in the lifespan
    engine = QuotaEngine(
        redis=redis,
        tier_provider=provider,
        registry=registry,
        tiers=tiers,
        resource_bundles=bundles,
    )

    # Alert manager: log dispatcher always; webhook if configured
    dispatchers = [LogAlertDispatcher()]
    alerts_cfg = config.get("alerts", {})
    if alerts_cfg.get("webhook_url"):
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
        ) if enable_paid else None
    )

    # === ASYNC PHASE — composed into the app's lifespan =======================

    existing_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def composed_lifespan(_app: FastAPI):
        # Inner: existing user lifespan (if any), wrapped by our async init/teardown
        store: Optional[QuotaStore] = None
        if storage.get("persistence_enabled", True):
            try:
                store = QuotaStore(
                    table_name=storage.get("dynamodb_table", "ab0t_quota_state"),
                    region=storage.get("dynamodb_region", os.getenv("AWS_REGION", "us-east-1")),
                    endpoint_url=os.getenv("DYNAMODB_ENDPOINT") or None,
                )
                await store.initialize()
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
                restored = await store.seed_redis(redis, registry)
                if restored:
                    logger.info("seeded %d quota counters from DynamoDB", restored)
            except Exception as e:
                logger.warning("seed_redis failed (non-fatal): %s", e)

            # Start snapshot worker
            interval = int(storage.get("persistence_sync_interval_seconds", 300))
            store.start_sync_worker(redis, registry, interval_seconds=interval)

        # Start heartbeat monitor if paid-tier wired one up
        heartbeat_task = None
        if paid_state and paid_state.get("heartbeat_monitor"):
            import asyncio
            heartbeat_task = asyncio.create_task(
                paid_state["heartbeat_monitor"].start(),
                name="ab0t_quota_heartbeat",
            )

        # Publish QuotaContext on app.state for route handlers to use
        ctx = QuotaContext(engine=engine, registry=registry, redis=redis, store=store)
        _app.state.quota = ctx

        # Auto-publish tier catalog to billing so cross-service admin views
        # (`/billing/{org}/tier/limits`) reflect the consumer's actual limits
        # instead of library defaults. Best-effort.
        service_name = _resolve_service_name(config, registry)
        if service_name and enable_paid:
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

        try:
            if existing_lifespan is not None:
                async with existing_lifespan(_app):
                    yield
            else:
                yield
        finally:
            # Teardown: stop heartbeat, close store (also stops snapshot worker), close Redis
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

    mesh_key = os.getenv("AB0T_MESH_API_KEY", "")
    if not mesh_key:
        logger.error("bridge mode requires AB0T_MESH_API_KEY — quota ops will fail-open")

    service_name = (
        os.getenv("AB0T_SERVICE_NAME")
        or config.get("service_name")
        or ""
    )
    if not service_name:
        logger.error("bridge mode requires service_name (config.service_name or "
                     "AB0T_SERVICE_NAME env) — quota ops will fail-open")

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
        emitter = LifecycleEmitter(
            engine=engine,
            cost_resource_key=cost_resource_key,
        )
        app.state.quota_emitter = emitter

        # Heartbeat monitor — emits synthetic stop events for stale resources
        try:
            from .billing.heartbeat import HeartbeatMonitor
            state["heartbeat_monitor"] = HeartbeatMonitor(redis=redis, emitter=emitter)
        except Exception as e:
            logger.warning("heartbeat monitor init failed: %s", e)

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
        )
        app.include_router(router)
        logger.info("paid-tier proxy router mounted at prefix=%s", route_prefix)
    except Exception as e:
        logger.warning("paid-tier router mount failed: %s", e)

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
