# Adding Billing to a Mesh Service

## What You Get

`ab0t-quota[billing]` gives your service drop-in billing with:

- **Budget checks** — reserve funds before provisioning, reject 402 if insufficient
- **Proration** — charge per-minute, refund unused time on stop/delete
- **Lifecycle events** — emit resource.started/stopped/deleted, billing service handles the rest
- **Heartbeat monitoring** — detect crashed resources, auto-prorate
- **Promotional credits** — apply trial credits, credit-first deduction
- **20 payment routes** — checkout, portal, subscriptions, invoices, payment methods, webhooks

## Quick Start

### 1. Add pricing to `quota-config.json`

```json
{
  "pricing": {
    "currency": "USD",
    "billing_model": "per_minute",
    "min_billing_seconds": 60,
    "refund_on_stop": true,
    "surge": {
      "enabled": true,
      "multiplier": 1.5,
      "description": "Applied when enterprise exceeds tier allotment"
    },
    "products": {
      "my_resource": {
        "display_name": "My Resource",
        "description": "What this resource does",
        "variants": {
          "default": {
            "compute": "fargate",
            "cpu": 1024,
            "memory": 2048,
            "cost_per_hour": 0.049,
            "price_per_hour": 0.10,
            "allocation_cost": 0.002,
            "allocation_price": 0.01,
            "default": true
          }
        }
      }
    }
  }
}
```

### 2. Initialize at startup

```python
from ab0t_quota.billing import (
    BillingServiceClient, BudgetChecker,
    LifecycleEmitter, HeartbeatMonitor, load_pricing,
    create_billing_router,
)

# Load pricing from config
pricing = load_pricing("quota-config.json")

# Create clients
billing = BillingServiceClient(
    base_url=os.getenv("BILLING_SERVICE_URL"),
    api_key=os.getenv("BILLING_SERVICE_API_KEY"),
)
budget = BudgetChecker(billing, pricing)
emitter = LifecycleEmitter()  # reads SNS_LIFECYCLE_TOPIC_ARN from env
monitor = HeartbeatMonitor(redis=redis_client, emitter=emitter)

# Mount billing/payment routes (checkout, portal, invoices, etc.)
app.include_router(create_billing_router(
    payment_url=os.getenv("PAYMENT_SERVICE_URL"),
    payment_api_key=os.getenv("PAYMENT_SERVICE_API_KEY"),
    billing_url=os.getenv("BILLING_SERVICE_URL"),
    billing_api_key=os.getenv("BILLING_SERVICE_API_KEY"),
    consumer_org_id=os.getenv("CONSUMER_ORG_ID"),
))

# Start heartbeat monitor
asyncio.create_task(monitor.start())
```

### 3. Wire into your resource lifecycle

```python
# BEFORE provisioning:
reservation_id = await budget.pre_launch_check(
    org_id, user_id, product_or_instance="my_resource",
)

# Store reservation_id on your resource record
resource.reservation_id = reservation_id
await db.save(resource)

# AFTER provisioning succeeds:
costs = budget.get_costs("my_resource")
await emitter.resource_started(
    org_id=org_id, user_id=user_id,
    resource_id=resource.id, resource_type="my_resource",
    reservation_id=reservation_id,
    hourly_rate=costs["hourly_rate"],
    allocation_fee=costs["allocation_fee"],
    started_at=resource.created_at,
    reason="provisioned",
)

# ON FAILURE:
await budget.on_failure(org_id, reservation_id)

# ON STOP/DELETE:
await emitter.resource_stopped(
    org_id=org_id, user_id=user_id,
    resource_id=resource.id, resource_type="my_resource",
    reservation_id=resource.reservation_id,
    hourly_rate=costs["hourly_rate"],
    allocation_fee=costs["allocation_fee"],
    started_at=resource.created_at,
    reason="user_stopped",
)

# HEARTBEATS (from your background cost tracker):
await monitor.record(resource.id, {
    "org_id": org_id, "user_id": user_id,
    "reservation_id": resource.reservation_id,
    "hourly_rate": str(costs["hourly_rate"]),
    "allocation_fee": str(costs["allocation_fee"]),
    "started_at": resource.created_at.isoformat(),
    "resource_type": "my_resource",
})
```

### 4. That's it

The billing service receives lifecycle events and automatically:
- Calculates prorated cost (allocation fee + per-minute runtime)
- Commits the reservation with actual cost
- Refunds the unused portion
- Deducts from promotional credits first, then cash

---

## Pricing Config Schema

```
pricing.currency              — "USD" (string)
pricing.billing_model         — "per_minute" | "per_hour" | "per_second"
pricing.min_billing_seconds   — 60 (minimum charge unit)
pricing.refund_on_stop        — true (refund unused time)
pricing.surge.enabled         — true (surge pricing for over-allotment)
pricing.surge.multiplier      — 1.5 (multiplier when over tier limits)

pricing.products.{product_id}
  .display_name               — "Browser" (shown to users)
  .description                — "Cloud browser" (shown to users)
  .variants.{variant_name}
    .compute                  — "fargate" | "fargate_pool" | "ec2" | "ec2_gpu" | "eks"
    .cpu                      — 1024 (CPU units, 1024 = 1 vCPU)
    .memory                   — 2048 (MB)
    .cost_per_hour            — 0.049 (INTERNAL: what we pay AWS)
    .price_per_hour           — 0.10  (CUSTOMER: what we charge)
    .allocation_cost          — 0.002 (INTERNAL: provisioning cost)
    .allocation_price         — 0.01  (CUSTOMER: one-time fee)
    .default                  — true (default variant for this product)
```

**cost_* fields are internal** — never exposed to customers.
**price_* fields are customer-facing** — shown in UI, used for billing.
Margin = price - cost.

---

## Lifecycle Event Schema (SNS)

```json
{
  "event_type": "resource.started | resource.stopped | resource.deleted | resource.heartbeat",
  "org_id": "string (required)",
  "user_id": "string",
  "resource_id": "string (required)",
  "resource_type": "string (required) — product ID",
  "reservation_id": "string | null",
  "instance_type": "string | null",
  "hourly_rate": "string | null — customer price per hour",
  "allocation_fee": "string | null — customer allocation price",
  "started_at": "ISO 8601 datetime | null",
  "stopped_at": "ISO 8601 datetime | null",
  "reason": "string — why this event occurred",
  "metadata": "object — custom fields",
  "emitted_at": "ISO 8601 datetime"
}
```

**Reasons:** `provisioned`, `user_stopped`, `user_deleted`, `user_restarted`,
`idle_timeout`, `max_runtime_exceeded`, `heartbeat_timeout`,
`released_to_pool`, `launch_failed`

**SNS MessageAttributes:** `event_type` (String), `resource_type` (String)

---

## Billing API Contract

### Reserve Funds
```
POST /billing/{org_id}/reserve
Body: {org_id, user_id, tool_id, estimated_cost, session_id, operation_type, metadata}
200: {reservation_id}
402: {error: "insufficient_balance", available_balance, requested_amount}
```

### Commit Reservation
```
POST /billing/{org_id}/commit
Body: {reservation_id, actual_usage}
200: {committed}
```

### Refund Reservation
```
POST /billing/{org_id}/refund
Body: {reservation_id, reason}
200: {refunded}
```

### Get Balance
```
GET /billing/{org_id}/balance
200: {balance, credit_balance, reserved_balance, available_balance, currency}
```

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `BILLING_SERVICE_URL` | Yes | Billing service URL |
| `BILLING_SERVICE_API_KEY` | Yes | API key for billing service |
| `PAYMENT_SERVICE_URL` | Yes | Payment service URL |
| `PAYMENT_SERVICE_API_KEY` | Yes | API key for payment service |
| `SNS_LIFECYCLE_TOPIC_ARN` | Yes | SNS topic for lifecycle events |
| `AWS_ENDPOINT_URL` | Dev only | LocalStack endpoint |
| `AWS_REGION` | No | AWS region (default: from topic ARN) |

---

## Architecture

```
Your Service                    Billing Service              Payment Service
    │                                │                            │
    ├── BudgetChecker               │                            │
    │   └── reserve_funds ─────────→│ reserve                    │
    │                                │                            │
    ├── LifecycleEmitter             │                            │
    │   └── resource.stopped ──SNS──→│ lifecycle_consumer          │
    │                                │   └── prorate + commit     │
    │                                │                            │
    ├── HeartbeatMonitor             │                            │
    │   └── stale → resource.stopped │                            │
    │                                │                            │
    ├── create_billing_router ──────→│ balance/usage/transactions │
    │                         ──────→│                     ──────→│ checkout/portal
    │                                │                            │
```

Billing service is generic — commits amounts, tracks balances.
Your service owns pricing, proration timing, and resource health.
