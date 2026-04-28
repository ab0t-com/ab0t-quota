# ab0t-quota — Quickstart for External Clients

Add quota enforcement, rate limiting, tier management, and the full
billing/payment surface (pricing pages, checkout, customer portal,
invoices) to your service in **one line of code**, **two env vars**,
and **one config file**.

---

## What you need to know first

- **You** are a service that wants to enforce per-customer quotas (sandbox
  counts, API requests, monthly spend, anything you can count) against
  named tiers (free / starter / pro / enterprise — or whatever you call
  them).
- **ab0t-quota** is a Python library that does the enforcement. It talks
  to the ab0t mesh on your behalf for tier resolution and (optionally)
  payment/checkout. You never call the mesh directly.
- **Two ways to deploy** — pick based on your latency budget:

  | Mode | Latency | What you provision | Right when |
  |---|---|---|---|
  | **`bridge`** | 10–100 ms per check | Nothing. HTTPS-only. | Prototyping. Low-volume per-org checks. Cross-cloud / cross-region from ab0t. |
  | **`byo_redis`** | <5 ms per check | A Redis instance (Upstash / ElastiCache / Fly.io — 2 min to provision) | Production. Anything in your hot request path. Rate-limit middleware. |

  Same code in your service. Same config. Mode is a deployment choice.

---

## 5-minute setup

### 1. Get a mesh credential

Register your service with ab0t (one-time). You get back:
- `AB0T_MESH_API_KEY` — your service's mesh credential
- `AB0T_CONSUMER_ORG_ID` — your service's identity in the mesh

Set both as environment variables in your deployment.

### 2. Install the library

```bash
pip install "ab0t-quota[all] @ git+https://github.com/ab0t-com/ab0t-quota.git"
```

The `[all]` extra includes the optional billing/payment proxy router.
Skip it (`pip install ab0t-quota`) if you only need quota enforcement.

### 3. Drop a `quota-config.json` next to your service

Minimal example — replace `widget` with your domain:

```json
{
  "service_name": "my-widget-service",

  "tier_provider": { "type": "mesh", "default_tier": "free" },

  "resources": [
    {
      "service": "my-widget-service",
      "resource_key": "widget.concurrent",
      "display_name": "Concurrent Widgets",
      "counter_type": "gauge",
      "unit": "widgets"
    },
    {
      "service": "my-widget-service",
      "resource_key": "api.requests_per_hour",
      "display_name": "API Requests / Hour",
      "counter_type": "rate",
      "unit": "requests",
      "window_seconds": 3600
    }
  ],

  "resource_bundles": {
    "widget": ["widget.concurrent"]
  },

  "tiers": [
    {
      "tier_id": "free",
      "display_name": "Free",
      "sort_order": 0,
      "limits": { "widget.concurrent": 1, "api.requests_per_hour": 1000 }
    },
    {
      "tier_id": "pro",
      "display_name": "Pro",
      "sort_order": 1,
      "default_per_user_fraction": 0.5,
      "limits": { "widget.concurrent": 25, "api.requests_per_hour": 50000 },
      "upgrade_url": "/billing/upgrade"
    }
  ]
}
```

See the full schema in [`quota-config.example.json`](../quota-config.example.json).

### 4. Wire it into your FastAPI app

```python
from fastapi import FastAPI
from ab0t_quota import setup_quota

app = FastAPI()
setup_quota(app)        # one line. Done.
```

That's everything. After this call:

- `/api/quotas/usage`, `/api/quotas/tiers`, `/api/quotas/check/{key}`,
  `/api/quotas/check-bundle/{name}` are mounted
- `QuotaGuard` rate-limit middleware enforces `api.requests_per_hour`
- `/api/billing/*` and `/api/payments/*` routes (pricing, checkout,
  customer portal, invoices, webhooks) are mounted via the library
- App lifespan composed: engine warm-up, snapshot worker, clean teardown
- A `QuotaContext` is published on `app.state.quota` for in-route use

### 5. Use it in your routes

```python
from fastapi import Request, HTTPException

@app.post("/widgets")
async def create_widget(request: Request, user):
    quota = request.app.state.quota

    # Pre-flight check (raises 429 if denied)
    await quota.check_bundle(user.org_id, "widget", user_id=user.user_id)

    # Provision your resource
    widget = await provision_widget(...)

    # After success, increment the counter
    await quota.increment_bundle(user.org_id, "widget", user_id=user.user_id)
    return widget

@app.delete("/widgets/{widget_id}")
async def delete_widget(widget_id: str, request: Request, user):
    await actually_delete(widget_id)
    quota = request.app.state.quota
    await quota.decrement_bundle(user.org_id, "widget", user_id=user.user_id)
    return {"ok": True}
```

When a customer hits their tier limit, your endpoint automatically
returns:

```json
{
  "error": "quota_exceeded",
  "resource": "widget.concurrent",
  "current": 1, "limit": 1,
  "tier": "free", "tier_display": "Free",
  "message": "You've reached the maximum of 1 widgets on the Free plan. Upgrade to Pro for a higher limit.",
  "upgrade_url": "/billing/upgrade"
}
```

Designed for end-user display — no technical jargon, includes upgrade
hint and link.

### 6. (Optional) Pick deployment mode

Default is engine-local. For BYO-Redis or bridge mode, add to
`quota-config.json`:

```json
{ "engine_mode": "bridge" }
```

Or pass at the call site:

```python
setup_quota(app, mode="bridge")
```

For BYO-Redis, just point the `storage.redis_url` at your managed Redis
in the config — engine-local mode against your own Redis instead of
ab0t's shared one.

---

## End-user-facing pages you get for free

The library mounts these — you don't write any of it:

| URL | What it is |
|---|---|
| `GET /api/payments/plans` | Public pricing data |
| `POST /api/payments/checkout/{plan_id}` | Authenticated subscription checkout |
| `POST /api/payments/checkout/anonymous/{plan_id}` | Account-first anonymous checkout (account created before Stripe redirect → captures lead even if checkout abandoned) |
| `POST /api/payments/topup` | One-time balance top-up |
| `POST /api/payments/portal` | Stripe Customer Portal session |
| `GET /api/payments/subscriptions` | List subscriptions |
| `DELETE /api/payments/subscriptions/{id}` | Cancel subscription |
| `GET /api/payments/invoices` | List invoices |
| `GET /api/payments/invoices/{id}/pdf` | Invoice PDF download |
| `GET /api/payments/methods` | List saved payment methods |
| `PUT /api/payments/methods/{id}/default` | Set default payment method |
| `DELETE /api/payments/methods/{id}` | Remove payment method |
| `GET /api/billing/balance` | Account balance |
| `GET /api/billing/usage/summary` | Usage summary for the period |
| `GET /api/billing/usage/records` | Detailed usage records |
| `GET /api/billing/transactions` | Transaction history |
| `POST /api/webhooks/stripe` | Stripe webhook receiver |
| `GET /checkout/success` | Post-checkout success page |

Build whatever frontend you want against these. Your customers never
talk to ab0t directly — they hit your service, your service hits the
mesh.

---

## What you DON'T have to do

- **No `BillingClient` to write.** The library generates and serves all
  routes.
- **No Stripe code.** Stripe runs inside the ab0t payment service. You
  see `POST /api/payments/checkout/{plan_id}` returning a redirect URL.
- **No webhook signing.** The webhook proxy forwards to ab0t and
  verifies signatures upstream.
- **No PCI scope.** Card numbers never touch your service.
- **No tier definitions in code.** Edit `quota-config.json` (no deploy
  needed, library publishes the new catalog to ab0t on next startup).
- **No upstream URLs in your env.** One credential
  (`AB0T_MESH_API_KEY`), library resolves URLs internally.
- **No counter implementation.** Library handles gauges, rates,
  accumulators, sliding windows, idempotency, per-user partitions.

---

## Per-user fairness out of the box

Set `default_per_user_fraction` on a tier and one user can never
exhaust the org's entire quota. Example:

```json
{
  "tier_id": "starter",
  "default_per_user_fraction": 0.5,
  "limits": { "widget.concurrent": 10 }
}
```

Each user is automatically capped at 5 (`ceil(10 * 0.5)`). Override
per-resource via `per_user_limit` if you need different ratios.

---

## Resource bundles — declarative dispatch

Don't write `if instance_type.startswith("g")` heuristics in your
routes. Declare bundles in config:

```json
"resource_bundles": {
  "widget":         ["widget.concurrent"],
  "premium_widget": ["widget.concurrent", "widget.premium_slots"]
}
```

Then dispatch by name:

```python
await quota.check_bundle(user.org_id, "premium_widget" if is_premium else "widget")
```

Library batch-checks both resources for the premium case, single-checks
for normal. No branching logic in your routes.

---

## Cost cap auto-enforcement

If you charge customers per resource-hour, declare a cost accumulator
and the library auto-records on resource teardown:

```json
{
  "billing_integration": { "cost_resource_key": "widget.monthly_cost" },
  "resources": [
    { "resource_key": "widget.monthly_cost",
      "counter_type": "accumulator",
      "reset_period": "monthly",
      "unit": "USD",
      "precision": 2,
      "service": "my-widget-service",
      "display_name": "Monthly Cost"
    }
  ],
  "tiers": [
    { "tier_id": "free", "limits": { "widget.monthly_cost": 10.00 } }
  ],
  "pricing": {
    "products": {
      "widget": {
        "display_name": "Widget",
        "variants": {
          "default": {
            "price_per_hour": 0.10,
            "allocation_price": 0.01,
            "default": true
          }
        }
      }
    }
  }
}
```

Then in your code:

```python
from ab0t_quota.billing.lifecycle import LifecycleEmitter
emitter: LifecycleEmitter = app.state.quota_emitter   # set by setup_quota

# When the widget is provisioned:
await emitter.resource_started(
    org_id=user.org_id, user_id=user.user_id,
    resource_id=widget.id, resource_type="widget",
    hourly_rate=Decimal("0.10"), allocation_fee=Decimal("0.01"),
    started_at=widget.created_at,
    reason="provisioned",
)

# When the widget is stopped:
await emitter.resource_stopped(
    org_id=user.org_id, user_id=user.user_id,
    resource_id=widget.id, resource_type="widget",
    hourly_rate=Decimal("0.10"), allocation_fee=Decimal("0.01"),
    started_at=widget.created_at, stopped_at=widget.stopped_at,
    reason="user_stopped",
)
```

The library:
1. Computes `cost = duration × hourly_rate + allocation_fee`
2. Increments `widget.monthly_cost` accumulator
3. Publishes a `resource.stopped` event for billing proration

When `widget.monthly_cost` hits the tier cap, the next
`quota.check(...)` for that resource returns 429.

---

## Configuration env vars

The full set you need:

| Variable | Required | Default | Notes |
|---|---|---|---|
| `AB0T_MESH_API_KEY` | yes | — | Your mesh credential |
| `AB0T_CONSUMER_ORG_ID` | yes (paid mode) | — | Your service's mesh org UUID |
| `QUOTA_CONFIG_PATH` | no | `./quota-config.json` | Library auto-discovers |
| `AB0T_SERVICE_NAME` | no | from config or first resource's `service` field | Identity for the catalog publish |
| `AB0T_MESH_BILLING_URL` | no — local dev only | `https://billing.service.ab0t.com` | Override for testing against local stack |
| `AB0T_MESH_PAYMENT_URL` | no — local dev only | `https://payment.service.ab0t.com` | Same |
| `AB0T_MESH_SNS_LIFECYCLE_TOPIC_ARN` | no — production sets via mesh defaults | — | LocalStack ARN for dev |
| `AB0T_AUTH_WEBHOOK_SECRET` | no (required for auto-credit-grant) | — | HMAC secret. When set, lib mounts `POST /api/quotas/_webhooks/auth` and grants `tier.initial_credit` on `auth.user.registered`. Operator runs `python -m ab0t_quota subscribe-events` once per env to register the subscription with auth. |

That's it. **Two required env vars.** Compare to a typical
hand-rolled integration: 6+ URLs/keys/ARNs across multiple service clients.

---

## What's next

- **API reference** — `docs/mesh-quota-api.md` — full wire protocol for
  bridge mode and the mesh quota API
- **Architecture** — `ARCHITECTURE.md` — how the library, billing
  service, and mesh fit together
- **Config schema** — `quota-config.example.json` — every field with
  inline comments
- **Why we built it this way** — `dev/ARCHITECTURE_LEARNINGS_20260425.md`
  — the design rationale and the three deployment modes

---

## Get help

- File issues at https://github.com/ab0t-com/ab0t-quota
- Onboarding questions → mesh team
- Tier / pricing questions → ab0t.com/billing
