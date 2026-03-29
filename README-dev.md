# ab0t-quota

Shared quota, rate-limit, and tier enforcement for ab0t platform services.

## Install

```bash
# In requirements.txt or pip install:
pip install git+https://github.com/ab0t-com/ab0t-quota.git

# With optional extras:
pip install "ab0t-quota[webhooks] @ git+https://github.com/ab0t-com/ab0t-quota.git"  # httpx (webhook alerts)
pip install "ab0t-quota[dynamo] @ git+https://github.com/ab0t-com/ab0t-quota.git"    # aioboto3 (persistence)
pip install "ab0t-quota[all] @ git+https://github.com/ab0t-com/ab0t-quota.git"       # everything

# Local development:
pip install -e ".[dev]"
```

## Quickstart

```python
from redis.asyncio import Redis
from ab0t_quota import QuotaEngine, QuotaGuard, QuotaCheckRequest, QuotaIncrementRequest
from ab0t_quota.providers import JWTTierProvider
from ab0t_quota.registry import ResourceRegistry, SANDBOX_RESOURCES
from ab0t_quota.tiers import DEFAULT_TIERS

# 1. Setup
redis = Redis.from_url("redis://localhost:6379/0")
registry = ResourceRegistry()
registry.register(*SANDBOX_RESOURCES)

engine = QuotaEngine(
    redis=redis,
    tier_provider=JWTTierProvider(),
    registry=registry,
    tiers=DEFAULT_TIERS,
)

# 2. Check before creating a sandbox
result = await engine.check(
    QuotaCheckRequest(org_id="org-123", resource_key="sandbox.concurrent"),
    token_claims={"org_tier": "starter"},
)
if result.denied:
    raise HTTPException(status_code=429, detail=result.to_api_error())

# 3. Increment after creation succeeds
await engine.increment(
    QuotaIncrementRequest(org_id="org-123", resource_key="sandbox.concurrent")
)

# 4. Decrement on teardown
from ab0t_quota import QuotaDecrementRequest
await engine.decrement(
    QuotaDecrementRequest(org_id="org-123", resource_key="sandbox.concurrent")
)
```

## Rate Limiting Middleware

```python
app.add_middleware(
    QuotaGuard,
    engine=engine,
    resource_key="api.requests_per_hour",
)
```

## Counter Types

| Type | Use Case | Example |
|------|----------|---------|
| **Gauge** | Current level, inc/dec | Concurrent sandboxes, CPU cores |
| **Rate** | Sliding window, auto-expires | API requests per hour |
| **Accumulator** | Monotonic within period | Monthly spend (USD) |

## Tiers

Default tiers: `free`, `starter`, `pro`, `enterprise`. Each defines limits
per resource and feature flags. Override per-org with `QuotaOverride`.

## Architecture

```
Service → QuotaEngine.check() → Redis counters → QuotaResult (allow/deny/warn)
                ↓
        TierProvider → JWT claim or auth-service API (cached)
```
