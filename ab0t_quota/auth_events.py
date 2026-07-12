"""Auth-event webhook receiver — pluggable handler registry.

Drop-in pattern. Consumers register handlers; the lib mounts the webhook
endpoint, verifies the HMAC, and dispatches to every handler registered
for the matching event_type.

Quick start (in your consumer's app/quota.py or similar):

    from ab0t_quota.auth_events import on_auth_event, register_handler

    # Decorator style — preferred for app code:
    @on_auth_event("auth.user.registered")
    async def grant_initial_credit(event):
        user_id = event["data"]["user_id"]
        org_id  = event["data"]["org_id"]
        ...

    # Function style — preferred for dynamic / conditional registration:
    register_handler("auth.user.login", track_last_seen)

The lib auto-registers a default `grant_initial_credit` handler when
`setup_quota(enable_paid=True)` runs. Consumers can unregister or shadow
it with their own.

How it works end-to-end:
  1. Consumer's module is imported at app startup → decorators run →
     handlers land in the module-level _HANDLERS registry.
  2. setup_quota() mounts POST /api/quotas/_webhooks/auth on the app.
  3. setup_quota() lifespan calls subscribe_on_startup() which looks at
     _HANDLERS.keys() and registers a webhook subscription with auth for
     exactly those event types.
  4. Auth fires events → POSTs to our endpoint → we verify HMAC → we
     dispatch to every handler registered for that event_type.

Handler signature:
    async def handler(event: dict) -> None
        # event = {"event_type": "...", "data": {...}, ...}
        # Anything raised is logged but does not bubble out to auth.
        # Auth gets a 200 as long as HMAC verifies and event_type is known.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
from typing import Any, Awaitable, Callable, Optional

import httpx
from fastapi import APIRouter, Header, HTTPException, Request

logger = logging.getLogger(__name__)

WEBHOOK_PATH = "/_webhooks/auth"
SUBSCRIPTION_NAME = "ab0t-quota-credit-grant"

# ---------------------------------------------------------------------------
# Handler registry — module-level singleton
# ---------------------------------------------------------------------------

Handler = Callable[[dict], Awaitable[None]]
_HANDLERS: dict[str, list[Handler]] = {}

# Shared fallback ledger store (QC-05). When a consumer mounts make_router with
# no ledger_store, dispatch must NOT mint a fresh InMemoryLedgerStore per call
# (which makes delivery dedup a silent no-op). It gets ONE process-wide store and
# a loud one-time warning instead.
_fallback_ledger_store: Any = None
_fallback_ledger_warned: bool = False


def _get_fallback_ledger_store() -> Any:
    global _fallback_ledger_store, _fallback_ledger_warned
    from .handler_ledger import InMemoryLedgerStore
    if _fallback_ledger_store is None:
        _fallback_ledger_store = InMemoryLedgerStore()
    if not _fallback_ledger_warned:
        logger.warning(
            "auth-event dispatch has NO persistent ledger store — falling back to a "
            "SHARED in-process InMemoryLedgerStore. Delivery dedup / retry / replay "
            "work within this process only and are LOST on restart or across replicas. "
            "Pass ledger_store=... (setup_quota wires one automatically) to fix. (QC-05)"
        )
        _fallback_ledger_warned = True
    return _fallback_ledger_store


def _reset_fallback_ledger_store() -> None:
    """Test hook: drop the shared fallback so a test starts from a clean slate."""
    global _fallback_ledger_store, _fallback_ledger_warned
    _fallback_ledger_store = None
    _fallback_ledger_warned = False


def register_handler(event_type: str, handler: Handler) -> Handler:
    """Register a coroutine handler for an auth event type.

    Idempotent: registering the same handler twice is a no-op (deduped
    by identity). Returns the handler so it can be used inline.
    """
    _HANDLERS.setdefault(event_type, [])
    if handler not in _HANDLERS[event_type]:
        _HANDLERS[event_type].append(handler)
    return handler


def on_auth_event(event_type: str) -> Callable[[Handler], Handler]:
    """Decorator form of register_handler.

        @on_auth_event("auth.user.registered")
        async def grant_initial_credit(event): ...
    """
    def _decorator(fn: Handler) -> Handler:
        return register_handler(event_type, fn)
    return _decorator


def unregister_handler(event_type: str, handler: Handler) -> bool:
    """Remove a handler. Returns True if it was registered."""
    handlers = _HANDLERS.get(event_type, [])
    if handler in handlers:
        handlers.remove(handler)
        return True
    return False


def registered_event_types() -> list[str]:
    """Event types that have at least one handler. Used by auto-subscribe."""
    return [et for et, hs in _HANDLERS.items() if hs]


def clear_handlers() -> None:
    """Test helper: drop all registrations. Don't call in production."""
    _HANDLERS.clear()


# ---------------------------------------------------------------------------
# HMAC verify
# ---------------------------------------------------------------------------

def verify_hmac(body: bytes, signature: Optional[str], secret: str) -> bool:
    """Constant-time HMAC-SHA256 verify. Auth signs with the secret set
    at subscription-create time. Accepts `sha256=<hex>` or `<hex>`."""
    if not signature or not secret:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    sig = signature.split("=", 1)[1] if "=" in signature else signature
    return hmac.compare_digest(expected, sig)


# ---------------------------------------------------------------------------
# Webhook receiver
# ---------------------------------------------------------------------------

def make_router(*, webhook_secret: str, ledger_store: Any = None) -> APIRouter:
    """Build the webhook receiver router. Mounted by setup_quota under
    the consumer's `/api/quotas` prefix.

    Behavior:
      - 401 if HMAC missing/invalid.
      - 400 if body isn't JSON.
      - 200 with `{"status": "ignored"}` if event_type has no handlers.
      - 200 with `{"status": "ok", "ran": N}` after dispatching.
      - Handler exceptions are logged but never bubble out — auth needs
        a 200 to mark the event delivered, otherwise it'll retry forever.

    Handlers decorated with `@idempotent` get delivery dedup + ledger
    persistence + auto-retry. Plain handlers run as before.
    """
    router = APIRouter()

    @router.post(WEBHOOK_PATH, include_in_schema=False)
    async def on_auth_webhook(
        request: Request,
        x_event_signature: Optional[str] = Header(None),
        x_webhook_signature: Optional[str] = Header(None),  # legacy publisher
    ):
        body = await request.body()
        sig = x_event_signature or x_webhook_signature
        if not verify_hmac(body, sig, webhook_secret):
            raise HTTPException(status_code=401, detail="invalid signature")

        try:
            payload = json.loads(body)
        except Exception:
            raise HTTPException(status_code=400, detail="invalid json")

        event_type = payload.get("event_type") or payload.get("type") or ""
        handlers = _HANDLERS.get(event_type, [])
        if not handlers:
            return {"status": "ignored", "event_type": event_type}

        ran = 0
        for h in handlers:
            try:
                await _dispatch_handler(h, payload, ledger_store)
                ran += 1
            except Exception as e:
                logger.warning("auth-event handler %s for %s failed: %s",
                               getattr(h, "__name__", "?"), event_type, e)
        return {"status": "ok", "ran": ran, "event_type": event_type}

    return router


async def _dispatch_handler(handler, event: dict, ledger_store: Any) -> None:
    """Dispatch one handler. Wraps with @idempotent machinery if applicable;
    otherwise calls the handler directly (v0.2.6 behavior).
    """
    from .handler_ledger import (
        is_idempotent_handler, idempotent_config, HandlerContext,
        SkipOutcome, SuccessOutcome, LedgerStatus, InMemoryLedgerStore,
    )

    if not is_idempotent_handler(handler):
        # Plain handler — call directly (v0.2.6 compatibility)
        await handler(event)
        return

    cfg = idempotent_config(handler)
    # QC-05: never mint a per-dispatch InMemoryLedgerStore — that makes delivery
    # dedup a silent no-op. Use the shared, loudly-warned fallback instead.
    store = ledger_store or _get_fallback_ledger_store()
    handler_name = cfg["handler_name"]
    event_id = event.get("event_id") or event.get("id") or _content_hash(event)
    event_type = event.get("event_type") or event.get("type") or ""
    user_id = (event.get("data") or {}).get("user_id") or event.get("user_id")
    org_id = (event.get("data") or {}).get("org_id") or event.get("org_id")

    # Delivery dedup — check if we've already processed this event
    attempt = await store.record_attempt(
        handler_name=handler_name, event_id=event_id, event_type=event_type,
        event_payload=event, user_id=user_id, org_id=org_id,
        lease_seconds=cfg.get("lease_seconds", 60),
    )
    if not attempt.proceed:
        logger.info("handler %s already processed event %s (status=%s) — skipping",
                    handler_name, event_id, attempt.cached_row.status.value if attempt.cached_row else "?")
        return

    # Build context with the business dedup key
    dedup_key = None
    key_fn = cfg.get("key_fn")
    if key_fn is not None:
        try:
            dedup_key = key_fn(event)
        except Exception as e:
            logger.warning("handler %s: key function raised %s; running without business dedup",
                           handler_name, e)
    ctx = HandlerContext(handler_name, event_id, event_type, event, store, _dedup_key=dedup_key)

    # Run with retry
    await _run_with_retry(handler, event, ctx, cfg, store, handler_name, event_id)


async def _run_with_retry(handler, event, ctx, cfg, store, handler_name, event_id) -> None:
    """Execute handler with retry policy. Records final outcome to ledger."""
    from .handler_ledger import SkipOutcome, SuccessOutcome, LedgerStatus

    retry = cfg.get("retry")
    max_attempts = retry["attempts"] if retry else 1
    initial = retry["initial_seconds"] if retry else 1.0
    max_delay = retry["max_seconds"] if retry else 30.0
    last_error: Optional[str] = None

    for attempt_num in range(1, max_attempts + 1):
        try:
            outcome = await handler(event, ctx)
            if isinstance(outcome, SkipOutcome):
                await store.record_outcome(
                    handler_name=handler_name, event_id=event_id,
                    status=LedgerStatus.SKIPPED, reason=outcome.reason,
                    attempts=attempt_num,
                )
            elif isinstance(outcome, SuccessOutcome):
                await store.record_outcome(
                    handler_name=handler_name, event_id=event_id,
                    status=LedgerStatus.SUCCESS, side_effect_id=outcome.side_effect_id,
                    attempts=attempt_num,
                )
            else:
                # Handler returned nothing → treat as success without side_effect_id
                await store.record_outcome(
                    handler_name=handler_name, event_id=event_id,
                    status=LedgerStatus.SUCCESS, attempts=attempt_num,
                )
            return  # success, exit retry loop
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            logger.warning("handler %s attempt %d/%d failed: %s",
                           handler_name, attempt_num, max_attempts, last_error)
            if attempt_num < max_attempts:
                delay = min(initial * (2 ** (attempt_num - 1)), max_delay)
                await asyncio.sleep(delay)
                # Re-record_attempt to bump the lease (in case of long retries)
                # but DON'T short-circuit on cached row — we're inside the retry loop
                continue

    # All attempts failed
    await store.record_outcome(
        handler_name=handler_name, event_id=event_id,
        status=LedgerStatus.FAILED_PERMANENT, error=last_error,
        attempts=max_attempts,
    )


async def redispatch_stale_row(row, ledger_store) -> None:
    """D1 / QC-02 — re-drive a RECLAIMED stale-lease row (a handler that crashed
    mid-delivery). The StaleLeaseSweeper's ``_drain_stale`` has ALREADY atomically
    re-claimed the lease (a fresh ``record_attempt``), so this does NOT record a
    new attempt — it reconstructs the handler context and re-runs the business
    handler, recording the final outcome via ``_run_with_retry``.

    Re-running is SAFE by construction: an ``@idempotent`` handler's business dedup
    (``already_done``/``mark_done`` via ``key_fn``) makes a completed grant a no-op
    on replay, so a crash BEFORE the grant recovers it and a crash AFTER (but before
    the outcome was recorded) does not double-grant. If no handler is registered for
    the row, it is logged LOUD and left in_progress (the operator must register it —
    silently dropping it would re-hide the guarantee)."""
    from .handler_ledger import is_idempotent_handler, idempotent_config, HandlerContext
    event = row.event_payload or {}
    for h in _HANDLERS.get(row.event_type, []):
        if not is_idempotent_handler(h):
            continue
        cfg = idempotent_config(h)
        if cfg.get("handler_name") != row.handler_name:
            continue
        dedup_key = None
        key_fn = cfg.get("key_fn")
        if key_fn is not None:
            try:
                dedup_key = key_fn(event)
            except Exception as e:
                logger.warning("stale redispatch %s: key fn raised %s; running without "
                               "business dedup", row.handler_name, e)
        ctx = HandlerContext(row.handler_name, row.event_id, row.event_type, event,
                             ledger_store, _dedup_key=dedup_key)
        await _run_with_retry(h, event, ctx, cfg, ledger_store, row.handler_name, row.event_id)
        return
    logger.error("stale_lease_redispatch: NO registered handler '%s' for event_type '%s' — "
                 "cannot recover stranded row event_id=%s (it stays in_progress). Register "
                 "the handler, or the grant stays stranded (QC-02).",
                 row.handler_name, row.event_type, row.event_id)


def _content_hash(event: dict) -> str:
    """Stable hash of event payload for events without an event_id."""
    return hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Auto-subscribe — register THIS service's webhook with auth at startup
# ---------------------------------------------------------------------------

async def _resolve_org_id_from_slug(auth_url: str, slug: str) -> Optional[str]:
    """Auth's hosted login HTML embeds orgId in window.__AUTH_CONFIG__.
    Public, no auth needed. Returns None on any failure."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{auth_url.rstrip('/')}/login/{slug}")
            if r.status_code != 200:
                return None
            m = re.search(r'"orgId"\s*:\s*"([0-9a-f-]{36})"', r.text)
            return m.group(1) if m else None
    except Exception:
        return None


async def subscribe_on_startup(
    *,
    auth_url: Optional[str] = None,
    admin_token: Optional[str] = None,
    public_url: Optional[str] = None,
    secret: Optional[str] = None,
    event_types: Optional[list[str]] = None,
    watch_org_slug: Optional[str] = None,
    watch_org_id: Optional[str] = None,
    name: str = SUBSCRIPTION_NAME,
) -> Optional[str]:
    """Register THIS service's webhook receiver with auth, idempotently.

    All inputs default to env vars when not given:
      - auth_url      ← AB0T_AUTH_AUTH_URL
      - admin_token   ← AB0T_AUTH_ADMIN_TOKEN
      - public_url    ← AB0T_AUTH_WEBHOOK_PUBLIC_URL
      - secret        ← AB0T_AUTH_WEBHOOK_SECRET
      - watch_org_slug ← AB0T_AUTH_WATCH_ORG_SLUG, then AB0T_AUTH_ORG_SLUG

    `event_types` defaults to `registered_event_types()` — the lib
    subscribes to exactly the event types that have handlers. Returns
    None (no-op) if no handlers are registered.

    Failures (auth unreachable, missing env, bad token) log a warning
    and return None — they MUST NOT block app startup. Subscribe re-runs
    on every container start, so a fix sticks on the next deploy.
    """
    auth_url = auth_url if auth_url is not None else os.getenv("AB0T_AUTH_AUTH_URL", "")
    admin_token = admin_token if admin_token is not None else os.getenv("AB0T_AUTH_ADMIN_TOKEN", "")
    public_url = public_url if public_url is not None else os.getenv("AB0T_AUTH_WEBHOOK_PUBLIC_URL", "")
    secret = secret if secret is not None else os.getenv("AB0T_AUTH_WEBHOOK_SECRET", "")
    if watch_org_slug is None:
        watch_org_slug = os.getenv("AB0T_AUTH_WATCH_ORG_SLUG", "") or os.getenv("AB0T_AUTH_ORG_SLUG", "")

    if event_types is None:
        event_types = registered_event_types()

    if not event_types:
        logger.info("auth-event auto-subscribe skipped: no handlers registered")
        return None

    if not (auth_url and admin_token and public_url and secret):
        logger.info("auth-event auto-subscribe skipped: missing one of "
                    "AB0T_AUTH_AUTH_URL, AB0T_AUTH_ADMIN_TOKEN, "
                    "AB0T_AUTH_WEBHOOK_PUBLIC_URL, AB0T_AUTH_WEBHOOK_SECRET")
        return None

    endpoint = f"{public_url.rstrip('/')}/api/quotas{WEBHOOK_PATH}"
    headers = {"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"}

    org_id = watch_org_id
    if watch_org_slug and not org_id:
        org_id = await _resolve_org_id_from_slug(auth_url, watch_org_slug)
        if not org_id:
            logger.warning("auth-event auto-subscribe: could not resolve slug=%s; subscribing without org filter",
                           watch_org_slug)

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Idempotency: GET first, look for matching endpoint
            r = await client.get(f"{auth_url.rstrip('/')}/events/subscriptions", headers=headers)
            if r.status_code == 200:
                payload = r.json()
                items = payload if isinstance(payload, list) else (payload or {}).get("items") or []
                for sub in items:
                    if sub.get("endpoint") == endpoint:
                        sid = sub.get("subscription_id") or sub.get("id")
                        logger.info("auth-event auto-subscribe: already subscribed (id=%s)", sid)
                        return sid
            elif r.status_code in (401, 403):
                logger.warning("auth-event auto-subscribe: admin token rejected (HTTP %s); "
                               "no subscription created", r.status_code)
                return None

            # Create
            body: dict = {
                "name": name,
                "event_types": event_types,
                "endpoint": endpoint,
                "secret": secret,
            }
            if org_id:
                body["filters"] = [{"field": "org_id", "value": org_id}]

            r = await client.post(f"{auth_url.rstrip('/')}/events/subscriptions",
                                  headers=headers, json=body)
            if r.status_code in (200, 201):
                sub = r.json()
                sid = sub.get("subscription_id") or sub.get("id")
                logger.info("auth-event auto-subscribe: created subscription "
                            "(id=%s, events=%s, endpoint=%s)", sid, event_types, endpoint)
                return sid
            logger.warning("auth-event auto-subscribe: create failed HTTP %s body=%s",
                           r.status_code, r.text[:200])
    except Exception as e:
        logger.warning("auth-event auto-subscribe: error %s", e)
    return None


# ---------------------------------------------------------------------------
# Reusable primitives — workspace resolution and credit-grant
# ---------------------------------------------------------------------------
# Consumers writing their own handlers can import these directly:
#
#   from ab0t_quota.auth_events import on_auth_event, PinStore, resolve_billing_org
#
#   pin_store = PinStore(table="my_table", ddb=my_ddb)
#
#   @on_auth_event("auth.user.registered")
#   async def my_handler(event):
#       data = event["data"]
#       org = await resolve_billing_org(
#           data["user_id"], fallback_org_id=data["org_id"],
#           auth_url="...", mesh_api_key="...", pin_store=pin_store,
#       )
#       # ...do something with `org`
#
# OR skip the plugin system entirely and call these from a consumer-owned
# webhook receiver. They're standalone.


async def resolve_billing_org(
    user_id: str,
    fallback_org_id: str,
    *,
    auth_url: str,
    mesh_api_key: str,
    pin_store: Any,
) -> str:
    """Return the org to bill against. Sticky: first call writes a pin to
    DDB; subsequent calls return the pinned value.

    Resolution rule for first call: prefer the user's owner-role org
    (workspace if workspace-per-user is enabled; first such org otherwise).
    Falls back to the event's org_id if no owner-role org found.
    """
    pinned = await pin_store.get(user_id)
    if pinned:
        return pinned

    resolved = fallback_org_id
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{auth_url.rstrip('/')}/users/{user_id}/organizations",
                headers={"X-API-Key": mesh_api_key},
            )
            if r.status_code == 200:
                orgs = r.json() or []
                owner_orgs = [o for o in orgs if o.get("role") == "owner"]
                if owner_orgs:
                    resolved = owner_orgs[0]["id"]
    except Exception as e:
        logger.warning("resolve_billing_org auth lookup failed user=%s err=%s; using fallback",
                       user_id, e)

    await pin_store.set(user_id, resolved, source="auto")
    return resolved


def compose_credit_dedup_key(
    policy: str,
    *,
    user_id: str,
    org_id: str,
    tier_id: str,
    prefix: str = "credit_granted",
) -> str:
    """Build a business-dedup key for credit grants per the policy.

    Used by the lib's default handler and exposed for consumer custom
    handlers that want to share the same dedup semantics. Policies:

      per_user_per_tier (default) — anti-farming, one credit per (user, tier)
      per_org_per_tier            — B2B "one credit per (org, tier)"
      per_user_global             — one human, one credit, ever
      per_org_global              — one org, one credit, ever
    """
    if policy == "per_org_per_tier":
        return f"{prefix}:org:{org_id}:{tier_id}"
    if policy == "per_user_global":
        return f"{prefix}:user:{user_id}"
    if policy == "per_org_global":
        return f"{prefix}:org:{org_id}"
    # default + explicit per_user_per_tier
    return f"{prefix}:user:{user_id}:{tier_id}"


async def grant_initial_credit_for_user(
    user_id: str,
    org_id: str,
    *,
    initial_credits: dict[str, float],
    tier_provider: Any,
    redis: Any,
    billing_url: str,
    billing_api_key: str,
    tier_registry: Optional[dict[str, Any]] = None,
    ledger_store: Any = None,
) -> None:
    """Grant the configured signup credit for the user's tier, idempotently.

    Resolution order (per ticket 20260516_paid_plan_balance_model_gap, D5):
      1. If `tier_registry` is provided AND the tier has a `credit_grant`
         with trigger=signup configured, use THAT (the new schema).
      2. Otherwise, fall back to the legacy `initial_credits[tier_id]`
         dict (the pre-schema shape).

    This makes the handler back-compat across the rollout:
      - Pre-Phase-1 callers passing only `initial_credits` keep working.
      - Post-Phase-1 callers passing `tier_registry` honor the new
        TierConfig.credit_grant policy (including destination + lifecycle).

    The `initial_credits` dict path always lands credit in `credit_balance`
    (legacy behavior, preserves the existing free-tier signup grant). The
    `tier_registry` path uses whatever `destination` the tier declares —
    typically `credit_balance` for signup grants, but consumers may
    declare otherwise.

    Safe to call from a handler OR directly from a consumer-owned receiver.
    """
    try:
        tier_id = await tier_provider.get_tier(org_id)
    except Exception as e:
        logger.warning("credit grant: tier lookup failed user=%s org=%s err=%s",
                       user_id, org_id, e)
        return

    # New path: read tier.credit_grant if available + applicable.
    tier_grant = None
    if tier_registry is not None:
        tier = tier_registry.get(tier_id)
        if tier is not None:
            grant = getattr(tier, "credit_grant", None)
            if grant is not None:
                trigger_val = getattr(grant.trigger, "value", grant.trigger)
                if trigger_val == "signup":
                    tier_grant = grant

    if tier_grant is not None:
        amount = float(tier_grant.amount_per_period)
        destination = getattr(tier_grant.destination, "value", tier_grant.destination)
    else:
        # Legacy path: dict lookup, always credit_balance.
        amount = initial_credits.get(tier_id)
        if not amount:
            return
        destination = "credit_balance"

    if not amount:
        return

    # v0.5.2 — dedup key composition honors tier.credit_grant.dedup field
    # if the new schema is in use. Legacy path keeps per_user_per_tier.
    dedup_policy = "per_user_per_tier"
    if tier_grant is not None:
        dedup_policy = getattr(tier_grant, "dedup", "per_user_per_tier")
    flag_key = compose_credit_dedup_key(dedup_policy, user_id=user_id, org_id=org_id, tier_id=tier_id)
    try:
        if await redis.get(flag_key):
            return
    except Exception:
        pass  # rely on billing's own idempotency if redis check fails

    # BACKWARD-COMPAT (v0.5.1 and earlier): billing's idempotency_key was
    # always `user:{user_id}:initial_credit:{tier_id}`. Keep that shape for
    # the default policy so in-flight grants stay aligned with billing's
    # idempotency records. Only diverge when a non-default policy is set.
    if dedup_policy == "per_user_per_tier":
        idempotency_key = f"user:{user_id}:initial_credit:{tier_id}"
    elif dedup_policy == "per_org_per_tier":
        idempotency_key = f"org:{org_id}:initial_credit:{tier_id}"
    elif dedup_policy == "per_user_global":
        idempotency_key = f"user:{user_id}:initial_credit"
    elif dedup_policy == "per_org_global":
        idempotency_key = f"org:{org_id}:initial_credit"
    else:
        idempotency_key = f"user:{user_id}:initial_credit:{tier_id}"  # safe fallback

    # Durable dedup via the @idempotent ledger (D-13 / R2). The Redis flag above
    # is a fast path only; it is volatile (flush / TTL / failover). The ledger's
    # business-dedup rows have NO TTL, so a redelivery at ANY latency — past the
    # 30-day flag, past billing's 24h window, after a Redis loss — is a no-op
    # here instead of a double credit. Uses the shared fallback store when the
    # caller supplies none (P1.8), so the durable check always runs.
    store = ledger_store or _get_fallback_ledger_store()
    try:
        if await store.already_done(dedup_key=idempotency_key):
            logger.info("credit grant already recorded (durable ledger) user=%s org=%s key=%s",
                        user_id, org_id, idempotency_key)
            return
    except Exception as e:
        # A ledger read error must not fail OPEN into a double grant silently;
        # log and fall through — billing's own idempotency + the Redis flag are
        # the remaining backstops.
        logger.warning("credit grant durable dedup check failed user=%s err=%s", user_id, e)

    # Choose the billing-service endpoint based on destination. The legacy
    # `/promotional-credit` endpoint always writes to credit_balance; the
    # new `/apply-credit-grant` endpoint honors the destination + lifecycle.
    if tier_grant is None:
        # Legacy path: existing /promotional-credit endpoint.
        url = f"{billing_url.rstrip('/')}/billing/{org_id}/promotional-credit"
        body = {
            "amount": amount,
            "reason": f"initial_credit_{tier_id}",
            "idempotency_key": idempotency_key,
        }
    else:
        # New path: route through /apply-credit-grant with the declared
        # destination + lifecycle. For signup grants the typical lifecycle
        # is `persistent` (one-shot, never expires); the validator on
        # TierConfig.credit_grant prevents misconfiguration.
        url = f"{billing_url.rstrip('/')}/billing/{org_id}/apply-credit-grant"
        lifecycle_val = getattr(tier_grant.lifecycle, "value", tier_grant.lifecycle)
        body = {
            "amount": amount,
            "destination": destination,
            "lifecycle": lifecycle_val,
            "idempotency_key": idempotency_key,
            "reason": f"signup_grant_{tier_id}",
        }
        if lifecycle_val == "rollover_capped" and tier_grant.rollover_max_periods is not None:
            body["rollover_max"] = float(
                tier_grant.rollover_max_periods * tier_grant.amount_per_period
            )

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                headers={"X-API-Key": billing_api_key, "Content-Type": "application/json"},
                json=body,
            )
            if resp.status_code in (200, 201):
                # Mark granted. For "one credit EVER" (*_global) policies the
                # guard must be DURABLE — a 30-day TTL would let a later
                # redelivery re-grant after the flag expires (QC-04 / D-13).
                try:
                    if dedup_policy in ("per_user_global", "per_org_global"):
                        await redis.set(flag_key, "1")            # no expiry
                    else:
                        await redis.set(flag_key, "1", ex=86400 * 30)
                except Exception:
                    pass
                # Durable, no-TTL record in the ledger (D-13 / R2) so a later
                # redelivery is deduped even after the Redis flag is gone.
                try:
                    await store.mark_done(
                        dedup_key=idempotency_key,
                        source_handler="grant_initial_credit",
                        source_event_id=idempotency_key,
                    )
                except Exception as e:
                    logger.warning("credit grant durable dedup mark failed user=%s err=%s", user_id, e)
                logger.info(
                    "credit granted user=%s org=%s tier=%s amount=%s destination=%s",
                    user_id, org_id, tier_id, amount, destination,
                )
            elif resp.status_code == 400:
                # D-13 / QC-04: a 400 is a validation / permanent-request failure,
                # NOT a success. Do NOT set the dedup flag — doing so permanently
                # suppresses a corrected redelivery. Log loudly; this is a
                # request/config bug to fix, then redeliver.
                logger.error(
                    "credit grant REJECTED (400) user=%s org=%s tier=%s body=%s — "
                    "grant NOT recorded; fix the request/config and redeliver",
                    user_id, org_id, tier_id, resp.text[:200],
                )
            else:
                logger.warning("credit grant unexpected status=%s body=%s",
                               resp.status_code, resp.text[:200])
    except Exception as e:
        logger.warning("credit grant failed user=%s err=%s", user_id, e)


class PinStore:
    """DDB-backed user_id -> billing org_id pinning. Sticky on first set.

    Lives in the existing QUOTA_STATE_TABLE. Schema:
      PK: USER#{user_id}
      SK: BILLING_ORG
      attrs: org_id, set_at, source ("auto" | "operator")

    Conditional write: source='auto' will NOT overwrite an existing
    'operator' value (operator override wins).
    """

    def __init__(self, table_name: str, ddb_client: Any):
        self.table = table_name
        self.ddb = ddb_client

    async def get(self, user_id: str) -> Optional[str]:
        try:
            res = await self.ddb.get_item(
                TableName=self.table,
                Key={"PK": {"S": f"USER#{user_id}"}, "SK": {"S": "BILLING_ORG"}},
            )
            item = res.get("Item")
            return item["org_id"]["S"] if item else None
        except Exception as e:
            logger.warning("PinStore.get failed user=%s err=%s", user_id, e)
            return None

    async def set(self, user_id: str, org_id: str, *, source: str = "auto") -> None:
        from datetime import datetime, timezone
        try:
            kwargs: dict = dict(
                TableName=self.table,
                Item={
                    "PK": {"S": f"USER#{user_id}"},
                    "SK": {"S": "BILLING_ORG"},
                    "org_id": {"S": org_id},
                    "set_at": {"S": datetime.now(timezone.utc).isoformat()},
                    "source": {"S": source},
                },
            )
            if source == "auto":
                kwargs["ConditionExpression"] = "attribute_not_exists(org_id) OR #s = :auto"
                kwargs["ExpressionAttributeNames"] = {"#s": "source"}
                kwargs["ExpressionAttributeValues"] = {":auto": {"S": "auto"}}
            await self.ddb.put_item(**kwargs)
        except Exception as e:
            if "ConditionalCheckFailed" not in str(e):
                logger.warning("PinStore.set failed user=%s err=%s", user_id, e)


# ---------------------------------------------------------------------------
# Built-in default handler factory
# ---------------------------------------------------------------------------
# Registered by setup_quota(enable_paid=True) using the primitives above.
# Consumer can shadow via unregister_handler + their own @on_auth_event.

def _build_default_credit_grant_handler(
    *,
    initial_credits: dict[str, float],
    tier_provider: Any,
    redis: Any,
    billing_url: str,
    billing_api_key: str,
    auth_url: str = "",
    mesh_api_key: str = "",
    pin_store: Optional[PinStore] = None,
    tier_registry: Optional[dict[str, Any]] = None,
) -> Handler:
    """Returns a handler that resolves billing org (if pin_store provided)
    then grants initial_credit. Composes the two primitives above.

    When `tier_registry` is provided, the underlying call honors
    `TierConfig.credit_grant` (new schema, can route to any destination
    bucket). Without it, falls back to the legacy `initial_credits` dict
    (always lands in credit_balance).

    T11 in ticket 20260516_auto_credit_invoice_paid_wiring threads
    tier_registry through so setup_quota(enable_paid=True) can auto-register
    this handler with the consumer's loaded TierConfig dict — no custom
    consumer code required for signup credit grants.
    """

    async def grant_initial_credit(event: dict) -> None:
        data = event.get("data") or event
        user_id = data.get("user_id")
        event_org_id = data.get("org_id")
        if not user_id or not event_org_id:
            return

        # If pin_store is available, resolve to user's primary billable org
        # (workspace if workspace-per-user mode). Otherwise use event org.
        if pin_store is not None and auth_url and mesh_api_key:
            org_id = await resolve_billing_org(
                user_id, fallback_org_id=event_org_id,
                auth_url=auth_url, mesh_api_key=mesh_api_key, pin_store=pin_store,
            )
        else:
            org_id = event_org_id

        await grant_initial_credit_for_user(
            user_id, org_id,
            initial_credits=initial_credits,
            tier_provider=tier_provider,
            redis=redis,
            billing_url=billing_url,
            billing_api_key=billing_api_key,
            tier_registry=tier_registry,
        )

    return grant_initial_credit
