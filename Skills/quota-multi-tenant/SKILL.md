---
name: quota-multi-tenant
description: Design and enforce quota tiers across a multi-tenant mesh network of private companies. Use when reasoning about org isolation in quota enforcement, designing how tiers flow from payment through billing to consumer services, understanding the dual-account architecture (APP vs SANDBOX), wiring quota checks across service boundaries, implementing per-org overrides for enterprise customers, handling tier inheritance in nested org hierarchies, or debugging cross-tenant quota leaks. Covers the mesh network tenancy model, tier ownership patterns, and multi-org isolation guarantees.
---

# Multi-Tenant Quota Design

## Mesh Network Tenancy Model

Each service in the mesh is a **separate company** with its own pricing:
- Auth service has its own tiers (users/teams/API keys) — auth's business
- Billing service owns **commercial tiers** for consumer services
- Each consumer (sandbox-platform, resource-service) reads tiers from billing

No god services. Auth doesn't know about sandbox tiers. Billing doesn't know about auth tiers.

## Tier Ownership Chain

```
Stripe → Payment Service (maps plan→tier)
              → Billing Service PUT /{org_id}/tier (stores tier)
                    → DynamoDB ab0t_quota_state (durable)
                    → Redis DEL quota:tier:{org_id} (cache bust)
Consumer services → Billing GET /{org_id}/tier (cached 5min in Redis)
              → QuotaEngine.check() (enforce locally)
```

Tier data is written by billing, read by consumers. No circular dependencies.

## Org Isolation Guarantees

All DynamoDB operations are scoped to `PK=ORG#{org_id}`. An org can never read or write another org's data through the quota system.

**Enforcement layers:**
1. `verify_org_access(org_id, user)` in every route handler
2. DynamoDB PK scoping — queries always filter on `PK=ORG#{org_id}`
3. `BillingPlatformAdmin` (cross_tenant) required for override writes
4. Per-org Redis cache keys — `quota:tier:{org_id}`

See [references/isolation-model.md](references/isolation-model.md) for the full isolation analysis.

## Nested Orgs

The auth mesh supports arbitrarily deep org hierarchies (parent → child → grandchild). For quota:

- **Current:** Tiers are per-org. No inheritance from parent to child.
- **Override:** Enterprise parent org can have platform admin set overrides on child orgs.
- **Future:** Tier inheritance (child inherits parent's tier unless overridden) is not yet implemented.

## Consumer Service Integration Pattern

Every consumer service follows the same pattern. See [references/consumer-pattern.md](references/consumer-pattern.md).

1. `app/quota.py` — engine init, fetches tier from billing
2. `quota-config.json` — tier definitions, resource limits
3. `enforce_user_cost_limit()` — dispatches by resource_key
4. Lifecycle hooks — increment on create, decrement on terminate
5. `/api/quotas/*` endpoints — usage bars, tier comparison, pre-flight checks

## Per-User Sub-Quotas Within an Org

The `per_user_limit` field in `TierLimits` prevents one team member from exhausting the org's entire quota. When `user_id` is passed to `engine.check()`:

1. Org-level check first (total usage vs org limit)
2. User-level check second (user's usage vs per_user_limit)
3. Denied result includes `denied_level: "org"` or `"user"`

Redis stores per-user gauges at `quota:{org_id}:{resource_key}:gauge:user:{user_id}`.
