# Initial credit grant — why free accounts see $0 (investigation)

**Reported:** 2026-04-28 — free accounts are not receiving the configured $10 promotional credit.
**Verified:** Configuration is correct. The bug is architectural — credit grant is lazy, only fires on first resource creation, not on signup.

## What the user expects

Sign up → dashboard shows $10 balance → user can experiment without paying.

## What actually happens

Sign up → dashboard shows $0 balance → user assumes no credit → bounces.

## Trace (verified live, all curls passed)

### 1. Config is correct

`sandbox-platform/quota-config.json .tiers[0]`:
```json
{ "tier_id": "free", "initial_credit": 10.00, ... }
```

### 2. The grant function exists and is wired

`sandbox-platform/app/quota.py:319` — `maybe_apply_initial_credit_for_org(org_id, tier_id, user_id, email_verified)`:
- Reads `_initial_credits()` (cached) → returns `{"free": 10.0}` ✅
- Checks `REQUIRE_EMAIL_VERIFICATION_FOR_PROMO` env (default `true`)
- Checks Redis dedup flag `promo_applied:user:{user_id}:{tier_id}`
- Calls `POST {billing_url}/billing/{org_id}/promotional-credit` with `amount`, `reason`, `idempotency_key`

### 3. Env gate is correctly disabled

Container env (`docker compose exec sandbox-platform env | grep EMAIL`):
```
REQUIRE_EMAIL_VERIFICATION_FOR_PROMO=false
```
So the email-verification gate is **not** the blocker. Even though the JWT carries `email_verified=null` (verified live — prod auth doesn't set this claim on auto-confirmed registrations), the code path proceeds.

### 4. Redis is reachable

`QUOTA_REDIS_URL=redis://host.docker.internal:6382/4` set in container.

### 5. The actual bug — only ONE call site, only fires on first resource creation

```
$ grep -rn 'maybe_apply_initial_credit' app/
app/main.py:279:    await quota_module.maybe_apply_initial_credit_for_org(...)
```

That call is inside `enforce_user_cost_limit()` (`app/main.py:253`), which is called from:
- Sandbox create (`/api/sandboxes` POST)
- Sandbox restart
- Browser session create
- Desktop session create

**There is no call on signup, no call on first dashboard load, no call on first balance check.**

So a fresh user who signs up and visits the dashboard → never triggered a quota check → `maybe_apply_initial_credit_for_org` never ran → no credit in billing → balance shows $0.

### 6. Secondary risk — billing idempotency replay

The function sends `idempotency_key = "user:{user_id}:initial_credit:{tier_id}"`. If billing-service has seen this key before (e.g. from a previous run of the same user against a wiped Redis), it returns 400 (idempotency replay). The code treats both 200 and 400 as success and sets the Redis flag for 30 days. So if billing's idempotency record outlasts a Redis wipe, you get a no-op that looks like success.

## Why this is "related" to the plans ticket

Both are the same shape of "drop-in promise broken":
- Paid tier purchase: lib has tier definitions (good) but plan publication / Stripe sync is in a per-consumer ad-hoc bash script (bad)
- Free tier credit: lib has `initial_credit` in config (good) but grant logic is in a per-consumer one-off function (`sandbox-platform/app/quota.py`) and only fires lazily (bad)

In both cases, the lib should own the lifecycle:
- Plan ticket: `python -m ab0t_quota sync-plans` (operator-driven)
- Credit grant: should fire automatically on first balance check, OR on signup webhook, OR be a library primitive that any consumer can wire to its own auth-event handler

## Recommendation — add to this ticket's scope

Add a Phase 6 to `IMPLEMENTATION_PLAN.md` for credit grant:

### Phase 6 — Credit grant as a library primitive

- `ab0t_quota/credits.py` (new): `apply_initial_credit(payment_url, billing_url, api_key, org_id, user_id, tier_id) -> Optional[Decimal]`
  - Idempotent (Redis flag + billing-side idempotency key)
  - Returns granted amount or None
  - Reads `tier.initial_credit` (NEW first-class field on `TierConfig` — currently silently dropped by `load_tiers()`)
- `ab0t_quota/credits.py:apply_initial_credit_lazy(...)` — same fn but checks the Redis dedup flag first; safe to call on every request
- Wire into `setup_quota`'s billing router: middleware that runs `apply_initial_credit_lazy()` on first authenticated request, **before** returning the balance
  - Removes the "user sees $0 forever" failure mode
  - Idempotent so no perf hit on subsequent requests (Redis check is sub-ms)
- Bonus: a new admin route `POST /api/quotas/credits/grant` for one-off operator backfill of users who signed up before the fix

### Why this isn't a separate ticket

Same architectural failure (consumer reimplements lib responsibility), same component (ab0t-quota), same files touched (`config.py`, `setup.py`), same release cycle. Splitting tickets would force coordinating two PRs for one user-facing fix.

### Acceptance criteria additions

- [ ] `TierConfig.initial_credit: Decimal` first-class field
- [ ] `load_tiers()` parses it (no longer silently dropped)
- [ ] `apply_initial_credit_lazy()` wired into balance route middleware
- [ ] First dashboard visit after signup → balance shows configured credit (live test on dev env)
- [ ] Anti-farming gate (`REQUIRE_EMAIL_VERIFICATION_FOR_PROMO`) preserved as a setup_quota arg, not just an env var
- [ ] Backfill admin route documented

## Critical context — the workspace flag is auth-owned, not lib-owned

Verified by reading code:

- The auth service has a per-end-users-org config: `login_config.registration.org_structure.pattern`
- Values: `"flat"` (default — user stays on parent org) or `"workspace-per-user"` (registration spawns a child workspace org with user as owner)
- Code: `auth/output/appv2/modules/hosted_login/api/login_config.py:92-104`, dispatched by `event_handlers/workspace_provisioning.py:308`
- Live state: sandbox-platform-users has `workspace-per-user`. billing/payment/integration have no hosted-login config at all — they're internal mesh services without end-users orgs.

**This means the flag is auth's concern, not ab0t-quota's.** Auth decides which org_id to put in the JWT based on its own setting. Downstream services just see "JWT has an org_id" — they don't need to know what pattern the auth org is configured with.

## Resolution to "which org gets the credit" (Q1 below)

### Verified facts about the workspace flow (curl-tested 2026-04-28)

- Registration JWT.org_id = **parent end-users org** (e.g. `cd790b95-…` "sandbox-platform Users"). NOT the workspace.
- The user is ALREADY a member of that parent org at registration — that IS their identity. There is no separate "personal org" concept.
- The workspace org materializes **asynchronously** as a sibling. Visible in `/users/me/organizations` after a few seconds, with `role: owner` and `settings.type: user_workspace`.
- Auth does **NOT** auto-re-issue a workspace-scoped JWT. Verified: zero call sites in auth/appv2 for any "switch token to workspace post-registration" path.
- The JWT only carries workspace org_id if the **consumer's dashboard** explicitly calls `/auth/switch-organization` (sandbox-platform's `callback.html` does this; new consumers wouldn't unless they remember to).

### Implication

A naive "lib trusts JWT.org_id" approach **fails** in the workspace-per-user case if the consumer's dashboard hasn't switched: credit lands in the shared parent org, not the user's workspace. The credit is technically still attributed to the user (anti-farming dedup), but the dollars are in a shared pool — effectively lost from the user's POV.

### The right resolution

**The lib looks up the user's workspace before crediting**, with safe fallback to JWT.org_id. Behavior:

```
def resolve_billable_org(jwt, user_id):
    orgs = await auth_client.list_user_orgs(user_id)
    workspace = next(
        (o for o in orgs if o.role == "owner" and o.settings.type == "user_workspace"),
        None,
    )
    return workspace.id if workspace else jwt.org_id
```

This:
- ✅ Works for workspace-per-user mode without consumer-side dashboard discipline
- ✅ Works for flat mode (no workspace exists → fallback to JWT.org_id = parent, which IS the billable entity in flat mode)
- ✅ Stays drop-in (no new consumer config knob)
- ✅ Lib remains agnostic to whether consumer chose workspace mode — auto-detects from the user's org graph
- ⚠️ Costs one extra auth call per credit grant. Credits are rare (once per user lifetime), so the cost is negligible. Cache the result for the request lifetime.

Anti-farming dedup remains keyed on `user_id` regardless of which org receives the credit.

### Alternatives considered and rejected

- **A. Status quo (lib trusts JWT.org_id, dashboard must switch).** Brittle — consumer-side failure modes silently misroute credits.
- **C. Auth re-issues JWT when workspace materializes.** Cleanest separation but bigger architectural change; workspace creation is async so registration response can't include it. Defer.
- **D. Consumer config arg (`billable_entity="workspace"|"user"|"current"`).** Adds a knob the consumer must remember to flip in two places. Rejected for violating the drop-in principle.

## Historical note (does not require lib changes)

Before workspace-per-user shipped, the user was effectively the billable entity (one user = one parent-org membership = one balance). Some legacy billing-service records exist keyed to old user-org pairs. The lib doesn't need a migration path because:
- The lib already credits whatever org the JWT names
- Pre-workspace data lives where it lives; nothing moves it silently
- A backfill admin route (Phase 6) handles edge cases per-org explicitly

## Open questions for this sub-issue

1. ~~**Which org does the credit land in?**~~ — answered above. **JWT.org_id, no consumer config needed.** The auth-side workspace flag determines what org_id the JWT carries; the lib just trusts it. Drop-in stays drop-in.
2. **What about users who signed up before this lands?** Either backfill via admin route, OR let the lazy grant fire next time they hit the dashboard (already idempotent, so no risk). For users on the legacy user-as-billable model, backfill needs to write to their user-org rather than a workspace.
3. **Should `email_verified` checking move to a setup_quota config option?** Right now it's a bare env var read by the consumer's quota.py. The lib should own this gate too.
4. **NEW: Migration for existing billing-service balance records.** If old records are keyed to user-orgs that don't match the new workspace model, do we leave them as-is (legacy users stay on their old orgs forever), or write a migration that transfers balances to the workspace? Recommend leave-as-is — moving money around silently is a support nightmare.
