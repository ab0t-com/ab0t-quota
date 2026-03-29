# Stripe Setup for Paid Tiers

## Products & Prices

Create in Stripe Dashboard or via API:

```bash
# Products (one per tier)
stripe products create --name="Sandbox Starter" --metadata[tier_id]=starter
stripe products create --name="Sandbox Pro" --metadata[tier_id]=pro

# Prices (monthly + annual per product)
stripe prices create \
  --product=prod_starter_id \
  --unit-amount=2900 \
  --currency=usd \
  --recurring[interval]=month \
  --lookup-key=price_starter_monthly

stripe prices create \
  --product=prod_pro_id \
  --unit-amount=9900 \
  --currency=usd \
  --recurring[interval]=month \
  --lookup-key=price_pro_monthly
```

## Plan → Tier Mapping

Set `QUOTA_PLAN_TIER_MAP` env var (JSON) in payment service:

```bash
QUOTA_PLAN_TIER_MAP='{"price_starter_monthly":"starter","price_pro_monthly":"pro","price_starter_annual":"starter","price_pro_annual":"pro"}'
```

Payment service reads this in `core/quota.py:resolve_price_to_tier()`.

## Webhook Configuration

1. In Stripe Dashboard → Webhooks → Add endpoint
2. URL: `https://payment.service.ab0t.com/webhooks/stripe`
3. Events to listen for:
   - `checkout.session.completed`
   - `customer.subscription.created`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `invoice.payment_succeeded`
   - `invoice.payment_failed`
4. Copy webhook signing secret to `STRIPE_WEBHOOK_SECRET` env var

## Test Cards

| Card | Result |
|---|---|
| `4242424242424242` | Succeeds |
| `4000000000000002` | Declines |
| `4000000000003220` | Requires 3D Secure |

## Checkout Session Metadata

The sandbox-platform passes `org_id` in checkout session metadata:

```python
session = stripe.checkout.Session.create(
    line_items=[{"price": price_id, "quantity": 1}],
    mode="subscription",
    success_url=f"{base_url}/billing/checkout/success?session_id={{CHECKOUT_SESSION_ID}}",
    cancel_url=f"{base_url}/billing/checkout/cancel",
    metadata={"org_id": org_id, "plan_id": plan_id},
)
```

The webhook handler reads `org_id` from metadata to set the tier for the correct org.
