# Counter keyspace versioning — `keyspace_version` / `keyspace_dual_write`

**Default: v1, dual-write off. An existing consumer that changes nothing is
unaffected** — the keys your Redis holds today keep working, byte-for-byte,
and the dual-write machinery is dormant unless declared.

## The two config keys (`storage` block, both runtimes)

| Key | Default | Meaning |
|---|---|---|
| `keyspace_version` | absent = `1` | the READ-authoritative counter key shape. `1` = today's untagged `quota:…` keys. `2` = `quota:v2:{<service>/<org>}:<resource>` — service-scoped, hash-tag co-slotted |
| `keyspace_dual_write` | `false` | maintain BOTH shapes during a v1→v2 migration (reads stay on `keyspace_version`; writes land on both, idempotency latches claimed on both so a retry can never double-charge) |

Only `1` and `2` are defined; any other value refuses at boot — an unknown
version silently falling back to a shape would orphan every live counter.
Version `2` requires a declared `service_name` (v2 keys carry a service
segment).

## Current wiring status — WIRED in both runtimes

**Declaring `keyspace_version` / `keyspace_dual_write` in `quota-config.json`
now works from the normal entry point.** You do not need to construct an engine
by hand.

* **Python:** `setup_quota()` resolves both keys through the declared-sources
  resolver and threads the keyspace into the engine, counters, reconciler and
  seed path. Boot guards (QUOTA-CFG-011/012) protect a migrated keyspace in
  either direction.
* **Go:** `quota.Setup` consumes both keys; the dual-write path and the
  migration verbs exist, and `Capabilities.Keyspace` reports the live shape.

**Defaults are `keyspace_version: 1`, `keyspace_dual_write: false`. An existing
consumer who changes nothing is unaffected — the v1 key shape is byte-identical
to pre-migration.**

**One deliberate exception:** in **bridge mode** `setup_quota()` still refuses a
declared v2/dual state. Bridge's counters live server-side, so honouring the key
locally would let a consumer *believe* they were migrating while nothing was —
a refusal is the honest state (D-KS-8).

> Everything below has been verified against **fakeredis / miniredis** only.
> The operator-gated real-Redis and real-cluster legs have **not** been run.
> Treat the runbook as reviewed, not rehearsed, until they are.

## Migrating v1 → v2

The operator sequence (dual-on → backfill → verify → flip → soak → reap) is
driven by `ab0t_quota.keyspace_migration.KeyspaceMigrator`, with a persistent
marker, a time-based flip gate (dual-write must have run at least a day AND
longer than your longest rate window), and boot guards:

* **QUOTA-CFG-011** — config declares v1 but storage records a COMPLETED v2
  migration: refused, not overridable (your counters would all read zero).
* **QUOTA-CFG-012** — config declares v2 (dual off) but live v1 keys exist
  with no completed migration: run the migration or declare v1.

See `docs/error-codes.md` for the codes and
`tickets/20260721_keyspace_versioning/RUNBOOK_keyspace_migration.md` for the
full step-by-step (shipped with the repo; it becomes a docs/ page when the
setup wiring lands). Shared-Redis (bridge) caution from the runbook: v1 keys
carry no service scope, so never reap until every scope on that Redis has
flipped — the reap verb requires an explicit on-the-record confirmation flag.
