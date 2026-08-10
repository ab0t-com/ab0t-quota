# Changelog

## [0.6.5] — Recount-before-deny, read-repair, and one-call recalculate

### Your quota can no longer show — or enforce — a wrong number

The fast counter is now treated as a cache of a per-type derived truth, and the
library repairs it before it can ever harm a user:

- **Recount-before-deny.** When the cached counter would deny a request, the
  engine first recomputes the true usage from the resource's *type* source of
  truth — a **gauge** from your live-count callback (`observed_usage_provider`),
  a **meter/accumulator** by re-summing your durable period ledger
  (`accumulator_usage_provider`) — and only denies if the truth is really at the
  limit. A stale-high gauge (the "shows 5 with 0 running" class) now self-heals
  and allows.
- **Type-aware fail direction on a truth-source outage** (money-safety). A
  **gauge** fails *open* (a stale gauge must never block a user; over-admission
  is bounded and healed). A **meter** does *not* — it keeps the last-known
  ledger-backed decision, so a ledger-source outage can never let spend blow past
  the cap. Both still alert.
- **`reconcile_org(org_id, resource_key=None)`** — recompute every resource from
  its type truth, repair the counter, and get a structured before→after back.
  Idempotent and safe to call anytime; it powers a user-facing "Recalculate
  usage" button.
- **Read-repair.** A usage read opportunistically recomputes and repairs a
  drifted counter (throttled, best-effort).
- **Detection.** `quota.drift_detected` now carries a `type` label and fires on
  recount-repair and `reconcile_org`, not only the periodic reconciler.

Non-breaking and purely additive: wire `observed_usage_provider` /
`accumulator_usage_provider` (both optional) into `setup_quota` to opt in; a
service that wires neither behaves exactly as before.

## [0.6.4] — Drift observability + idempotency-key contract guard

### Your quota gauges now report when they drift

The reconciler emits a `quota.drift_detected` metric every time it corrects a
gauge back to live truth, plus a sustained-drift alert — so a slow counter leak
shows up on a dashboard instead of as a surprised customer.

### A guard rail on idempotency keys

The counter API now warns when a decrement idempotency key looks like a
recyclable resource id — the shape that could cause a missed decrement to be
silently dropped. It stays a warning by default; set
`AB0T_QUOTA_STRICT_IDEMPOTENCY_KEYS=1` to make it a hard error. Keys built from a
unique per-lifecycle-event id are unaffected.

Non-breaking: no config or call-site change is required to upgrade from 0.6.x.

## [0.6.3] — DECLARED, NOT DISCOVERED

### Getting started is now self-serve

Registering for a mesh credential no longer requires contacting us. Signup is
open, and the quickstart shows the whole path:

```sh
curl -fsSL https://raw.githubusercontent.com/ab0t-com/clientsetup/main/install.sh | sh
setup run 07
```

The installer is POSIX sh, HTTPS-only, verifies a published sha256, keeps the
previous binary for one-step rollback, and is idempotent — **re-run the same
command to update**. Pin a version with `REF=vX.Y.Z`, relocate with `PREFIX=`.

**One thing to watch:** the onboarding client prints your credential as
`BILLING_SERVICE_API_KEY`; this library reads `AB0T_MESH_API_KEY`. Same value,
different name. `docs/quickstart.md` and `docs/deployment.md` both show the
one-line export that bridges them — without it you get `QUOTA-CFG-007`.

Onboarding reference: <https://github.com/ab0t-com/clientsetup>

### Clearer configuration errors

* **An empty configuration value is treated as unset, not as a declaration.**
  If you write `"redis_url": "${QUOTA_REDIS_URL}"` and that variable is not set
  in some environment, you now get `QUOTA-CFG-001` naming `storage.redis_url`
  and the variable to set — instead of a low-level URL-parsing error from the
  Redis client. Resolution still falls through to the namespaced environment
  variable first. Applies to text values only; an explicitly empty list or
  object remains a valid declaration.

* **A password inside your Redis URL counts as declared.** `redis://:pw@host:6379/4`
  is the conventional form, and using it no longer produces a startup error
  telling you to rename a `REDIS_PASSWORD` variable the library is not reading.
  The underlying protection is unchanged: if your URL carries no password and a
  generic variable is set, you are still told.

### Documentation

Onboarding and deployment docs are now written for any service, using
placeholders (`<your-service>`, `<billing-service>`) rather than one
deployment's directory layout. Applies across `docs/` and the bundled Skills.

### THE CONFIG IS KING — end-customer messages are config-driven

**Behaviour change, no action required** (ticket 20260722, D-CK-1…D-CK-5).

* `messages.py` no longer carries **any** consumer's vocabulary. The two
  lookup tables are DELETED: `ACTION_HINTS` (copy keyed on one deployment's
  resource keys) and `UPGRADE_TIER_MAP` (a fixed free → Starter → Pro →
  Enterprise ladder). A consumer whose plans are `free`/`pro` was previously
  told to *"upgrade to Starter"* — a plan that does not exist in their product
  — and got a dangling *"Or"* when no ladder row matched.
* The upgrade prompt now names **the next tier in YOUR OWN `tiers[]`**, found
  by `sort_order` and printed as its `display_name`, and only when that tier
  genuinely grants more of the resource in question. No higher tier ⇒ **the
  clause is omitted entirely**; the sentence reads correctly with and without
  it. The tier's own `upgrade_url` is rendered as the CTA.
* **New optional resource field `action_hint`** — end-customer remediation
  copy shown in 429 responses ("Archive a project to free up a slot."),
  distinct from `description` (admin-facing prose). Absent ⇒ the sentence is
  omitted rather than invented.
* Units **pluralise**: `1 widget` / `2 widgets` (was `1 widgets`). Declare
  `unit` in its plural form; the library prints the singular at exactly one.
* **Thresholds are config everywhere**: `QuotaState.severity` and the warning
  wording now read the configured `warning_threshold`/`critical_threshold`,
  the same values the enforcement path already used. A consumer with custom
  thresholds previously got correct enforcement beside a wrong severity.
* Copy lives in an overridable `Templates` dataclass (the shape the Go runtime
  had from day one). There is deliberately **no `messages` config section**
  (D-CK-2): config supplies the facts, the library owns the sentence.
* `quota-config.example.json` is rebuilt around **neutral generic-SaaS
  vocabulary** with a populated, annotated `resources[]` section (every
  `counter_type` demonstrated once), plus `engine_mode`, `shadow_mode`, burst,
  thresholds and per-user sub-quotas shown doing real work. It previously
  declared **zero resources** while its tier limits referenced resource keys
  keys it never defined — it only "worked" because the library had been taught
  that one consumer's vocabulary.
* **Permanent control:** the key/const census now forbids any
  consumer-specific identifier — resource key, tier name, service name — from
  library LOGIC (`tests/test_declared_not_discovered_20260721.py`, D-CK-5).


**⚠️ Breaking, action-required — shipped as a PATCH by operator decision.**
D-1 originally set this release at `0.7.0` on this file's own stated policy
("a change that requires you to do something to keep working is at least a
MINOR"). The operator — who owns publishing and was given an explicit veto on
that point — chose a patch bump, consistent with standing practice for this
library. **The version number is therefore NOT a reliable signal of breakage
for this release; the migration note is.**
(amended). Note that consumers install `git+…@main`, so no version number
reaches them today either way — see the pinned-releases follow-up.
**Read `docs/migrating-from-ambient-resolution.md`
before upgrading** and run the new `python -m ab0t_quota preflight` in CI —
it reproduces every startup verdict read-only, before any deploy.

* The library resolves every dependency from **declared sources only**
  (config / namespaced `QUOTA_*`/`AB0T_*` env). Generic `REDIS_URL`,
  `REDIS_PASSWORD`, `STRIPE_WEBHOOK_SECRET`, `AUTH_SERVICE_URL` are **never
  read**; a leftover generic name logs a startup ERROR naming the
  replacement. No value is ever invented (no `redis://localhost:6379/0`, no
  default region, no default tier catalog).
* Missing `quota-config.json`, missing `tiers`, undeclared
  `storage.redis_url` (local/byo_redis): **fatal, typed errors**
  (QUOTA-CFG-001…006) naming the fix and what pre-0.7 would have used.
* Config is schema-validated before any I/O; `null` ≠ absent ≠ invalid;
  unknown/mistyped storage keys are errors; custom `storage.redis_key_prefix`
  is refused (announced in 0.6.x; was silently ignored).
* **Table creation is opt-in**: `storage.auto_create_tables` (default false).
  Existing environments unaffected; fresh deploys pre-create or opt in. All
  self-created tables are tagged. Pre-0.7 self-created handler-ledger tables
  must add `gsi1`/`gsi2` online before upgrading (they now refuse boot).
* Startup logs the RESOLVED DEPENDENCIES plan (with provenance, secrets
  redacted) and the OUTBOUND TARGETS inventory before contacting anything;
  `AB0T_QUOTA_OFFLINE=true` boots contacting nothing (dev/CI).
* The Stripe webhook route mounts/verifies only with
  `AB0T_QUOTA_STRIPE_WEBHOOK_SECRET`; unset ⇒ loud refusal (was: silently
  used the generic name — a co-deployed service's secret 400s every webhook
  and credit grants never land).
* Bridge mode hard-requires `AB0T_MESH_API_KEY` + `service_name`
  (QUOTA-CFG-007/008). Bridge tier/usage reads never invent `"free"`: a
  billing outage raises a typed `BridgeUnavailableError` (default,
  fail-closed) or reports tier UNKNOWN under `AB0T_QUOTA_BRIDGE_FAIL_OPEN`.
* The 2026-07-21 incident's fix: an unreachable or unauthenticated Redis now
  refuses with a typed reachability error naming the credential/network cause
  and WHICH declared source supplied the URL — never a Redis Cluster topology
  verdict (the misdiagnosis that opened the incident is structurally
  impossible; no `*_confirmed_*` assertion masks it). Transient boot blips are
  absorbed by a bounded retry (`storage.connect_retry_seconds`, default 30s,
  0 = immediate); auth failures never consume the budget. Topology is now
  probed primarily on the DATA PLANE (a multi-key op in the library's own
  keyspace), so a least-privilege ACL user needs no `INFO`/`CLUSTER`/`CONFIG`
  privileges — see docs/requirements.md for the minimal ACL.
* New: `python -m ab0t_quota preflight` (`check` alias) — schema, resolved
  plan + provenance, every startup gate read-only, exit codes separating
  config (2) / gate refusal (1) / unreachable-or-credentials (3).
  `--check-mesh` is opt-in and read-only; the only server-visible write is
  the counter's SCRIPT LOAD (skippable).
* New operator assertions: `storage.redis_scripting_confirmed` (D-73's hatch
  for an unrunnable SCRIPT probe — never overrides a rejected script);
  documented provider notes for CONFIG-less managed Redis.
* New CLI verbs (names are contract, shared with Go's `quotactl`;
  `docs/cli.md`): **`doctor`** — grades production POSTURE (persistence
  behind assertions, PITR asserted-not-observed, eviction facts, ACL/IAM
  breadth, encryption, retention) over the same evaluators boot uses,
  reports what it could not check as `not_checked`, `--json` extends
  `preflight-report/v1` with a posture section; **`provision`** — emits
  conforming infra artifacts (`--emit compose|terraform|acl|iam`) generated
  from the enforcing gate registry, or `--local` for one verified local dev
  Redis. Never creates cloud resources.
* New `storage` keys (schema-strict, all optional): `connect_retry_seconds`
  (D-2 boot-retry budget, default 30); `keyspace_version` /
  `keyspace_dual_write` — counter key shape v1/v2 + migration dual-write.
  **Defaults are v1 / no-dual: an existing consumer changing nothing is
  unaffected.** Declaring v2/dual is refused by `setup_quota` until the
  setup wiring lands (`docs/keyspace.md`); boot guards QUOTA-CFG-011/012
  protect a migrated keyspace either way.
* `QUOTA-CFG-nnn` is now ONE registry shared byte-identically with the Go
  runtime (`conformance/quota-cfg-registry.json`, D-13); every code is
  documented with its remedy in `docs/error-codes.md`.
* Billing mesh API: tier-limit reads and per-org overrides accept
  `?service=` — an override can bind to one mesh service; omitted stays
  org-wide (pre-existing overrides keep their org-wide meaning). See
  `docs/mesh-quota-api.md`.
* New docs: `docs/requirements.md` (the prerequisites contract),
  `docs/migrating-from-ambient-resolution.md` (self-audit + renames),
  `docs/cli.md`, `docs/error-codes.md`, `docs/keyspace.md`.

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
