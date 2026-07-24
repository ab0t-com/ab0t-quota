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
   - `invoice.paid`
   - `invoice.payment_succeeded` (older Stripe API versions; safe to also enable — lib accepts both)
   - `invoice.payment_failed`
4. Copy webhook signing secret to **`AB0T_QUOTA_STRIPE_WEBHOOK_SECRET`** env var.
   (Before 0.7 the generic `STRIPE_WEBHOOK_SECRET` was consulted as a fallback;
   it no longer is — see `docs/migrating-from-ambient-resolution.md`. If your
   webhooks 400 and credits stop landing after upgrading, this rename is why;
   startup logs an ERROR naming both variables when the old name is still set.)

## Test Cards

| Card | Result |
|---|---|
| `4242424242424242` | Succeeds |
| `4000000000000002` | Declines |
| `4000000000003220` | Requires 3D Secure |

## Checkout Session Metadata

Pass `org_id` in the checkout session metadata:

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
