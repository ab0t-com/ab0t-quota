# quota-config.json Schema

## Top-Level Keys

`service_name` (string; required for bridge mode and keyspace v2),
`engine_mode` (`local` | `byo_redis` | `bridge`), `offline` (bool — boot
contacting nothing, dev/CI; same effect as `AB0T_QUOTA_OFFLINE=true`),
`storage`, `tiers`, plus the blocks below.

## Top-Level Structure

<!-- doc-exec: fragment (ellipsis key map — every block is expanded with real values in the sections below and in quota-config.example.json) -->
```json
{
  "storage": { ... },
  "tier_provider": { ... },
  "alerts": { ... },
  "enforcement": { ... },
  "reconciliation": { ... },
  "tiers": [ ... ],
  "billing_integration": { ... }
}
```

## storage

| Field | Type | Default | Description |
|---|---|---|---|
| `redis_url` | string | **required — no default** | Redis for hot-path counters. `null` and absent are distinct and BOTH are config errors for local/byo_redis modes (`QUOTA_REDIS_URL` env is the other declared source; no Redis ⇒ `engine_mode: "bridge"`). Since 0.7 the library never invents `localhost`. |
| `redis_password` | string | — | Redis credential. A declared field beats a URL-embedded password (D-5(a)) |
| `redis_key_prefix` | string | `quota` | Must be `"quota"` or omitted — custom prefixes are refused (they would fork the keyspace) |
| `connect_retry_seconds` | number | `30` | Boot-retry budget for a DECLARED but unreachable Redis, then a typed refusal. `0` = fail immediately; auth failures never retry. Never degrades to in-memory (D-2) |
| `dynamodb_table` | string | `ab0t_quota_state` | DynamoDB table for durable state |
| `dynamodb_region` | string | AWS SDK chain | Unset defers to the SDK's own resolution (`AWS_REGION`/profile/IMDS) — never an invented `us-east-1` (0.7) |
| `dynamodb_endpoint` | string | — | Dev-only endpoint override, allowlisted to localhost/dev hosts |
| `persistence_enabled` | bool | `true` | Enable DynamoDB persistence |
| `persistence_sync_interval_seconds` | int | `300` | How often to snapshot Redis → DynamoDB |
| `auto_create_tables` | bool | `false` | Opt-in table creation (D-3). Default false: pre-create the four tables (`provision --emit terraform`) or opt in once — self-created tables are tagged |
| `redis_cluster_confirmed_disabled` | bool | `false` | Operator assertion: not clustered, for a Redis whose topology cannot be probed. Never overrides an observed `cluster_enabled:1` |
| `redis_durability_confirmed` | bool | `false` | Operator assertion: does not evict, for a Redis whose `CONFIG` is unreadable. Never overrides an observed `allkeys-*` policy |
| `redis_scripting_confirmed` | bool | `false` | Operator assertion for when the `SCRIPT` command is unavailable/denied while `EVAL` works (D-73). A script the server *rejected* — recognised by the `compil` substring of the server's error, case-insensitive: a disclosed HEURISTIC (real Redis/Valkey emit "Error compiling script"), not an exact match — is never overridable; an unproven script fails CLOSED at first acquire |
| `ddb_pitr_confirmed` | bool | `false` | Operator assertion where the control plane cannot report PITR (e.g. DynamoDB Local) |
| `keyspace_version` | int | `1` | Counter key shape: `1` (today's `quota:…`) or `2` (service-scoped). **Absent = v1; an existing consumer changing nothing is unaffected.** Declaring 2 today is refused until the setup wiring lands — see `docs/keyspace.md` |
| `keyspace_dual_write` | bool | `false` | Maintain both key shapes during a v1→v2 migration. Same wiring status as `keyspace_version` — see `docs/keyspace.md` |

The `storage` block is STRICT: an unknown or mistyped key is a QUOTA-CFG-006
startup refusal (see `docs/error-codes.md` in the library), never silently
ignored.

## tier_provider

| Field | Type | Default | Description |
|---|---|---|---|
| `type` | string | `jwt` | Provider type: `jwt`, `auth_service`, `static` |
| `jwt_claim_key` | string | `org_tier` | JWT claim that carries tier ID |
| `default_tier` | string | `free` | Fallback when claim is missing |
| `cache_ttl_seconds` | int | `300` | Cache TTL for auth_service provider |

## resources[] (array)

Every resource you want counted. **Nothing is enforced for a resource that is
not declared here**, and every sentence the library shows an end customer is
built from these fields.

| Field | Type | Required | Description |
|---|---|---|---|
| `service` | string | yes | Owning service name |
| `resource_key` | string | yes | `<domain>.<name>`, e.g. `projects.active` |
| `display_name` | string | yes | Human-readable name shown in dashboards **and 429 responses** |
| `description` | string | no | Longer prose for **admin** UIs and docs — never shown to end customers |
| `action_hint` | string | no | **End-customer remediation copy shown in 429 responses**: one sentence saying what the user can do right now ("Archive a project to free up a slot."). Absent ⇒ the library omits the sentence rather than inventing one |
| `counter_type` | enum | yes | `gauge` (live count, up and down) · `rate` (events in a rolling window) · `accumulator` (grows until reset) |
| `unit` | string | no | Unit label in its PLURAL form (`projects`, `requests`); the library prints the singular at exactly one |
| `window_seconds` | int | rate only | Rolling-window length |
| `reset_period` | enum | accumulator only | `daily` · `weekly` · `monthly` |
| `precision` | int | no | Decimal places (use for money / fractional volume) |

`display_name`, `unit` and `action_hint` are the only vocabulary the library
has: it owns the SENTENCE, you own every noun in it. It will never name a tier,
resource or unit you did not declare — if a fact is missing, the clause is
omitted.

## tiers[] (array)

Each tier object:

| Field | Type | Required | Description |
|---|---|---|---|
| `tier_id` | string | yes | Machine name: `free`, `starter`, `pro`, `enterprise` |
| `display_name` | string | yes | Human-readable: "Starter Plan" |
| `description` | string | no | One-line description for pricing page |
| `sort_order` | int | no | 0=lowest tier. Also how the library finds the **next plan up** when it writes an upgrade prompt — it never names a tier outside your own `tiers[]`, and says nothing when there is no higher tier |
| `features` | string[] | no | Feature flags: `gpu_access`, `sso`, etc. |
| `upgrade_url` | string | no | URL shown in 429 responses |
| `limits` | object | yes | `resource_key` → limit value or limit object |

### Limit values

Simple form (just a number):
```json
"sandbox.concurrent": 5
```

Object form (with thresholds):
```json
"sandbox.concurrent": {
  "limit": 5,
  "warning_threshold": 0.8,
  "critical_threshold": 0.95,
  "burst_allowance": 2,
  "per_user_limit": 3
}
```

- `null` = unlimited
- `0` = feature not available on this tier

## billing_integration

| Field | Type | Description |
|---|---|---|
| `stripe_price_to_tier` | object | Maps Stripe price IDs to tier IDs |
| `downgrade_grace_period_days` | int | Days before over-limit resources are stopped |
| `payment_failure_grace_period_days` | int | Days before tier downgrade on payment failure |

## enforcement

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Master switch — false means log-only (shadow mode) |
| `shadow_mode` | bool | `false` | Log denials but don't block (for rollout) |
| `global_kill_switch` | bool | `false` | Emergency: disable all quota checks |
