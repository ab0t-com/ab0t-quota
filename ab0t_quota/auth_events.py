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

def make_router(*, webhook_secret: str) -> APIRouter:
    """Build the webhook receiver router. Mounted by setup_quota under
    the consumer's `/api/quotas` prefix.

    Behavior:
      - 401 if HMAC missing/invalid.
      - 400 if body isn't JSON.
      - 200 with `{"status": "ignored"}` if event_type has no handlers.
      - 200 with `{"status": "ok", "ran": N}` after dispatching.
      - Handler exceptions are logged but never bubble out — auth needs
        a 200 to mark the event delivered, otherwise it'll retry forever.
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
                await h(payload)
                ran += 1
            except Exception as e:
                logger.warning("auth-event handler %s for %s failed: %s",
                               getattr(h, "__name__", "?"), event_type, e)
        return {"status": "ok", "ran": ran, "event_type": event_type}

    return router


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


async def grant_initial_credit_for_user(
    user_id: str,
    org_id: str,
    *,
    initial_credits: dict[str, float],
    tier_provider: Any,
    redis: Any,
    billing_url: str,
    billing_api_key: str,
) -> None:
    """Grant the configured initial_credit for the user's tier, idempotently.
    Safe to call from a handler OR directly from a consumer-owned receiver."""
    try:
        tier_id = await tier_provider.get_tier(org_id)
    except Exception as e:
        logger.warning("credit grant: tier lookup failed user=%s org=%s err=%s",
                       user_id, org_id, e)
        return

    amount = initial_credits.get(tier_id)
    if not amount:
        return

    flag_key = f"credit_granted:user:{user_id}:{tier_id}"
    try:
        if await redis.get(flag_key):
            return
    except Exception:
        pass  # rely on billing's own idempotency if redis check fails

    idempotency_key = f"user:{user_id}:initial_credit:{tier_id}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{billing_url.rstrip('/')}/billing/{org_id}/promotional-credit",
                headers={"X-API-Key": billing_api_key, "Content-Type": "application/json"},
                json={"amount": amount,
                      "reason": f"initial_credit_{tier_id}",
                      "idempotency_key": idempotency_key},
            )
            if resp.status_code in (200, 400):
                try:
                    await redis.set(flag_key, "1", ex=86400 * 30)
                except Exception:
                    pass
                logger.info("credit granted user=%s org=%s tier=%s amount=%s",
                            user_id, org_id, tier_id, amount)
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
) -> Handler:
    """Returns a handler that resolves billing org (if pin_store provided)
    then grants initial_credit. Composes the two primitives above."""

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
        )

    return grant_initial_credit
