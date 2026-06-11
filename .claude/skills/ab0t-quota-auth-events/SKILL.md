---
name: ab0t-quota-auth-events
description: Wire auth events (auth.user.registered, auth.user.login, org.created, permission.granted, etc.) from your auth provider into your service via ab0t-quota's handler registry. Use when registering @on_auth_event handlers, configuring AB0T_AUTH_WEBHOOK_SECRET, setting up the webhook receiver at /api/quotas/_webhooks/auth, registering a subscription against auth's /events/subscriptions, or using the lib's primitives (PinStore, resolve_billing_org, grant_initial_credit_for_user, compose_credit_dedup_key) to grant credits, track activity, provision accounts on signup, or send welcome emails.
---

# ab0t-quota — Auth events

## Quick start

```python
from ab0t_quota.auth_events import on_auth_event

@on_auth_event("auth.user.registered")
async def welcome(event):
    user_id = event["data"]["user_id"]
    org_id  = event["data"]["org_id"]
    # ...your logic
```

That's it. Set `AB0T_AUTH_WEBHOOK_SECRET` in your env, call
`setup_quota(app)` once, and your handler fires on every signup.

## What the library does for you

1. **Mounts the receiver** at `POST /api/quotas/_webhooks/auth` when
   `AB0T_AUTH_WEBHOOK_SECRET` is set. Verifies HMAC on every delivery.
2. **Dispatches to your handlers.** Each event is delivered to every
   handler registered for that event_type, in registration order.
3. **Auto-subscribes** with your auth provider's `/events/subscriptions`
   endpoint at startup, when `AB0T_AUTH_ADMIN_TOKEN` and
   `AB0T_AUTH_WEBHOOK_PUBLIC_URL` are also set.

You write handlers. The library handles the wire.

## Two registration styles

**Decorator** (preferred for app code):
```python
@on_auth_event("auth.user.registered")
async def my_handler(event): ...
```

**Function call** (preferred for dynamic / conditional registration):
```python
register_handler("auth.user.login", track_last_seen)
if FEATURE_GRANT_CREDIT:
    register_handler("auth.user.registered", grant_initial_credit)
```

Both styles are idempotent — registering the same callable twice is a
no-op. Multiple handlers per event are all called.

## Common event types

Hit `GET <your-auth-url>/events/types` for the authoritative list. The
ones consumers most often handle:

| Event type | Typical use |
|---|---|
| `auth.user.registered` | initial credit, welcome email, provision first resource |
| `auth.user.login` | last-seen tracking, anomaly detection |
| `org.created` | provision billing account, initial config |
| `org.member.added` | seat tracking, team welcome |
| `auth.permission.granted` / `revoked` | invalidate caches, audit log |

## Reusable primitives

Importable from `ab0t_quota.auth_events`:

- **`PinStore(table_name, ddb_client)`** — DDB-backed `user_id → billing org_id` storage. Sticky on first set; conditional write protects operator-set values.
- **`resolve_billing_org(user_id, fallback_org_id, *, auth_url, mesh_api_key, pin_store) -> str`** — finds the user's owner-role org (workspace if workspace-per-user, else first owner org). First call hits auth + writes the pin; subsequent calls return the pinned value.
- **`grant_initial_credit_for_user(user_id, org_id, *, initial_credits, tier_provider, redis, billing_url, billing_api_key, tier_registry=None) -> None`** — idempotent credit grant. Resolves tier, looks up `initial_credit`, hits billing's `/promotional-credit` with an idempotency key, sets Redis dedup flag.
- **`compose_credit_dedup_key(policy, *, user_id, org_id, tier_id) -> str`** — composes a business-dedup key. Policies: `per_user_per_tier` (default, anti-farming), `per_org_per_tier` (B2B), `per_user_global`, `per_org_global`.

You can use these in your handler, or skip them entirely if you have your own.

## Required env vars

| Var | Required for | Notes |
|---|---|---|
| `AB0T_AUTH_WEBHOOK_SECRET` | mounting the receiver | Per-subscription, operator-generated. e.g. `openssl rand -hex 32`. Auth signs payloads with this; the receiver verifies. |
| `AB0T_AUTH_ADMIN_TOKEN` | auto-subscribe at startup | Bearer token with `events.subscribe` permission. |
| `AB0T_AUTH_WEBHOOK_PUBLIC_URL` | auto-subscribe at startup | Externally-reachable base URL of your service. Auth POSTs to `<this>/api/quotas/_webhooks/auth`. |
| `AB0T_AUTH_AUTH_URL` | auto-subscribe at startup | Your auth provider's URL. Usually already set for JWT validation. |
| `AB0T_AUTH_WATCH_ORG_SLUG` | filtering events | Auth org slug to filter for. Without it, the subscription matches all events of the given types. |

## Manual subscription (alternative to auto-subscribe)

If you don't want admin credentials in your container, register the
subscription externally:

```bash
python -m ab0t_quota subscribe-events \
  --auth-url https://<your-auth>.com \
  --endpoint https://<your-service>.com/api/quotas/_webhooks/auth \
  --org-id <end-users-org-id>
```

Or raw curl:

```bash
curl -X POST https://<your-auth>.com/events/subscriptions \
  -H "Authorization: Bearer $AB0T_AUTH_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-service-credit-grant",
    "event_types": ["auth.user.registered"],
    "endpoint": "https://<your-service>.com/api/quotas/_webhooks/auth",
    "secret": "<same as AB0T_AUTH_WEBHOOK_SECRET>"
  }'
```

## Handler signature

```python
async def handler(event: dict) -> None:
    # event = {
    #   "event_type": "auth.user.registered",
    #   "event_id":   "evt_xxxxxxxxxxx",
    #   "data": { "user_id": "...", "org_id": "...", "email": "..." }
    # }
```

Rules:
- **Async only.** The receiver awaits each handler.
- **Don't raise unless you mean it.** Handler exceptions are caught and logged; the receiver still returns 200 to auth (no retry). If you need retry-on-failure, see [`ab0t-quota-idempotent-handlers`](../ab0t-quota-idempotent-handlers/SKILL.md).
- **Idempotent or it gets ugly.** Auth retries; adjacent events (`user.registered` + `user.login`) may fire for the same signup. Use a dedup mechanism — or use `@idempotent` from the idempotent-handlers skill.
- **Keep it fast.** Each handler runs inline. Long work belongs in a background task fired from the handler.

## Full guide

See [`docs/auth-events.md`](../../../docs/auth-events.md) for:
- Mental model + ASCII diagram of the lifecycle
- 6-recipe cookbook (credit grant, last-seen, billing init, perm cache bust, welcome email, multi-handler composition)
- Configuration reference and operator workflows
- Testing patterns
- "When NOT to use this pattern" honest limitations
