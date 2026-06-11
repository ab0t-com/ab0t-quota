# Auth-event handler observability, replay, and business-dedup framework

**Date:** 2026-04-28
**Owner:** ab0t-quota library
**Status:** Proposal — needs sign-off before implementation
**Target release:** v0.2.7 (single ship)
**Predecessor:** v0.2.6 shipped the registry pattern + receiver + auto-subscribe. This adds the *correctness* layer for handlers that have side effects on money/state.
**Self-review:** See [`REVIEW.md`](./REVIEW.md) — earlier draft was over-engineered; this version reflects the critique.
**Related:**
- [`docs/auth-events.md`](../../docs/auth-events.md) — user-facing pattern doc (includes the existing "Zero-code default")
- [`ab0t_quota/auth_events.py`](../../ab0t_quota/auth_events.py) — registry, receiver, default handler
- [`ab0t_quota/__main__.py`](../../ab0t_quota/__main__.py) — existing CLI entry point

## What exists today (v0.2.6, important context)

The lib **already** handles the common case:

1. Consumer puts `credit_grant: {trigger, amount_per_period, lifecycle, destination}` in their `quota-config.json` tier definition.
2. `setup_quota(enable_paid=True)` auto-registers a default handler on `auth.user.registered`.
3. Default handler uses a Redis flag `credit_granted:user:{user_id}:{tier_id}` + billing's `idempotency_key` for dedup.
4. Consumer writes **zero Python** for that path.

That works for 80% of consumers. This ticket addresses the gaps for the other 20% — and gives all consumers visibility into what happened.

## The 3 gaps this ticket closes

| Gap | Symptom | What you can't do today |
|---|---|---|
| **Observability** | "Did the credit fire for user X?" | No surfaced answer. Redis flag is opaque. No query interface. |
| **Replay** | Billing was down for 5 min, 47 handlers failed silently | The lib logs a warning and moves on. Operator has no way to retry without re-triggering the real-world event. |
| **Business-key dedup** | New user joins existing org; we'd grant credit twice for the same org | The redis flag is hard-coded to `user_id+tier_id`. No path to express "per-org-per-tier" semantics. |

Everything else (delivery dedup, anti-farming, billing idempotency) the lib already does well enough.

## Design

### A. One pluggable LedgerStore

The lib needs persistent rows recording handler outcomes. Storage backend depends on what the consumer has available:

```python
class LedgerStore(Protocol):
    async def record_attempt(self, *, handler, event_id, event_payload) -> None: ...
    async def record_outcome(self, *, handler, event_id, status, reason, side_effect_id) -> None: ...
    async def already_processed(self, *, handler, event_id) -> Optional[LedgerRow]: ...
    async def already_done(self, *, dedup_key: str) -> bool: ...
    async def mark_done(self, *, dedup_key: str, handler, event_id, side_effect_id) -> None: ...
    async def query_by_user(self, user_id, *, limit=50, since=None) -> list[LedgerRow]: ...
    async def query_by_status(self, status, *, limit=50, since=None) -> list[LedgerRow]: ...
```

Three implementations, auto-selected at `setup_quota`:

| Backend | When picked | Retention | Notes |
|---|---|---|---|
| `RedisLedgerStore` | Redis available, no DDB | 72h | Default for bridge-mode / external clients. TTL is short by design — replay window, not audit log. |
| `DDBLedgerStore` | DDB available (via `app.state.ddb_client`) | 90d (DDB TTL attr) | Default for mesh services. Persistent ledger + GSI for cross-user/status queries. |
| `InMemoryLedgerStore` | Tests, degraded mode | session | Logs loudly. Fail-safe for missing infra; never use in production. |

Consumer can pass `setup_quota(ledger_store=MyStore())` to override.

### B. One decorator

```python
from ab0t_quota.auth_events import on_auth_event, idempotent

@on_auth_event("auth.user.registered")
@idempotent(
    handler="grant_initial_credit",
    key=lambda e: f"initial_credit:org:{e['data']['org_id']}:tier:free",
    retry={"attempts": 3, "backoff": "exponential"},   # default; pass retry=False to disable
)
async def grant_credit(event, ctx):
    org_id = event["data"]["org_id"]
    if await ctx.already_done():           # uses the `key` from the decorator
        return ctx.skip("already granted for this org+tier")
    txn = await billing.grant_credit(org_id, amount=10)
    await ctx.mark_done(side_effect_id=txn.id)
    return ctx.success(side_effect_id=txn.id)
```

What `@idempotent` does, in order:

1. **Delivery dedup.** Before running the handler body, check `ledger.already_processed(handler, event_id)`. If `status=success`, return cached outcome. If `status=in_progress` and lease alive, return 409 (another worker). Else: write `status=in_progress` row, run handler.
2. **Provide `ctx` arg.** `HandlerContext` has 4 methods: `already_done()`, `mark_done(side_effect_id=...)`, `skip(reason)`, `success(side_effect_id=...)`. The dedup key was supplied to the decorator, so `ctx` already knows it.
3. **Handle handler return.** `ctx.skip` / `ctx.success` are sentinel returns. Lib writes the matching ledger row.
4. **Handle handler exception.** Catch, log, mark `status=failed`. Trigger retry if configured. After max attempts, mark `status=failed_permanent`.
5. **Auto-retry (default).** In-process via `asyncio.sleep`. Bounded: 3 attempts, exponential backoff (1s, 2s, 4s), max delay 30s. Does NOT survive process restart in v1; that's v2.

Consumer concerns: business key construction (one lambda) + the handler body. Everything else is the lib.

### C. CLI for observability + replay

Extends `python -m ab0t_quota` (which already has `subscribe-events`):

```bash
# "What happened?"
python -m ab0t_quota events --user-id u123
python -m ab0t_quota events --status failed --since 1h
python -m ab0t_quota events --handler grant_initial_credit --since 1d

# "Run it again"
python -m ab0t_quota replay --event-id evt_xxx
python -m ab0t_quota replay --status failed --since 1h --confirm    # batch retry

# "Run for users who pre-existed the handler"
python -m ab0t_quota backfill --handler grant_initial_credit --user-ids u1,u2,u3
```

No HTTP admin surface in v1. CLI is enough for operators; consumers who want HTTP can wrap the CLI calls themselves. Defer to v2 if a consumer asks.

### D. Config-driven dedup key for the default handler

The lib's auto-registered `grant_initial_credit` handler reads the `dedup` field from `credit_grant`:

```jsonc
{
  "tier_id": "free",
  "credit_grant": {
    "trigger": "signup",
    "amount_per_period": "10.00",
    "lifecycle": "persistent",
    "destination": "credit_balance",
    "dedup": "per_user_per_tier"   // default; also: per_org_per_tier | per_user_global | per_org_global
  }
}
```

Default value: `per_user_per_tier` (matches today's behavior — no migration needed).

Mapping (lib-internal):

| Value | Composed key |
|---|---|
| `per_user_per_tier` (default) | `initial_credit:user:{user_id}:tier:{tier_id}` |
| `per_org_per_tier` | `initial_credit:org:{org_id}:tier:{tier_id}` |
| `per_user_global` | `initial_credit:user:{user_id}` |
| `per_org_global` | `initial_credit:org:{org_id}` |

This change is **schema-additive**. Existing configs without `dedup` keep working identically.

## Schema details

### Ledger row (DDB; analogous shape in Redis JSON)

```
PK: HANDLER#{handler_name}#{event_id}
SK: META
attrs:
  status: in_progress | success | failed | skipped | failed_permanent
  handler_name, event_id, event_type
  user_id, org_id              (extracted from payload for indexing)
  reason                       (free-text, used for skipped/failed)
  side_effect_id               (whatever the handler returns)
  attempts: int                (retry count)
  attempted_at, completed_at
  lease_expires_at             (in_progress only)
  error                        (failed only)
  event_payload                (full event JSON, captured at first attempt)
  ttl                          (epoch, for DDB TTL — 90 days)

GSI1: PK = USER#{user_id},    SK = {attempted_at}
GSI2: PK = STATUS#{status},   SK = {attempted_at}
```

### Business-dedup row (separate entity, same table)

```
PK: BIZDEDUP#{sha256(key)}
SK: META
attrs:
  raw_key                      (the consumer-supplied string, for debugging)
  source_handler, source_event_id, side_effect_id
  marked_at
  ttl                          (long — promotional credits don't expire)
```

The hash is for partition-key size safety; `raw_key` is preserved for operator debugging via the CLI.

## Answers to the four open questions

### Q1: Dedup policy — extends existing schema

Add `dedup: per_user_per_tier | per_org_per_tier | per_user_global | per_org_global` to the existing `credit_grant` block. Default `per_user_per_tier` (no behavior change). Lib's default handler uses it; custom handlers build their own keys via the `key=` decorator arg.

### Q2: Replay reads from snapshot

Lib captures full event payload at first attempt. Replay re-runs the handler with that payload. Auth-side dependencies (event log retention, subscription state) don't matter for replay. GDPR-cascade `delete_user` CLI subcommand handles the PII concern.

### Q3: Auto-retry IS the default (flipped from earlier draft)

`@idempotent` defaults to 3-attempt exponential backoff in-process. External clients without an ops team get reliability automatically. Mesh services opt out with `retry=False` if they prefer operator-explicit replay.

Cross-restart durability is v2 (would require a background worker reading from the ledger queue).

### Q4: Pluggable LedgerStore

Three backends: Redis / DDB / Memory. Auto-selected by `setup_quota` based on what's available. Consumer can pass an override. Bridge clients get Redis; mesh services get DDB; tests use memory.

## What this doesn't cover (deferred)

- **HTTP admin surface** — CLI is enough. Add later if a consumer asks.
- **Cross-restart auto-retry** — in-process retry only in v1. v2 needs a separate worker reading the ledger queue.
- **Cross-tenant / cross-service aggregation** — observability concern, not a lib concern. Each consumer has its own ledger.
- **High-volume events** (`auth.api_key.used`, `auth.token.refreshed`) — `@idempotent` is opt-in; don't apply it to event types that fire 1000s/sec. Docs make this loud.
- **Schema evolution of stored events** — if auth changes event shape between original delivery and replay, document and move on.

## Migration story (honest)

- **Handlers without `@idempotent` keep working identically.** No change.
- **Adding `@idempotent` IS a code change to the handler body.** The signature changes from `async def h(event)` to `async def h(event, ctx)`. Document this clearly in the upgrade notes. Not "transparent" — it's an explicit opt-in with a small code change.
- **Lib's auto-registered default handler adopts `@idempotent` automatically.** Consumers using the zero-code config-driven path get the framework for free, no code change.
- **Existing redis flag (`credit_granted:user:{user_id}:{tier_id}`)** stays for one release as a fast-path cache. Removed in v0.2.8 once everyone is on the ledger.

## Acceptance criteria

- [ ] `LedgerStore` Protocol + 3 implementations (Redis / DDB / Memory)
- [ ] `@idempotent(handler, key, retry)` decorator with delivery dedup + business dedup + retry
- [ ] `HandlerContext` with `already_done`, `mark_done`, `skip`, `success`
- [ ] `credit_grant.dedup` enum parsed by config loader; default handler uses it
- [ ] CLI subcommands: `events`, `replay`, `backfill`, `delete-user` (GDPR cascade)
- [ ] Tests:
  - Delivery dedup blocks 2nd run with same event_id
  - Business dedup blocks 2nd run with same key
  - Replay re-runs handler from snapshot, ledger updates correctly
  - Failed → auto-retry → success
  - Failed → max attempts → failed_permanent
  - 4 dedup policies produce the right keys
  - Each LedgerStore backend passes the same conformance suite
- [ ] UJs in sandbox-platform:
  - Adopt `@idempotent` on `grant_credit_on_signup`
  - UJ-213: signup → ledger row exists → re-deliver same event → no double-grant
  - UJ-214: two users in same org, `per_org_per_tier` policy → only first gets credit
- [ ] Docs: extend `docs/auth-events.md` with "Idempotency, replay, observability" section
- [ ] Migration note: existing handlers unchanged; `@idempotent` is opt-in with explicit signature change

## Implementation order — single release

1. Phase A: `LedgerStore` + 3 backends + conformance tests (~1 week)
2. Phase B: `@idempotent` decorator + `HandlerContext` + retry logic + tests (~1 week)
3. Phase C: CLI subcommands + sandbox-platform handler adoption + UJ tests (~3 days)
4. Phase D: docs + `credit_grant.dedup` config wiring + version bump + ship

Total: ~3 weeks for one developer. Ships as v0.2.7. Cross-restart auto-retry is v2 (v0.2.8 or later when needed).
