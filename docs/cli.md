# CLI reference — `python -m ab0t_quota` (and `quotactl` parity)

Three infrastructure verbs — **`provision`, then `preflight`, then
`doctor`** — plus the handler-ledger verbs. All three infra verbs read the
**same evaluator set** the boot gates use: boot raises, `preflight` exits,
`doctor` explains, `provision` emits, from one judgement. Go parity:
`quotactl provision` / `quotactl doctor` with the same names, exit taxonomy
and JSON schemas, pinned by conformance `ST-CLI-1`. The verb names are a
released contract — they do not get renamed.

Shared exit taxonomy: `0` ok · `1` gate refusal · `2` config error (nothing
contacted; the message carries a `QUOTA-CFG-nnn` code — see
`docs/error-codes.md`) · `3` declared infra unreachable / credentials ·
`4` internal error.

## `preflight` — "would this boot?" (CI)

```bash
python -m ab0t_quota preflight [--config PATH] [--json] [--offline]
    [--check-mesh] [--strict] [--timeout SECONDS] [--no-script-load]
```

`check` is a legacy alias for `preflight` — same behaviour.

| Flag | Meaning |
|---|---|
| `--config` | path to `quota-config.json` (default: the same search order as boot) |
| `--json` | machine-readable `preflight-report/v1` on stdout (human report goes to stderr) |
| `--offline` | schema + resolved plan + provenance only; contacts NOTHING |
| `--check-mesh` | ALSO probe mesh `/health` endpoints (GET only; OFF by default per D-6 — a CI tool must not contact production unless asked) |
| `--strict` | warnings also fail the exit code |
| `--timeout` | per-probe timeout in seconds (default 5) |
| `--no-script-load` | skip the counter's `SCRIPT LOAD` — the one server-visible write; boot will still perform it |

## `doctor` — "is this production-grade?" (humans + auditors)

```bash
python -m ab0t_quota doctor [--config PATH] [--json] [--offline]
    [--check-mesh] [--strict] [--fail-on-risk] [--timeout SECONDS]
    [--no-script-load]
```

Grades POSTURE over the same evaluators: persistence behind a durability
assertion, PITR asserted-not-observed, TTL, eviction facts (already-evicted
keys = a counter that is wrong now), ACL/IAM breadth, encryption in transit,
retention. Dimensions it cannot observe are reported `not_checked` with the
reason — never implied covered. `--json` extends `preflight-report/v1` with a
`posture` section you can hand to an auditor. Posture findings are advice by
default; `--fail-on-risk` turns RISK findings into exit 1. Honest asymmetry:
Go's `quotactl doctor` runs full `quota.Setup` (it may create the declared
tables and loads the counter script) and says so in its output; the Python
doctor's only server-visible write is the disclosed `SCRIPT LOAD`.

## `provision` — "give me conforming infrastructure" (the vehicle)

```bash
python -m ab0t_quota provision --emit compose|terraform|acl|iam [--config PATH]
python -m ab0t_quota provision --emit iam --include-create
python -m ab0t_quota provision --local [--port N] [--name NAME] [--dry-run]
    [--timeout SECONDS]
```

Artifacts are generated **from the enforcing gate registry**, so what it
emits and what boot verifies cannot drift. It **never creates cloud
resources** — emit-and-let-them-apply.

| Flag | Meaning |
|---|---|
| `--emit` | print one artifact on stdout: `compose` (dev stack fragment), `terraform` (the four DDB tables + IAM), `acl` (least-privilege Redis ACL), `iam` (runtime IAM policy) |
| `--config` | use the declared table names from this `quota-config.json` (defaults otherwise) |
| `--include-create` | with `--emit iam`: include the self-provisioning actions — pair only with `storage.auto_create_tables: true` |
| `--local` | start ONE conforming local Docker Redis, then verify it with the boot evaluator (the only side effect, stated in its output) |
| `--port` | host port for `--local` (default 6399 — deliberately not 6379: declare, never discover) |
| `--name` | container name for `--local` (default `ab0t-quota-dev-redis`) |
| `--dry-run` | print the docker command `--local` would run; run nothing |
| `--timeout` | verification timeout in seconds |

## Handler-ledger verbs (auth-events; see `docs/auth-events.md`)

```bash
python -m ab0t_quota subscribe-events --endpoint URL [--auth-url URL]
    [--org-id ORG] [--name NAME]
python -m ab0t_quota events [--user-id ID] [--status S] [--handler H]
    [--event-id ID] [--since 24h] [--limit N] [--format table|json]
python -m ab0t_quota replay --handler H --event-id ID [--webhook-url URL]
python -m ab0t_quota backfill --handler H --user-ids a,b --org-id ORG
    [--event-type auth.user.registered] [--webhook-url URL]
python -m ab0t_quota delete-user --user-id ID --confirm
```

| Verb | Purpose | Notes |
|---|---|---|
| `subscribe-events` | register the webhook subscription against auth | `--auth-url` defaults to `$AB0T_AUTH_AUTH_URL`; `--org-id` filter recommended; `--name` defaults to `ab0t-quota-credit-grant` |
| `events` | query the handler ledger | `--status` one of `in_progress`/`success`/`skipped`/`failed`/`failed_permanent`; `--since` accepts `1h`/`24h`/`7d`/ISO; `--limit` default 50; `--format` default `table` |
| `replay` | re-fire a handler for one event from the stored snapshot | `--webhook-url` defaults to `$AB0T_AUTH_WEBHOOK_PUBLIC_URL/api/quotas/_webhooks/auth` |
| `backfill` | fire synthetic events for users who pre-existed the handler | `--event-type` defaults to `auth.user.registered` |
| `delete-user` | delete all ledger rows for a user (GDPR cascade) | no-op without `--confirm` |
