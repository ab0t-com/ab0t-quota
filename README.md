# ab0t-quota

Shared quota, rate-limiting, and tier enforcement library for the [ab0t mesh network](https://ab0t.com). Any service in the mesh can use this library to enforce usage limits, manage billing tiers, and gate features — without building its own rate-limiting infrastructure.

## What it does

- **Tier-based limits** — Free, Starter, Pro, Enterprise tiers with per-resource limits
- **Three counter types** — Gauges (concurrent resources), Rates (sliding-window), Accumulators (monthly spend)
- **Per-org and per-user enforcement** — Org-level limits with optional per-user sub-quotas
- **Self-service increase requests** — Users request higher limits, admins approve/deny
- **Per-org overrides** — Enterprise customers get custom limits without a new tier
- **Drop-in FastAPI middleware** — One line to add rate limiting to any service
- **Alerts** — Webhook and log dispatchers when usage crosses warning/critical thresholds
- **DynamoDB persistence** — Redis is the hot path; DynamoDB backs up state for recovery
- **Feature gating** — Check if a tier includes a feature (e.g. `gpu_access`, `sso`)
- **Idempotency** — Built-in deduplication prevents double-counting on retries

## Install

```bash
# requirements.txt
ab0t-quota @ git+https://github.com/ab0t-com/ab0t-quota.git

# With optional extras
ab0t-quota[webhooks] @ git+https://github.com/ab0t-com/ab0t-quota.git   # httpx for webhook alerts
ab0t-quota[dynamo] @ git+https://github.com/ab0t-com/ab0t-quota.git     # aioboto3 for DynamoDB persistence
ab0t-quota[all] @ git+https://github.com/ab0t-com/ab0t-quota.git        # everything

# Pin to a release tag
ab0t-quota @ git+https://github.com/ab0t-com/ab0t-quota.git@v0.1.0

# Local development
pip install -e ".[dev]"
```

Core dependencies: `redis`, `pydantic`, `fastapi`. That's it.

## Quick start

```python
from redis.asyncio import Redis
from ab0t_quota import (
    QuotaEngine, QuotaGuard, QuotaCheckRequest,
    QuotaIncrementRequest, ResourceDef, CounterType,
)
from ab0t_quota.providers import JWTTierProvider
from ab0t_quota.registry import ResourceRegistry
from ab0t_quota.tiers import DEFAULT_TIERS

# 1. Set up
redis = Redis.from_url("redis://localhost:6379/0")
registry = ResourceRegistry()
registry.register(
    ResourceDef(
        service="my-service",
        resource_key="sandbox.concurrent",
        display_name="Concurrent Sandboxes",
        counter_type=CounterType.GAUGE,
        unit="sandboxes",
    ),
)

engine = QuotaEngine(
    redis=redis,
    tier_provider=JWTTierProvider(),
    registry=registry,
    tiers=DEFAULT_TIERS,
)

# 2. Check before provisioning
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
from ab0t_quota.models.requests import QuotaDecrementRequest
await engine.decrement(
    QuotaDecrementRequest(org_id="org-123", resource_key="sandbox.concurrent")
)
```

## Rate-limiting middleware

One line to protect any FastAPI service:

```python
app.add_middleware(
    QuotaGuard,
    engine=engine,
    resource_key="api.requests_per_hour",
)
```

The middleware extracts `org_id` from the JWT (via `request.state.user`), checks the rate counter, returns `429` with a structured error body when denied, and adds `X-Quota-Limit` / `X-Quota-Remaining` headers to every response. Health, docs, and metrics paths are exempt by default.

By default the middleware **fails closed** — if Redis is unavailable, requests get `503` rather than bypassing quota. Set `fail_open=True` if your service prefers availability over enforcement.

## Counter types

| Type | Behaviour | Use case | Example |
|------|-----------|----------|---------|
| **Gauge** | Inc on create, dec on destroy | Current resource level | Concurrent sandboxes, active CPU cores |
| **Rate** | Sliding window, auto-expires | Request throughput | API calls per hour |
| **Accumulator** | Monotonic within period, resets on boundary | Spend tracking | Monthly compute cost (USD) |

## Default tiers

| Resource | Free | Starter | Pro | Enterprise |
|----------|------|---------|-----|------------|
| `sandbox.concurrent` | 1 | 5 | 25 | Unlimited |
| `sandbox.monthly_cost` | $10 | $100 | $1,000 | Unlimited |
| `sandbox.gpu_instances` | 0 | 1 | 5 | 50 |
| `resource.cpu_cores` | 4 | 32 | 256 | 1,000 |
| `api.requests_per_hour` | 1K | 10K | 50K | 100K |
| `auth.users_per_org` | 5 | 25 | 100 | 10,000 |

Tiers are fully configurable via `quota-config.json`. See [`quota-config.example.json`](quota-config.example.json) for the full schema.

## 429 response format

When a request is denied, the engine returns a structured error designed for end-user display:

```json
{
  "error": "quota_exceeded",
  "resource": "sandbox.concurrent",
  "current": 5,
  "limit": 5,
  "tier": "starter",
  "message": "You've reached the max of 5 sandboxes on Starter. Upgrade to Pro for up to 25.",
  "upgrade_url": "/billing/upgrade"
}
```

Messages are human-readable, tier-aware, and include upgrade hints. No technical jargon.

## Architecture

```
Service --> QuotaEngine.check() --> Redis counters --> QuotaResult (allow/deny/warn)
                |
        TierProvider --> JWT claim or billing-service API (cached 5 min)
                |
        DynamoDB <-- periodic sync (backup/recovery, not in hot path)
```

- **Redis** is the hot path. Every check and increment hits Redis only. Sub-5ms p99.
- **DynamoDB** is the durable store. Single-table design with GSI for cross-org queries. Counter snapshots sync every 5 minutes. On cold start, `seed_redis()` restores counters from DynamoDB via GSI query (not scan).
- **Tier resolution** reads the `org_tier` claim from the JWT (zero-latency). Falls back to billing-service API with Redis cache.

## DynamoDB access patterns

| # | Pattern | Key | Index |
|---|---------|-----|-------|
| 1 | Get/set org tier | `PK=ORG#{org_id} SK=TIER` | Table |
| 2 | Get/set override | `PK=ORG#{org_id} SK=OVERRIDE#{resource}` | Table |
| 3 | Get/set counter snapshot | `PK=ORG#{org_id} SK=COUNTER#{resource}` | Table |
| 4 | List all org data | `PK=ORG#{org_id}` (query all SK) | Table |
| 5 | List org overrides | `PK=ORG#{org_id} SK begins_with OVERRIDE#` | Table |
| 6 | List org increase requests | `PK=ORG#{org_id} SK begins_with INCREASE#` | Table |
| 7 | List all counters (seed) | `GSI1PK=COUNTER` | GSI1 |
| 8 | List all overrides (admin) | `GSI1PK=OVERRIDE` | GSI1 |
| 9 | List all tiers (admin) | `GSI1PK=TIER` | GSI1 |

All hot-path reads are single-item `GetItem` (1 RCU). Cross-org queries use GSI1. No scans.

## Alerts

```python
from ab0t_quota import AlertManager, WebhookAlertDispatcher

alert_mgr = AlertManager(
    redis=redis,
    dispatchers=[WebhookAlertDispatcher(url="https://hooks.slack.com/services/...")],
    cooldown_seconds=3600,  # 1 alert per resource per org per hour
)
engine.set_alert_manager(alert_mgr)
```

Alerts fire at WARNING (80%), CRITICAL (95%), and EXCEEDED (100%) thresholds. Cooldown and severity escalation prevent alert spam.

## Configuration

Copy `quota-config.example.json` to `quota-config.json` or set `QUOTA_CONFIG_PATH`:

```json
{
  "storage": { "redis_url": "redis://localhost:6379/0" },
  "tier_provider": { "type": "jwt", "jwt_claim_key": "org_tier" },
  "enforcement": { "enabled": true, "shadow_mode": false },
  "tiers": [ ... ]
}
```

## Testing

```bash
pip install -e ".[dev]"
pytest
```

## License

Proprietary. Copyright ab0t.com. See LICENSE for details.
