# Implementation Plan — Canonical Plans in ab0t-quota

Phased so each step is independently shippable. Stop at any phase if priorities shift.

## Phase 1 — Schema + dataclass (lib-internal, no behavior change)

Add the data model. Nothing else uses it yet. Safe to ship.

- `ab0t_quota/plans.py` (new): `Plan` dataclass — `plan_id`, `tier_id`, `display_name`, `description`, `pricing` (currency/amount/interval), `visibility`, `sync_to_stripe`, `metadata`.
- `ab0t_quota/config.py`: `load_plans(config) -> list[Plan]`. Returns `[]` if no `plans[]` block. Validates `tier_id` references an existing tier (loud error if not).
- Tests: schema parsing happy-path, missing optional fields, dangling `tier_id` rejection.

**Ship as v0.3.0 (minor bump).** No consumer needs to change anything.

## Phase 2 — Sync logic + CLI

Add the `sync_plans()` function and CLI entrypoint. Still no consumer-side change required.

- `ab0t_quota/plans.py`: `async sync_plans(payment_url, api_key, consumer_org_id, plans, dry_run=False) -> SyncResult`
  - Lists existing plans via `GET /checkout/{org}/plans?include_prices=true`
  - Diffs against config's `plans[]`
  - Creates missing plans via `POST /plans/{org}/`
  - Creates missing prices via `POST /plans/{org}/{plan_id}/prices`
  - Returns `SyncResult { created_plans, created_prices, skipped, errors }`
  - Never deletes
  - Never modifies config
- `ab0t_quota/__main__.py` (new): argparse-based CLI dispatcher
  - `python -m ab0t_quota sync-plans [--dry-run] [--config path] [--payment-url ...] [--api-key ...]`
  - Reads from env if flags not given (`AB0T_MESH_PAYMENT_URL`, `AB0T_MESH_PAYMENT_API_KEY`, `AB0T_CONSUMER_ORG_ID`)
  - Exit code 0 = success or no-op, non-zero = anything failed
- Tests: dry-run, all-new (creates), all-existing (no-op), partial (some new), payment-service down (clean error)

**Ship as v0.4.0.**

## Phase 3 — Sandbox-platform migration

Move sandbox-platform off `seed_plans.sh` to use the lib.

- Add `plans[]` block to `sandbox-platform/quota-config.json` mirroring current bash arrays:
  ```json
  "plans": [
    { "plan_id": "starter-monthly", "tier_id": "starter", "display_name": "Starter Monthly",
      "pricing": {"currency":"usd","amount":29,"interval":"month"}, "visibility":"public", "sync_to_stripe":true },
    { "plan_id": "starter-annual",  "tier_id": "starter", ... amount: 290, interval: year ... },
    { "plan_id": "pro-monthly",     "tier_id": "pro",     ... amount: 99, interval: month ... },
    { "plan_id": "pro-annual",      "tier_id": "pro",     ... amount: 990, interval: year ... }
  ]
  ```
- Replace `scripts/seed_plans.sh` body with: `exec python -m ab0t_quota sync-plans "$@"` (keeps backwards-compat for anyone who has the script in muscle memory)
- Or delete `seed_plans.sh` entirely and update docs to reference `python -m ab0t_quota sync-plans`
- Also fix the `AUTH_URL` vs `AUTH_SERVICE_URL` env-var-name bug while we're here

**Ship as a sandbox-platform commit, no version bump for the lib.**

## Phase 4 — Auto-sync option (opt-in)

For consumers who want the convenience and accept the boot-time dependency.

- `ab0t_quota/setup.py:setup_quota(...)` gains `sync_plans_on_startup: bool = False` flag
- When True, lifespan adds a startup task that calls `sync_plans()` and logs the result
- Failure is logged but does NOT block startup (paid surface might be silently unsync'd until next boot — make this loud in logs)
- Tests: opt-in path runs, opt-out path doesn't make any payment-service calls at boot

**Ship as v0.5.0.**

## Phase 6 — Initial credit grant as a library primitive

See [`INITIAL_CREDIT_INVESTIGATION.md`](./INITIAL_CREDIT_INVESTIGATION.md) for the full trace. Summary: free accounts show $0 because the only call site for `maybe_apply_initial_credit_for_org` is inside `enforce_user_cost_limit`, which fires on first sandbox creation — not on signup or first dashboard load.

- `TierConfig.initial_credit: Optional[Decimal]` — first-class field (currently silently dropped by `load_tiers()`)
- `ab0t_quota/credits.py` (new): `apply_initial_credit_lazy(org_id, user_id, tier_id)` — idempotent (Redis flag + billing idempotency-key), safe to call on every request
- Wire into the billing router middleware: runs once per (user_id, tier_id) on first authenticated request, before the balance response is built. Removes the "user sees $0 forever" failure mode.
- Move `REQUIRE_EMAIL_VERIFICATION_FOR_PROMO` env into a `setup_quota(require_email_verified_for_credit=True)` arg
- Admin route `POST /api/quotas/credits/backfill` for one-shot grant of users who signed up before the fix
- Tests: lazy-grant on first hit, idempotent on subsequent hits, anti-farming gate, billing-side idempotency replay handled cleanly

**Ship as v0.5.0 alongside Phase 4 (or earlier — this is the higher-impact user-visible fix).**

## Phase 5 — Docs + deprecation

- Update `README.md`: mention `plans[]` in the quota-config example
- Update `docs/quickstart.md`: add a "Selling your tiers" section
- Update `docs/deployment.md`: replace the manual seed step with `python -m ab0t_quota sync-plans`
- Add a migration section: "If you have a hand-written seed_plans.sh, here's how to convert it"
- Mark the old approach (consumer-managed seed scripts) as deprecated in changelog

**Ship as v0.5.1 doc-only release.**

---

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| `payment-service` API contract changes (plan or price create endpoint) | Cover with a UJ test in payment-service's repo that asserts the schema we depend on |
| Two consumers run `sync-plans` concurrently | Idempotency at the payment-service level (existing dedupe by name); worst case is one creates, one no-ops |
| Consumer accidentally removes a plan from config and re-runs sync | Sync never deletes — orphan plans stay in payment-service, customers on those plans keep their subscription. Document this clearly. |
| Stripe price IDs change between syncs | They don't if payment-service is properly idempotent on `(plan_id, amount, interval)`. Verify. |
| Existing `seed_plans.sh` users miss the deprecation | Phase 3's wrapper script keeps the old invocation working |

## Files touched

| File | Phase | Change |
|---|---|---|
| `ab0t_quota/plans.py` | 1 | new — Plan dataclass + Pydantic-free parser |
| `ab0t_quota/config.py` | 1 | add `load_plans()` |
| `ab0t_quota/__main__.py` | 2 | new — CLI dispatcher |
| `ab0t_quota/plans.py` | 2 | extend — `sync_plans()` async fn |
| `ab0t_quota/setup.py` | 4 | add `sync_plans_on_startup` flag to `setup_quota()` |
| `tests/test_plans.py` | 1+2 | parsing + sync tests |
| `README.md`, `docs/*.md` | 5 | docs + deprecation note |
| `sandbox-platform/quota-config.json` | 3 | add `plans[]` block |
| `sandbox-platform/scripts/seed_plans.sh` | 3 | shrink to wrapper or delete |

## Estimate

- Phase 1: ~half day
- Phase 2: ~1 day (most of it is sync diffing logic + tests)
- Phase 3: ~1 hour
- Phase 4: ~half day
- Phase 5: ~half day

Total: ~3 days, or ship 1+2+3 (the load-bearing slice) in ~2 days.
