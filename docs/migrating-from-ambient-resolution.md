# Migrating from ambient resolution (0.6.x → 0.7)

**What changed, in one paragraph.** A production consumer declared "no Redis"
in three places; the library harvested one from the ambient environment anyway
and crash-looped blaming Redis Cluster. 0.7 removes the whole class: the
library resolves every dependency from **declared** sources only
(quota-config.json, namespaced `QUOTA_*`/`AB0T_*` env), refuses with one typed
error naming what is missing, and never invents a value (no localhost Redis,
no default region, no default tier catalog). If you deploy this version with
an environment that relied on the old harvesting, **boot will refuse and tell
you exactly what to declare** — run `python -m ab0t_quota preflight` in CI
first and it tells you before any deploy.

## Self-audit — run this over YOUR repo

Our own integration templates (before 2026-07-21) taught the defect, so it may
be compiled into your service where no library release reaches it:

```bash
grep -rnE 'os\.getenv\("(REDIS_URL|REDIS_PASSWORD|STRIPE_WEBHOOK_SECRET|AUTH_SERVICE_URL)"' .
grep -rn 'redis://localhost:6379/0' .
grep -rn 'DEFAULT_TIERS' .
```

| Hit | Replacement |
|---|---|
| `os.getenv("REDIS_URL", …)` or-chain (from the old template) | delete the chain; declare `storage.redis_url` in quota-config.json (or `QUOTA_REDIS_URL`). The library validates it and refuses with QUOTA-CFG-001 naming both sources. |
| `os.getenv("REDIS_PASSWORD")` | `storage.redis_password` / `QUOTA_REDIS_PASSWORD`. A declared field beats a URL-embedded password; both-set-differing logs a warning. |
| `redis://localhost:6379/0` as a default | delete — an invented endpoint. Local dev declares its localhost explicitly. |
| `tiers = load_tiers(config) or DEFAULT_TIERS` | `tiers = load_tiers(config)` — a missing `tiers` array is fatal (QUOTA-CFG-004). `DEFAULT_TIERS` survives only as an explicit test/dev opt-in. |

## The Stripe secret rename (silent-money failure signature)

`STRIPE_WEBHOOK_SECRET` → **`AB0T_QUOTA_STRIPE_WEBHOOK_SECRET`**. The generic
name is never read (0.7). The failure signature of missing this: **webhooks
400, credit grants never land, no crash** — which is why startup logs an
ERROR when the generic name is set and the namespaced one is not
(`AB0T_QUOTA_SUPPRESS_DEPRECATION_ERRORS=true` downgrades it to a warning
mid-migration). The webhook route now refuses to verify as UNCONFIGURED
rather than silently using a co-deployed service's secret.

## Other action-required changes

* **Missing `quota-config.json` or missing `tiers`**: fatal (was: silent
  defaults). Add a minimal config — see `quota-config.example.json`.
* **Table auto-creation is opt-in**: `storage.auto_create_tables: true`, or
  pre-create the four `ab0t_quota_*` tables (`docs/requirements.md` §2).
  Fresh environments only; existing tables are used as-is.
* **Pre-0.7 self-created handler-ledger tables** lack the `gsi1`/`gsi2`
  indexes their queries need and now REFUSE boot: add both online
  (`aws dynamodb update-table … --global-secondary-index-updates`) before
  upgrading.
* **`storage.redis_key_prefix`**: custom values are now a config error (they
  were announced disallowed in 0.6.x and silently ignored by Python).
* **Legacy names on a transition window** (retire 0.8.0, warn every boot):
  `SNS_LIFECYCLE_TOPIC_ARN` → `AB0T_MESH_SNS_LIFECYCLE_TOPIC_ARN`;
  `DYNAMODB_ENDPOINT` → `QUOTA_DYNAMODB_ENDPOINT`.
* **Bridge mode** hard-requires `AB0T_MESH_API_KEY` + `service_name`
  (QUOTA-CFG-007/008; it used to boot broken). Bridge billing outages now
  raise a typed `BridgeUnavailableError` (default fail-closed) or report tier
  UNKNOWN under `AB0T_QUOTA_BRIDGE_FAIL_OPEN=true` — never an invented
  `"free"` tier.
* **Stripe webhook route**: unset secret now means the route refuses — a 500
  naming the config error where monitoring may have keyed on other statuses.

## Verify before deploying

```bash
python -m ab0t_quota preflight --offline   # config contract, contacts nothing
python -m ab0t_quota preflight             # + every startup gate, read-only
```

Preflight prints the resolved plan with provenance (which source supplied
every value) and the deprecation call-outs — it validates the exact config
your patched module will load.
