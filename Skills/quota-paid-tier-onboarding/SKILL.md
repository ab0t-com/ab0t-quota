---
name: quota-paid-tier-onboarding
description: Connect a mesh service to paid tiers via Stripe checkout, billing account auto-creation, and quota enforcement. Use when hooking a new service into the payment→billing→quota pipeline, adding a pricing page, wiring Stripe checkout buttons, connecting payment webhooks to tier changes, auto-creating billing accounts on first contact, displaying tiers and usage in a frontend, or following the reference implementation for paid plan activation. This is the end-to-end guide from "user sees pricing" to "user pays and gets new limits."
---

# Paid Tier Onboarding

This skill documents how to connect
any mesh service to the paid tier pipeline.

## The Pipeline

```
User sees pricing → clicks "Upgrade to Pro"
    → consumer POST /api/payments/checkout/{plan_id}
    → payment-service creates Stripe checkout session
       (with subscription_data.metadata = {org_id, plan_id} — required so the
        invoice.paid event downstream carries the org context)
    → user completes payment on Stripe
    → Stripe webhook fires customer.subscription.created
    → payment-service _sync_subscription_tier()
    → payment-service calls billing PUT /{org_id}/tier {tier_id: "pro"}
    → billing modules/quota stores tier in DynamoDB
    → billing invalidates Redis cache
    → consumer next quota check reads "pro" from billing
    → user gets pro LIMITS (25 sandboxes instead of 1)
    
    ↓ Then, if the tier's billing_model = subscription_with_credits:
    
    → Stripe webhook fires invoice.paid (immediately, then once per period)
    → payment-service reads org_id from subscription metadata
    → payment-service calls billing POST /{org_id}/apply-credit-grant
    → billing populates `subscription_credit` bucket with the period's amount
    → user gets pro CREDIT (the configured monthly bundled spend amount)

    ↓ On downgrade (customer.subscription.updated → lower tier), if the old
      tier's credit_grant has reset_on_downgrade=true:
    
    → payment-service calls billing POST /{org_id}/reset-subscription-credit
       with expected_source_tier = <old_tier_id>
    → billing zeros the `subscription_credit` bucket only if the source tier
      still matches (safety check for enterprise multi-sub orgs)
```

Limits and credit are decoupled: tier change updates limits; the invoice.paid grant populates credit. They can ship to different services on different cadences — limits via `PUT /tier`, credit via `POST /apply-credit-grant`.

## Billing Account Auto-Creation

Billing accounts are created lazily — on first contact with the billing service.
No signup hook, no auth event, no manual API call needed.

`billing_repo.ensure_account(org_id)` checks for an existing account and creates
one with defaults if missing. Called from every billing service operation:
- `get_account_balance()` — balance reads
- `validate_access()` — spending permission checks
- `reserve_funds()` / `commit()` — financial operations

**Every mesh consumer that calls billing triggers this.** The first time a
a user checks their balance or creates a resource (which calls
billing reserve), the account appears automatically.

See [references/auto-create-pattern.md](references/auto-create-pattern.md) for the implementation.

## Connecting a New Service

### 0. Pick a billing model per tier

Before writing `quota-config.json`, decide WHICH primitives each tier uses. The `billing_model` discriminator on TierConfig picks the wiring:

| Value | When to use |
|---|---|
| `capacity_only` *(default)* | Free / limits-only tiers, no money side-effects |
| `consumption_only` | Pay-as-you-go top-ups, no subscription |
| `subscription_with_credits` | Paid subscription where each period grants $Y of bundled spend |
| `subscription_unlock_only` | Paid subscription that only raises limits — no credit grant |

`subscription_with_credits` requires `price` + `credit_grant` blocks on the tier:

```json
{
  "tier_id": "starter",
  "billing_model": "subscription_with_credits",
  "price": { "amount_per_period": "10.00", "currency": "USD", "period": "month" },
  "credit_grant": {
    "trigger": "subscription_invoice_paid",
    "amount_per_period": "10.00",
    "lifecycle": "use_it_or_lose_it",
    "destination": "subscription_credit",
    "reset_on_downgrade": true
  },
  "limits": { "sandboxes": 25, ... }
}
```

`initial_credit: "X"` on a tier is a back-compat alias for `credit_grant: {trigger: "signup", amount_per_period: X, lifecycle: "persistent", destination: "credit_balance"}`. New configs should declare `credit_grant` explicitly. See the full cookbook at `BILLING_MODELS_GUIDE.md` in the ab0t-quota library root.

### 1. Backend (quota enforcement)

Follow the `quota-service-integration` skill:
- Add `ab0t-quota` to requirements.txt
- Create `app/quota.py` with billing-backed tier provider
- Deploy `quota-config.json` with your service's resource limits + `billing_model` per tier
- Wire enforcement in route handlers (check before create, increment after, decrement on destroy)

### 2. Frontend (pricing + upgrade flow)

See [references/frontend-integration.md](references/frontend-integration.md):
- Pricing page: load plans from `GET /api/payments/plans`, render cards with upgrade buttons
- Checkout: button calls `POST /api/payments/checkout/{plan_id}`, redirect to Stripe
- Success page: show new tier and limits
- Dashboard: show tier badge + usage bars from `GET /api/quotas/usage`
- Feature gating: grey out tier-locked features based on `GET /api/quotas/tiers`

### 3. Stripe Setup

See [references/stripe-setup.md](references/stripe-setup.md):
- Create products + prices in Stripe
- Map price IDs → tier IDs in `QUOTA_PLAN_TIER_MAP` env var
- Configure webhook endpoint for payment service
- Test with Stripe test cards

### 4. Deployment

- Rebuild billing + payment + your service
- Seed Stripe plans (test then production)
- Run end-to-end test: signup → pay → tier change → quota enforce
