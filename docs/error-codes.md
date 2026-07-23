# QUOTA-CFG error codes — the registry, with remedies

`QUOTA-CFG-nnn` is **one namespace shared by both runtimes** (Python
`ab0t-quota`, Go `ab0t-quota-go`). The machine-readable source is
`conformance/quota-cfg-registry.json`, mirrored byte-identically into the Go
repo (D-13); a conformance test keeps the copies in sync and this page is
checked against the registry by the doc-completeness control. A code is a
contract: it is what you grep for, alert on, and find here.

Every code below is a **startup refusal** (boot raises; `preflight` exits 2)
unless noted. The library refuses loudly instead of inventing a value —
"declared, not discovered".

| Code | Meaning | Remedy |
|---|---|---|
| QUOTA-CFG-000 | Unspecified config error — the resolver's default when no specific code was assigned | Read the message text; it names the field. If you see 000 from a released build, report it — a real code should have been assigned before shipping |
| QUOTA-CFG-001 | Redis counter store undeclared (`storage.redis_url` absent/null and no `QUOTA_REDIS_URL`) in `local`/`byo_redis` mode | Declare your Redis in `storage.redis_url` or `QUOTA_REDIS_URL`. No local Redis? Python: use `engine_mode: "bridge"`; Go: declare `"redis_url": "memory://"` for single-process dev |
| QUOTA-CFG-002 | `quota-config.json` missing — a missing file is a config error, never an empty config | Create the file (start from `quota-config.example.json`) and point `QUOTA_CONFIG_PATH` at it, or place it in the documented search order |
| QUOTA-CFG-003 | `quota-config.json` malformed (unparseable JSON) | Fix the JSON; the error names the parse position. `python -m ab0t_quota preflight --offline` reproduces this without contacting anything |
| QUOTA-CFG-004 | Tier catalog undeclared (`tiers` absent or null) — the library never invents policy | Declare your `tiers` array (see `quota-config.example.json`); an empty catalog is a decision the operator must make explicitly |
| QUOTA-CFG-005 | `redis_url: "memory://"` refused on Python — there is no in-memory counter backend | Use `engine_mode: "bridge"` for a no-Redis Python deployment, or declare a real Redis. Go accepts `memory://` (documented divergence, D-5(b)) |
| QUOTA-CFG-006 | Config schema violation — unknown/mistyped storage key, unknown `engine_mode`, non-array `tiers`; ALL violations listed at once | Fix every listed violation. A typo must never silently change enforcement, so unknown keys refuse rather than being ignored |
| QUOTA-CFG-007 | Bridge mode without its mesh API key | Set `AB0T_MESH_API_KEY` (the single mesh credential; see `docs/mesh-quota-api.md`) |
| QUOTA-CFG-008 | Bridge mode without a service identity | Set `service_name` in the config or `AB0T_SERVICE_NAME` — the bridge routes are scoped per consumer service |
| QUOTA-CFG-009 | DynamoDB region undeclared AND the AWS SDK chain resolved nothing (Go; Python defers region errors to the SDK today) | Declare `storage.dynamodb_region`, or provide `AWS_REGION`/a profile the SDK can resolve. The library never invents a region |
| QUOTA-CFG-010 | SNS region undeclared AND the AWS SDK chain resolved nothing (Go) | Declare `outbox.sns_region` or provide a resolvable SDK region |
| QUOTA-CFG-011 | Keyspace version regression: storage records a COMPLETED v2 migration but the config declares v1-authoritative — a v1 engine would read orphaned keys and every counter would read zero | Set `storage.keyspace_version: 2` to match the completed migration. Not operator-overridable (ST-KEYSPACE-1); see `docs/keyspace.md` |
| QUOTA-CFG-012 | Brownfield keyspace orphaning: config declares `keyspace_version: 2` (dual off) but live v1 counter keys exist with no completed migration recorded | Run the keyspace migration (`docs/keyspace.md`), or declare `keyspace_version: 1` until you do |

Exit-code context: `preflight`/`doctor` exit `2` for every code above (config,
nothing contacted), `1` for a gate refusal against reachable infra, `3` for
declared-but-unreachable infra or credentials, `4` internal. The 1-vs-3
split is deliberate: unreachable is never an infrastructure-quality verdict.
