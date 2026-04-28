# BillingHome — design draft

**Date:** 2026-04-28
**Status:** Draft for review. Replaces earlier "lib looks up workspace" sketch.
**Scope:** Phase 6 of the parent ticket. Owns the user→billable-org relationship inside ab0t-quota itself.

## The contract

Every paid operation in the lib resolves the target org via:

```
explicit (set by consumer or operator) > stored (auto-set on first paid touch) > JWT.org_id (last-resort fallback)
```

One read API:
```python
billable_org = await quota.billing_home.resolve(user_id, fallback_org_id=jwt.org_id)
```

One write API:
```python
await quota.billing_home.set(user_id, org_id, source="consumer" | "operator")
```

Sticky-on-first-resolve: when `resolve()` falls through to `fallback`, it ALSO writes that value back as the stored billing_home with `source="auto"`. Future calls hit the stored value, no inference.

## Code change locations

### New files
- `ab0t_quota/billing_home.py` — `BillingHome` dataclass, `BillingHomeStore` class with `get/set/resolve`. ~120 lines.
- `tests/test_billing_home.py` — unit tests for the store. ~150 lines.

### Edited files
- `ab0t_quota/setup.py` — `setup_quota()` instantiates `BillingHomeStore` against `QUOTA_STATE_TABLE` and exposes it via `app.state.quota_billing_home`. ~10 lines.
- `ab0t_quota/billing/router.py:142` (`/billing/balance` handler), `:298` (`/checkout`), `:389` (tier-resolve) — every place that uses `consumer_org_id` parameter directly switches to `await store.resolve(user_id, fallback_org_id=consumer_org_id)`. ~5 places, 1 line each.
- `ab0t_quota/persistence.py` — add DDB schema constants for the new entity (PK pattern `USER#{user_id}`, SK `BILLING_HOME`). ~20 lines.

### Consumer-side (sandbox-platform)
- `app/quota.py:319` `maybe_apply_initial_credit_for_org()` — wraps the existing `org_id` parameter with the resolver: `org_id = await store.resolve(user_id, fallback_org_id=org_id)`. 1 line.
- **Optional**: `app/main.py` post-registration hook (or workspace-creation event handler) calls `await quota.billing_home.set(user_id, workspace_org_id, source="consumer")` when sandbox-platform knows the workspace just landed. ~5 lines. Recommended but not required — Path A handles it correctly without this.

## DDB schema

```
PK: USER#{user_id}
SK: BILLING_HOME
attrs:
  org_id: str          # the billable org
  set_at: ISO timestamp
  source: "auto" | "consumer" | "operator"
  prev_org_id: str?    # only set if value was changed (audit trail)
GSI1: org_id (for "list users billing against this org" — operator queries)
```

Lives in the existing `QUOTA_STATE_TABLE` (`ab0t_quota_state`). No new table.

## Specific drawbacks (honest)

1. **Sticky-on-first-resolve = sticky to whatever was wrong if the first touch was wrong.** If a sandbox-platform user signs up, dashboard fails to switch to their workspace, then they create a sandbox → first paid touch resolves to parent org → that becomes their billing home forever. Mitigation: ship the explicit-set hook (Path B) for sandbox-platform so this race never matters. Also: operator override endpoint to fix outliers.
2. **DDB write on first paid call adds latency.** ~10ms p50 for a single PutItem. Hidden inside the existing reserve flow which already does multiple DDB ops, so net impact is small but non-zero. Could batch with the reservation write to avoid the extra round-trip.
3. **`source="auto"` rows can't be distinguished from intentional ones at a glance.** If an operator wants to clean up bad auto-set values, they'd have to filter on `source` AND check whether the org still exists / matches the user's current workspace. Documentation problem.
4. **No multi-billing-home support.** A user can have exactly one billing home today. Future "user has separate personal vs work accounts" requires schema change. Not painful but worth noting.
5. **Migration for existing users.** Anyone who already has a balance in some org is "already pinned" implicitly; on their next paid touch the lib would see no `BILLING_HOME` row and write whatever JWT.org_id is at that moment — could be different from where their actual money is. Need a one-shot backfill: scan `BillingAccount` rows, write `BILLING_HOME` for every (org_id, user with balance) pair. Doable but real work.
6. **The library now owns persistent user-keyed state.** Today the lib only owns org-keyed state (tier assignments, snapshots). Adding user-keyed adds GDPR-deletion considerations — when a user is deleted, the lib needs to clean up too. Add a `delete_user(user_id)` API.
7. **Two consumers concurrently calling `set()` with different values race.** Last-write-wins, no conflict detection. Could add `If-Match` semantics on prev value but adds complexity. For Path A this can't happen (single resolver path); for Path B + operator-set, the race is real but narrow.
8. **The `source` field is advisory only.** Nothing prevents Path A's auto-set from overwriting an operator-set value if the read-then-write isn't atomic. Need a conditional write: "set only if no row exists." Standard DDB pattern, but easy to forget.

## What this displaces from earlier in the ticket

- "Lib looks up workspace via auth" — gone. No auth dependency.
- "billable_entity setup_quota arg" — gone. No consumer config needed.
- "New billing-service endpoint" — gone. Billing stays dumb.
- "JWT inference inside the lib" — gone. JWT.org_id is only ever the last-resort fallback.

## Acceptance criteria

- [ ] `BillingHomeStore.set/get/resolve` round-trips correctly
- [ ] First call to `resolve()` with no stored value writes the fallback (sticky)
- [ ] Subsequent calls return the stored value even if `fallback_org_id` differs
- [ ] Conditional write prevents auto from overwriting consumer/operator values
- [ ] Credit grant on `/api/billing/balance` lands in the resolved org, not JWT.org_id
- [ ] All three paths (auto, consumer, operator) are covered by UJs
- [ ] Operator backfill script for existing users
- [ ] `delete_user(user_id)` for GDPR

## UJ tests for this design

Three tests covering the load-bearing claims. All RED today, will go GREEN as Phase 6 lands. Sandbox-platform tests (it's the consumer; lib has no HTTP surface to UJ directly).

| UJ | Path | Proves |
|---|---|---|
| **UJ-208** | A — auto sticky | First paid touch writes BILLING_HOME = JWT.org_id; second touch with different JWT scope still hits the stored org |
| **UJ-209** | B — explicit set | Consumer calls `billing_home.set()` for the user → credit goes to that org regardless of JWT scope |
| **UJ-210** | Security regression | Credit cannot accidentally leak to a different org via JWT manipulation once billing home is pinned |

UJ scripts at `resource/output/sandbox-platform/scripts/curl_tests/user_journeys/UJ-208..210_*.sh` (created alongside this draft).
