# ab0t-quota — Infrastructure Prerequisites (the contract)

**Since 0.7 the library is DECLARED, NOT DISCOVERED**: it uses exactly the
infrastructure you give it, refuses with one typed error naming what is
missing, and never harvests an endpoint, credential, table, region, or topic
from the ambient environment — and never invents one. This page is what you
must provide, derived from the enforcing code. Verify all of it before
deploying:

```
python -m ab0t_quota preflight            # full: schema + plan + gates, read-only
python -m ab0t_quota preflight --offline  # config contract only, contacts nothing
```

Exit codes: `0` boot would start · `1` a gate would refuse · `2` config error
(no network contacted) · `3` declared infra unreachable / credentials /
not-permitted · `4` internal error. The 1-vs-3 distinction is deliberate:
unreachable is never an infrastructure verdict.

*(Preflight write disclosure: it writes nothing except loading the counter's
Lua script into Redis's script cache — content-addressed, idempotent, the same
load boot performs. Skip with `--no-script-load`.)*

## 1. Redis (required for `local` / `byo_redis`; absent by design in `bridge`)

| Requirement | Why | Escape hatch |
|---|---|---|
| Reachable, authenticated Redis at the **declared** `storage.redis_url` (or `QUOTA_REDIS_URL`) | the counter IS the admission gate | none — declare one, or use bridge mode |
| **Non-clustered** (single node / primary-replica; never Redis Cluster) | multi-key Lua CROSSSLOTs on a cluster | `storage.redis_cluster_confirmed_disabled: true` for an UNVERIFIABLE topology only; never overrides an observed `cluster_enabled:1` |
| `maxmemory-policy noeviction` (any `volatile-*` also safe — counter keys carry no TTL) | an evicted live gauge under-counts → over-admission | `storage.redis_durability_confirmed: true` when `CONFIG` is unreadable (ElastiCache/Upstash); never overrides an observed `allkeys-*` |
| Lua scripting (`EVAL`; `SCRIPT LOAD` at boot) | every counter op is EVAL | `storage.redis_scripting_confirmed: true` ONLY when the SCRIPT command itself is unavailable/denied while EVAL works. A script the server *rejected* is never overridable. Rejection is recognised by the `compil` substring of the server's error (case-insensitive) — a disclosed HEURISTIC, not an exact match: real Redis/Valkey emit "Error compiling script", but a differently-worded rejection would be classed unverifiable instead, and an unproven script fails CLOSED at first acquire |
| Version **>= 6.0.0** | tested floor | none, deliberately: an observed below-floor version is definitive; an unreadable version only degrades |
| If the **outbox** lands on this Redis (no DDB outbox): persistence configured AND working (`appendonly yes` or save points; `aof_last_write_status=ok`) | money events nothing can reconstruct | `outbox.redis_durability_confirmed: true` (unverifiable CONFIG only) · `outbox.allow_ephemeral: true` starts a DEV service with billing DISABLED · a paid service otherwise refuses (D-34) |
| Memory headroom (warned at >= 90% of maxmemory) | `noeviction` fails closed at the cliff — the service dies | degrade-only, never refuses |

A **declared but unreachable** Redis is retried with backoff for up to
`storage.connect_retry_seconds` (default 30; `0` = fail immediately), then
refused with a typed reachability error naming the credential/network cause
and which declared source supplied the URL. Auth failures refuse immediately
and never consume the retry budget. It never degrades to in-memory.

Keyspace: the library reads/writes only keys under the `quota:` prefix
(v1 default; see `docs/keyspace.md` for `storage.keyspace_version` /
`storage.keyspace_dual_write` — absent means v1/no-dual and an existing
consumer changing nothing is unaffected).

**Minimal ACL** (Redis >= 6):

```
ACL SETUSER ab0t-quota on >CHANGE_ME ~quota:* \
    +@read +@write +@scripting +ping
# OPTIONAL — enables full machine-verification at boot/preflight. WITHOUT
# these the library cannot READ topology/policy/version and you put the
# assertions on the record instead (redis_cluster_confirmed_disabled,
# redis_durability_confirmed):
#   +info +config|get +cluster|info
```

**`INFO` and `CLUSTER` are NOT required.** Topology is verified primarily by
the data-plane CROSSSLOT probe (a multi-key op inside `~quota:*`), which works
under exactly the ACL above; `CONFIG GET` denied routes to the documented
assertion flags. A least-privilege deployment is supported, not punished.

Managed providers: see the matrix in `docs/quickstart.md` §6 ("Managed Redis
providers"). Any provider without EVAL does not work — no flag exists, by
design.

## 2. DynamoDB (when persistence / paid / activations / handler ledger are on)

Four tables, all `PAY_PER_REQUEST`, TTL expected on attribute `ttl`,
PITR expected (assertable via `storage.ddb_pitr_confirmed` where the control
plane cannot report it):

| Table (default name) | Keys | Required GSIs |
|---|---|---|
| `ab0t_quota_state` | PK/SK (S) | `GSI1` (GSI1PK/GSI1SK, ALL) |
| `ab0t_quota_outbox` | PK/SK (S) | `gsi_status` (gsi_status_pk S / gsi_status_sk N, ALL) |
| `ab0t_quota_activations` | PK/SK (S) | `GSI1` (GSI1PK/GSI1SK, ALL) |
| `ab0t_quota_handler_ledger` | PK/SK (S) | `gsi1` (gsi1_pk/gsi1_sk S), `gsi2` (gsi2_pk/gsi2_sk S) |

**Nothing is created without `storage.auto_create_tables: true`** (default
false since 0.7). Pre-create the tables, or opt in once — self-created tables
are tagged `Service: ab0t-quota`, `ManagedBy: ab0t-quota-library`. A required
GSI must be PRESENT and ACTIVE (a backfilling or missing GSI is a boot
refusal); TTL on any attribute other than `ttl` is fatal; TTL disabled is a
warning. Pre-0.7 self-created handler-ledger tables lack `gsi1`/`gsi2` — add
them online (`UpdateTable`) before upgrading; see the migration notice.

**Minimal IAM** (runtime):

```json
{"Version": "2012-10-17", "Statement": [{
  "Sid": "Ab0tQuotaRuntime", "Effect": "Allow",
  "Action": [
    "dynamodb:DescribeTable", "dynamodb:DescribeTimeToLive",
    "dynamodb:DescribeContinuousBackups",
    "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
    "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:Scan"],
  "Resource": [
    "arn:aws:dynamodb:REGION:ACCOUNT:table/ab0t_quota_state",
    "arn:aws:dynamodb:REGION:ACCOUNT:table/ab0t_quota_state/index/*",
    "arn:aws:dynamodb:REGION:ACCOUNT:table/ab0t_quota_outbox",
    "arn:aws:dynamodb:REGION:ACCOUNT:table/ab0t_quota_outbox/index/*",
    "arn:aws:dynamodb:REGION:ACCOUNT:table/ab0t_quota_activations",
    "arn:aws:dynamodb:REGION:ACCOUNT:table/ab0t_quota_activations/index/*",
    "arn:aws:dynamodb:REGION:ACCOUNT:table/ab0t_quota_handler_ledger",
    "arn:aws:dynamodb:REGION:ACCOUNT:table/ab0t_quota_handler_ledger/index/*"]
}]}
```

Add `dynamodb:CreateTable`, `dynamodb:UpdateTimeToLive`,
`dynamodb:TagResource` on the same ARNs **only** with
`storage.auto_create_tables: true`. `DescribeContinuousBackups` denied ⇒
assert `storage.ddb_pitr_confirmed` (the refusal names the IAM action — an
AccessDenied is reported as a permission problem, never as a missing table).
`UpdateTimeToLive` denied ⇒ warning only. Preflight itself needs only the
three `Describe*` actions. Region: declared `storage.dynamodb_region` wins;
unset defers to the AWS SDK chain (never an invented default).
`DYNAMODB_ENDPOINT`-style endpoints are allowlisted to localhost/dev hosts.

## 2b. The declared config surface (`storage` block — STRICT)

The schema is strict: an unknown or mistyped `storage` key is a
QUOTA-CFG-006 refusal, never silently ignored. The full accepted set —
each key is covered in detail where noted:

| Key | Default | Purpose |
|---|---|---|
| `redis_url` / `redis_password` | — (undeclared refuses) | the declared counter store + its credential (a declared `redis_password` beats a URL-embedded one, D-5(a)) |
| `redis_key_prefix` | `"quota"` | must be `"quota"` or omitted — custom prefixes are refused (§1) |
| `connect_retry_seconds` | `30` | D-2 boot-retry budget for a declared-but-unreachable store (§1) |
| `redis_cluster_confirmed_disabled` | `false` | operator assertion for an unverifiable topology (§1) |
| `redis_durability_confirmed` | `false` | operator assertion for unreadable `CONFIG` (§1) |
| `redis_scripting_confirmed` | `false` | operator assertion when `SCRIPT` itself is unavailable while `EVAL` works (§1) |
| `dynamodb_table` / `dynamodb_region` / `dynamodb_endpoint` | see §2 | declared tables/region; `dynamodb_endpoint` is allowlisted to localhost/dev hosts |
| `ddb_pitr_confirmed` | `false` | operator assertion where the control plane cannot report PITR (§2) |
| `auto_create_tables` | `false` | opt-in table creation (§2) |
| `persistence_enabled` / `persistence_sync_interval_seconds` | on / 300 | counter snapshot persistence |
| `keyspace_version` / `keyspace_dual_write` | `1` / `false` | counter key shape + migration dual-write — `docs/keyspace.md` |

Top level: `service_name`, `engine_mode` (`local`/`byo_redis`/`bridge`),
`offline` (boot contacting nothing — dev/CI, same effect as
`AB0T_QUOTA_OFFLINE=true`), `storage`, `tiers`. Full CLI: `docs/cli.md`;
error codes: `docs/error-codes.md`.

## 3. Mesh credentials

* `AB0T_MESH_API_KEY` — required for the paid surface and for bridge mode.
* `AB0T_CONSUMER_ORG_ID` — required with `enable_paid`.
* `AB0T_QUOTA_STRIPE_WEBHOOK_SECRET` — the ONLY Stripe webhook secret name
  read (0.7); unset ⇒ the webhook route refuses as unconfigured, loudly.
* Optional auth-events: `AB0T_AUTH_WEBHOOK_SECRET`,
  `AB0T_AUTH_WEBHOOK_PUBLIC_URL`, `AB0T_AUTH_ADMIN_TOKEN`,
  `AB0T_AUTH_AUTH_URL`.

## 4. Outbound hosts contacted at boot

The startup log prints the RESOLVED DEPENDENCIES block and the OUTBOUND
TARGETS inventory before contacting anything. The set: the declared Redis;
the DynamoDB regional endpoint (SDK-resolved or the declared dev endpoint);
`https://billing.service.ab0t.com` (tier-catalog publish, paid surface;
every check in bridge mode); `https://payment.service.ab0t.com` (checkout
proxy, paid); the declared auth URL (auto-subscribe, opt-in); the declared
SNS topic's regional endpoint (opt-in). `AB0T_QUOTA_OFFLINE=true` suppresses
every startup reach-out (dev/CI).

## 5. Failure-direction contract (what refuses, what degrades)

* Quota/billing paths fail **CLOSED** (0.6.1/0.6.2). Bridge outages deny by
  default; `AB0T_QUOTA_BRIDGE_FAIL_OPEN=true` opts into availability, with
  tiers reported UNKNOWN — since 0.7 a billing outage can never silently
  enforce an invented `"free"` tier (`BridgeUnavailableError` is the typed
  signal).
* A refused gate names one cause and one remedy; assertions cover only
  genuinely unverifiable signals, never observed negatives.
* Preflight judges the DEFAULT boot posture (`enable_paid=True`). A service
  that deliberately runs `enable_paid=False` may see a D-32/D-34 FAIL line
  its own boot would tolerate — the remedy text says so.
* Missing tables degrade loudly at boot (activation → Redis fallback under
  the durability machine-check; state store → persistence off) — except a
  paid service with no durable outbox, which refuses (D-34).

## 6. Deprecated generic names (migration window, retires 0.8.0)

The library never reads generic `REDIS_URL`, `REDIS_PASSWORD`,
`STRIPE_WEBHOOK_SECRET`, or `AUTH_SERVICE_URL`. If one is set while its
namespaced replacement is undeclared, startup logs an ERROR naming both
(`AB0T_QUOTA_SUPPRESS_DEPRECATION_ERRORS=true` downgrades to a warning).
Legacy `SNS_LIFECYCLE_TOPIC_ARN` and `DYNAMODB_ENDPOINT` still resolve for a
documented transition window, warning on every boot. See
`docs/migrating-from-ambient-resolution.md`.

## 7. The vehicle: `provision`, then `preflight`, then `doctor`

This page describes the destination; the CLI hands you the transport. All
three verbs read the **same evaluator set** as the boot gates — boot raises,
`preflight` exits, `doctor` explains, `provision` emits, from one judgement.

```bash
# 1. Get conforming infrastructure (emit-and-let-them-apply; creates NOTHING):
python -m ab0t_quota provision --emit compose      # dev stack fragment
python -m ab0t_quota provision --emit terraform    # the four DDB tables + IAM policy
python -m ab0t_quota provision --emit acl          # least-privilege Redis ACL
python -m ab0t_quota provision --emit iam          # runtime IAM (create-path is --include-create)
python -m ab0t_quota provision --local             # ONE conforming local Docker Redis, verified

# 2. Will it boot? (CI; typed exits 0/1/2/3/4)
python -m ab0t_quota preflight --json

# 3. Is it production-grade? (humans + auditors)
python -m ab0t_quota doctor --json                 # preflight-report/v1 + posture section
```

`provision` never creates cloud resources — you apply the artifacts with your
own tooling and credentials. `--local`'s only side effect is one local Docker
container, stated in its output. The verb is `provision`, not `setup`:
`setup_quota()` is the library's own entry point.

`doctor` grades what "bootable" deliberately lets through: persistence that is
off behind a durability assertion (the outbox is lost on restart), PITR that
is asserted rather than observed (a promise, not a backup), **already-evicted
keys** (a counter that is wrong now), ACL breadth, encryption in transit,
TTL/retention. Dimensions it cannot observe are reported `not_checked` with
the reason — a denied introspection is a permission answer, never a verdict.
Posture grades are advice: the exit code stays the boot verdict unless you
pass `--fail-on-risk`.

Go parity: `quotactl provision` / `quotactl doctor` (same flags, same exit
taxonomy, same JSON schemas; pinned by conformance `ST-CLI-1`). The honest
asymmetry, stated: Go's `doctor` runs full `quota.Setup` — it may create the
library's declared tables and loads the counter script — and says so in its
output; the Python doctor's only server-visible write is the disclosed
`SCRIPT LOAD`.
