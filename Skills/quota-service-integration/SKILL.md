---
name: quota-service-integration
description: Integrate ab0t-quota into a FastAPI microservice. Use when adding quota enforcement to a new or existing service, wiring QuotaEngine startup/shutdown in lifespan, adding check/increment/decrement calls to route handlers, adding QuotaGuard rate-limiting middleware, or creating quota API endpoints (/quotas/usage, /quotas/tiers, /quotas/check). Covers the full integration lifecycle from requirements.txt through engine init, resource registration, enforcement wiring, counter lifecycle management, and API exposure.
---

# Quota Service Integration

## Integration Checklist

1. Add dependencies to `requirements.txt`
2. Deploy `quota-config.json` alongside the service (copy from `quota-config.example.json`)
3. Create `app/quota.py` module (engine init, helpers, lifecycle hooks)
4. Wire engine startup/shutdown in app lifespan (includes Redis + DynamoDB persistence)
5. Register service-specific resources
6. Add quota checks before resource creation
7. Add counter increments after successful creation
8. Add counter decrements on resource termination
9. Add QuotaGuard middleware for API rate limiting
10. Expose quota API endpoints

## Step 1: Dependencies

```
# requirements.txt
redis>=5.0
git+https://github.com/ab0t-com/ab0t-quota.git
```

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
- If engine is not initialized, fall back gracefully (fail-open)
- DynamoDB persistence is non-fatal — if it fails, Redis-only mode continues
