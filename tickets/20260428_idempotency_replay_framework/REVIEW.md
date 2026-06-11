# Self-review of TICKET.md — what I missed and what I'd change

After re-reading the system, my own ticket has real problems. Honest critique here so we don't build the wrong thing.

## Things I didn't know when I wrote TICKET.md (and they matter)

### 1. The "Zero-code default" already exists

`docs/auth-events.md:55-103` documents a working pattern:
- Consumer puts `credit_grant: {trigger, amount_per_period, lifecycle, destination}` in their `quota-config.json` tier definition
- `setup_quota(enable_paid=True)` **auto-registers** a default handler that grants credit on `auth.user.registered`
- Consumer writes **zero Python** for the common case

The schema is richer than I thought — `lifecycle`, `destination`, `trigger`, `amount_per_period`. There's clearly been design work on this already.

**What this changes:** my "consumer-declared dedup policy" proposal isn't introducing a new concept; it's extending an existing schema. The framing in TICKET.md was wrong — I wrote it as if the lib was a clean slate. The honest framing is: "extend the existing `credit_grant` block with a `dedup` field."

### 2. The lib already has a de-facto dedup policy

`auth_events.py:208-244` — the existing default handler:
```python
flag_key = f"credit_granted:user:{user_id}:{tier_id}"
if await redis.get(flag_key):
    return
# ...
await redis.set(flag_key, "1", ex=86400 * 30)
```

Plus billing's own `idempotency_key = f"user:{user_id}:initial_credit:{tier_id}"`.

So the lib has already chosen `per_user_per_tier` with a 30-day TTL on the redis flag. My ticket said "lib is opinion-free" — that's not actually true. The lib is opinionated; I should propose making the opinion **configurable**, not invent the concept.

### 3. The lib is positioned for EXTERNAL clients, not just mesh services

`docs/quickstart.md:1` literally titled "Quickstart for External Clients." `README.md:3` says "any service in the mesh." The lib is sold to random SaaS companies, not just ab0t-internal services.

**What this changes:**
- External clients may not have DDB access (no AWS account, running on Fly.io or Railway)
- External clients may not have an ops team to run `python -m ab0t_quota replay --status failed`
- External clients want the lib to handle correctness for them, not give them tools to handle it themselves

My proposed framework assumes mesh-service-style ops. For external clients it's over-engineered.

### 4. Bridge mode exists and changes the storage equation

`docs/quickstart.md:23` — `bridge` mode is HTTPS-only, no Redis assumed. Bridge clients have no DDB, may have no Redis, may have just an external HTTP API to call.

**What this changes:** my proposal mandates a new DDB table. Bridge clients can't use DDB. The ledger storage layer needs to be pluggable, or the framework only applies to `byo_redis` / mesh-deployed consumers.

### 5. Real consumers today

The grep showed three real ab0t-quota consumers:
- `billing/output/app/modules/quota/` — billing-service uses the lib internally (interesting — it's not just consumers; the mesh service itself depends on it)
- `resource/output/sandbox-platform/app/` — sandbox-platform
- That's it for now in this repo

Both are mesh-internal. So today's actual clients all have DDB + Redis + ops. But the README is selling externally, so the design has to anticipate the external case.

### 6. 25 event types, not just user.registered

Auth's `/events/types` returns 25 event types across 6 categories. Common ones include:
- `security.suspicious.activity` — likely needs DIFFERENT semantics than credit-grant (alert routing, not idempotent grant)
- `auth.api_key.used` — high-volume, could be 1000s per second; ledger row per event would explode storage
- `auth.token.refreshed` — also high-volume

**What this changes:** the framework can't assume "every handler needs a ledger row." High-volume events should have a way to opt out of ledger persistence and just dispatch. My proposed `@idempotent` was opt-in, which is correct — but the lib's docs should make crystal clear "don't put @idempotent on high-volume events."

### 7. The `ctx` arg is a breaking change to handler signatures

`auth_events.py:36`:
```python
async def handler(event: dict) -> None
```

My proposal:
```python
async def handler(event: dict, ctx: HandlerContext) -> ...
```

I called this "backwards compatible because opt-in" but it's not — if a consumer adds `@idempotent` to an existing handler, their handler signature now has to accept `ctx`. That's an in-handler-body change, not just a decorator wrap. The migration story needs to be honest about this.

### 8. Multiple handlers per event = ledger schema needs `handler_name`

My proposed schema had `PK: HANDLER#{handler_name}#{event_id}` — which I already keyed on handler_name. Good. But if three handlers fire on `auth.user.registered` and one fails, the ledger has 3 rows for the same event with different statuses. That's fine, but the CLI `replay --event-id` needs to be clear about which handlers it's replaying. My ticket glossed over this.

## Things I overstated in TICKET.md

### 1. "11 edge cases the framework must address"

Not all 11 are equally important. The critical 3:
- **Auth retry** (same event_id twice) — common, money implications
- **Mid-handler crash** (work done, dedup flag not set) — common, double-grant risk
- **Adjacent events firing same outcome** (login + register both grant) — design issue we should solve at the handler-key level

The rest are valid but lower priority:
- Operator backfill — needed, but a CLI fixes it
- Tier downgrade/upgrade — rare
- Time-window credits — out of scope
- Event missing stable id — speculative

I should rank these, not list them flat. The frame "must address" overstates urgency.

### 2. "Two layers, distinct on purpose" — was confusing

Re-reading my TICKET.md framing: delivery dedup and business dedup are distinct concepts, but I drew them as separate decorators (`@idempotent` for delivery, `ctx.dedup_key` for business). That's two-layer for the consumer to think about. Cleaner: one `@idempotent` decorator that takes a `key` arg, and the lib handles both layers internally.

```python
@idempotent(handler="grant_credit", key=lambda e: f"credit:org:{e['data']['org_id']}:tier:free")
```

The key function expresses the business dedup; the lib uses it for both deduping (no row twice) and observability (ledger lookup by key).

That's a substantially smaller surface than my original `dedup_key()` + `already_done()` + `mark_done()` + `skip()` + `success()` proposal. I had API bloat.

### 3. "Operator-explicit default, opt-in auto-retry in v2"

For mesh-internal services this is right. For external clients, "operator-explicit" means "your handler failures vanish into a ledger nobody checks." External clients want the lib to retry on their behalf, with bounded backoff, and surface the eventual failure via standard FastAPI logs.

I should propose: auto-retry IS the default for `@idempotent` handlers, with a low max-attempts (3) and an explicit `retry=False` opt-out. The Phase-1 implementation can do this in-process (asyncio.sleep + retry); cross-restart durability is v2.

### 4. "Its own DDB table"

For mesh services: yes. For bridge/external clients: doesn't apply. The lib should pick storage based on what's available:
- Redis available → Redis-backed ledger (24h-72h TTL — short, enough for replay window)
- DDB available → DDB ledger (90-day TTL)
- Neither → in-memory dict that doesn't survive restart (degraded mode, log loudly)

This is a `LedgerStore` interface with 3 implementations, not a hard DDB requirement.

### 5. Phasing was too granular

I had 4 phases. For an external consumer reading the README, what they need is:
- "Set this env var, get credits granted reliably"

That's one shippable thing. Phases 1-3 should collapse into one v0.2.7 release. Phase 4 (auto-retry sophistication) stays separate.

## What I'd write instead

A scaled-back ticket that:

1. **Acknowledges what exists** — the Zero-code default, the de-facto `per_user_per_tier` dedup, the schema-rich `credit_grant` block.

2. **Identifies the 3 critical gaps:**
   - **Observability:** operator/dev can't ask "did the handler fire for user X?" There's no surfaced answer.
   - **Replay:** if a handler failed (billing was down for 5 min), there's no way to recover. The Redis flag is sticky-on-success; failures are silent.
   - **Business-key dedup:** the redis flag is hard-coded to `per_user_per_tier`. For B2B "credit per org" semantics there's no path.

3. **Proposes a small framework:**
   - `LedgerStore` interface with 3 backends (Redis, DDB, memory). Auto-selects based on what's available.
   - `@idempotent(handler="...", key=<callable>)` decorator. Single decorator. Handles delivery dedup AND business dedup using the same key.
   - Auto-retry default: 3 attempts with exponential backoff, in-process. Disable with `retry=False`.
   - CLI: `events <user_id>`, `replay <event_id>`, `backfill <handler> <user_ids>`. That's it for v1.

4. **Honest about migration:**
   - Handlers using just `@on_auth_event` keep working (no ledger, no retry — same as today).
   - Adding `@idempotent` is opt-in AND changes the handler signature to accept `ctx`. Document this as a known migration cost.
   - The lib's auto-registered default handler adopts `@idempotent` automatically (consumers using zero-code config get the framework for free).

5. **Defers a lot:**
   - HTTP admin surface — not v1, CLI is enough
   - Cross-tenant aggregation — out of scope, that's an observability problem
   - Cross-handler workflows / sagas — out of scope
   - Auto-retry across restarts — v2

This is roughly half the surface area of my original TICKET.md.

## Updated answers to the 4 questions

### Q1: Dedup policy — still consumer-declared, but extends existing schema

Add a `dedup` field to the existing `credit_grant` block (which already has `trigger`, `amount_per_period`, `lifecycle`, `destination`):

```jsonc
{
  "tier_id": "free",
  "credit_grant": {
    "trigger": "signup",
    "amount_per_period": "10.00",
    "lifecycle": "persistent",
    "destination": "credit_balance",
    "dedup": "per_user_per_tier"   // also: per_org_per_tier | per_user_global | per_org_global
  }
}
```

Default: `per_user_per_tier` (matches today's behavior, so no behaviour change unless they ask for it).

### Q2: Replay UX — snapshot, but document the privacy/retention angle clearly

Same as TICKET.md: snapshot the event payload in the ledger row. But add an explicit `retention_days` config (default 30) and a GDPR-cascade CLI subcommand. Don't bury these in caveats.

### Q3: Failure retry policy — auto-retry IS the default for external clients

Flipped from my original recommendation. Reason: external clients don't have an ops team to run replay manually. `@idempotent` defaults to `retry={attempts: 3, backoff: "exponential", initial: 1s, max: 30s}`. In-process, no cross-restart durability in v1. Opt out via `@idempotent(retry=False)` for handlers that should fail loudly.

For mesh services where auto-retry could compound an outage: they can pass `retry=False` and rely on the CLI replay path. Two valid configurations, lib picks the consumer-friendly default.

### Q4: Ledger location — pluggable, not "its own table"

`LedgerStore` interface. Three implementations:
- `RedisLedgerStore` — 72h TTL, works in bridge mode, default when redis is available
- `DDBLedgerStore` — 90-day TTL via DDB TTL attr, default when DDB is available
- `InMemoryLedgerStore` — for tests and degraded mode

Lib auto-picks at setup_quota. Consumer can pass `ledger_store=...` to override.

External clients with Redis-only get a working ledger without DDB. Mesh services with DDB get the persistent ledger. Tests use in-memory.

## Recommendation

Don't build TICKET.md as I wrote it. Build the scaled-back version sketched above.

Specifically:
- One DDB OR Redis table (auto-detected)
- One decorator (`@idempotent`)
- One CLI (with 3 subcommands)
- Three-week implementation, single v0.2.7 release

The original TICKET.md was a 3-month project that would slow the lib's evolution and create a maintenance burden out of proportion to the problem.

## What I'd do next

1. Replace TICKET.md with the scaled-back design (I can write that on request)
2. Decide on `dedup` field defaults — should be a config schema discussion separate from the framework
3. Confirm the `LedgerStore` interface signature with whoever owns the bridge-mode code
4. Then implement Phase 1 (now ~half of original Phase 1 + 2)

Want me to rewrite TICKET.md with the scaled-back design, or stop here and discuss the critique first?
