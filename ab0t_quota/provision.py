"""T-2/T-3 (program board, tooling lane) — `python -m ab0t_quota provision`.

The missing VEHICLE: docs/requirements.md describes the destination; this
module emits the transport. `provision --emit compose|terraform|acl|iam`
prints an artifact that satisfies the SAME contract the boot gates enforce,
generated FROM the enforcing registry below — the artifact and the gate
cannot drift, because they read one source (the never-drift rule of
tickets/20260721_setup_and_doctor_verbs/TICKET.md §2).

`provision --local` is the one-command conforming dev Redis (T-3): every
consumer inventing this differently is how ambient-REDIS_URL habits formed.
After starting the container it verifies conformance with
`verify_redis_invariants` — the SAME evaluator boot and preflight use.

What provision must never do:
  * NEVER create cloud resources — emit-and-let-them-apply (the D-3 rule
    `auto_create_tables` follows). `--local` creates ONLY a local Docker
    container, loudly, and only when asked.
  * NEVER read a generic env var or invent a value: table names come from
    the declared config (when given) or the documented defaults.

Registry provenance: Redis constants are IMPORTED from the enforcing gate
module (`redis_preflight`); table schemas mirror the four `ensure_table`
create sites and are pinned to them by a drift test that EXECUTES those
sites against a recording fake (tests/test_tool_provision_20260721.py) —
change a schema without changing the registry and the test goes red.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Optional, Tuple

# ONE evaluator set: the enforcing constants, imported — never re-declared.
from .redis_preflight import EVICTING_POLICIES, REDIS_VERSION_FLOOR

#: The policy the emitted artifacts configure. D-72's gate refuses every
#: member of EVICTING_POLICIES; `_self_check` proves this value is not one.
REQUIRED_MAXMEMORY_POLICY = "noeviction"

#: D-32/D-81: the outbox needs persistence, not just non-eviction.
REQUIRED_APPENDONLY = "yes"

#: The only keyspace the library touches (requirements §5.1).
KEYSPACE_PATTERN = "quota:*"

#: Default host port for the local/compose Redis. Deliberately NOT 6379:
#: a conforming dev Redis must be DECLARED, never discovered on the default
#: port by habit (the ambient-REDIS_URL incident's root).
DEFAULT_LOCAL_PORT = 6399

#: Container/service naming for `--local` and the compose fragment.
LOCAL_CONTAINER_NAME = "ab0t-quota-dev-redis"

#: Redis image major pinned at (or above) the D-74 floor.
REDIS_IMAGE = "redis:7-alpine"

#: Minimal ACL (requirements §5.1) — hand-reviewed per design OD-9 (a wrong
#: auto-derived ACL is worse than a reviewed one); lint-anchored + pinned by
#: the conformance binding tests.
ACL_REQUIRED_RULES = ("+@read", "+@write", "+@scripting", "+ping")
ACL_OPTIONAL_RULES = ("+info", "+config|get", "+cluster|info")

#: IAM actions the RUNTIME needs (requirements §5.4; derived by grep over the
#: boto3 calls, reviewed). Create-path actions are opt-in, mirroring D-3.
IAM_RUNTIME_ACTIONS = (
    "dynamodb:DescribeTable", "dynamodb:DescribeTimeToLive",
    "dynamodb:DescribeContinuousBackups",
    "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
    "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:Scan",
)
IAM_CREATE_ACTIONS = (
    "dynamodb:CreateTable", "dynamodb:UpdateTimeToLive", "dynamodb:TagResource",
)


@dataclass(frozen=True)
class GsiSpec:
    name: str
    hash_key: str
    hash_type: str
    range_key: str
    range_type: str
    projection: str = "ALL"


@dataclass(frozen=True)
class TableSpec:
    cap_key: str            # the capability/gate key preflight reports under
    default_name: str
    config_path: Optional[str]  # dotted config key overriding the name (None: wired programmatically)
    ttl_attribute: str
    gsis: Tuple[GsiSpec, ...]
    purpose: str
    created_by: str         # the enforcing ensure_table site this mirrors
    pitr_required: bool = True
    #: preflight checks these GSI names (existence/ACTIVE); () = existence-only
    preflight_gsis: Tuple[str, ...] = field(default=None)  # type: ignore[assignment]

    def __post_init__(self):
        if self.preflight_gsis is None:
            object.__setattr__(self, "preflight_gsis",
                               tuple(g.name for g in self.gsis))


#: THE table registry. Preflight's DDB phase derives its checks from this
#: (preflight._check_ddb), and every emitter below renders it — one source.
TABLE_SPECS: Tuple[TableSpec, ...] = (
    TableSpec(
        cap_key="ddb_state", default_name="ab0t_quota_state",
        config_path="storage.dynamodb_table", ttl_attribute="ttl",
        gsis=(GsiSpec("GSI1", "GSI1PK", "S", "GSI1SK", "S"),),
        purpose="counter snapshots, org tiers, overrides",
        created_by="ab0t_quota/persistence.py QuotaStore.initialize",
        # boot never gate-verifies the state table (existence only) — preflight
        # mirrors boot (never-drift rule 7), so no GSI check there.
        preflight_gsis=(),
    ),
    TableSpec(
        cap_key="ddb_outbox", default_name="ab0t_quota_outbox",
        config_path="outbox.ddb_table", ttl_attribute="ttl",
        gsis=(GsiSpec("gsi_status", "gsi_status_pk", "S", "gsi_status_sk", "N"),),
        purpose="durable outbox — money lifecycle events nothing can reconstruct",
        created_by="ab0t_quota/billing/outbox.py DDBOutboxStore.ensure_table",
    ),
    TableSpec(
        cap_key="ddb_activations", default_name="ab0t_quota_activations",
        config_path="activations.ddb_table", ttl_attribute="ttl",
        gsis=(GsiSpec("GSI1", "GSI1PK", "S", "GSI1SK", "S"),),
        purpose="activation ledger — authoritative for identity + cost",
        created_by="ab0t_quota/activations.py DDBActivationStore.ensure_table",
    ),
    TableSpec(
        cap_key="ddb_handler_ledger", default_name="ab0t_quota_handler_ledger",
        config_path=None, ttl_attribute="ttl",
        gsis=(GsiSpec("gsi1", "gsi1_pk", "S", "gsi1_sk", "S"),
              GsiSpec("gsi2", "gsi2_pk", "S", "gsi2_sk", "S")),
        purpose="idempotent-handler ledger (opt-in wiring; GSIs required by "
                "the events/delete-user CLI and GDPR queries)",
        created_by="ab0t_quota/handler_ledger.py DDBLedgerStore.ensure_table",
        preflight_gsis=(),  # D-82: checked at boot only when explicitly wired
    ),
)

EMIT_KINDS = ("compose", "terraform", "acl", "iam")


def _self_check() -> None:
    """Registry sanity against the enforcing gates — a provision that emits a
    policy the gates refuse is our bug (exit 4), never the consumer's."""
    if REQUIRED_MAXMEMORY_POLICY in EVICTING_POLICIES:
        raise AssertionError(
            f"provision registry emits maxmemory-policy="
            f"{REQUIRED_MAXMEMORY_POLICY}, which D-72's gate REFUSES "
            f"(EVICTING_POLICIES={EVICTING_POLICIES})")
    major = int(REDIS_IMAGE.split(":", 1)[1].split(".")[0].split("-")[0])
    if major < REDIS_VERSION_FLOOR[0]:
        raise AssertionError(
            f"provision registry pins image {REDIS_IMAGE} below the D-74 "
            f"floor {'.'.join(map(str, REDIS_VERSION_FLOOR))}")
    names = [t.default_name for t in TABLE_SPECS]
    if len(set(names)) != len(names):
        raise AssertionError(f"duplicate table names in registry: {names}")


def _cfg_get(config: Optional[dict], dotted: Optional[str]):
    if not config or not dotted:
        return None
    node = config
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def resolve_table_names(config: Optional[dict] = None) -> dict:
    """cap_key -> table name, honouring the DECLARED config overrides."""
    return {t.cap_key: (_cfg_get(config, t.config_path) or t.default_name)
            for t in TABLE_SPECS}


# ---------------------------------------------------------------------------
# emitters — every value traceable to the registry above
# ---------------------------------------------------------------------------

def _header(kind: str) -> str:
    floor = ".".join(map(str, REDIS_VERSION_FLOOR))
    return (
        f"# GENERATED by `python -m ab0t_quota provision --emit {kind}` — from the\n"
        f"# enforcing gate registry (D-71/72/73/74 Redis contract, D-76 tables).\n"
        f"# Regenerate rather than hand-edit; the gates and this artifact share one source.\n"
        f"# Contract: non-clustered Redis >= {floor}, maxmemory-policy "
        f"{REQUIRED_MAXMEMORY_POLICY}, Lua scripting enabled;\n"
        f"# keyspace {KEYSPACE_PATTERN}; four DynamoDB tables with GSIs, TTL "
        f"attribute 'ttl', PITR.\n")


def emit_compose(config: Optional[dict] = None, *, port: int = DEFAULT_LOCAL_PORT) -> str:
    _self_check()
    return _header("compose") + f"""services:
  quota-redis:
    image: {REDIS_IMAGE}
    container_name: {LOCAL_CONTAINER_NAME}
    # D-72 (never an evicting policy) + D-32/D-81 (outbox durability):
    command: ["redis-server", "--maxmemory-policy", "{REQUIRED_MAXMEMORY_POLICY}", "--appendonly", "{REQUIRED_APPENDONLY}"]
    ports:
      - "{port}:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 20
  quota-dynamodb:
    image: amazon/dynamodb-local
    ports:
      - "8000:8000"

# DECLARE the stores (never discovered — the library reads no generic env var):
#   QUOTA_REDIS_URL=redis://localhost:{port}/0        <!-- doc-lint:allow R2 local-dev -->
#   DYNAMODB_ENDPOINT=http://localhost:8000
#   AB0T_QUOTA_DDB_PITR_CONFIRMED=true   # DynamoDB Local cannot report PITR; assertion on the record
# Then verify: python -m ab0t_quota preflight
"""


def emit_acl(config: Optional[dict] = None) -> str:
    _self_check()
    req = " ".join(ACL_REQUIRED_RULES)
    opt = " ".join(ACL_OPTIONAL_RULES)
    return _header("acl") + f"""# Minimal least-privilege user (Redis >= 6 ACL syntax). Replace CHANGE_ME.
ACL SETUSER ab0t-quota on >CHANGE_ME ~{KEYSPACE_PATTERN} {req}

# OPTIONAL — grants full machine-verification at boot/preflight/doctor.
# WITHOUT these the library cannot READ topology/policy/version and you must
# put the assertions on the record instead
# (storage.redis_cluster_confirmed_disabled, storage.redis_durability_confirmed):
#   ACL SETUSER ab0t-quota {opt}
#
# Deliberately NOT granted: admin categories, FLUSH*, SCRIPT FLUSH, any
# keyspace outside {KEYSPACE_PATTERN}. Topology is verified by the data-plane
# CROSSSLOT probe, which works under exactly this ACL.
"""


def _table_arns(names: dict) -> list:
    arns = []
    for cap_key in (t.cap_key for t in TABLE_SPECS):
        base = f"arn:aws:dynamodb:REGION:ACCOUNT:table/{names[cap_key]}"
        arns.append(base)
        arns.append(base + "/index/*")
    return arns


def emit_iam(config: Optional[dict] = None, *, include_create: bool = False) -> str:
    _self_check()
    names = resolve_table_names(config)
    statements = [{
        "Sid": "Ab0tQuotaRuntime",
        "Effect": "Allow",
        "Action": list(IAM_RUNTIME_ACTIONS),
        "Resource": _table_arns(names),
    }]
    if include_create:
        statements.append({
            "Sid": "Ab0tQuotaSelfProvision",
            "Effect": "Allow",
            "Action": list(IAM_CREATE_ACTIONS),
            "Resource": _table_arns(names),
        })
    doc = {"Version": "2012-10-17", "Statement": statements}
    note = ("" if include_create else
            "// Self-provisioning actions (CreateTable/UpdateTimeToLive/TagResource)\n"
            "// are NOT included — add them only with storage.auto_create_tables=true\n"
            "// (re-run with --include-create). Preflight itself needs only the three\n"
            "// Describe* actions.\n")
    return (_header("iam").replace("#", "//")
            + note + json.dumps(doc, indent=2) + "\n")


def _tf_table(spec: TableSpec, name: str) -> str:
    attrs = {"PK": "S", "SK": "S"}
    for g in spec.gsis:
        attrs[g.hash_key] = g.hash_type
        attrs[g.range_key] = g.range_type
    attr_blocks = "\n".join(
        f'  attribute {{\n    name = "{k}"\n    type = "{v}"\n  }}'
        for k, v in attrs.items())
    gsi_blocks = "\n".join(f"""  global_secondary_index {{
    name            = "{g.name}"
    hash_key        = "{g.hash_key}"
    range_key       = "{g.range_key}"
    projection_type = "{g.projection}"
  }}""" for g in spec.gsis)
    return f"""# {spec.purpose}
# mirrors: {spec.created_by}
resource "aws_dynamodb_table" "{spec.cap_key}" {{
  name         = "{name}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

{attr_blocks}

{gsi_blocks}

  ttl {{
    attribute_name = "{spec.ttl_attribute}"
    enabled        = true
  }}

  point_in_time_recovery {{
    enabled = {"true" if spec.pitr_required else "false"}
  }}

  server_side_encryption {{
    enabled = true
  }}

  tags = {{
    Service   = "ab0t-quota"
    ManagedBy = "ab0t-quota-library"
  }}
}}
"""


def emit_terraform(config: Optional[dict] = None) -> str:
    _self_check()
    names = resolve_table_names(config)
    tables = "\n".join(_tf_table(t, names[t.cap_key]) for t in TABLE_SPECS)
    policy = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "Ab0tQuotaRuntime", "Effect": "Allow",
            "Action": list(IAM_RUNTIME_ACTIONS),
            "Resource": [f"${{aws_dynamodb_table.{t.cap_key}.arn}}" for t in TABLE_SPECS]
                        + [f"${{aws_dynamodb_table.{t.cap_key}.arn}}/index/*" for t in TABLE_SPECS],
        }],
    }, indent=2)
    return _header("terraform") + f"""# Emit-and-let-them-apply: this module CREATES NOTHING when printed — apply it
# with your own tooling and credentials (provision never touches your cloud).
# Redis is deliberately absent: pick a provider meeting the gate contract
# (docs/requirements.md provider matrix) — the library refuses at boot if it
# does not, and `python -m ab0t_quota doctor` grades the posture.

{tables}
resource "aws_iam_policy" "ab0t_quota_runtime" {{
  name   = "ab0t-quota-runtime"
  policy = <<POLICY
{policy}
POLICY
}}
"""


_EMITTERS: dict = {
    "compose": emit_compose,
    "terraform": emit_terraform,
    "acl": emit_acl,
    "iam": emit_iam,
}


def emit(kind: str, config: Optional[dict] = None, **kw) -> str:
    if kind not in _EMITTERS:
        raise ValueError(f"unknown emit kind {kind!r}; choose from {EMIT_KINDS}")
    text = _EMITTERS[kind](config, **kw)
    problems = check_artifact_conformance(kind, text, config)
    if problems:  # our bug, never the consumer's environment
        raise AssertionError(
            f"emitted {kind} artifact does not satisfy the enforcing registry: "
            f"{problems}")
    return text


def check_artifact_conformance(kind: str, text: str,
                               config: Optional[dict] = None) -> list:
    """The D-14 instrument: verify an emitted artifact against the SAME
    enforcing constants the gates read. Used on every emit (a nonconforming
    artifact is exit 4) and by the planted-offender tests, which prove this
    check can go RED. LIMIT (D-14 rule 4): this is token-level verification
    of the contract values, not a parse of compose/HCL semantics."""
    problems = []
    if kind in ("compose",):
        if REQUIRED_MAXMEMORY_POLICY not in text:
            problems.append(f"missing maxmemory-policy {REQUIRED_MAXMEMORY_POLICY}")
        for policy in EVICTING_POLICIES:
            if policy in text:
                problems.append(f"contains D-72-refused policy {policy}")
        if "--appendonly" not in text or REQUIRED_APPENDONLY not in text:
            problems.append("missing appendonly persistence (D-32/D-81)")
    if kind == "acl":
        if f"~{KEYSPACE_PATTERN}" not in text:
            problems.append(f"ACL not scoped to ~{KEYSPACE_PATTERN}")
        for rule in ACL_REQUIRED_RULES:
            if rule not in text:
                problems.append(f"ACL missing {rule}")
        if "+@all" in text or "allkeys" in text or "~*" in text:
            problems.append("ACL is over-broad (+@all / ~*)")
    if kind in ("terraform", "iam"):
        names = resolve_table_names(config)
        for spec in TABLE_SPECS:
            if names[spec.cap_key] not in text:
                problems.append(f"missing table {names[spec.cap_key]}")
    if kind == "terraform":
        for spec in TABLE_SPECS:
            for g in spec.gsis:
                if g.name not in text:
                    problems.append(f"missing GSI {g.name} ({spec.cap_key})")
            if f'attribute_name = "{spec.ttl_attribute}"' not in text:
                problems.append(f"missing ttl attribute {spec.ttl_attribute}")
        if "point_in_time_recovery" not in text:
            problems.append("missing PITR block")
    if kind == "iam":
        for action in IAM_RUNTIME_ACTIONS:
            if action not in text:
                problems.append(f"missing IAM action {action}")
    return problems


# ---------------------------------------------------------------------------
# --local (T-3): one command to a CONFORMING dev Redis, verified by the
# same evaluator boot uses.
# ---------------------------------------------------------------------------

def local_docker_command(*, port: int = DEFAULT_LOCAL_PORT,
                         name: str = LOCAL_CONTAINER_NAME) -> list:
    _self_check()
    return ["docker", "run", "-d", "--name", name,
            "-p", f"{port}:6379", REDIS_IMAGE,
            "redis-server",
            "--maxmemory-policy", REQUIRED_MAXMEMORY_POLICY,
            "--appendonly", REQUIRED_APPENDONLY]


async def _verify_local(url: str, *, timeout: float) -> Tuple[bool, list]:
    """Verify the started container with the ONE evaluator set — the same
    judgement boot raises on and preflight exits on."""
    import redis.asyncio as _aredis
    from .redis_preflight import verify_redis_invariants
    client = _aredis.Redis.from_url(url, socket_connect_timeout=timeout,
                                    socket_timeout=timeout)
    try:
        caps, unsafe = await verify_redis_invariants(
            client, {}, outbox_on_redis=False, script_load=True)
    finally:
        try:
            await client.aclose()
        except Exception:
            pass
    return (not unsafe), [f"{k}: {d}" for k, d in unsafe]


def run_local(*, port: int = DEFAULT_LOCAL_PORT, name: str = LOCAL_CONTAINER_NAME,
              dry_run: bool = False, timeout: float = 5.0,
              emit_line: Callable[[str], None] = print,
              _runner=None, _verifier=None) -> int:
    """Start (or reuse) a conforming local dev Redis in Docker, then verify it
    with `verify_redis_invariants`. Side effects, stated: creates/starts ONE
    local Docker container named `{name}`; nothing else, never cloud.

    Exit: 0 conforming & verified · 1 started but NONCONFORMING (our image
    args vs the gates — a bug in us or a reused foreign container) · 3 docker
    unavailable / container did not come up · 4 internal.
    """
    import asyncio
    import subprocess
    import time as _time

    cmd = local_docker_command(port=port, name=name)
    url = f"redis://localhost:{port}/0"
    emit_line("provision --local — side effect statement: this creates/starts ONE "
              f"local Docker container ({name}); it never touches cloud resources.")
    if dry_run:
        emit_line("DRY RUN — would execute:")
        emit_line("  " + " ".join(cmd))
        emit_line(f"then verify with verify_redis_invariants against {url}")
        return 0

    runner = _runner or (lambda c: subprocess.run(
        c, capture_output=True, text=True, timeout=60))
    try:
        probe = runner(["docker", "ps", "-a", "--filter", f"name=^{name}$",
                        "--format", "{{.Names}} {{.Status}}"])
    except FileNotFoundError:
        emit_line("error: docker is not available on PATH. Equivalent manual command:")
        emit_line("  " + " ".join(cmd))
        return 3
    existing = (probe.stdout or "").strip()
    if existing.startswith(name):
        if "Up" in existing:
            emit_line(f"container {name} already running — reusing it (verifying below)")
        else:
            emit_line(f"container {name} exists but is stopped — starting it")
            res = runner(["docker", "start", name])
            if res.returncode != 0:
                emit_line(f"error: docker start failed: {res.stderr.strip()}")
                return 3
    else:
        res = runner(cmd)
        if res.returncode != 0:
            emit_line(f"error: docker run failed: {res.stderr.strip()}")
            return 3
        emit_line(f"started {name} ({REDIS_IMAGE}) on localhost:{port}")

    verifier = _verifier or (lambda: asyncio.run(_verify_local(url, timeout=timeout)))
    ok = False
    unsafe: list = []
    deadline = _time.monotonic() + timeout * 3
    while True:
        # A just-started container may not accept connections yet. Retry the
        # REACHABILITY class within the window — and never report it as a gate
        # verdict (GATE-01: unreachable is a reachability answer, nothing else).
        try:
            ok, unsafe = verifier()
        except Exception as e:
            ok, unsafe = False, [f"redis_reachable: {type(e).__name__}: {e}"]
        reach_only = (not ok) and unsafe \
            and all(u.startswith("redis_reachable") for u in unsafe)
        if ok or not reach_only:
            break
        if _time.monotonic() >= deadline:
            emit_line(f"error: started container is not reachable at {url} "
                      f"within {timeout * 3:.0f}s: {unsafe[0]} — this is a "
                      f"REACHABILITY failure, not a gate verdict (the gates "
                      f"never ran)")
            return 3
        _time.sleep(0.3)
    if not ok:
        emit_line("NONCONFORMING — the started Redis fails the boot gates "
                  "(same evaluator: verify_redis_invariants):")
        for line in unsafe:
            emit_line(f"  {line}")
        return 1
    emit_line("VERIFIED conforming by verify_redis_invariants (the boot gates' own evaluator).")
    emit_line("Declare it (never discovered):")
    emit_line(f"  QUOTA_REDIS_URL={url}")
    emit_line(f'  or quota-config.json: {{"storage": {{"redis_url": "{url}"}}}}')
    emit_line("Then: python -m ab0t_quota preflight")
    return 0
