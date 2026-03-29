# Payment → Billing → Consumer Tier Flow

## Overview

```
Stripe webhook → Payment Service → Billing Service PUT tier → DynamoDB + Redis
                                                                    ↓
                                              Consumer services read via GET tier (cached)
```

Auth service is NOT involved. Billing owns tiers.

## Payment Service Webhook Handler

In `payment-service/app/api/webhooks.py`, the `_sync_subscription_tier()` function fires after every subscription lifecycle event:

```python
# subscription.created / subscription.updated:
tier_id = resolve_price_to_tier(price_id=price_id, plan_id=plan_id)
await billing_service.set_org_tier(org_id, tier_id, reason="subscription_created")

# subscription.deleted:
await billing_service.set_org_tier(org_id, "free", reason="subscription_deleted")

# canceled / unpaid status:
await billing_service.set_org_tier(org_id, "free", reason="subscription_canceled")
```

`resolve_price_to_tier()` maps Stripe price IDs to tier IDs via `QUOTA_PLAN_TIER_MAP` env var or default config.

## Billing Service Tier Endpoint

Payment calls `PUT /billing/{org_id}/tier` on the billing service, which:

1. Validates `tier_id` against `KNOWN_TIERS`
2. Writes tier + history atomically via DynamoDB `TransactWriteItems`
3. Invalidates Redis cache: `DEL quota:tier:{org_id}`
4. Returns `TierChangeResponse` with previous and new tier

Idempotent: setting the same tier twice is a no-op (no duplicate history).

## Consumer Service Cache

Consumer services (sandbox-platform, resource-service) call `GET /billing/{org_id}/tier` via `AuthServiceTierProvider`. Response is cached in Redis for 5 minutes.

After billing writes a tier change and deletes the Redis key, the next consumer request triggers a fresh fetch from billing and caches the new tier.

## Failure Modes

- **Payment webhook fails:** Tier not updated. Stripe retries webhook.
- **Billing service down:** Payment logs error, returns. Webhook retried by Stripe.
- **Redis cache stale:** Consumer serves old tier for up to 5 minutes. Self-heals.
- **DynamoDB down:** Billing returns 503. Payment retries on next webhook.
