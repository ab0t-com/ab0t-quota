---
name: ab0t-quota-idempotent-handlers
description: Make auth-event handlers idempotent, observable, and replayable using ab0t-quota's @idempotent decorator (v0.5.2+). Use when adding @idempotent to a handler, configuring credit_grant.dedup policy (per_user_per_tier, per_org_per_tier, per_user_global, per_org_global), choosing a LedgerStore backend (DDB, Redis, InMemory), running CLI subcommands (events, replay, backfill, delete-user) to query the handler ledger or recover from failures, building HandlerContext with ctx.skip / ctx.success / ctx.already_done / ctx.mark_done, migrating a plain @on_auth_event handler to the v0.5.2 ctx-arg signature, or handling auth webhook retries and adjacent-event double-fires (auth.user.registered + auth.user.login).
---

# ab0t-quota — Idempotent handlers

For handlers that have **side effects on money or persistent state**.
Wraps a handler with delivery dedup + business dedup + auto-retry +
ledger persistence, in one decorator.

If your handler just tracks last-seen or fires a notification, you don't
need this — use plain `@on_auth_event` (see `ab0t-quota-auth-events`).

## Quick start

```python
from ab0t_quota.auth_events import on_auth_event, compose_credit_dedup_key
from ab0t_quota.handler_ledger import idempotent

@on_auth_event("auth.user.registered")
@idempotent(
    handler="grant_credit_on_signup",
    key=lambda e: compose_credit_dedup_key(
        "per_user_per_tier",
        user_id=e["data"]["user_id"],
        org_id=e["data"]["org_id"],
        tier_id="free",
    ),
)
async def grant_credit(event, ctx):
    if await ctx.already_done():
        return ctx.skip("credit already granted")
    txn = await billing.grant_credit(event["data"]["org_id"], amount=10)
    await ctx.mark_done(side_effect_id=txn.id)
    return ctx.success(side_effect_id=txn.id)
```

## What the decorator guarantees

1. **Delivery dedup.** If `(handler_name, event_id)` was already
   processed to a terminal state, the handler body never runs again —
   the cached outcome is returned.
2. **Business dedup via `key`.** Consumer provides a lambda that
   composes a string. `ctx.already_done()` checks it; `ctx.mark_done()`
   records it. Compose it yourself or use `compose_credit_dedup_key`.
3. **Auto-retry.** Default 3 attempts, exponential backoff
   (1s → 2s → 4s, max 30s). Disable with `retry=False`.
4. **Ledger persistence.** Every outcome (success / skipped / failed /
   failed_permanent) is recorded with the full event payload for
   replay.

## Dedup policies

Use `compose_credit_dedup_key(policy, ...)` to pick. Or write your own
key — the lib doesn't care about its shape.

| Policy | Key shape | Use when |
|---|---|---|
| `per_user_per_tier` (default) | `credit_granted:user:{user_id}:{tier_id}` | Self-service SaaS. Anti-farming, one credit per human per tier. |
| `per_org_per_tier` | `credit_granted:org:{org_id}:{tier_id}` | B2B. "One credit per company per tier" — adding employees doesn't multiply. |
| `per_user_global` | `credit_granted:user:{user_id}` | Pay-per-seat with one-time grant. "$X per seat, ever." |
| `per_org_global` | `credit_granted:org:{org_id}` | One credit per org, ever. |

You can also drive the policy from config — add `credit_grant.dedup` to
your tier in `quota-config.json`:

```jsonc
{
  "tier_id": "free",
  "credit_grant": {
    "trigger": "signup",
    "amount_per_period": "10.00",
    "dedup": "per_user_per_tier"
  }
}
```

The lib's default signup-credit handler honors this automatically.

## Storage backends (auto-selected)

| Backend | Picked when | Retention |
|---|---|---|
| `DDBLedgerStore` | `app.state.ddb_client` is set before `setup_quota()` | 90 days (DDB TTL) |
| `RedisLedgerStore` | Redis is configured | 72 hours |
| `InMemoryLedgerStore` | Neither — logs a loud warning | session only |

Consumer can override: `setup_quota(app, ledger_store=MyStore())`.

## Handler signature changes

Adding `@idempotent` adds a `ctx` arg to the handler:

```python
# Plain @on_auth_event (no idempotency machinery):
async def handler(event): ...

# With @idempotent:
async def handler(event, ctx): ...
```

`ctx` is a `HandlerContext` with 4 methods:

- **`await ctx.already_done()`** — `bool`. True if the business dedup
  key was previously marked done.
- **`await ctx.mark_done(side_effect_id=...)`** — write the business
  dedup row. Call this after the side effect succeeds.
- **`ctx.skip(reason)`** — sentinel return. Lib records
  `status=skipped` with your reason.
- **`ctx.success(side_effect_id=...)`** — sentinel return. Lib records
  `status=success`.

If the handler returns nothing, the lib records `status=success` with
no `side_effect_id`. If it raises, the lib catches, retries per the
policy, then records `status=failed_permanent` on max attempts.

## Operator CLI

```bash
# What happened?
python -m ab0t_quota events --user-id u123
python -m ab0t_quota events --status failed --since 1h
python -m ab0t_quota events --status failed_permanent --since 24h --format json

# Run it again (from the stored event payload — no auth needed)
python -m ab0t_quota replay \
  --handler grant_credit_on_signup --event-id evt_xxx

# Synthesize events for users who pre-existed the handler
python -m ab0t_quota backfill \
  --handler grant_credit_on_signup \
  --user-ids u1,u2,u3 \
  --org-id <end-users-org-id>

# GDPR cascade
python -m ab0t_quota delete-user --user-id u123 --confirm
```

Env vars the CLI reads:
- `AB0T_QUOTA_DDB_TABLE` — DDB table name (preferred)
- `QUOTA_REDIS_URL` / `REDIS_URL` — Redis URL (fallback)
- `AB0T_AUTH_WEBHOOK_PUBLIC_URL` + `AB0T_AUTH_WEBHOOK_SECRET` — for `replay` and `backfill`

## When NOT to use `@idempotent`

- **High-volume events** (e.g. `auth.api_key.used`, `auth.token.refreshed`). Ledger row per event would explode storage. Use plain handlers with in-memory aggregation.
- **Strictly one-shot effects with their own idempotency** (e.g. a Stripe call with its own `Idempotency-Key`). The `@idempotent` is defense in depth; skip if you trust the downstream.
- **Events you want auth to retry on failure.** `@idempotent` always returns 200 to auth, no retry. If you need auth-side retry, write a plain handler that re-raises.

## Migration from plain handlers

`@idempotent` is opt-in, per-handler. Plain handlers without it keep
working unchanged.

To migrate one handler:

```python
# Before:
@on_auth_event("auth.user.registered")
async def handler(event):
    await do_thing(event["data"]["user_id"])

# After:
@on_auth_event("auth.user.registered")
@idempotent(handler="do_thing", key=lambda e: f"do:{e['data']['user_id']}")
async def handler(event, ctx):
    if await ctx.already_done():
        return ctx.skip("already done")
    await do_thing(event["data"]["user_id"])
    await ctx.mark_done()
    return ctx.success()
```

Decorator order matters: `@on_auth_event` is outermost (registry
wraps), `@idempotent` directly above the function body.

## Full guide

See the "Idempotency, replay, and observability" section in
[`docs/auth-events.md`](../../../docs/auth-events.md) for:
- Full storage schema details
- Conformance-tested behavior across all three backends
- Test patterns for handlers with `@idempotent`
