# ab0t-quota — Billing Models Guide

A consumer-facing guide. If you maintain a service that uses `ab0t-quota` and you want to configure (or change) the billing relationship for a tier, start here.

This guide covers:
- **What the library does for you** (and what it doesn't)
- **How to declare your tiers** in `quota-config.json`
- **Walk-throughs for each supported billing model**, with concrete config
- **Required wiring** in your service + payment-service
- **Common gotchas** when integrating

For internal API contract details, see `ab0t_quota/billing/BILLING_INTEGRATION.md`.

---

## TL;DR

You declare your tier policy as JSON. The library handles the rest.

```jsonc
// In your service's quota-config.json
{
  "tier_id": "pro",
  "billing_model": "subscription_with_credits",
  "price": {"amount_per_period": 10.00, "currency": "USD", "period": "month"},
  "credit_grant": {
    "trigger": "subscription_invoice_paid",
    "amount_per_period": 10.00,
    "lifecycle": "use_it_or_lose_it",
    "destination": "subscription_credit"
  },
  "limits": {
    "api.requests_per_hour": 100000,
    "compute.monthly_spend": 1000.00
  }
}
```

That's it. On every successful subscription invoice, the user gets +$10 of usable credit. At the next renewal, unused remainder is forfeit. Spending drains the credit first; only after it's exhausted does the user need to top up.

If you want a different model, change `billing_model` + `credit_grant`. The library reads your config; you write zero code.

---

## What the library does

When you wire `ab0t-quota` into your service via `setup_quota(app, ...)`:

- **Quota enforcement.** Every resource you track (sandboxes, API calls, GPU hours, etc.) has a tier-defined limit. The library rejects requests that would exceed the cap.
- **Reservation + commit.** Before provisioning a resource, you call `BudgetChecker.reserve_funds(...)` to lock available balance. On stop, the library commits the actual cost (drains the right buckets in priority order).
- **Credit grants.** When a subscription invoice is paid (Stripe webhook), the library reads your tier config and lands the grant per the configured `lifecycle` (use-it-or-lose-it / rollover / persistent).
- **Tier-change side effects.** On downgrade, the library calls a reset endpoint that safely zeros the previous tier's `subscription_credit` (with a safety check against multi-subscription orgs).
- **Top-up.** Users top up via Stripe Checkout (`type=account_funding`) → balance is credited as cash.

## What you do

- **Declare your tiers** in `quota-config.json` (see below).
- **Wire payment-service (or your delivery mechanism) to call the library's webhook handler** on paid-invoice events (`invoice.paid` and `invoice.payment_succeeded` — the lib accepts both since Stripe emits one or both depending on API version). See "Required wiring" below.
- **Call the library at provisioning points.** `BudgetChecker.reserve_funds()` before, `LifecycleEmitter.resource_stopped()` after. The library handles the rest.

## What the library does NOT do

- It doesn't decide pricing (you do, in `price.amount_per_period`).
- It doesn't pick a billing relationship for you (you pick a `billing_model`).
- It doesn't run Stripe Checkout (that's `payment-service`'s job, which the library calls into).
- It doesn't enforce its DEFAULT values silently — every default kicks in only when you omit a field.

---

## How to declare your tiers

Your service ships a `quota-config.json` (typically in the repo root). Library reads it on startup.

Top-level shape:

<!-- doc-exec: fragment (ellipsis shape illustration of the tiers key only — not a bootable config; see quota-config.example.json) -->
```jsonc
{
  "tiers": [
    { "tier_id": "free", ... },
    { "tier_id": "pro",  ... },
    { "tier_id": "enterprise", ... }
  ],
  "pricing": { /* optional: per-resource costs */ }
}
```

Each tier has these fields (✦ = required, ✧ = library default applies if omitted):

| Field | Type | Notes |
|---|---|---|
| `tier_id` ✦ | string | Lowercase, snake_case-friendly. Stable identifier. |
| `display_name` ✦ | string | Shown to users in the UI. |
| `sort_order` ✦ | int | Used for downgrade detection. Free=0, paid tiers increase from there. |
| `billing_model` ✧ | enum | Default: `capacity_only`. See list below. |
| `price` ✧ | object | Required if `billing_model` is subscription-based. |
| `credit_grant` ✧ | object | Required for `subscription_with_credits`. See lifecycle table. |
| `limits` ✦ | object | Resource caps. Keys are resource_keys you define. |
| `features` ✧ | list of strings | Feature flags this tier unlocks. |
| `initial_credit` | Decimal | **Deprecated** back-compat alias. Use `credit_grant` with `trigger: "signup"` instead. |

---

## Walk-through: every billing model with an example

For each model: **when to use it**, **what it does**, **config example**, **what the user experiences**.

---

### Archetype A — Pure subscription (capacity unlock only)

**When:** classic SaaS. The user pays a flat fee for higher limits; no consumption metering. Example: Slack, Linear, Notion.

**What happens:** Subscription pays for higher caps. No credit grants fire. Spending isn't tracked against a balance (limits are just numerical caps).

```jsonc
{
  "tier_id": "team",
  "display_name": "Team",
  "sort_order": 1,
  "billing_model": "capacity_only",
  "price": {"amount_per_period": 20.00, "currency": "USD", "period": "month"},
  "limits": {
    "users.total": 25,
    "history.retention_days": 365
  }
}
```

**User experience:** subscribe, get higher limits, spend at will (within caps), repeat next month.

---

### Archetype B — Pure consumption (pay-as-you-go, no plan)

**When:** API-style products. No tier hierarchy. Users top up, spend it down. Example: Anthropic API, OpenAI API.

**What happens:** Users top up via Stripe Checkout (`type=account_funding`) → balance increases. Each API call reserves+commits cost against the balance. When balance hits zero, requests are blocked.

```jsonc
{
  "tier_id": "payg",
  "display_name": "Pay as you go",
  "sort_order": 0,
  "billing_model": "consumption_only",
  "initial_credit": 5.00,   // optional one-shot signup grant
  "limits": {
    "api.requests_per_hour": 10000  // optional rate cap
  }
}
```

**User experience:** sign up, get $5 free credit, spend it, then top up via Stripe to keep going.

---

### Archetype C — Subscription with bundled credits

**When:** the most common consumer-SaaS pattern. Pay $X/month, get $Y of usage credit. Example: OpenAI ChatGPT Plus, Vercel Pro, Cloudflare Workers Paid.

**What happens:**
- Each successful subscription invoice grants `amount_per_period` of credit, written to `subscription_credit`.
- Spending drains `subscription_credit` first (use-it-or-lose-it incentive).
- At next renewal, unused remainder is forfeit (with `lifecycle: "use_it_or_lose_it"`) — or carries forward (with `rollover_*`).

```jsonc
{
  "tier_id": "pro",
  "display_name": "Pro",
  "sort_order": 2,
  "billing_model": "subscription_with_credits",
  "price": {"amount_per_period": 10.00, "currency": "USD", "period": "month"},
  "credit_grant": {
    "trigger": "subscription_invoice_paid",
    "amount_per_period": 10.00,        // give back what we charge
    "currency": "USD",
    "lifecycle": "use_it_or_lose_it",  // resets each cycle
    "destination": "subscription_credit",
    "reset_on_downgrade": true,        // wipe on downgrade (default)
    "reset_on_upgrade": false          // don't wipe on upgrade (default)
  },
  "limits": { "compute.monthly_spend": 500.00 }
}
```

**User experience:**
- Day 1: subscribe for $10, see "$10 credit available".
- Days 1-30: spend $6 on compute, see "$4 credit left" each day.
- Day 30: renewal — $10 charged, balance resets to $10 ($4 forfeit).
- Customer mental model: "I'm paying $10/mo and getting $10 to spend — fair."

**Variant: pay for less than you get** (loss leader marketing):

```jsonc
"credit_grant": {
  "trigger": "subscription_invoice_paid",
  "amount_per_period": 12.00,    // 20% bonus over the $10 price
  "lifecycle": "use_it_or_lose_it",
  "destination": "subscription_credit"
}
```

**Variant: roll over unused balance up to N periods** (enterprise-friendly):

```jsonc
"credit_grant": {
  "trigger": "subscription_invoice_paid",
  "amount_per_period": 10.00,
  "lifecycle": "rollover_capped",
  "rollover_max_periods": 3,     // up to 3 months' worth can stack
  "destination": "subscription_credit"
}
```

**Variant: unlimited rollover** (rare, business-critical use cases):

```jsonc
"credit_grant": {
  "trigger": "subscription_invoice_paid",
  "amount_per_period": 10.00,
  "lifecycle": "rollover_unlimited",
  "destination": "subscription_credit"
}
```

---

### Archetype D — Subscription unlock only + separate top-up

**When:** enterprise / dev tooling. Subscription unlocks higher LIMITS but the customer's procurement team approves a separate top-up for actual spending. Example: AWS Savings Plans, Snowflake commit-based pricing.

**What happens:** Subscription writes nothing to balance. The customer tops up separately via Stripe Checkout `type=account_funding`. Limits are governed by the tier; spend is governed by top-up balance.

```jsonc
{
  "tier_id": "enterprise",
  "display_name": "Enterprise",
  "sort_order": 3,
  "billing_model": "subscription_unlock_only",
  "price": {"amount_per_period": 1000.00, "currency": "USD", "period": "month"},
  // NO credit_grant — this is the explicit "no credit" archetype
  "limits": {
    "api.requests_per_hour": null,    // null = unlimited
    "compute.monthly_spend": null
  }
}
```

**User experience:**
- Subscribe for $1000/mo (procurement approves).
- AP team tops up $5000 in account_funding.
- Spend draws down the $5000.
- $1000 sub never appears as spendable credit; it bought you the privilege of having no caps.

**Why not just `capacity_only`?** Because `subscription_unlock_only` is the EXPLICIT version. The library's validator rejects "subscription_unlock_only + credit_grant" — it's a self-defense flag that says "I really mean no credit here."

---

### Archetype E — Hybrid: bundled base + overage charges

**Status:** schema-supported, **not implemented yet.**

**When:** "your plan includes $X of usage, anything over auto-bills your card." Example: Twilio Voice, GitHub Actions.

The schema accepts the model declaration:

```jsonc
{
  "tier_id": "pro",
  "billing_model": "subscription_with_overage",
  "price": {"amount_per_period": 10.00, ...},
  "credit_grant": {
    "trigger": "subscription_invoice_paid",
    "amount_per_period": 10.00,
    "lifecycle": "use_it_or_lose_it"
  },
  "overage_policy": {
    "enabled": true,
    "payment_method": "card_on_file",
    "max_overage_per_period": 1000
  }
}
```

But: the library doesn't yet implement off-session Stripe charging or accumulated-overage invoicing. **Add this when the first consumer asks for it.**

---

### Archetype F — Seat-based / Per-user

**Status:** schema-supported, **not fully implemented yet.**

**When:** classic per-user SaaS. Price scales with active seats. Example: Slack, Notion, Linear.

```jsonc
{
  "tier_id": "team",
  "billing_model": "seat_based",
  "price": {"amount_per_user_per_period": 10.00, "currency": "USD", "period": "month"},
  "min_seats": 1,
  "limits": {
    "per_user.api.requests_per_day": 1000   // (TODO: per-seat enforcement)
  }
}
```

The library tracks `default_per_user_fraction` for splitting org-level quotas across seats, but Stripe subscription `quantity` sync is not yet wired up.

---

### Archetype G — Freemium (free tier with limits)

**When:** every SaaS startup. Free tier with restrictive caps; users upgrade when they outgrow.

```jsonc
{
  "tier_id": "free",
  "display_name": "Free",
  "sort_order": 0,
  "billing_model": "consumption_only",       // or "capacity_only" if no balance
  "initial_credit": 10.00,                    // optional one-shot signup grant
  "limits": {
    "api.requests_per_hour": 1000,
    "compute.monthly_spend": 10.00,
    "users.total": 3
  }
}
```

The `initial_credit: 10.00` is back-compat shorthand for:

```jsonc
"credit_grant": {
  "trigger": "signup",
  "amount_per_period": 10.00,
  "lifecycle": "persistent",
  "destination": "credit_balance"
}
```

Both forms work; prefer the new explicit form for clarity.

---

### Archetype H — Free trial → auto-convert

**Status:** schema-supported, **needs trial-period setup in payment-service.**

```jsonc
{
  "tier_id": "pro_trial",
  "display_name": "Pro (14-day trial)",
  "billing_model": "subscription_with_credits",
  "price": {"amount_per_period": 10.00, ...},
  "trial": {"duration_days": 14, "requires_card": true, "auto_convert": true},
  ...
}
```

If the underlying `Plan` in payment-service has `trial_period_days` set, the Stripe Checkout session honors it. Today this works end-to-end if you set `trial_period_days` on the Plan; the library doesn't yet provide a richer trial UX (status displays, expiry warnings).

---

### Archetypes I, J, K — Volume commit, metered, referral credits

Schema-supported, not implemented. Open a ticket if you need any of:

- **Annual commit discount** — pay $1188 upfront for 12 months at a discounted rate.
- **Pure metered billing** — Stripe `usage_record_summary` push per period.
- **Referral / promo credit grants** — `trigger: "manual"` is supported on the request side; an admin tool to issue them on a human-driven path is the missing piece.

---

## Required wiring

For each archetype above, here's what your service + payment-service + ab0t-quota need to have hooked up.

### For any model with `subscription_*`:

1. **`payment-service`** must set `subscription_data.metadata = {"org_id": org_id, "plan_id": plan_id}` on Stripe Checkout sessions. (Lands in the payment service's `app/api/routes/checkout.py`.)

2. **`ab0t-quota`** must receive paid-invoice events — `invoice.paid` and/or `invoice.payment_succeeded` (the lib accepts both). Options:
   - Direct HTTP POST from payment-service to a consumer endpoint (consumer registers its URL).
   - SNS event mesh (payment-service publishes; consumers subscribe).
   - (Today: this is consumer-specific wiring; not built into the library.)

3. **Your service** must call `handle_subscription_invoice_paid()` from `ab0t_quota.billing.subscription_credit` when an invoice event arrives. The library does the lookup, dispatch, and billing-service call.

### For any model with `credit_grant`:

4. **Your `quota-config.json` must declare it.** No code-level config exists; everything is JSON.

### For downgrade reset support:

5. **Your service must call** `reset_subscription_credit_on_tier_change()` from `ab0t_quota.billing.subscription_credit` AFTER `billing.set_tier()` succeeds. The library handles the policy check + safety guard internally.

### For top-up (Archetype B / D):

6. **`payment-service`** mounts `/api/payments/topup` (already wired by `setup_quota(enable_paid=True)`).
7. **Your service** exposes the topup UI / button (or relies on the library's checkout flow).

---

## Library defaults — what kicks in when you omit a field

If you only declare `tier_id` + `display_name` + `sort_order` + `limits`:

| Field | Default | Means |
|---|---|---|
| `billing_model` | `capacity_only` | No money side-effects from this tier |
| `price` | `None` | Free tier |
| `credit_grant` | `None` | No grants fire |
| (within credit_grant if you declare it) `lifecycle` | `use_it_or_lose_it` | Consumer-SaaS norm |
| `destination` | `subscription_credit` | For subscription grants |
| `currency` | `"USD"` | |
| `period` | `"month"` | |
| `reset_on_downgrade` | `true` | Voluntary downgrade wipes the credit |
| `reset_on_upgrade` | `false` | Upgrade preserves existing credit |
| `rollover_max_periods` | `None` | Only set for `lifecycle: rollover_capped` |

**No default is consumer-specific.** Defaults reflect the most common SaaS expectation; if your product is different, override per-tier.

---

## Common gotchas

### "My tier upgrade isn't granting credit"

Check that:
1. The `Plan` record in payment-service has the right `plan_id` set when checkout is created.
2. The Stripe Checkout session has `subscription_data.metadata.org_id` populated (post-Phase-2.1).
3. Your `quota-config.json` tier has `billing_model: "subscription_with_credits"` AND `credit_grant.trigger: "subscription_invoice_paid"`.
4. The webhook handler is wired (Phase 2.2 delivery — consumer-specific today).
5. The Stripe Invoice has `metadata.org_id` set (Stripe propagates this from subscription_data, not from session.metadata).

### "Downgrade isn't resetting subscription_credit"

Check that:
1. The old tier has `credit_grant.reset_on_downgrade: true` (it's the default — only check if you explicitly turned it off).
2. The `sort_order` on both tiers is set and the new one is strictly less than the old.
3. Your service is calling `reset_subscription_credit_on_tier_change()` after `set_tier()`.
4. The stored `subscription_credit_source_tier` matches the old tier you're downgrading from (the safety check). If a different subscription's grant overwrote this, the reset is correctly refused — see Option C migration ticket for the long-term fix.

### "Spending isn't draining `subscription_credit`"

This works ONLY when Phase 3.1 (commit Lua spend order) is deployed. Without Phase 3.1, the commit Lua reads only 3 fields and never touches `subscription_credit`. Deploy order is **billing-service Phase 3 BEFORE ab0t-quota Phase 2 fires grants** — otherwise `subscription_credit` accumulates and never drains.

### "Why is `subscription_credit` always 0 even though my tier has `credit_grant`?"

Either:
- The paid-invoice webhook (`invoice.paid` or `invoice.payment_succeeded`) isn't reaching the library (delivery not wired, or Stripe Dashboard endpoint not subscribed to either event type).
- The `subscription_data.metadata` wasn't set on the Stripe Checkout (Phase 2.1 not deployed).
- The library's webhook handler returned a non-`applied` status (check logs for `subscription_invoice_paid_skip` or `subscription_invoice_paid_failed_*`).
- The grant landed but you're reading from a stale cache (Redis `balance:{org_id}` TTL is 60s).

### "My pre-existing `initial_credit: 10` stopped working"

It shouldn't have — the back-compat shim auto-synthesizes a `credit_grant` from it. File a bug if you see this; it's a regression.

### "My consumption_only tier rejects `price`"

Yes, that's intentional. If you want to charge a fixed fee on a usage-only tier, use `subscription_unlock_only` instead. `consumption_only` means literally that — no recurring price.

---

## Migration checklist (existing consumers)

If you have an existing `quota-config.json` with `initial_credit` and want to opt into the new model:

1. **Free tier:** leave `initial_credit` as-is. The library auto-synthesizes a `credit_grant`. Or migrate to the explicit form:
   ```jsonc
   "billing_model": "consumption_only",
   "credit_grant": {
     "trigger": "signup",
     "amount_per_period": 10.00,
     "lifecycle": "persistent",
     "destination": "credit_balance"
   }
   ```
   (Drop `initial_credit` if you do this.)

2. **Paid tiers:** add `billing_model` + `price` + `credit_grant`. Choose `lifecycle` based on your customer-trust posture (use-it-or-lose-it = standard; rollover = more friendly).

3. **Verify with a single test invoice** in Stripe test mode: subscribe a test org, complete checkout, watch the invoice fire, confirm:
   - `subscription_credit` increases by `amount_per_period`.
   - `subscription_credit_source` is set to the invoice ID.
   - `subscription_credit_source_tier` matches your tier.

4. **Deploy in the right order** (see `BILLING_INTEGRATION.md` "Required wiring"). Don't ship Phase 2 (grants firing) before Phase 3 (commit Lua draining).

---

## Operational notes

### Observability keys to wire dashboards / alerts to

| Key | Severity | Meaning |
|---|---|---|
| `subscription_invoice_paid_applied` | INFO | Grant landed; happy path. |
| `subscription_invoice_paid_skip` | INFO | Expected skip (no metadata, no tier, no grant, wrong trigger). |
| `subscription_invoice_paid_failed_permanent` | ERROR | Billing-service refused; investigate. |
| `credit_grant_applied` | INFO | (billing-service) Grant successfully landed. |
| `reset_subscription_credit_applied` | INFO | Downgrade reset succeeded. |
| `reset_subscription_credit_tier_mismatch` | WARNING | Safety check refused — recorded credit belonged to a different tier. |
| `lifecycle_commit_lost_to_expiry` | **ERROR** | **Revenue loss** — reservation expired before commit could land. Page on-call. |

### When you find revenue drift

Symptom: org's transaction log shows commits, but `subscription_credit` doesn't match expected balance.

Run through:
1. Check `subscription_credit_source` + `_source_tier` — do they match the most recent invoice?
2. Check the Redis cache TTL — could be stale. Force a refresh by clearing `balance:{org_id}`.
3. Check the commit Lua return values — `result[4]` is `new_sub_credit`. If it's diverging from your expectation, the Lua's math is suspect (open a bug).

---

## Where to read next

- `ab0t_quota/billing/BILLING_INTEGRATION.md` — internal API contract details + endpoint specs.
- `ab0t_quota/models/core.py` — `TierConfig`, `CreditGrant`, and the enum definitions referenced throughout this guide.
- `ab0t_quota/billing/subscription_credit.py` — the `handle_subscription_invoice_paid` and `reset_subscription_credit_on_tier_change` helpers.
- A future migration path supports multi-source credit and per-grant expiry without changing the public endpoint contract — discussed in the library's internal design notes.
