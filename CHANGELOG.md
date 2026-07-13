# Changelog

## [0.6.2] — 2026-07-13

### ⚠️ Breaking (bridge mode) — bridge now FAILS CLOSED by default
Ticket `20260712_payment_credit_calls_404` (D5). In **bridge mode**, when the mesh
billing service is unreachable or errors, the quota gate previously **failed OPEN**
(`decision: "allow"`) — which, during a billing outage, admits usage that can't be
recorded = **lost revenue**. It now **fails CLOSED** (`decision: "deny"`, the guard
returns 429) by default, so a billing outage can never admit unbilled usage.

**Action:** a consumer that prefers *availability over billing* (let traffic through
during a billing outage, accepting some unbilled usage) must now **opt in** by setting
`AB0T_QUOTA_BRIDGE_FAIL_OPEN=true`. Every outage fallback is logged loudly with the
policy in effect. Matches the middleware default (`fail_open=False`). Engine-local mode
is unaffected (only bridge mode had the fail-open fallback).

All notable changes to `ab0t-quota`. This project follows semantic versioning:
a change that requires you to do something to keep working is at least a MINOR.

## [0.6.1] — 2026-07-12

**Theme: the library now refuses to lose your money silently — it fails loud
instead of failing quiet. You write *less* integrity code, not more.**

The library now owns idempotency, generation counters, TTL windows,
reconciliation, event ordering, outbox delivery, drift detection, and crash
recovery. If your service hand-built any of these, you can delete them.

### ⚠️ Action required (money-safety) — a mis-configured service now refuses to start

These are new. Each fails **loudly at startup** rather than coming up and quietly
mis-billing. If you take money, the library will not let you do it on storage
that can silently drop an event.

1. **Paid billing needs a durable store.** With `enable_paid` set and no durable
   outbox store, the service **refuses to start**. Use DynamoDB (the default), or
   Redis confirmed durable (below).
2. **Redis used for the outbox must be durable and non-evicting.** At startup the
   library checks persistence + eviction policy and refuses if pending billing
   events could be evicted. On ElastiCache (where `CONFIG` is disabled) set
   `outbox.redis_durability_confirmed: true` to assert you've checked.
   *Recommended: use DynamoDB and skip this.*
3. **Custom Redis key prefixes are no longer allowed.** `storage.redis_key_prefix`
   must be the default — a custom value forks the keyspace and breaks cross-runtime
   sharing.
4. **Operator, once per environment (~30s): confirm your Redis is not clustered.**
   The atomic counter uses multi-key Lua; on a clustered Redis those fail
   (`CROSSSLOT`) without a keyspace migration. Single-node Redis is unaffected.

### The one thing you still reason about

If you register an **auth-event handler with a side effect** (sends email, calls a
third party) **and give it no business idempotency key**, a crash-recovery replay
could run that side effect twice. The built-in credit-grant handler is safe. **If
you write a custom handler with side effects, pass a `key`.**

### Compatibility

- Requires the billing service to expose the activation-settlement endpoint
  (`/settle`) and an inputs-aware commit. **Confirm your billing deployment
  supports these before adopting this version.**
- Config surface: one `quota-config.json` + (if your usage lives in your own
  tables) one `observed_usage_provider` callback answering "what is live right
  now?" — the only domain knowledge the library cannot have.

### Migration checklist

- [ ] Point the outbox at DynamoDB (or a confirmed-durable Redis).
- [ ] Remove any custom `redis_key_prefix`.
- [ ] Confirm Redis is single-node (or migrate keyspace for cluster).
- [ ] Add a `key` to any custom side-effecting auth-event handler.
- [ ] Confirm billing is deployed with `/settle` before you adopt.

See `docs/quickstart.md` and `docs/deployment.md` for the full setup.
