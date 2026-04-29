# Auth-event registry pattern

ab0t-quota provides a generic registry for handling events delivered by the
auth service's webhook system. The lib mounts the receiver, verifies the
HMAC, and dispatches to handlers **you** register. The lib has no opinion
on what those handlers do — every consumer plugs in their own logic.

This doc covers the full lifecycle: how the pattern works, how to use it,
which event types are available, what reusable primitives the lib ships,
and a cookbook of common use cases.

---

## Mental model

```
                ┌────────────────────────────────────────────┐
                │ auth.service.ab0t.com                      │
                │   /events/subscriptions  (webhook system)  │
                └───────────────┬────────────────────────────┘
                                │ HTTP POST (HMAC-signed)
                                │ on auth.user.registered etc.
                                ▼
            ┌─────────────────────────────────────────────────┐
            │ your service                                     │
            │  ┌───────────────────────────────────────────┐  │
            │  │ ab0t_quota.auth_events.make_router(...)   │  │
            │  │   POST /api/quotas/_webhooks/auth         │  │
            │  │   1. verify HMAC (X-Event-Signature)      │  │
            │  │   2. dispatch to all _HANDLERS[event_type]│  │
            │  └───────────────────────────────────────────┘  │
            │                       │                          │
            │                       ▼                          │
            │  ┌───────────────────────────────────────────┐  │
            │  │ YOUR handlers (in app/quota.py or wherever│  │
            │  │   @on_auth_event("auth.user.registered")  │  │
            │  │   async def my_handler(event): ...        │  │
            │  └───────────────────────────────────────────┘  │
            └─────────────────────────────────────────────────┘
```

Three pieces, two of them yours:

1. **Registry** (lib) — module-level dict mapping `event_type → [handlers]`.
   Populated at import time when your decorators run. Module-level singleton
   so it's shared across the process.
2. **Receiver** (lib) — the FastAPI route at `/api/quotas/_webhooks/auth`.
   Mounted by `setup_quota` when `AB0T_AUTH_WEBHOOK_SECRET` is set. Verifies
   HMAC, dispatches to all handlers for the matching event_type.
3. **Subscription with auth** (operator-or-lib) — tells auth where to POST.
   Either auto-registered at startup (lib reads env vars and hits auth's
   `/events/subscriptions`) or registered manually via the CLI.

---

## The two registration styles

Both produce the same result. Use whichever fits your code shape.

### Decorator (preferred for app code)

```python
from ab0t_quota.auth_events import on_auth_event

@on_auth_event("auth.user.registered")
async def grant_initial_credit(event):
    user_id = event["data"]["user_id"]
    org_id  = event["data"]["org_id"]
    # ...
```

### Function (preferred for dynamic / conditional registration)

```python
from ab0t_quota.auth_events import register_handler

if FEATURE_TRACK_LAST_SEEN:
    register_handler("auth.user.login", track_last_seen)

if BILLING_ENABLED:
    register_handler("auth.user.registered", grant_initial_credit)
    register_handler("org.created", initialize_billing_account)
```

Both are idempotent — registering the same callable twice is a no-op
(deduped by identity). Multiple distinct handlers for the same event are
all called in registration order.

---

## Where to put your handlers

Convention: a single file per service that imports the registrations.

For sandbox-platform that's `app/quota.py` (which is also where
`setup_quota()` is called — keeps all the lib wiring together):

```python
# sandbox-platform/app/quota.py
from ab0t_quota import setup_quota
from ab0t_quota.auth_events import on_auth_event

def bind_app(app):
    setup_quota(app, ...)            # mounts the lib's webhook receiver
    _register_auth_event_handlers()  # populates the registry

def _register_auth_event_handlers():
    @on_auth_event("auth.user.registered")
    async def grant_credit_on_signup(event):
        # sandbox-platform-specific logic here
        ...
```

Other services (billing, payment, integration, your future thing) put
their own registrations in their own equivalent file. Each consumer
decides which events to handle, and what to do.

---

## Handler signature

```python
async def handler(event: dict) -> None:
    # event = {
    #   "event_type": "auth.user.registered",
    #   "event_id":   "evt_xxxxxxxxxxx",
    #   "occurred_at":"2026-04-28T...",
    #   "data": { "user_id": "...", "org_id": "...", "email": "...", ... }
    # }
```

Rules:

- **Async only.** The receiver awaits each handler.
- **Don't raise unless you mean it.** Exceptions are caught, logged, and
  the receiver still returns 200 to auth (so auth marks the event delivered
  and doesn't retry forever). If you want auth to retry, you'd have to drop
  the receiver entirely and write your own.
- **Idempotent or it gets ugly.** Auth's webhook delivery has retries, plus
  you might receive both `user.registered` AND `user.login` for the same
  signup. Use a dedup key (Redis, DB unique constraint, idempotency-key on
  downstream calls).
- **Keep it fast.** Each handler runs inline before the receiver responds.
  Long work belongs in a background task fired from the handler.

---

## Available event types

From auth's `GET /events/types` (the source of truth). Common ones:

| Event type | When it fires | Useful for |
|---|---|---|
| `auth.user.registered` | New user completes signup | initial credits, welcome email, provision first resource |
| `auth.user.login` | User logs in | last-seen tracking, anomaly detection, session metrics |
| `auth.user.logout` | User logs out | session cleanup |
| `auth.token.refreshed` | Refresh-token grant succeeded | rolling token rotation, idle-timeout tracking |
| `auth.token.revoked` | Token explicitly revoked | invalidate caches, kick websockets |
| `auth.permission.granted` | Permission added to user | refresh perm caches, audit log |
| `auth.permission.revoked` | Permission removed | same |
| `org.created` | Organization created | provision billing account, initial config |
| `org.member.added` | User joined an org | seat-tracking, welcome to team |
| `org.member.removed` | User left an org | revoke per-org resources |

The full list is environment-dependent. Hit `GET <auth_url>/events/types`
on your auth instance to see what's actually available.

---

## Reusable primitives the lib ships

Importable from `ab0t_quota.auth_events`. Use them inside your handlers
or skip them if you have your own way:

### `resolve_billing_org(user_id, fallback_org_id, *, auth_url, mesh_api_key, pin_store) -> str`

Returns the org to bill against. First call resolves via auth (looks for
the user's owner-role org — workspace if workspace-per-user is enabled),
writes a sticky pin to DDB, and returns it. Subsequent calls return the
pinned value without contacting auth. Sticky-on-first-resolve pattern.

### `grant_initial_credit_for_user(user_id, org_id, *, initial_credits, tier_provider, redis, billing_url, billing_api_key) -> None`

Idempotent credit grant. Resolves the user's tier, looks up the
configured `initial_credit` amount, hits billing's
`/billing/{org}/promotional-credit` endpoint with an `idempotency_key`,
and sets a Redis dedup flag. Safe to call from a handler that fires on
multiple event types (login + register) or from a backfill script.

### `PinStore(table_name, ddb_client)`

DDB-backed `user_id → billing_org_id` storage. Schema:
`PK=USER#{user_id}, SK=BILLING_ORG, attrs={org_id, set_at, source}`.
Conditional write protects operator-set values from being clobbered by
the auto path. Lives in the existing `QUOTA_STATE_TABLE`.

### `subscribe_on_startup(...)` and `make_router(...)`

Mostly internal — `setup_quota()` calls these. You'd call them directly
only if you're not using `setup_quota` (rare).

---

## Cookbook

### 1. Grant initial credit on signup (sandbox-platform's actual handler)

```python
from ab0t_quota.auth_events import (
    on_auth_event, resolve_billing_org, grant_initial_credit_for_user, PinStore,
)

@on_auth_event("auth.user.registered")
async def grant_credit_on_signup(event):
    data = event["data"]
    user_id, event_org_id = data["user_id"], data["org_id"]

    pin_store = PinStore(os.getenv("QUOTA_STATE_TABLE", "ab0t_quota_state"), ddb_client)
    billing_org = await resolve_billing_org(
        user_id, fallback_org_id=event_org_id,
        auth_url=os.getenv("AB0T_AUTH_AUTH_URL", ""),
        mesh_api_key=os.getenv("AB0T_MESH_API_KEY", ""),
        pin_store=pin_store,
    )
    await grant_initial_credit_for_user(
        user_id, billing_org,
        initial_credits=load_initial_credits_from_quota_config(),
        tier_provider=engine._tier_provider,
        redis=engine.redis,
        billing_url=os.getenv("AB0T_MESH_BILLING_URL"),
        billing_api_key=os.getenv("AB0T_MESH_BILLING_API_KEY"),
    )
```

### 2. Track last-seen on login (analytics)

```python
@on_auth_event("auth.user.login")
async def update_last_seen(event):
    user_id = event["data"]["user_id"]
    await app.state.db.put_item(
        TableName="user_metadata",
        Item={"user_id": {"S": user_id},
              "last_seen": {"S": event["occurred_at"]}},
    )
```

### 3. Provision a billing account when an org is created

```python
@on_auth_event("org.created")
async def init_billing_account(event):
    org_id = event["data"]["org_id"]
    # Create a zero-balance account for the new org so the first
    # /balance call returns 200, not 404.
    await billing_client.ensure_account(org_id)
```

### 4. Invalidate caches when permissions change

```python
@on_auth_event("auth.permission.granted")
@on_auth_event("auth.permission.revoked")
async def bust_perm_cache(event):
    user_id = event["data"]["user_id"]
    await app.state.redis.delete(f"perms:user:{user_id}")
```

### 5. Welcome email on signup (delegated to background task)

```python
import asyncio

@on_auth_event("auth.user.registered")
async def schedule_welcome_email(event):
    # Don't block the webhook; fire and forget.
    asyncio.create_task(send_welcome_email(event["data"]["email"]))
```

### 6. Multiple handlers for one event — composes naturally

```python
@on_auth_event("auth.user.registered")
async def grant_credit(event): ...

@on_auth_event("auth.user.registered")
async def send_welcome_email(event): ...

@on_auth_event("auth.user.registered")
async def emit_analytics(event): ...
```

All three fire in registration order. If one raises, the others still run.

---

## Lifecycle: from container start to event delivery

1. **Process import.** Python imports your `quota.py` (or wherever you
   put the registrations). Decorators execute → handlers land in
   `_HANDLERS`. Order doesn't matter; only that they're registered before
   the app starts serving.
2. **`setup_quota(app)`** runs synchronously. If `AB0T_AUTH_WEBHOOK_SECRET`
   is set, it mounts `make_router(...)` at `/api/quotas/_webhooks/auth`.
3. **App lifespan starts.** If any handlers are registered AND the
   subscribe env vars are set (`AB0T_AUTH_ADMIN_TOKEN`,
   `AB0T_AUTH_WEBHOOK_PUBLIC_URL`), the lib calls `subscribe_on_startup()`
   in the background:
   - `GET /events/subscriptions`; if one matches our endpoint URL, no-op.
   - Otherwise `POST /events/subscriptions` with `event_types =
     registered_event_types()`.
4. **App serves traffic.** Auth fires events, POSTs to our receiver, we
   verify HMAC, dispatch to handlers, return 200.
5. **Container restart.** Same dance — registrations re-fire, subscribe
   sees the existing subscription, no-op. Idempotent.

If the auto-subscribe fails (auth down, bad token, missing env), the app
still boots and the receiver still works for any subscription registered
manually. The lib logs a warning; the operator fixes the underlying issue
and the next restart picks it up.

---

## Configuration reference

All env vars are optional. The pattern degrades gracefully — set just
the secret and you can manually-register the subscription via the CLI;
set all four and the lib does it automatically on startup.

| Env var | Required for | Notes |
|---|---|---|
| `AB0T_AUTH_WEBHOOK_SECRET` | mounting the receiver | HMAC secret. Per-subscription, operator-generated (e.g. `openssl rand -hex 32`). Auth signs payloads with this; receiver verifies. |
| `AB0T_AUTH_ADMIN_TOKEN` | auto-subscribe at startup | Bearer token with `events.subscribe` permission on auth. |
| `AB0T_AUTH_WEBHOOK_PUBLIC_URL` | auto-subscribe at startup | Externally-reachable base URL of this service (e.g. `https://sandbox.dev.ab0t.com`). Auth POSTs to `<this>/api/quotas/_webhooks/auth`. |
| `AB0T_AUTH_AUTH_URL` | auto-subscribe at startup | Auth service URL. Usually already set for JWT validation. |
| `AB0T_AUTH_WATCH_ORG_SLUG` (or `AB0T_AUTH_ORG_SLUG`) | filtering events | Auth org slug to filter for. Resolved to org_id at subscribe time. Without it, the subscription matches all events of the given types across all orgs (probably not what you want). |

---

## Operator workflows

### Workflow A — fully automatic (preferred for new deploys)

```bash
# .env
AB0T_AUTH_WEBHOOK_SECRET=<openssl rand -hex 32>
AB0T_AUTH_ADMIN_TOKEN=<bearer>
AB0T_AUTH_WEBHOOK_PUBLIC_URL=https://sandbox.service.ab0t.com
AB0T_AUTH_WATCH_ORG_SLUG=sandbox-platform-users

./rebuild.sh
# done. Container starts, lib subscribes itself, events flow.
```

### Workflow B — manual subscription (no admin token in container)

Set just `AB0T_AUTH_WEBHOOK_SECRET` in container env, then register the
subscription externally:

```bash
# CLI shipped with the lib:
AB0T_AUTH_ADMIN_TOKEN=<bearer> AB0T_AUTH_WEBHOOK_SECRET=<same secret> \
python -m ab0t_quota subscribe-events \
  --auth-url https://auth.service.ab0t.com \
  --endpoint https://sandbox.service.ab0t.com/api/quotas/_webhooks/auth \
  --org-id <end-users-org-id>

# Or raw curl:
curl -X POST https://auth.service.ab0t.com/events/subscriptions \
  -H "Authorization: Bearer $AB0T_AUTH_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "ab0t-quota-credit-grant",
    "event_types": ["auth.user.registered"],
    "endpoint": "https://sandbox.service.ab0t.com/api/quotas/_webhooks/auth",
    "secret": "<same secret>"
  }'
```

Both paths produce equivalent subscriptions.

### Verifying it's working

```bash
# 1. Container logs at startup should show:
#    "auth-event webhook mounted at /api/quotas/_webhooks/auth"
#    "auth-event auto-subscribe: created subscription (id=sub_xxx)"
# (or "already subscribed" on subsequent boots)

# 2. Probe the receiver directly:
curl -X POST https://<your>/api/quotas/_webhooks/auth -d '{}'
# Expect: 401 (signature missing — good, means it's mounted)

# 3. Fire a test event from auth:
curl -X POST https://auth.service.ab0t.com/events/subscriptions/$SUB_ID/test \
  -H "Authorization: Bearer $ADMIN_TOKEN"
# Expect: {"success": true, "status_code": 200, ...}

# 4. Trigger the real flow — register a user, watch /balance.
```

---

## Testing handlers

The registry is a module-level dict. Tests must clear it between cases:

```python
import pytest
from ab0t_quota import auth_events as ae

@pytest.fixture(autouse=True)
def _reset_registry():
    ae.clear_handlers()
    yield
    ae.clear_handlers()

def test_my_handler_fires():
    @ae.on_auth_event("auth.user.registered")
    async def captured(event):
        captured.event = event
    # ... fire a fake event via the receiver, assert captured.event is set
```

The lib's own tests in `tests/test_auth_events.py` show the full pattern:
HMAC-signed POST through `TestClient`, mocked `httpx.AsyncClient` for
the auto-subscribe path, monkeypatched env for env-defaults coverage.
26 tests covering registry, dispatch, HMAC, and subscribe — copy the
pattern for your own handlers.

---

## When NOT to use this pattern

- **You need at-least-once delivery semantics.** Auth's webhook system
  retries on non-2xx, but our receiver always returns 200 (so handler
  exceptions don't trigger replay). If you need replay, write your own
  receiver that returns 5xx on handler failure — bypass the lib's
  registry.
- **You need ordered processing.** Webhook delivery is parallel; events
  may arrive out of order. Encode ordering needs in your handler logic
  (timestamps, sequence numbers).
- **You need handler-level retry policies.** All-or-nothing: handler
  succeeds → 200 to auth → no retry. Handler fails → still 200 → no
  retry. If you want per-handler retry, push to a queue inside your
  handler and let the queue handle it.

---

## Future use cases this enables

The registry is generic; the lib doesn't know what events mean. Anything
auth emits can have handlers registered. Examples we'd consider building
into other services:

- **payment-service**: `@on_auth_event("auth.permission.granted")` →
  enable enterprise features when an admin grants the perm.
- **integration-service**: `@on_auth_event("org.created")` →
  provision a Slack workspace mapping.
- **resource-service**: `@on_auth_event("auth.user.logout")` →
  pause idle sandboxes belonging to that user.
- **audit-service**: register handlers for everything → write to a
  compliance log without polling.

The registry, receiver, and auto-subscribe scale to all of these without
changes — the lib is the bus, consumers plug in.
