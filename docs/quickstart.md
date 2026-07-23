# ab0t-quota — Quickstart for External Clients

Add quota enforcement, rate limiting, tier management, and the full
billing/payment surface (pricing pages, checkout, customer portal,
invoices) to your service in **one line of code**, **two env vars**,
and **one config file** — plus the infrastructure prerequisites in
`docs/requirements.md`. Verify your environment before deploying:
`python -m ab0t_quota preflight`.

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
  | **`byo_redis`** | <5 ms per check | A Redis instance (Upstash / ElastiCache / Fly.io — 2 min to provision, plus one on-the-record assertion for managed providers: see "Managed Redis providers" in §6) | Production. Anything in your hot request path. Rate-limit middleware. |

  Same code in your service. Same config. Mode is a deployment choice.

---

## 5-minute setup

### 0. Try it first — 2 commands, no account, no credential

Before any of the steps below, you can run the whole enforcement path locally:

```bash
python -m ab0t_quota provision --local   # a conforming local Redis
AB0T_QUOTA_OFFLINE=true python -m ab0t_quota preflight   # verifies it, contacts nothing
```

`provision --local` starts a Redis meeting every requirement the library enforces
at boot, and `preflight` re-runs those same checks read-only, printing what it
resolved and from where. **Neither needs a mesh credential and neither contacts
anything** — evaluate the library completely before step 1.

Steps 1-2 below are only needed for the **mesh commercial surface** (tiers synced
from billing, checkout, invoices, the customer portal). Quota enforcement itself
does not require them.

### 1. Get a mesh credential

Register your service with ab0t (one-time). You get back:
- `AB0T_MESH_API_KEY` — your service's mesh credential
- `AB0T_CONSUMER_ORG_ID` — your service's identity in the mesh

Set both as environment variables in your deployment.

> **How to register:** this is currently a manual step — contact ab0t
> (support@ab0t.com) to have your service registered and your credentials
> issued. There is no self-service portal yet. **If you only want to evaluate
> the library, skip this entirely and use step 0.**

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

  "storage": {
    "$_comment_redis_url": "REQUIRED for local/byo_redis modes — declare YOUR Redis here or via the QUOTA_REDIS_URL env var. No Redis? Use \"engine_mode\": \"bridge\" instead and delete this block.",
    "redis_url": "redis://your-redis:6379/0"
  },

  "tier_provider": { "type": "mesh", "default_tier": "free" },

  "resources": [
    {
      "service": "my-widget-service",
      "resource_key": "widget.concurrent",
      "display_name": "Concurrent Widgets",
      "action_hint": "Delete a widget to free up a slot.",
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

That's everything — provided the prerequisites in step 3 are real: the Redis
you declared (or bridge mode) must exist and meet `docs/requirements.md`.
No conforming Redis yet? `python -m ab0t_quota provision --local` starts a
verified local dev one (or `provision --emit compose|terraform|acl|iam` emits
the artifacts to apply yourself). `python -m ab0t_quota preflight` verifies
it all before you deploy, and `python -m ab0t_quota doctor` grades production
posture before go-live — full CLI in `docs/cli.md`. After this call:

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

For BYO-Redis, point the `storage.redis_url` at your managed Redis
in the config — engine-local mode against your own Redis instead of
ab0t's shared one.

**Managed Redis providers.** At boot the library machine-checks the Redis
it was given: topology (no Redis Cluster), eviction policy (must not evict
counter keys), Lua scripting, and a version floor. A check it cannot RUN
fails closed unless you put the matching assertion on the record:

| Provider reality | What the gate sees | The assertion you set |
|---|---|---|
| `CONFIG` disabled (ElastiCache, Upstash) | eviction policy unverifiable | set `maxmemory-policy noeviction` in the provider's parameter group, then `storage.redis_durability_confirmed: true` |
| `INFO cluster` / `CLUSTER INFO` unavailable | topology unverifiable | `storage.redis_cluster_confirmed_disabled: true` |
| `SCRIPT` command disabled/renamed (EVAL still works) | scripting unverifiable | `storage.redis_scripting_confirmed: true` |

An assertion never overrides a negative the server actually reported
(an observed cluster, an observed `allkeys-*` policy, a rejected script,
a below-floor version): those refusals have no flag, deliberately — fix
the Redis instead. (A rejected script is recognised by the server's
"Error compiling" message — a disclosed heuristic; if it ever misses,
the first counter operation fails closed, never open.) Run
`python -m ab0t_quota preflight` in CI to see every verdict before you
deploy.

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
- **No webhook signature code.** Stripe-signed events arrive at the
  library's `POST /api/webhooks/stripe` route; the library verifies the
  HMAC-SHA256 signature against `AB0T_QUOTA_STRIPE_WEBHOOK_SECRET`
  before forwarding to payment-service for processing. Forged or
  unsigned requests are rejected with 400 *before* leaving your edge.
  You configure the secret once; the library does the cryptography.
- **No tier policy in payment-service.** Paid-invoice events
  (`invoice.paid` and `invoice.payment_succeeded` — the lib accepts both)
  are dispatched into the lib's tier-aware handler, which reads your
  `credit_grant` config and grants accordingly. No tier IDs or amounts
  in payment-service code.
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

<!-- doc-exec: fragment (feature illustration — shows ONLY the cost-cap keys; merge into the step-3 config, which declares the store) -->
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
| `QUOTA_REDIS_URL` | yes for local/byo_redis, unless `storage.redis_url` is set in the config | — | The DECLARED Redis (0.7: never harvested, never invented; bridge mode needs neither) |
| `QUOTA_CONFIG_PATH` | no | `./quota-config.json` | Library auto-discovers |
| `AB0T_SERVICE_NAME` | no | from config or first resource's `service` field | Identity for the catalog publish |
| `AB0T_MESH_BILLING_URL` | no — local dev only | `https://billing.service.ab0t.com` | Override for testing against local stack |
| `AB0T_MESH_PAYMENT_URL` | no — local dev only | `https://payment.service.ab0t.com` | Same |
| `AB0T_MESH_SNS_LIFECYCLE_TOPIC_ARN` | no — production sets via mesh defaults | — | LocalStack ARN for dev |
| `AB0T_QUOTA_STRIPE_WEBHOOK_SECRET` | no (required for Stripe webhook routes) | — | HMAC-SHA256 secret from your Stripe Dashboard webhook endpoint config. When set, the library's `POST /api/webhooks/stripe` route verifies the `Stripe-Signature` header before forwarding to payment-service. Missing/forged signatures rejected with 400. **If unset, the route 503s** — Stripe Dashboard cutover MUST set this. |
| `AB0T_AUTH_WEBHOOK_SECRET` | no (required for auth-event handlers) | — | HMAC secret. When set, lib mounts `POST /api/quotas/_webhooks/auth` and dispatches received events to handlers you register via `@on_auth_event` / `register_handler`. With `enable_paid=True`, the lib AUTO-REGISTERS a default signup-credit handler that reads `tier.credit_grant` (signup trigger) and `tier.initial_credit` (legacy shim) — so a consumer with config alone gets initial-credit grants without writing any handler code. Consumer-registered handlers coexist with the default; the lib dedups via Redis flag + billing idempotency_key. |
| `AB0T_AUTH_ADMIN_TOKEN` | no (required for auto-subscribe) | — | Bearer token with `events.subscribe` permission on auth. When set with `AB0T_AUTH_WEBHOOK_PUBLIC_URL`, lib auto-registers the subscription with auth at startup (idempotent). |
| `AB0T_AUTH_WEBHOOK_PUBLIC_URL` | no (required for auto-subscribe) | — | Externally-reachable base URL of this service. Auth POSTs events to `<this>/api/quotas/_webhooks/auth`. |
| `AB0T_AUTH_WATCH_ORG_SLUG` | no | from `AB0T_AUTH_ORG_SLUG` | Auth org slug to filter events for. Resolved to org_id at subscribe time. |

**Two required env vars for the mesh surface** — plus the storage
prerequisites in `docs/requirements.md` (declared Redis or bridge mode; DDB
tables pre-created or opted in), which `python -m ab0t_quota preflight`
verifies before you deploy. Compare to a typical hand-rolled integration:
6+ URLs/keys/ARNs across multiple service clients.

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
