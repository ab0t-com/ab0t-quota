# ab0t-quota Architecture

## System Overview

```
┌──────────────────────────────────────────────────────────────────────────┐
│                           USER JOURNEY                                    │
│                                                                           │
│  1. User signs up ──► Auth Service creates org (tier=free)                │
│  2. User pays ──────► Payment Service ──► Stripe                          │
│  3. Stripe webhook ─► Payment Service ──► Auth Service                    │
│                                           PUT /orgs/{id}/tier = "pro"     │
│  4. Auth Service ───► DynamoDB: ORG#org-1 / TIER = "pro"                  │
│                   ───► Redis: invalidate quota:tier:org-1                  │
│                   ───► Next JWT includes org_tier="pro" claim              │
│                                                                           │
│  5. User creates sandbox ──► Sandbox Platform                             │
│                                  │                                        │
│                                  ▼                                        │
│                          QuotaEngine.check()                              │
│                          ├─ Read tier from JWT org_tier claim (0ms)        │
│                          ├─ Read counter from Redis (1-2ms)               │
│                          ├─ Read override from DynamoDB cache (0ms hit)   │
│                          ├─ Evaluate: current + requested vs limit        │
│                          └─ Return ALLOW / DENY / ALLOW_WARNING           │
│                                  │                                        │
│                          ┌───────┴────────┐                               │
│                          │                │                               │
│                     ALLOW ▼           DENY ▼                              │
│                 Provision EC2      Return 429                             │
│                 Increment Redis    {                                      │
│                 Record billing       "error": "quota_exceeded",           │
│                                      "resource": "sandbox.concurrent",   │
│                                      "current": 5, "limit": 5,           │
│                                      "tier": "starter",                  │
│                                      "message": "You've reached the      │
│                                        max of 5 sandboxes on Starter.    │
│                                        Upgrade to Pro for up to 25.",    │
│                                      "upgrade_url": "/billing/upgrade"   │
│                                    }                                      │
│                                                                           │
│  6. Sandbox terminates ──► Decrement Redis counter                        │
│  7. Periodic sync ───────► Redis counters ──► DynamoDB snapshots          │
│  8. Cost accumulates ────► cost_manager ──► quota.record_cost()           │
│                                          ──► billing-service (financial)  │
└──────────────────────────────────────────────────────────────────────────┘
```

## Storage Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     HOT PATH (every request)                     │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                    Redis                                  │   │
│  │                                                           │   │
│  │  Gauge counters:                                          │   │
│  │    quota:org-1:sandbox.concurrent:gauge = 3               │   │
│  │    quota:org-1:sandbox.concurrent:gauge:user:alice = 2    │   │
│  │    quota:org-1:sandbox.gpu_instances:gauge = 1            │   │
│  │                                                           │   │
│  │  Rate counters (sorted set, auto-expiring):               │   │
│  │    quota:org-1:api.requests:rate = {ts1:1, ts2:1, ...}    │   │
│  │                                                           │   │
│  │  Accumulators (period-keyed):                             │   │
│  │    quota:org-1:sandbox.monthly_cost:acc:2026-03 = 47.52   │   │
│  │                                                           │   │
│  │  Tier cache:                                              │   │
│  │    quota:tier:org-1 = "pro"  (TTL 5min)                   │   │
│  │                                                           │   │
│  │  Alert cooldown:                                          │   │
│  │    quota:alert:org-1:sandbox.concurrent = "warning" (TTL) │   │
│  │                                                           │   │
│  │  Idempotency keys:                                        │   │
│  │    quota:org-1:sandbox.concurrent:idem:create-abc (24h)   │   │
│  └──────────────────────────────────────────────────────────┘   │
│                          < 5ms p99                                │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                  DURABLE STATE (DynamoDB)                         │
│                  Table: ab0t_quota_state                          │
│                                                                  │
│  ┌───────────────┬─────────────────────┬──────────────────────┐ │
│  │ PK            │ SK                  │ Data                 │ │
│  ├───────────────┼─────────────────────┼──────────────────────┤ │
│  │ ORG#org-1     │ TIER                │ tier_id="pro"        │ │
│  │               │                     │ changed_by="payment" │ │
│  │               │                     │ changed_at=...       │ │
│  ├───────────────┼─────────────────────┼──────────────────────┤ │
│  │ ORG#org-1     │ OVERRIDE#sandbox.   │ limit=50             │ │
│  │               │ concurrent          │ reason="Enterprise"  │ │
│  │               │                     │ expires_at=...       │ │
│  │               │                     │ created_by="admin-1" │ │
│  ├───────────────┼─────────────────────┼──────────────────────┤ │
│  │ ORG#org-1     │ COUNTER#sandbox.    │ value=3.0            │ │
│  │               │ concurrent          │ snapshotted_at=...   │ │
│  ├───────────────┼─────────────────────┼──────────────────────┤ │
│  │ ORG#org-1     │ COUNTER#sandbox.    │ value=47.52          │ │
│  │               │ monthly_cost        │ snapshotted_at=...   │ │
│  ├───────────────┼─────────────────────┼──────────────────────┤ │
│  │ ORG#org-1     │ INCREASE#req-uuid   │ resource_key=...     │ │
│  │               │                     │ status="pending"     │ │
│  │               │                     │ justification=...    │ │
│  └───────────────┴─────────────────────┴──────────────────────┘ │
│                                                                  │
│  GSI1 (overloaded — type-based access across orgs):              │
│  ┌───────────────┬──────────────────────────────────────────┐    │
│  │ GSI1PK        │ GSI1SK                                   │    │
│  ├───────────────┼──────────────────────────────────────────┤    │
│  │ TIER          │ ORG#org-1                                │    │
│  │ COUNTER       │ ORG#org-1#sandbox.concurrent             │    │
│  │ OVERRIDE      │ ORG#org-1#sandbox.concurrent             │    │
│  └───────────────┴──────────────────────────────────────────┘    │
│                                                                  │
│  Access Patterns:                                                │
│  1. Get org tier:      PK=ORG#org-1, SK=TIER             (table)│
│  2. Get override:      PK=ORG#org-1, SK=OVERRIDE#res     (table)│
│  3. Get counter:       PK=ORG#org-1, SK=COUNTER#res      (table)│
│  4. List all org data: PK=ORG#org-1 (query all SK)       (table)│
│  5. List increase reqs: PK=ORG#org-1, SK bw INCREASE#    (table)│
│  6. Seed all counters: GSI1PK=COUNTER (query, not scan)  (GSI1) │
│  7. List all overrides: GSI1PK=OVERRIDE                  (GSI1) │
│  8. List all tiers:    GSI1PK=TIER                       (GSI1) │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                   CONFIG (read on startup)                        │
│                                                                  │
│  quota-config.json (or QUOTA_CONFIG_PATH env var)                │
│  ├── tiers[]:        Tier definitions, limits, features          │
│  ├── storage:        Redis URL, DynamoDB table, sync interval    │
│  ├── tier_provider:  JWT claim key, default tier, cache TTL      │
│  ├── alerts:         Webhook URL, cooldown, dispatchers          │
│  ├── enforcement:    enabled, shadow_mode, kill_switch           │
│  └── billing_integration:                                        │
│      ├── stripe_price_to_tier:  Maps Stripe plan → tier_id       │
│      ├── downgrade_grace_period_days: 7                          │
│      └── payment_failure_grace_period_days: 3                    │
└─────────────────────────────────────────────────────────────────┘
```

## Service Integration Map

```
                     ┌──────────────┐     ┌──────────────┐
                     │   Payment    │     │   Billing    │
                     │   Service    │────►│   Service    │
                     │              │     │              │
                     │ Stripe       │ tier│ Tier owner   │
                     │ webhooks     │ set │ Accounts     │
                     │              │     │ Balance      │
                     └──────────────┘     └──────┬───────┘
                                                  │
                                    GET /{org_id}/tier (cached 5min)
                                                  │
┌─────────────────────────────────────────────────┴───────────┐
│                    Sandbox Platform                           │
│                                                              │
│  ┌──────────┐  ┌──────────────┐  ┌────────────────────────┐ │
│  │ ab0t-auth│  │  ab0t-quota  │  │   billing_client.py    │ │
│  │ (JWT     │  │  (QuotaEngine│  │   (reserve/commit/     │ │
│  │  verify) │  │   + Redis)   │  │    record usage)       │ │
│  └────┬─────┘  └──────┬───────┘  └────────────────────────┘ │
│       │               │                                      │
│       │    ┌──────────┴──────────┐                           │
│       │    │ enforce_user_cost_  │   Tier lookup:            │
│       │    │ limit()             │   billing GET /{org}/tier │
│       │    │                     │   cached in Redis (5min)  │
│       │    │ 1. quota check      │                           │
│       │    │ 2. billing reserve  │   Auth owns: identity     │
│       │    │ 3. provision        │   Billing owns: tiers,    │
│       │    │ 4. quota increment  │     plans, commercial     │
│       │    │ 5. billing commit   │                           │
│       │    └─────────────────────┘                           │
│                                                              │
│  Same pattern for:                                           │
│  - create_sandbox    (sandbox.concurrent + gpu_instances)    │
│  - create_browser    (sandbox.browser_sessions)              │
│  - create_desktop    (sandbox.desktop_sessions)              │
│  - stop_sandbox      (decrement sandbox.concurrent)          │
│  - delete_sandbox    (decrement if not already stopped)      │
└──────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│                   Resource Service                            │
│                                                              │
│  Same ab0t-quota engine, different resource keys:            │
│  - resource.cpu_cores      (gauge)                           │
│  - resource.allocations    (gauge)                           │
│  - resource.monthly_cost   (accumulator)                     │
└──────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│                    API Gateway                                │
│                                                              │
│  QuotaGuard middleware:                                      │
│  - api.requests_per_hour   (rate, sliding window)            │
│  - Tier-based: free=1K, starter=10K, pro=50K, enterprise=100K│
└──────────────────────────────────────────────────────────────┘
```

## Tier Lifecycle

```
Signup ──► Billing creates account (tier=free, no payment needed)
              │
              ▼
         Stripe checkout → payment_intent.succeeded
              │
              ▼
         Payment Service webhook handler
              │
              ▼
         Billing Service: PUT /{org_id}/tier
         Maps stripe price_id → tier_id (from config)
              │
              ├─► DynamoDB: billing account tier = new_tier
              ├─► Quota DynamoDB: ORG#{org_id} / TIER = new_tier
              └─► Redis: DELETE quota:tier:{org_id} (cache invalidation)
                       │
                       ▼
                  Consumer services (sandbox, resource, gateway)
                  call billing GET /{org_id}/tier (cached 5 min in Redis)
                  Quota checks use new limits within 5 min (or instantly
                  if cache was invalidated)

Cancellation ──► subscription.deleted webhook
                      │
                      ▼
                 Billing Service: PUT /{org_id}/tier = "free"
                 7-day grace period:
                   - Existing resources keep running
                   - New creation blocked if over free limits
                   - Email: "Plan downgraded, resources stop in 7 days"
                      │
                      ▼ (after 7 days)
                 Background worker stops over-limit resources

NOTE: Auth service is NOT involved in tier management.
Auth owns identity and permissions. Billing owns commercial/tier logic.
Each service in the mesh is a separate company with its own pricing —
auth's tiers (users/teams/API keys) are auth's own business.
```

## Counter Types

```
GAUGE (bidirectional — inc on create, dec on destroy)
├── Redis key: quota:{org_id}:{resource_key}:gauge
├── Per-user:  quota:{org_id}:{resource_key}:gauge:user:{user_id}
├── Operations: INCRBYFLOAT +1 / -1 (atomic)
├── Floor at 0 (prevents negative drift)
├── Use: concurrent sandboxes, CPU cores, active sessions
└── Reconciliation: compare actual count in DynamoDB vs gauge (every 15 min)

RATE (sliding window — auto-expires)
├── Redis key: quota:{org_id}:{resource_key}:rate (sorted set)
├── Members: timestamp-keyed entries
├── Auto-prune: ZREMRANGEBYSCORE on every read/write
├── TTL: window + 60s buffer
├── Use: API requests/hour, operations/minute
└── No reconciliation needed (self-healing via expiry)

ACCUMULATOR (monotonic within period — resets on calendar boundary)
├── Redis key: quota:{org_id}:{resource_key}:acc:{period_key}
├── Period key: "2026-03" (monthly), "2026-03-27" (daily)
├── TTL: period length + 1 day buffer
├── Cannot be decremented (TypeError)
├── Use: monthly spend (USD), daily data transfer
└── DynamoDB snapshot: persists across Redis restarts
```

## DynamoDB Access Patterns

### Table (PK/SK)

| # | Access Pattern | PK | SK | Op |
|---|---|---|---|---|
| 1 | Get org tier | `ORG#{org_id}` | `TIER` | GetItem |
| 2 | Set org tier | `ORG#{org_id}` | `TIER` | PutItem |
| 3 | Get override | `ORG#{org_id}` | `OVERRIDE#{resource_key}` | GetItem |
| 4 | List org overrides | `ORG#{org_id}` | `begins_with(OVERRIDE#)` | Query |
| 5 | Get counter snapshot | `ORG#{org_id}` | `COUNTER#{resource_key}` | GetItem |
| 6 | Snapshot counter | `ORG#{org_id}` | `COUNTER#{resource_key}` | PutItem |
| 7 | List all org data | `ORG#{org_id}` | `*` | Query |
| 8 | List increase requests | `ORG#{org_id}` | `begins_with(INCREASE#)` | Query |

### GSI1 (overloaded — cross-org queries by item type)

| # | Access Pattern | GSI1PK | GSI1SK | Op |
|---|---|---|---|---|
| 9 | Seed all counters (startup) | `COUNTER` | — | Query |
| 10 | List all overrides (admin) | `OVERRIDE` | — | Query |
| 11 | List all tiers (admin) | `TIER` | — | Query |

All hot-path reads are single-item `GetItem` (1 RCU). Cross-org admin queries use GSI1 Query. **No table scans.**
