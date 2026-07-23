---
name: quota-service-integration
description: Integrate ab0t-quota into a FastAPI microservice. Use when adding quota enforcement to a new or existing service, wiring QuotaEngine startup/shutdown in lifespan, adding check/increment/decrement calls to route handlers, adding QuotaGuard rate-limiting middleware, or creating quota API endpoints (/quotas/usage, /quotas/tiers, /quotas/check). Covers the full integration lifecycle from requirements.txt through engine init, resource registration, enforcement wiring, counter lifecycle management, and API exposure.
---

# Quota Service Integration

## Integration Checklist

1. Add dependencies to `requirements.txt`
2. **Provision (or pick) the Redis this service will DECLARE** — set
   `storage.redis_url` in quota-config.json (or export `QUOTA_REDIS_URL`).
   No Redis in this deployment? Choose `engine_mode: "bridge"`. Requirements
   (topology, eviction policy, ACL, version): `docs/requirements.md`. The CLI
   hands you the artifacts instead of you transcribing them:
   `python -m ab0t_quota provision --emit compose|terraform|acl|iam` (emitted
   from the same registry the boot gates enforce; never touches your cloud),
   or `provision --local` for one conforming local dev Redis.
3. Deploy `quota-config.json` alongside the service (copy from `quota-config.example.json`)
4. Create `app/quota.py` module (engine init, helpers, lifecycle hooks)
5. Wire engine startup/shutdown in app lifespan (includes Redis + DynamoDB persistence)
6. Register service-specific resources
7. Add quota checks before resource creation
8. Add counter increments after successful creation
9. Add counter decrements on resource termination
10. Add QuotaGuard middleware for API rate limiting
11. Expose quota API endpoints
12. **Run `python -m ab0t_quota preflight` in CI** — it validates the config,
    prints the resolved plan with provenance, and re-runs every startup gate
    read-only. A refused preflight is a refused deploy, caught early.
13. **Before go-live, run `python -m ab0t_quota doctor`** — same evaluators,
    but it grades production POSTURE that "bootable" deliberately lets
    through (persistence off behind an assertion, PITR asserted-not-observed,
    already-evicted keys, ACL breadth, encryption). It reports what it could
    not check as `not_checked` with the reason; `--json` extends the
    preflight report with a posture section you can hand to an auditor.
    Full CLI reference: `docs/cli.md`; error codes: `docs/error-codes.md`.

## Step 1: Dependencies

```
# requirements.txt
redis>=5.0
git+https://github.com/ab0t-com/ab0t-quota.git
```

## Step 1b: Declare billing model in quota-config.json

If your service charges for anything, each tier in `quota-config.json` needs a `billing_model` plus optional `price` and `credit_grant` blocks. The library reads these and the ecosystem (payment-service webhooks + billing-service) handles the wiring — you don't call any credit-grant endpoints yourself.

Illustrative shape (prices and limits are placeholders — set yours):

```json
{
  "tiers": [
    {
      "tier_id": "free",
      "display_name": "Free",
      "sort_order": 0,
      "billing_model": "consumption_only",
      "initial_credit": 10.00,
      "limits": { "widgets.concurrent": 1, "widgets.monthly_cost": 10.00 }
    },
    {
      "tier_id": "starter",
      "display_name": "Starter",
      "sort_order": 1,
      "billing_model": "subscription_with_credits",
      "price": { "amount_per_period": 10.00, "currency": "USD", "period": "month" },
      "credit_grant": {
        "trigger": "subscription_invoice_paid",
        "amount_per_period": 10.00,
        "currency": "USD",
        "lifecycle": "use_it_or_lose_it",
        "destination": "subscription_credit",
        "reset_on_downgrade": true,
        "reset_on_upgrade": false
      },
      "limits": { "widgets.concurrent": 5, "widgets.monthly_cost": 100.00 }
    },
    {
      "tier_id": "enterprise",
      "display_name": "Enterprise",
      "sort_order": 3,
      "billing_model": "subscription_with_credits",
      "price": { "amount_per_period": 200.00, "currency": "USD", "period": "month" },
      "credit_grant": {
        "trigger": "subscription_invoice_paid",
        "amount_per_period": 200.00,
        "currency": "USD",
        "lifecycle": "rollover_capped",
        "rollover_max_periods": 3,
        "destination": "subscription_credit",
        "reset_on_downgrade": true,
        "reset_on_upgrade": false
      },
      "limits": { "widgets.concurrent": null, "widgets.monthly_cost": null }
    }
  ]
}
```

Money literals are JSON numbers, not strings. The `currency` field appears in BOTH `price` and `credit_grant` (a mismatch between them is a config error). `reset_on_upgrade: false` is the typical pairing with `reset_on_downgrade: true` — you don't want an upgrade to wipe what the user just got.

Pick one `billing_model` per tier:

| Value | Use for |
|---|---|
| `capacity_only` *(default)* | Limits-only tier with no money side-effects |
| `consumption_only` | Pay-as-you-go top-ups (no subscription). Combine with `initial_credit` to grant a starting balance — that's how sandbox-platform's free tier gives every user $10 to try things |
| `subscription_with_credits` | Paid sub that grants $Y of bundled spend each period (the dominant paid-tier model in sandbox-platform) |
| `subscription_unlock_only` | Paid sub that only raises limits — no credit grant. Use when the value of the tier is purely capacity, not bundled spend |

Lifecycle choices on `credit_grant`:
- `persistent` — adds amount, never expires (signup grants)
- `use_it_or_lose_it` — sets to amount each period, leftover forfeit (most consumer SaaS)
- `rollover_unlimited` / `rollover_capped` — carry forward

Once these are declared, no further code is needed for credit handling:
- **Signup grants** — `auth_events.grant_initial_credit_for_user(tier_registry=...)` reads the tier with `trigger: "signup"` and applies the grant
- **Subscription grants** — payment-service's `invoice.paid` webhook reads the tier and applies the period grant
- **Downgrade reset** — payment-service's `subscription.updated` webhook resets `subscription_credit` if the old tier had `reset_on_downgrade: true`

Full cookbook with all 11 billing archetypes + worked examples: `BILLING_MODELS_GUIDE.md` in the ab0t-quota library root (also at `Skills/quota-paid-tier-onboarding/references/billing-models-guide.md`).

## Step 2: Create quota.py Module

Create `app/quota.py` as the single integration point. See [references/quota-module-template.md](references/quota-module-template.md) for the full template.

Key exports:
- `startup()` / `shutdown()` — call from lifespan
- `check_quota(org_id, resource_key, user_id)` — raises 429 on deny
- `get_engine()` — access engine for advanced use

## Step 3: Lifespan Wiring

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    from . import quota as quota_module
    await quota_module.startup()
    yield
    await quota_module.shutdown()
```

## Step 4: Register Resources

Each service defines its own resource keys. Use pre-built definitions from `ab0t_quota.registry` or create custom ones.

```python
from ab0t_quota.models.core import ResourceDef, CounterType, ResetPeriod

MY_RESOURCES = [
    ResourceDef(
        service="my-service",
        resource_key="my.concurrent_items",
        display_name="Concurrent Items",
        counter_type=CounterType.GAUGE,
        unit="items",
    ),
]
registry.register(*MY_RESOURCES)
```

Counter type selection:
- **GAUGE** — bidirectional (concurrent sandboxes, CPU cores). Inc on create, dec on destroy.
- **RATE** — auto-expiring sliding window (API req/hour). Inc only, expires automatically.
- **ACCUMULATOR** — monotonic within period (monthly spend). Inc only, resets on period boundary.

## Step 5: Enforcement Pattern

```python
# Before provisioning:
from . import quota as quota_module
await quota_module.check_quota(user.org_id, "my.concurrent_items", user_id=user.user_id)

# After successful provisioning:
await quota_module.get_engine().increment(
    QuotaIncrementRequest(org_id=user.org_id, resource_key="my.concurrent_items",
                          user_id=user.user_id, delta=1)
)

# On termination:
await quota_module.get_engine().decrement(
    QuotaDecrementRequest(org_id=user.org_id, resource_key="my.concurrent_items",
                          user_id=user.user_id, delta=1)
)
```

Always wrap increment/decrement in try/except — quota tracking failures must not block the actual operation.

## Step 6: API Endpoints

Expose these for frontend usage bars and tier comparison:

- `GET /api/quotas/usage` — `engine.get_usage(org_id)` → usage bars
- `GET /api/quotas/tiers` — tier comparison for pricing page
- `GET /api/quotas/check/{resource_key}` — pre-flight check

See [references/api-endpoints.md](references/api-endpoints.md) for route handler code.

## Storage Architecture

The engine uses a two-layer storage model:

- **Redis** (hot path) — all counter reads/writes, tier cache, alert cooldowns. <5ms p99.
- **DynamoDB** (`ab0t_quota_state` table) — durable state for org tiers, per-org overrides, counter snapshots. Read on startup to seed Redis. Written to periodically by sync worker.

On startup, `quota.py` calls `store.seed_redis()` to recover counters from DynamoDB snapshots. If Redis restarts, counters are restored automatically.

Config file (`quota-config.json`) controls tiers, limits, features, Stripe mapping, and enforcement flags without code deploys. See [quota-tier-management](../quota-tier-management/SKILL.md) skill for config schema.

## Key Rules

- Quota check BEFORE provisioning, increment AFTER success, decrement on teardown
- Always pass `user_id` for per-user sub-quota support
- Wrap increment/decrement in try/except (non-fatal)
- Use `QuotaBatchCheckRequest` when creating resources that consume multiple quotas (e.g. GPU sandbox = sandbox.concurrent + sandbox.gpu_instances)
- The engine reads tier from billing service `GET /billing/{org_id}/tier` (cached 5min in Redis)
- If the engine is not initialized, that is a STARTUP defect to fix, not a state to serve from — quota/billing paths fail CLOSED (0.6.1/0.6.2); do not add consumer-side fail-open wrappers around money-path checks
- DynamoDB persistence is non-fatal — if it fails, Redis-only mode continues
