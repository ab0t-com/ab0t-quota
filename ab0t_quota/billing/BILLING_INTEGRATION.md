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

# Create clients.
# `service_name` is YOUR mesh service identity — it becomes the default
# `tool_id` on metering rows. Omit it only if you always pass `tool_id=`
# explicitly or you export AB0T_SERVICE_NAME; `record_resource_usage`
# REFUSES (ValueError + ERROR log) rather than guess a name.
billing = BillingServiceClient(
    base_url=os.getenv("BILLING_SERVICE_URL"),
    api_key=os.getenv("BILLING_SERVICE_API_KEY"),
    service_name=os.getenv("AB0T_SERVICE_NAME", "my-service"),
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
# Accepts a product id, a variant name, or the qualified "product:variant"
# form. Use the qualified form when two products share a variant name —
# ambiguous bare names are refused rather than mispriced.
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

---

## Billing relationships — choosing a model for your tier

ab0t-quota supports multiple SaaS billing archetypes via declarative
config per tier. The library implements primitives (reservation,
commit, debit, credit grant, counter); consumers declare which model
applies to each tier in their `quota-config.json`.

### Schema (TierConfig fields, all optional)

```jsonc
{
  "tier_id": "starter",
  "billing_model": "subscription_with_credits",
  "price": {
    "amount_per_period": 10.00,
    "currency": "USD",
    "period": "month"
  },
  "credit_grant": {
    "trigger": "subscription_invoice_paid",
    "amount_per_period": 10.00,
    "currency": "USD",
    "lifecycle": "use_it_or_lose_it",
    "destination": "subscription_credit",
    "reset_on_downgrade": true,
    "reset_on_upgrade": false
  },
  "limits": { ... }
}
```

### Supported `billing_model` values

| Value | Archetype | When to use |
|---|---|---|
| `capacity_only` *(default)* | Pure quota tier | Free tier with limits only, or paid capacity-unlock that grants no credit |
| `consumption_only` | Pay-as-you-go | No subscription concept; user tops up via Stripe Checkout |
| `subscription_with_credits` | Bundled credit | Most consumer SaaS: paying $X/mo grants $Y of credit per period (Y typically equals or exceeds X) |
| `subscription_unlock_only` | Capacity unlock | Subscription pays for higher LIMITS; user tops up separately for spending |
| `subscription_with_overage` | Hybrid | Subscription includes $X; usage beyond auto-charges card (NOT YET IMPLEMENTED) |
| `seat_based` | Per-user | Price scales with active users (NOT YET IMPLEMENTED) |
| `metered` | Usage-based | Stripe `usage_record` push per period (NOT YET IMPLEMENTED) |

### Credit grant triggers

| `trigger` | Fires when | Use case |
|---|---|---|
| `signup` | `auth.user.registered` webhook | One-shot signup grant. Replaces the legacy `initial_credit` field. |
| `subscription_invoice_paid` | Stripe `invoice.paid` OR `invoice.payment_succeeded` (with `subscription_data.metadata.org_id` set) | Period grant on every successful subscription invoice. The lib accepts both event types since Stripe emits one or both depending on API version (see WEBHOOK_AND_CREDIT_GRANT_ARCHITECTURE.md). |
| `scheduled_period_start` | Cron at start of each billing period | Alternative when Stripe isn't the source of truth. |
| `manual` | Admin POST to `/promotional-credit` | Referrals, support compensation. |
| `webhook_admin` | Custom event webhook | Consumer-defined triggers. |

### Lifecycle options

| `lifecycle` | Behavior | Example use |
|---|---|---|
| `persistent` | ADD amount; never expires | Signup grant ($10 free credit) |
| `use_it_or_lose_it` | SET to amount; previous remainder forfeit | Standard monthly subscription credit |
| `rollover_unlimited` | ADD amount; carry forward indefinitely | Annual prepay |
| `rollover_capped` | ADD amount, cap at `rollover_max_periods × amount_per_period` | Enterprise (e.g., 3 months max rollover) |

### Ledger destination

Three balance fields on `BillingAccount`. Each grant lands in exactly one:

| `destination` | Field | Lifecycle implication |
|---|---|---|
| `balance` | User cash (refundable). Topped up via Stripe Checkout `type=account_funding`. |
| `credit_balance` | Promo/signup grants. Non-refundable, typically persistent. |
| `subscription_credit` | Subscription-bundled credit. Period-bounded per `lifecycle`. Tracks provenance via `subscription_credit_source` + `_source_tier` + `_granted_at` pointers on the account. |

Spend order at commit time: `subscription_credit` → `credit_balance` → `balance`. Use-it-or-lose-it drains before promo; promo drains before user cash.

### Endpoints added for credit-grant management

| Endpoint | Purpose |
|---|---|
| `POST /billing/{org_id}/apply-credit-grant` | Apply a grant per the consumer-declared lifecycle. Idempotent on `idempotency_key` (typically the source invoice ID). |
| `POST /billing/{org_id}/reset-subscription-credit` | Zero `subscription_credit` after a tier downgrade, with `expected_source_tier` safety check (rejects 409 if the recorded credit came from a different tier — protects multi-sub orgs). |
| `POST /billing/{org_id}/promotional-credit` | Legacy one-shot promo credit. Kept for back-compat; new consumers should use `/apply-credit-grant` with `destination=credit_balance`. |

### Library helpers (in `ab0t_quota.billing.subscription_credit`)

| Function | Purpose |
|---|---|
| `handle_subscription_invoice_paid(invoice, ...)` | Webhook receiver for Stripe paid-invoice events (`invoice.paid` and `invoice.payment_succeeded`). Routes to `apply_credit_grant` per the org's tier config. Idempotent on `invoice:{id}:credit_grant` so accounts that emit both event types do not double-credit. |
| `reset_subscription_credit_on_tier_change(org_id, old_tier_id, new_tier_id, ...)` | Library helper: detects downgrade via `sort_order`, checks `reset_on_downgrade` policy, calls reset endpoint with safety check. |

### Required wiring for `subscription_with_credits` to function end-to-end

1. **`payment-service`**: subscription-mode Stripe Checkout sessions must set `subscription_data.metadata = {"org_id": org_id, "plan_id": plan_id}` so the metadata propagates to subscription + invoice records.
2. **`ab0t-quota`** (your service): wire `handle_subscription_invoice_paid` as a webhook receiver for Stripe paid-invoice events (`invoice.paid` and `invoice.payment_succeeded`; the lib accepts both). Delivery mechanism is consumer-specific — typically payment-service forwards to a consumer endpoint, or via SNS event mesh.
3. **Tier config**: `billing_model: "subscription_with_credits"` + `price` + `credit_grant` (with `trigger: "subscription_invoice_paid"`) in your `quota-config.json`.

### Observability — structured log events

The library emits the following keys via `structlog`. Wire alerts/dashboards to these:

| Event key | Severity | Meaning |
|---|---|---|
| `subscription_invoice_paid_applied` | INFO | Successful credit grant landed |
| `subscription_invoice_paid_skip` | INFO | Grant skipped (no metadata / no tier / no credit_grant configured / wrong trigger) — expected, not an error |
| `subscription_invoice_paid_transient` | WARNING | Billing-service returned 5xx / 429; retry-eligible |
| `subscription_invoice_paid_failed_permanent` | ERROR | Billing-service returned 4xx; investigation needed |
| `credit_grant_applied` | INFO | (billing-service) Credit landed; logs destination + lifecycle + amount + forfeit |
| `reset_subscription_credit_applied` | INFO | Downgrade reset succeeded |
| `reset_subscription_credit_tier_mismatch` | WARNING | Reset rejected by safety check (recorded credit belongs to different tier) |
| `downgrade_reset_applied` | INFO | (library helper) Reset succeeded |
| `downgrade_reset_skipped_safety_check` | INFO | (library helper) Safety check rejected; expected behavior in multi-sub orgs |
| `lifecycle_commit_lost_to_expiry` | ERROR | **Revenue loss**: reservation expired before commit could fire. Alertable. |

The `lifecycle_commit_lost_to_expiry` key in particular should fire a paging alert — it signals a usage event that wasn't billed.

### Future architecture: credit-entries table (multi-source / per-grant expiry)

The current `subscription_credit` field tracks a single provenance pointer per account (Option B in the parent design doc). This works for single-subscription-per-org and basic downgrade-reset.

When the product needs:
- Multiple subscriptions on one org with independent credit pools
- Per-grant expiry dates that differ from the subscription period
- Selective refund: clawback ONLY the credit from a specific cancelled subscription

…migrate to a credit-entries table (one item per grant). The public endpoints (`apply-credit-grant`, `reset-subscription-credit`) stay the same — only the internal representation changes.

A consumer-side migration ticket exists for this future work; the public contract (the `apply-credit-grant` + `reset-subscription-credit` endpoints) does not change when migrating.
