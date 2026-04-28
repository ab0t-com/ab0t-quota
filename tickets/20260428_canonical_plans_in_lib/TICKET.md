# Plans as a first-class ab0t-quota concept

**Date:** 2026-04-28
**Status:** Proposal — needs sign-off before implementation
**Owner:** ab0t-quota library
**Related sub-issue:** [`INITIAL_CREDIT_INVESTIGATION.md`](./INITIAL_CREDIT_INVESTIGATION.md) — free accounts not receiving the configured $10 credit (same architectural shape; folded into Phase 6)
**Final design for credit grant:** [`EVENT_DRIVEN_DESIGN.md`](./EVENT_DRIVEN_DESIGN.md) — verified auth's webhook system; supersedes the earlier "lazy on /balance" and "BillingHome HTTP endpoint" sketches

## Problem

ab0t-quota markets itself as a "drop-in billing and payment system." Today every consumer that wants to actually sell tiers has to maintain its own ad-hoc seeding script (e.g. `sandbox-platform/scripts/seed_plans.sh`) which:

1. Hardcodes monthly/annual prices in a bash assoc-array
2. Reads tier names/features from `quota-config.json` (lib-owned) but writes prices from a different source
3. Creates plans in payment-service + Stripe out-of-band from any library lifecycle
4. Has no idempotency contract codified anywhere — re-runs are best-effort
5. Means each new consumer of the lib has to copy/paste/maintain ~200 lines of bash

That defeats the "drop-in" promise. A consumer should be able to `pip install ab0t-quota`, declare their tiers, point at their payment-service, and have the lib handle everything from quota enforcement through plan publication.

## Decision needed

**Where does canonical plan information live, and who owns the lifecycle of pushing plans into payment-service / Stripe?**

## Recommendation

**Tiers and plans are different concepts. Keep them separate but sibling.** Both live in `quota-config.json` (the lib's owned config). The lib owns the sync.

### The conceptual split

| Concept | What it is | Who consumes it |
|---|---|---|
| **Tier** | Entitlement bucket. "Pro" = limits {sandbox.concurrent: 25, ...} + features [...] | Quota engine, runtime enforcement, dashboard tier-comparison page |
| **Plan** | Purchasable offering. "Pro Monthly" = $99/mo, references `tier_id: "pro"`. Has Stripe price ID, billing interval, visibility, sales status | Payment service, checkout, Stripe, sales/marketing pages |

A tier can have **many** plans:
- `Pro Monthly` ($99/mo) → tier `pro`
- `Pro Annual` ($990/yr) → tier `pro` (≈17% discount baked into price)
- `Pro Edu` ($49/mo, hidden) → tier `pro` (discount for verified edu accounts)
- `Pro Lifetime` ($999 one-time) → tier `pro` (sunset for existing customers, not for sale)

All four grant the same entitlements. Different SKUs.

**Why not collapse them?** If you put `pricing.monthly`/`pricing.annual` directly on the tier, you lock yourself into 1 plan = 1 tier and have nowhere to put: deprecated SKUs, regional pricing experiments, edu/non-profit discounts, partner co-marketed plans, lifetime / one-time variants. Migration later is painful (Stripe price IDs already minted against the old shape, webhooks already firing against the old map).

For consumers who only ever want the simple case (one monthly + one annual per tier), the lib can auto-generate plans from tiers if no `plans[]` block is present — keeps the easy path easy.

### Where it lives

Extend `quota-config.json` (lib already parses it):

```json
{
  "tiers": [
    { "tier_id": "free",  "display_name": "Free",  "limits": {...}, "features": [...] },
    { "tier_id": "pro",   "display_name": "Pro",   "limits": {...}, "features": [...] }
  ],
  "plans": [
    {
      "plan_id": "pro-monthly",
      "tier_id": "pro",
      "display_name": "Pro Monthly",
      "pricing": { "currency": "usd", "amount": 99, "interval": "month" },
      "visibility": "public",
      "sync_to_stripe": true
    },
    {
      "plan_id": "pro-annual",
      "tier_id": "pro",
      "display_name": "Pro Annual",
      "pricing": { "currency": "usd", "amount": 990, "interval": "year" },
      "visibility": "public",
      "sync_to_stripe": true
    }
  ]
}
```

Free tier has no plans (not for sale). Enterprise can have a `plans[]` entry with `visibility: "contact_sales"` and no Stripe sync.

### Who owns the sync

A new `ab0t_quota.plans` module exposing:
- `Plan` dataclass (parsed from config)
- `load_plans(config) -> list[Plan]` — explicit fields, no surprise pass-through, fails loud on schema errors
- `async sync_plans(payment_url, api_key, consumer_org_id, plans, dry_run=False) -> SyncResult` — idempotent: list existing → diff → create missing plans + prices. Returns what changed.

Operator surface: a CLI entrypoint `python -m ab0t_quota sync-plans`. Reads `quota-config.json` from the standard search paths, calls `sync_plans()`. Exits non-zero on failure.

**Why CLI, not auto-on-startup:** explicit, no boot-time payment-service dependency, easy to put in deploy runbooks, easy to dry-run before applying. Auto-on-startup adds a network call to every container boot; if payment-service is down the consumer service won't start.

## Side effects (intentionally none)

- `sync_plans()` is **read-modify-create only** — it never deletes plans (orphan plans in payment-service stay; flagging them is a separate manual concern)
- It never modifies `quota-config.json` back — config is one-way
- It is a no-op if the existing plans match
- It is safe to re-run; safe to invoke from N consumers concurrently (idempotent at the payment-service level via the existing slug/name dedupe)

## Alternatives considered

1. **`pricing` field on tier itself (1:1 collapse)** — simpler today, painful later. Rejected: locks out multi-plan-per-tier without a migration. The "drop-in lib" promise should not require a migration in 6 months when the first consumer wants an annual discount.
2. **Separate `plans-config.json` file** — keeps lib config "pure," but means two files for one logical config. Rejected: ergonomics worse; two files to keep in sync, two paths to document.
3. **Database-backed plans (admin UI to manage)** — too much for v1. Possible v2 if a consumer asks for runtime plan editing; for now config-as-code is enough and matches how `tiers` already work.
4. **Auto-on-startup sync** — convenient until payment-service is down at boot. Rejected for default; available as opt-in via `setup_quota(sync_plans_on_startup=True)` if a consumer prefers that tradeoff.
5. **Status quo (each consumer ships its own seed script)** — defeats "drop-in." Rejected.

## Constraint — billable entity resolved by lib, not by JWT

The auth service has a per-end-users-org flag `login_config.registration.org_structure.pattern` (`flat` or `workspace-per-user`). When set to `workspace-per-user`, registration spawns a child workspace org **asynchronously** — but auth does NOT auto-re-scope the JWT into it. The JWT keeps the parent end-users org unless the consumer dashboard explicitly calls `/auth/switch-organization`.

This means a "lib trusts JWT.org_id" approach silently misroutes credits to the shared parent org whenever the dashboard hasn't switched (race, broken flow, new consumer that forgot to wire the switch).

**Resolution:** the lib looks up the user's workspace itself before crediting:

- If user has an org where `role=owner` and `settings.type=user_workspace` → credit there
- Else → credit `JWT.org_id` (correct fallback for flat-mode consumers where parent IS the billable entity)
- Cache the lookup for the request's lifetime
- Anti-farming dedup stays keyed on `user_id` regardless — one human, one credit
- Legacy balance records keyed to old user-orgs stay where they are; no silent migration

Drop-in stays drop-in — no consumer-side config knob, no dashboard-side discipline required. The lib auto-detects from the user's org graph.

Verified curl trace + alternatives considered in [`INITIAL_CREDIT_INVESTIGATION.md`](./INITIAL_CREDIT_INVESTIGATION.md).

## Out of scope

- Changing payment-service's plan/price API (used as-is)
- Stripe price creation/deletion (delegated to payment-service via `sync_to_stripe: true`)
- Promo codes, coupons, trial periods (separate ticket)
- Migration tooling for consumers already on pre-`plans[]` configs (will write a one-shot helper if needed)

## Open questions

1. **Should `Plan.tier_id` be required, or can a plan grant multiple tiers (composite)?** Recommend required for v1. Composite is rare and can be added later via `tier_ids: [...]`.
2. **Should the CLI also delete plans removed from config?** Recommend NO for v1 — too easy to lose paying-customer subscriptions if a config edit is wrong. v2 could add `--prune` with confirmation.
3. **Should we ship a `migrate-from-seed-script` helper for sandbox-platform's existing setup?** Yes, but small — just generates the `plans[]` block from the current bash arrays.

## Acceptance criteria

- [ ] `quota-config.json` schema documents the `plans[]` block
- [ ] `ab0t_quota.plans` module exposes `Plan`, `load_plans()`, `sync_plans()`
- [ ] `python -m ab0t_quota sync-plans` works against a running payment-service
- [ ] Existing `sandbox-platform/scripts/seed_plans.sh` can be deleted (replaced by a 3-line wrapper or removed entirely)
- [ ] Tests cover: empty config, missing optional fields, plan-without-tier (error), idempotent re-run, dry-run mode
- [ ] Library docs (`README.md`, `docs/quickstart.md`, `docs/deployment.md`) updated
- [ ] No behaviour change for consumers that don't define `plans[]` — the engine still works for quota-only use cases
