---
name: quota-tier-management
description: Manage ab0t-quota tiers, pricing, Stripe-to-tier mapping, and the payment-to-auth tier lifecycle. Use when defining or modifying tier limits, mapping Stripe subscription plans to quota tiers, wiring payment webhooks to update org tiers in auth-service, handling subscription downgrades with grace periods, configuring quota-config.json tier definitions, setting per-org overrides for enterprise customers, or debugging tier assignment and cache invalidation issues.
---

# Quota Tier Management

## Tier Lifecycle

```
Signup → free (automatic)
Payment → auth sets tier → JWT carries org_tier → all services enforce
Cancel  → grace period → downgrade to free → over-limit resources stopped
```

## Configuring Tiers

Edit `quota-config.json` (no code deploy needed). See [references/config-schema.md](references/config-schema.md) for full schema.

Each tier defines:
- `tier_id` — machine name (free, starter, pro, enterprise)
- `display_name` — shown to users
- `features` — feature flags (gpu_access, sso, audit_logs)
- `limits` — resource_key → numeric limit (null = unlimited)
- `upgrade_url` — where to send denied users
- `billing_model` — discriminator picking which money primitives the tier wires up (see below)
- `price` *(optional)* — `{ amount_per_period, currency, period }` block; required for any paid tier
- `credit_grant` *(optional)* — `{ trigger, amount_per_period, lifecycle, destination, reset_on_downgrade, rollover_max_periods }`; defines whether/how the tier grants credit
- `initial_credit` *(back-compat alias)* — shorthand for `credit_grant` with `trigger=signup`, `lifecycle=persistent`, `destination=credit_balance`

Limit values:
- Integer/float: hard cap (e.g. `5` sandboxes)
- `null`: unlimited
- `0`: feature not available on this tier (triggers "not available" message)

`billing_model` values:
- `capacity_only` *(default)* — limits-only, no money side-effects
- `consumption_only` — pay-as-you-go top-ups, no subscription
- `subscription_with_credits` — paid subscription that grants credit each period (requires `price` + `credit_grant` with `trigger=subscription_invoice_paid`)
- `subscription_unlock_only` — paid subscription that only raises limits, no credit
- `subscription_with_overage` / `seat_based` / `metered` — schema-supported, implementation pending

Full schema reference + worked archetype examples: see `BILLING_MODELS_GUIDE.md` in the ab0t-quota library root (or copy in `Skills/quota-paid-tier-onboarding/references/billing-models-guide.md`).

## Stripe-to-Tier Mapping

In `quota-config.json` under `billing_integration.stripe_price_to_tier`:

```json
{
  "price_starter_monthly": "starter",
  "price_pro_annual": "pro",
  "price_enterprise": "enterprise"
}
```

Payment-service reads this mapping in the Stripe webhook handler and calls auth-service to set the tier.

## Wiring Payment → Auth → Quota

See [references/payment-tier-flow.md](references/payment-tier-flow.md) for the full implementation.

Summary:
1. Stripe webhook fires `subscription.created` / `subscription.updated` / `subscription.deleted`
2. Payment-service maps `price_id` → `tier_id` from config
3. Payment-service calls auth-service `PUT /organizations/{org_id}/tier`
4. Auth-service writes tier to DynamoDB and invalidates Redis cache
5. Next JWT issued carries new `org_tier` claim
6. All services read new limits from JWT (0ms, no network call)

## Per-Org Overrides (Enterprise)

For customers with negotiated limits:

```python
from ab0t_quota.persistence import QuotaStore
from ab0t_quota.models.core import QuotaOverride

override = QuotaOverride(
    org_id="org-enterprise-1",
    resource_key="sandbox.concurrent",
    limit=200,  # custom limit, overrides tier
    reason="Enterprise contract #4521",
    created_by="admin-user-id",
    expires_at=datetime(2027, 1, 1),  # or None for permanent
)
await store.set_override(override)
```

Overrides take precedence over tier limits. Expired overrides automatically fall back to tier default.

## Downgrade Handling

See [references/downgrade-flow.md](references/downgrade-flow.md) for implementation details.

Rules:
- Existing resources keep running during grace period (7 days default)
- New resource creation blocked if over new tier limits
- Email org admin with "resources will be stopped in 7 days" notice
- After grace: background worker stops idle resources exceeding new limits
- Payment failure: 3-day grace, then downgrade to free
