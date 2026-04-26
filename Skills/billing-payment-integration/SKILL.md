---
name: billing-payment-integration
description: Add billing, payments, and subscriptions to any mesh service using the billing (8002) and payment (8005) drop-in system. Use when a service needs to charge customers, manage subscriptions, accept payments, show invoices, manage payment methods, or implement usage-based billing. Covers the full integration from consumer registration through proxy routes, HTML buttons, Stripe Checkout, Customer Portal, top-ups, and webhook forwarding. No custom Stripe code needed on the consumer side.
---

# Billing & Payment Integration

Add billing, payments, and subscriptions to any mesh service using the ab0t billing (port 8002) and payment (port 8005) services.

## Architecture

```
End User (browser)
    │
    ├── /pricing page         → GET /api/payments/plans (public)
    ├── "Subscribe" button    → POST /api/payments/checkout/{plan_id} → Stripe Checkout
    ├── "Top Up" button       → POST /api/payments/topup → Stripe Checkout (mode=payment)
    ├── "Manage Billing"      → POST /api/payments/portal → Stripe Customer Portal
    │
    ↓ (all go through your service proxy)
    │
Your Service (proxy layer)
    │── Authenticates user via JWT
    │── Injects org_id from JWT (prevents spoofing)
    │── Calls payment/billing service with service API key
    │
    ├──→ Payment Service (8005)
    │    ├── Stripe Checkout sessions
    │    ├── Customer Portal sessions
    │    ├── Payment methods (SetupIntent + CRUD)
    │    ├── Subscriptions, invoices
    │    └── Webhooks from Stripe
    │
    └──→ Billing Service (8002)
         ├── Balance management (credit/debit/reserve/commit)
         ├── Tier management (free/starter/pro/enterprise)
         ├── Usage tracking
         └── Transaction history
```

## Quick Start (3 Steps)

### Step 1: Register as Consumer

```bash
cd your-service/setup
./setup run 07  # Register as payment + billing consumer
```

This creates:
- A consumer org under each provider
- A service account with API key
- Scoped permissions (payment.read, payment.write, payment.cross_org, etc.)

Save the API keys to your `.env`:
```
PAYMENT_SERVICE_URL=http://host.docker.internal:8005
PAYMENT_SERVICE_API_KEY=ab0t_sk_live_...
BILLING_SERVICE_URL=http://host.docker.internal:8002
BILLING_SERVICE_API_KEY=ab0t_sk_live_...
PAYMENT_CONSUMER_ORG_ID=<your consumer org UUID>
```

### Step 2: Add Proxy Routes

Add these routes to your FastAPI app. All follow the same pattern: authenticate user via JWT, inject org_id, call upstream with service API key.

```python
# Public — no auth needed (pricing page)
@app.get("/api/payments/plans")
async def get_plans(request: Request):
    consumer_org = os.getenv("PAYMENT_CONSUMER_ORG_ID")
    return await payment_client.get_plans(consumer_org, provider_org=consumer_org)

# Authenticated — subscribe to a plan
@app.post("/api/payments/checkout/{plan_id}")
async def create_checkout(request: Request, plan_id: str, user: AuthenticatedUser):
    base = str(request.base_url).rstrip("/")
    return await payment_client.create_checkout_session(
        user.org_id, plan_id,
        success_url=f"{base}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{base}/pricing?cancelled=true",
    )

# Authenticated — top up balance
@app.post("/api/payments/topup")
async def create_topup(request: Request, user: AuthenticatedUser, amount: float = Body(...)):
    base = str(request.base_url).rstrip("/")
    return await payment_client.create_topup_session(
        user.org_id, amount,
        success_url=f"{base}/billing?topup=success",
        cancel_url=f"{base}/billing?topup=cancelled",
    )

# Authenticated — Stripe Customer Portal (manage cards, invoices, subscriptions)
@app.post("/api/payments/portal")
async def create_portal(request: Request, user: AuthenticatedUser):
    base = str(request.base_url).rstrip("/")
    return await payment_client.create_portal_session(user.org_id, return_url=f"{base}/billing")

# Unauthenticated — Stripe webhook forwarding
@app.post("/api/webhooks/stripe")
async def stripe_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("stripe-signature", "")
    return await payment_client.forward_webhook(body, signature)
```

### Step 3: Add Buttons to Your HTML

```html
<!-- Pricing page: "Subscribe" -->
<button onclick="subscribe('pro')">Get Pro</button>

<!-- Billing page: "Top Up" -->
<button onclick="topUp()">Top Up Balance</button>

<!-- Billing page: "Manage Cards & Invoices" -->
<button onclick="manageCards()">Manage Billing</button>

<script>
async function subscribe(planName) {
    const plans = await fetch('/api/payments/plans').then(r => r.json());
    const plan = plans.plans.find(p => p.name.toLowerCase().includes(planName));
    const session = await fetch('/api/payments/checkout/' + plan.plan_id, {
        method: 'POST', credentials: 'same-origin'
    }).then(r => r.json());
    window.location.href = session.url;
}

async function topUp() {
    const amount = prompt('Amount (USD):');
    if (!amount) return;
    const session = await fetch('/api/payments/topup', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        credentials: 'same-origin', body: JSON.stringify({amount: parseFloat(amount)})
    }).then(r => r.json());
    window.location.href = session.url;
}

async function manageCards() {
    const portal = await fetch('/api/payments/portal', {
        method: 'POST', credentials: 'same-origin'
    }).then(r => r.json());
    window.location.href = portal.url;
}
</script>
```

That's it. No Stripe.js, no card forms, no PCI scope.

## Payment Service API Reference

### Checkout (Stripe hosted pages)

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `GET /checkout/{org_id}/plans` | GET | Optional | List public plans with prices |
| `POST /checkout/{org_id}/plan/{plan_id}` | POST | Optional | Create subscription checkout |
| `POST /checkout/{org_id}/session` | POST | Required | Create one-time payment checkout |
| `POST /checkout/init` | POST | None | Get anti-fraud token for anonymous checkout |
| `GET /checkout/sessions/{id}/verify` | GET | Required | Verify completed checkout session |

### Portal (Stripe hosted self-service)

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `POST /portal/{org_id}/session` | POST | Required | Create Customer Portal session |

### Payment Methods

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `POST /payment-methods/{org_id}/setup-intent` | POST | Required | Create SetupIntent for Stripe.js card collection |
| `POST /payment-methods/{org_id}/` | POST | Required | Save payment method (with stripe_payment_method_id) |
| `GET /payment-methods/{org_id}/` | GET | Required | List saved payment methods |
| `PUT /payment-methods/{org_id}/{id}/default` | PUT | Required | Set default payment method |
| `DELETE /payment-methods/{org_id}/{id}` | DELETE | Required | Remove payment method |

### Subscriptions

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `GET /subscriptions/{org_id}/` | GET | Required | List subscriptions |
| `DELETE /subscriptions/{org_id}/{id}` | DELETE | Required | Cancel subscription |

### Invoices

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `GET /invoices/{org_id}/` | GET | Required | List invoices |
| `GET /invoices/{org_id}/{id}/pdf` | GET | Required | Get invoice PDF URL |

### Webhooks

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `POST /webhooks/stripe` | POST | Stripe signature | Receive Stripe webhook events |

## Billing Service API Reference

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `GET /billing/{org_id}/balance` | GET | Required | Get account balance |
| `GET /billing/{org_id}/usage/summary` | GET | Required | Get usage summary |
| `GET /billing/{org_id}/transactions` | GET | Required | List transactions |
| `POST /billing/{org_id}/credit` | POST | Service key | Credit account (after payment) |
| `POST /billing/{org_id}/reserve` | POST | Service key | Reserve funds for operation |
| `POST /billing/{org_id}/commit` | POST | Service key | Commit reservation with actual cost |
| `POST /billing/{org_id}/refund` | POST | Service key | Refund unused reservation |
| `GET /billing/{org_id}/tier` | GET | Required | Get current tier |
| `PUT /billing/{org_id}/tier` | PUT | Service key | Set tier (after subscription change) |

## Flows

### Subscription Flow
```
User clicks "Subscribe" → proxy creates checkout → Stripe hosted page →
user pays → Stripe webhook → payment service creates subscription →
_sync_subscription_tier() → billing set_org_tier() → quota limits update
```

### Top-Up Flow
```
User clicks "Top Up $50" → proxy creates checkout session (mode=payment) →
Stripe hosted page → user pays → Stripe webhook (checkout.session.completed) →
payment service credits billing (account_funding) → billing balance updated
```

### Payment Method Flow (via Portal)
```
User clicks "Manage Billing" → proxy creates portal session →
Stripe Customer Portal (hosted) → user adds/removes cards →
Stripe manages everything → user returns to your billing page
```

### Payment Method Flow (via SetupIntent — advanced)
```
Consumer loads Stripe.js CDN → POST /payment-methods/{org_id}/setup-intent →
get client_secret → stripe.confirmCardSetup(client_secret) →
POST /payment-methods/{org_id}/ with stripe_payment_method_id → saved
```

## When to Use What

| Goal | Use | Why |
|------|-----|-----|
| Accept subscription payments | Stripe Checkout (`/checkout/{org}/plan/{id}`) | Hosted by Stripe, zero custom UI |
| Accept one-time payments | Stripe Checkout (`/checkout/{org}/session`) | Same hosted page, `mode=payment` |
| Let customers manage cards | Stripe Customer Portal (`/portal/{org}/session`) | Hosted by Stripe, manages everything |
| Let customers view invoices | Stripe Customer Portal | Included in portal |
| Custom inline card form | SetupIntent + Stripe.js CDN | Only if portal isn't enough |
| Track usage/costs | Billing service reserve/commit | Server-to-server, not user-facing |
| Show balance to user | Billing `GET /billing/{org}/balance` | Proxy through your backend |

## Webhook Setup

Register your public webhook URL in the Stripe Dashboard:

**URL:** `https://your-service.example.com/api/webhooks/stripe`

**Events to subscribe:**
- `checkout.session.completed` — triggers billing credit for top-ups
- `customer.subscription.created` — triggers tier sync
- `customer.subscription.updated` — triggers tier change
- `customer.subscription.deleted` — triggers downgrade to free
- `invoice.paid` — tracks successful invoice payments
- `invoice.payment_failed` — alerts on failed payments

Your webhook proxy forwards the raw body + `Stripe-Signature` header to the payment service. The payment service verifies the signature and processes the event.

## Reference Implementation

See `sandbox-platform` for a complete working implementation:
- `app/payment_client.py` — HTTP client for payment service
- `app/billing_client.py` — HTTP client for billing service
- `app/main.py` — All proxy routes (search for "Payments" tag)
- `templates/pricing.html` — Dynamic pricing cards from API
- `templates/billing.html` — Balance, top-up, manage cards buttons
- `templates/checkout_success.html` — Post-checkout processing page
- `scripts/curl_tests/user_journeys/UJ-050_*` through `UJ-053_*` — Security + functional tests
