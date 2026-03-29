---
name: quota-paid-tier-onboarding
description: Connect a mesh service to paid tiers via Stripe checkout, billing account auto-creation, and quota enforcement. Use when hooking a new service into the payment→billing→quota pipeline, adding a pricing page, wiring Stripe checkout buttons, connecting payment webhooks to tier changes, auto-creating billing accounts on first contact, displaying tiers and usage in a frontend, or following the sandbox-platform reference implementation for paid plan activation. This is the end-to-end guide from "user sees pricing" to "user pays and gets new limits."
---

# Paid Tier Onboarding

Reference implementation: sandbox-platform. This skill documents how to connect
any mesh service to the paid tier pipeline.

## The Pipeline

```
User sees pricing → clicks "Upgrade to Pro"
    → sandbox-platform POST /api/payments/checkout/{plan_id}
    → payment-service creates Stripe checkout session
    → user completes payment on Stripe
    → Stripe webhook fires subscription.created
    → payment-service _sync_subscription_tier()
    → payment-service calls billing PUT /{org_id}/tier {tier_id: "pro"}
    → billing modules/quota stores tier in DynamoDB
    → billing invalidates Redis cache
    → sandbox-platform next quota check reads "pro" from billing
    → user gets pro limits (25 sandboxes instead of 1)
```

## Billing Account Auto-Creation

Billing accounts are created lazily — on first contact with the billing service.
No signup hook, no auth event, no manual API call needed.

`billing_repo.ensure_account(org_id)` checks for an existing account and creates
one with defaults if missing. Called from every billing service operation:
- `get_account_balance()` — balance reads
- `validate_access()` — spending permission checks
- `reserve_funds()` / `commit()` — financial operations

**Every mesh consumer that calls billing triggers this.** The first time a
sandbox-platform user checks their balance or creates a sandbox (which calls
billing reserve), the account appears automatically.

See [references/auto-create-pattern.md](references/auto-create-pattern.md) for the implementation.

## Connecting a New Service

### 1. Backend (quota enforcement)

Follow the `quota-service-integration` skill:
- Add `ab0t-quota` to requirements.txt
- Create `app/quota.py` with billing-backed tier provider
- Deploy `quota-config.json` with your service's resource limits
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
