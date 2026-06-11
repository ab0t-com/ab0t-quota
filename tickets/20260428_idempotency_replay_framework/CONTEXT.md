# Context — files, current state, and constraints

Reference for whoever implements this. Written 2026-04-28 against ab0t-quota v0.2.6.

> **Note on scope.** The original TICKET.md was over-engineered. See [`REVIEW.md`](./REVIEW.md) for the critique. The current TICKET.md reflects the scaled-back design: pluggable LedgerStore (3 backends — Redis / DDB / Memory), one `@idempotent` decorator, CLI-only operator surface, auto-retry default, config-extends-existing `credit_grant`. Some details below (e.g. "own DDB table" framing, the larger HandlerContext API) reflect the older proposal — read TICKET.md as the source of truth.

## What exists today (v0.2.6)

- `ab0t_quota/auth_events.py` — registry (`register_handler`, `on_auth_event`), webhook receiver (`make_router`), auto-subscribe (`subscribe_on_startup`), and primitives (`PinStore`, `resolve_billing_org`, `grant_initial_credit_for_user`).
- `ab0t_quota/setup.py` — mounts the receiver and schedules `subscribe_on_startup` in lifespan. Also (recently) auto-registers a default signup-credit handler when `enable_paid=True` and tiers declare `initial_credit` or `credit_grant` — see `docs/auth-events.md` "Zero-code default" section.
- `ab0t_quota/__main__.py` — `subscribe-events` subcommand. Has a TODO for a future `sync-plans` subcommand. Argparse-based, easy to extend.
- `ab0t_quota/persistence.py` — `QuotaStore` (DDB-backed, used for tier assignments, overrides, snapshots). Pattern to mirror for the new `HandlerLedgerStore`.
- `tests/test_auth_events.py` — 26 tests covering registry, HMAC, dispatch, subscribe. Uses a `_MockAuth` helper (no `respx` dependency) — reuse the pattern for ledger tests.

## What changes (this ticket)

### New files
- `ab0t_quota/handler_ledger.py` — `HandlerLedgerStore`, `HandlerContext`, `@idempotent` decorator
- `tests/test_handler_ledger.py` — unit tests for the new module
- `docs/auth-events.md` extension: new "Idempotency & replay" section (don't replace the existing doc)

### Edited files
- `ab0t_quota/auth_events.py` — receiver wraps each handler invocation in the `@idempotent` machinery when present; otherwise behaves as today
- `ab0t_quota/setup.py` — auto-decorates the default signup-credit handler with `@idempotent`
- `ab0t_quota/__main__.py` — add `events`, `replay`, `backfill` subcommands
- `ab0t_quota/models/tier.py` (or wherever `TierConfig` lives) — `credit_grant.dedup` enum
- `ab0t_quota/config.py` — pass `credit_grant.dedup` through to the loader
- `pyproject.toml` + `__init__.py` — version bumps per phase
- `docs/quickstart.md` env table — note new optional `AB0T_QUOTA_HANDLER_LEDGER_TABLE` env var

### Consumer-side
- `resource/output/sandbox-platform/app/quota.py` — adopt `@idempotent` on `grant_credit_on_signup`
- `resource/output/sandbox-platform/quota-config.json` — add `credit_grant.dedup` to free tier (recommend `per_user_per_tier`)

## DDB table schema (proposed)

```
Table: ab0t_quota_handler_ledger
PK: HANDLER#{handler_name}#{event_id}
SK: META
attrs:
  status: "in_progress" | "success" | "failed" | "skipped"
  handler_name: str
  event_id: str
  event_type: str
  user_id: str (optional)
  org_id: str (optional)
  reason: str (free-text, used for skipped/failed)
  side_effect_id: str (optional — what the handler did downstream)
  attempted_at: ISO timestamp
  completed_at: ISO timestamp (success/failed/skipped only)
  lease_expires_at: ISO timestamp (in_progress only — for concurrent-worker arbitration)
  error: str (failed only)
  event_payload: str (JSON, full event body, captured at receive time for replay)
  ttl: epoch (90-day TTL, DDB-managed)

GSI1 (user lookup):
  PK: USER#{user_id}
  SK: {completed_at or attempted_at, ISO}

GSI2 (status queue):
  PK: STATUS#{status}
  SK: {attempted_at, ISO}

Business dedup entity in same table:
PK: BIZDEDUP#{key_hash}
SK: META
attrs:
  raw_key: str (the human-readable key the consumer supplied)
  source_handler, source_event_id, side_effect_id
  marked_at: ISO timestamp
  ttl: epoch (configurable per dedup key; defaults to "never" for promotional credits)
```

`key_hash` is `sha256(raw_key).hexdigest()` — lets consumers use arbitrarily long composed keys (`f"initial_credit:org:{org_id}:tier:{tier_id}:promo:{promo_id}"`) without hitting DDB partition-key size limits, while keeping the human-readable form for debugging via `raw_key` attribute.

## Constraints that aren't obvious from reading the code

1. **The lib doesn't own user-keyed state today.** Adding ledger rows keyed on user_id is the first time. This brings GDPR-deletion into scope — when a user is deleted, the lib needs `delete_user(user_id)` that scans GSI1 and removes matching rows. Document this for consumers; add a CLI subcommand `python -m ab0t_quota delete-user --user-id u123`.

2. **DDB conditional writes are how delivery dedup works correctly under concurrency.** Two webhook deliveries of the same event_id race; only one can win the `attribute_not_exists(PK)` on the in_progress write. Loser sees the existing row, returns the cached outcome (or 409 if still in_progress). This is the same pattern billing-service uses for `/reserve` (see `code/billing/output/app/api/billing.py:296` `reserve_funds` — read for inspiration).

3. **The receiver returns 200 to auth no matter what.** Don't change this. Handler-level failures DO NOT trigger auth-side retry. Auth-side retry happens only on HTTP 5xx from the receiver, and we control that path explicitly. This is intentional — see Q3 in TICKET.md.

4. **The current sandbox handler's redis flag (`credit_granted:user:{user_id}:{tier_id}`) becomes redundant after this lands.** Migration: keep the redis flag in place for backwards-compat for ~1 release, then remove it. The DDB ledger is the new source of truth; redis becomes a fast-path cache only.

5. **Event payload contains PII.** Encrypt at rest is automatic on DDB. Don't log payloads at INFO level. Operator-facing CLI must redact email/name unless an `--unredacted` flag is set (and that flag should require an MFA-confirmed admin token — punt to operator policy for now, just document).

6. **The `event_id` from auth's webhook is not guaranteed unique across all events.** Two different subscriptions could deliver the same event_id (auth may reuse), and a replay-via-test fires a synthetic event_id. Mitigation: the dedup key is `(handler_name, event_id)` — not `event_id` alone — so different handlers can process the same event independently and replays from `/test` won't collide with real events (which carry the real subscription's `event_id`).

7. **Sandbox-platform has UJ tests numbered 208-212.** New tests for this work start at UJ-213. Keep the numbering sequential.

## API contracts that must hold

### Handler signature evolution

```python
# v0.2.6 (current):
async def handler(event: dict) -> None: ...

# This ticket adds (optional second arg when @idempotent is used):
@idempotent(handler="x")
async def handler(event: dict, ctx: HandlerContext) -> Any: ...
```

The receiver dispatches differently based on whether `@idempotent` was applied. Detect via a sentinel attribute the decorator sets (`handler._ab0t_idempotent = True`). Backwards-compatible.

### `HandlerContext` shape

```python
class HandlerContext:
    handler_name: str
    event_id: str
    event_type: str
    event_payload: dict
    ledger: HandlerLedgerStore

    def dedup_key(self, name: str, **components) -> str:
        """Compose a key from named components. Order-independent, stable."""

    async def already_done(self, key: str) -> bool:
        """True if a BIZDEDUP row exists for this key."""

    async def mark_done(self, key: str, *, side_effect_id: Optional[str] = None) -> None:
        """Write the BIZDEDUP row."""

    def skip(self, reason: str) -> SkipOutcome:
        """Return-value sentinel. Lib records status=skipped with reason."""

    def success(self, *, side_effect_id: Optional[str] = None) -> SuccessOutcome:
        """Return-value sentinel. Lib records status=success."""
```

### Failure mode invariants

- Handler exception → status=failed, error stored, **handler does NOT run again** until operator replays
- Handler timeout (configurable, default 30s) → status=failed, error="timeout"
- Lease expiration (default 60s, longer than handler timeout) → next delivery can claim it
- DDB unavailable → log error, fall through to handler-without-ledger (don't lose events). Re-flag as needing replay once ledger recovers? Open question; recommend log+continue, don't block.

## Reading order for an implementer

1. `docs/auth-events.md` — understand the existing pattern
2. `ab0t_quota/auth_events.py` — see the registry + receiver
3. `ab0t_quota/persistence.py` — see the existing DDB pattern to mirror
4. `tests/test_auth_events.py` — see the existing test pattern (`_MockAuth` helper)
5. This ticket's TICKET.md — read the design
6. `billing/output/app/api/billing.py:296` `reserve_funds` — read how billing does concurrent-write idempotency in DDB (similar problem, different domain)

## Open questions for the implementer

1. Should `HandlerContext` be a sync or async context manager? Currently designed as a plain object; could be `async with` if we want explicit close semantics. Recommend plain object.
2. Should we record event payloads gzipped? Probably no — payloads are <1KB each, DDB row limit is 400KB, compression is fighting yesterday's problem.
3. Do we need a "soft-delete" for ledger rows for compliance audit purposes, or is hard-delete OK? Recommend hard-delete with TTL; audit happens via CloudWatch Logs of the ledger writes themselves.
4. The CLI's `replay` subcommand: should it write a new ledger row (with the same `event_id` but `attempted_at` updated) or update the existing one in place? Recommend update-in-place with an attribute history attribute, so the `event_id`-keyed row remains unique.
