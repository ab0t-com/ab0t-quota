"""T-12 — `python -m ab0t_quota preflight` (DOC-04; D-4 names the command).

Converts the whole D-71…D-81 refusal family from a 3am crash-loop into a CI
failure. Design: tickets/…/design_preflight_and_prerequisites_20260721.md §2.

PREFLIGHT OWNS NO JUDGMENT (§2.5, the never-drift rule):
  * one resolver  — phase 1 prints `resolve_dependencies`' plan, the identical
    calls `setup_quota` consumes (bridge identity via `resolve_bridge_identity`);
  * one evaluator set — phase 3 calls `verify_redis_invariants` /
    `verify_ddb_table`, the same functions the boot gates and the D-75
    revalidator call. Boot raises, the CLI exits: two consumers, one judgement.

What preflight must never do (§9): mutate consumer data (single stated
exception: D-73's SCRIPT LOAD warms the server's script cache — the same load
boot performs; skip with --no-script-load, D-73 reports SKIPPED); create
anything (a missing table is reported per storage.auto_create_tables, never
created); contact the mesh by default (D-6: --check-mesh is opt-in, GET
health probes only, never the tier-catalog PUT); contact ANYTHING before the
resolved plan is printed; read a generic env var or invent a value.

Exit taxonomy (§2.4 — 1 vs 3 is GATE-01's lesson encoded):
  0 boot would start · 1 a gate would refuse · 2 config error (no network
  contacted) · 3 declared infra unreachable / credentials / not-permitted ·
  4 internal error (a bug in us, never the consumer's environment).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .errors import QuotaConfigError

logger = logging.getLogger("ab0t_quota.preflight")

EXIT_OK = 0
EXIT_GATE = 1
EXIT_CONFIG = 2
EXIT_REACH = 3
EXIT_INTERNAL = 4

REPORT_SCHEMA = "ab0t-quota/preflight-report/v1"

#: capability key -> gate ID, for the report lines. Severity mirrors BOOT:
#: "refuse" keys raise at boot; "degrade" keys log loudly and boot continues
#: (never-drift rule 7: preflight must not refuse what boot would pass).
_REDIS_GATE_IDS = {
    "redis_topology": ("D-71", "refuse"),
    "counter_eviction_policy": ("D-72", "refuse"),
    "redis_scripting": ("D-73", "refuse"),
    "redis_version": ("D-74", "refuse"),
    "counter_evictions_observed": ("D-80", "degrade"),
    "redis_persist_status": ("D-81", "degrade"),
    "memory_headroom": ("D-77", "degrade"),
}


@dataclass
class GateLine:
    id: str
    name: str
    status: str            # pass | fail | warn | skip
    detail: str
    remedy: Optional[str] = None


@dataclass
class PreflightReport:
    config_path: str = "(search path)"
    config_sha256: Optional[str] = None
    engine_mode: str = "local"
    #: derived exactly as boot derives it (internal; doctor reuses the judgement)
    outbox_on_redis: Optional[bool] = None
    resolved_plan: list = field(default_factory=list)   # [{name, value, source, secret}]
    outbound_contacts: list = field(default_factory=list)
    deprecations: List[str] = field(default_factory=list)
    gates: List[GateLine] = field(default_factory=list)
    capabilities: dict = field(default_factory=dict)
    config_errors: List[str] = field(default_factory=list)
    reach_errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    exit_code: int = EXIT_OK

    def _fail(self, code: int) -> None:
        """First failure in boot order wins the exit code."""
        if self.exit_code == EXIT_OK:
            self.exit_code = code

    def to_json(self) -> dict:
        from . import __version__ as _v
        return {
            "schema": REPORT_SCHEMA,
            "library": {"runtime": "python", "version": _v},
            "config": {"path": self.config_path, "sha256": self.config_sha256,
                       "engine_mode": self.engine_mode},
            "resolved_plan": self.resolved_plan,
            "outbound_contacts": self.outbound_contacts,
            "deprecations": self.deprecations,
            "gates": [vars(g) for g in self.gates],
            "capabilities": self.capabilities,
            "config_errors": self.config_errors,
            "reach_errors": self.reach_errors,
            "warnings": self.warnings,
            "notes": self.notes,
            "verdict": {
                "boot": ("would_start" if self.exit_code == EXIT_OK
                         else {EXIT_GATE: "would_refuse (gate)",
                               EXIT_CONFIG: "would_refuse (config)",
                               EXIT_REACH: "unreachable/credentials",
                               }.get(self.exit_code, "internal_error")),
                "exit_code": self.exit_code,
            },
        }


def _capture_deprecations(config) -> List[str]:
    """Run the D-10 presence-only call-outs, capturing what they log."""
    from .resolve import check_deprecated_generic_env
    records: List[str] = []

    class _H(logging.Handler):
        def emit(self, record):
            records.append(f"{record.levelname}: {record.getMessage()}")

    h = _H()
    lg = logging.getLogger("ab0t_quota.resolve")
    lg.addHandler(h)
    try:
        check_deprecated_generic_env(config)
    finally:
        lg.removeHandler(h)
    return records


def _classify_reach(exc: Exception) -> str:
    name = type(exc).__name__
    text = f"{exc}".lower()
    if "auth" in name.lower() or "noauth" in text or "wrongpass" in text \
            or "invalid password" in text:
        return "credentials"
    if "accessdenied" in name.lower() or "accessdenied" in text \
            or "unauthorized" in name.lower():
        return "not-permitted"
    return "unreachable"


async def run_preflight(
    config_path: Optional[str] = None,
    *,
    offline: bool = False,
    check_mesh: bool = False,
    strict: bool = False,
    timeout: float = 5.0,
    script_load: bool = True,
    emit: Callable[[str], None] = print,
) -> PreflightReport:
    report = PreflightReport()

    # ---- Phase 0 — config schema (offline; no network) --------------------
    from .config import load_config
    resolved_path = config_path or os.environ.get("QUOTA_CONFIG_PATH")
    if resolved_path and os.path.exists(resolved_path):
        report.config_path = resolved_path
        report.config_sha256 = hashlib.sha256(
            open(resolved_path, "rb").read()).hexdigest()
    try:
        config = load_config(config_path)
    except QuotaConfigError as e:
        report.config_errors.append(str(e))
        report.exit_code = EXIT_CONFIG
        emit(f"CONFIG ERROR — no network was contacted:\n{e}")
        return report

    from . import __version__ as _v
    emit(f"ab0t-quota preflight {_v} — config: {report.config_path}"
         + (f" (sha256:{report.config_sha256[:12]}…)" if report.config_sha256 else ""))

    # D-10 deprecation call-outs (presence-only), surfaced in the report.
    report.deprecations = _capture_deprecations(config)
    for line in report.deprecations:
        emit(f"DEPRECATION: {line}")

    # ---- Phase 1 — resolved plan with provenance (before ANY socket) ------
    from .resolve import redact_url, resolve_dependencies, strip_url_password
    from .setup import _mesh_url, resolve_bridge_identity

    mode = (config.get("engine_mode") or "local").lower()
    if mode not in ("local", "byo_redis", "bridge"):
        mode = "local"   # setup_quota's exact fallback
    report.engine_mode = mode

    if mode == "bridge":
        try:
            key_row, svc_row = resolve_bridge_identity(config)
        except QuotaConfigError as e:
            report.config_errors.append(str(e))
            report.exit_code = EXIT_CONFIG
            emit(f"CONFIG ERROR — no network was contacted:\n{e}")
            return report
        emit("RESOLVED PLAN (bridge mode) — nothing has been contacted yet")
        for row in (key_row, svc_row):
            emit(f"  {row.config_key:<24} {row.display():<38} source={row.source}")
            report.resolved_plan.append({
                "name": row.config_key, "value": row.display(),
                "source": row.source, "secret": row.secret})
        report.outbound_contacts = [
            {"target": _mesh_url("billing"), "purpose": "every quota check (bridge mode)"}]
        emit(f"OUTBOUND CONTACTS at boot: {_mesh_url('billing')} (every quota check)")
        report.gates.append(GateLine(
            "D-71", "redis_topology", "skip",
            "n/a (no redis counter store — bridge mode)"))
        report.notes.append(
            "bridge mode is quota enforcement only today — no billing router, "
            "outbox, auth events, or health routes (FUTURE-1)")
        if offline:
            report.notes.append("offline: config + identity validated; nothing contacted")
        elif check_mesh:
            await _probe_mesh(report, emit, timeout, targets=("billing",))
        _finish(report, strict, emit)
        return report

    try:
        plan = resolve_dependencies(config, mode=mode)
    except QuotaConfigError as e:
        report.config_errors.append(str(e))
        report.exit_code = EXIT_CONFIG
        emit(f"CONFIG ERROR — no network was contacted:\n{e}")
        return report

    emit(plan.provenance_block() + "\n  (nothing has been contacted yet)")
    for key, row in plan.items():
        report.resolved_plan.append({
            "name": key, "value": row.display(), "source": row.source,
            "secret": row.secret})

    storage = config.get("storage", {}) or {}
    outbox_cfg = config.get("outbox", {}) or {}
    act_cfg = config.get("activations", {}) or {}
    auto_create = bool(storage.get("auto_create_tables", False))

    outbound = [
        {"target": redact_url(plan["redis_url"].value or ""),
         "purpose": "counter, activation ledger / outbox fallback"},
        {"target": plan["dynamodb_endpoint"].value or "dynamodb (AWS SDK-resolved endpoint)",
         "purpose": "state, outbox, activations, handler ledger"},
        {"target": _mesh_url("billing"), "purpose": "tier-catalog publish, paid surface"},
        {"target": _mesh_url("payment"), "purpose": "checkout/portal proxy (paid)"},
    ]
    report.outbound_contacts = outbound
    emit("OUTBOUND CONTACTS at boot (declared surface only):")
    for c in outbound:
        emit(f"  {c['target']:<45} {c['purpose']}")

    if offline:
        report.notes.append("offline: schema + plan + provenance only; nothing contacted")
        emit("OFFLINE — stopping before any network contact. Config is valid.")
        _finish(report, strict, emit)
        return report

    # ---- Phase 2+3 — DDB first (describe-only), then the Redis gates ------
    ddb_outbox_available = await _check_ddb(
        report, emit, plan=plan, storage=storage, outbox_cfg=outbox_cfg,
        act_cfg=act_cfg, auto_create=auto_create, timeout=timeout)

    outbox_pref = (outbox_cfg.get("store") or "ddb").lower()
    outbox_on_redis = outbox_pref == "redis" or not ddb_outbox_available
    report.outbox_on_redis = outbox_on_redis

    await _check_redis(
        report, emit, config=config, plan=plan, outbox_cfg=outbox_cfg,
        outbox_on_redis=outbox_on_redis, timeout=timeout, script_load=script_load)

    # ---- Optional mesh probes (D-6: OFF by default; GET health only) ------
    if check_mesh:
        await _probe_mesh(report, emit, timeout, targets=("billing", "payment"))
    else:
        report.notes.append("mesh not probed (--check-mesh is opt-in per D-6; "
                            "preflight never writes to the mesh)")

    _finish(report, strict, emit)
    return report


def _build_redis_client(plan, timeout):
    """ONE connection recipe for preflight + doctor (a second copy of the
    credential precedence would be D-5's drift one layer up). Returns
    (client, display_url)."""
    from .resolve import strip_url_password
    import redis.asyncio as _aredis

    redis_url = plan["redis_url"].value
    pw_row = plan["redis_password"]
    kwargs: dict = {"decode_responses": False,
                    "socket_connect_timeout": timeout, "socket_timeout": timeout}
    if pw_row.declared:
        redis_url, _url_pw = strip_url_password(redis_url)
        kwargs["password"] = pw_row.value
    return _aredis.Redis.from_url(redis_url, **kwargs), redis_url


def _ddb_client_kwargs(plan) -> dict:
    kwargs = {}
    if plan["dynamodb_region"].value:
        kwargs["region_name"] = plan["dynamodb_region"].value
    if plan["dynamodb_endpoint"].value:
        kwargs["endpoint_url"] = plan["dynamodb_endpoint"].value
    return kwargs


async def _check_redis(report, emit, *, config, plan, outbox_cfg,
                       outbox_on_redis, timeout, script_load) -> None:
    from .resolve import redact_url

    client, redis_url = _build_redis_client(plan, timeout)

    # GT T1: the SAME classified probe boot's reachability gate runs.
    from .redis_preflight import check_redis_reachable
    ok, kind, detail = await check_redis_reachable(client, timeout=timeout)
    if not ok:
        msg = (f"declared Redis {redact_url(redis_url)}: {kind} ({detail}) — "
               f"this is NOT a topology/eviction/scripting/version verdict "
               f"(GATE-01); those gates never ran and were SKIPPED")
        report.reach_errors.append(msg)
        report._fail(EXIT_REACH)
        emit(f"REACHABILITY: {msg}")
        for cap, (gate_id, _sev) in _REDIS_GATE_IDS.items():
            report.gates.append(GateLine(gate_id, cap, "skip",
                                         "blocked by reachability"))
        return

    from .billing.outbox import check_redis_outbox_durability  # boot's exact check
    from .redis_preflight import verify_redis_invariants
    caps, unsafe = await verify_redis_invariants(
        client, config, outbox_on_redis=outbox_on_redis, script_load=script_load)
    report.capabilities.update(caps)
    unsafe_keys = {k for k, _ in unsafe}
    for cap, (gate_id, severity) in _REDIS_GATE_IDS.items():
        detail = str(caps.get(cap, "(not evaluated)"))
        if cap == "redis_scripting" and not script_load:
            report.gates.append(GateLine(gate_id, cap, "skip", detail))
            continue
        if cap in unsafe_keys:
            if severity == "refuse":
                report.gates.append(GateLine(gate_id, cap, "fail", detail,
                                             remedy="see the gate's error text"))
                report._fail(EXIT_GATE)
            else:
                report.gates.append(GateLine(gate_id, cap, "warn",
                                             detail + " (boot starts; degraded, loudly)"))
                report.warnings.append(f"{gate_id} {cap}: {detail}")
        else:
            status = "warn" if str(detail).lower().startswith(("unknown", "unbounded")) \
                else "pass"
            if status == "warn":
                report.warnings.append(f"{gate_id} {cap}: {detail}")
            report.gates.append(GateLine(gate_id, cap, status, detail))

    # D-32/D-34 — outbox durability as boot (default enable_paid=True) judges it.
    if outbox_on_redis:
        confirmed = bool(outbox_cfg.get("redis_durability_confirmed", False))
        durable, detail = await check_redis_outbox_durability(client, confirmed=confirmed)
        if durable:
            report.gates.append(GateLine("D-32/D-34", "outbox_durability", "pass",
                                         f"outbox on Redis, durable: {detail}"))
        elif bool(outbox_cfg.get("allow_ephemeral", False)):
            report.gates.append(GateLine(
                "D-32/D-34", "outbox_durability", "warn",
                f"NOT durable ({detail}); allow_ephemeral=true — boot starts "
                f"with billing DISABLED (dev)"))
            report.warnings.append(f"outbox not durable; allow_ephemeral set: {detail}")
        else:
            report.gates.append(GateLine(
                "D-32/D-34", "outbox_durability", "fail",
                f"no durable outbox ({detail}) — boot with enable_paid=True (the "
                f"default) REFUSES (D-34)",
                remedy=("make the DDB outbox reachable, or a durable Redis "
                        "(persistence + non-evicting, or "
                        "outbox.redis_durability_confirmed), or "
                        "outbox.allow_ephemeral=true (dev), or enable_paid=False")))
            report._fail(EXIT_GATE)
    else:
        report.gates.append(GateLine("D-32/D-34", "outbox_durability", "pass",
                                     "outbox on DDB (durable)"))
    try:
        await client.aclose()
    except Exception:
        pass


async def _check_ddb(report, emit, *, plan, storage, outbox_cfg, act_cfg,
                     auto_create, timeout) -> bool:
    """Describe-only checks over the tables boot would use. NEVER creates
    (§9.2: auto_create_tables makes BOOT creation legal; preflight reports
    'WILL CREATE', it does not execute it). Returns outbox-DDB availability.

    Table names / TTL attributes / required GSIs come from the ONE registry
    (`provision.TABLE_SPECS`) — the same source `provision --emit` renders,
    so the gate and the emitted artifact cannot drift (T-2)."""
    from .provision import TABLE_SPECS, resolve_table_names

    names = resolve_table_names(
        {"storage": storage, "outbox": outbox_cfg, "activations": act_cfg})
    specs = {t.cap_key: t for t in TABLE_SPECS}
    checks = []
    if storage.get("persistence_enabled", True):
        s = specs["ddb_state"]
        checks.append((s.cap_key, names[s.cap_key], s.ttl_attribute,
                       s.preflight_gsis,
                       "state store degrades (persistence off, non-fatal)"))
    if (act_cfg.get("store") or "ddb").lower() != "redis":
        s = specs["ddb_activations"]
        checks.append((s.cap_key, names[s.cap_key], s.ttl_attribute,
                       s.preflight_gsis,
                       "activation ledger falls back to Redis under the durability machine-check"))
    outbox_pref = (outbox_cfg.get("store") or "ddb").lower()
    if outbox_pref != "redis":
        s = specs["ddb_outbox"]
        checks.append((s.cap_key, names[s.cap_key], s.ttl_attribute,
                       s.preflight_gsis,
                       "outbox falls back to Redis under the durability machine-check"))
    report.gates.append(GateLine(
        "D-82", "ddb_handler_ledger", "skip",
        "checked at boot only when a consumer explicitly wires a ledger + ddb client"))
    if not checks:
        return False

    from .ddb_preflight import pitr_confirmed_from, verify_ddb_table
    import aioboto3

    kwargs = _ddb_client_kwargs(plan)
    pitr_confirmed = pitr_confirmed_from({"storage": storage})
    outbox_available = False

    session = aioboto3.Session()
    try:
        async with session.client("dynamodb", **kwargs) as client:
            for cap_key, table, ttl_attr, gsis, consequence in checks:
                try:
                    await asyncio.wait_for(
                        client.describe_table(TableName=table), timeout)
                except Exception as e:
                    not_found = ("ResourceNotFound" in type(e).__name__
                                 or "ResourceNotFound" in str(e))
                    if not_found:
                        if auto_create:
                            report.gates.append(GateLine(
                                "T-6", cap_key, "warn",
                                f"table {table} MISSING — PLAN: boot WILL CREATE it "
                                f"(storage.auto_create_tables=true). Preflight never "
                                f"creates."))
                            report.warnings.append(f"{cap_key}: {table} will be created at boot")
                        else:
                            report.gates.append(GateLine(
                                "T-6", cap_key, "warn",
                                f"table {table} MISSING and storage.auto_create_tables "
                                f"is false (default) — {consequence}",
                                remedy=(f"pre-create {table} (docs/requirements.md) or "
                                        f"set storage.auto_create_tables: true")))
                            report.warnings.append(f"{cap_key}: table {table} missing")
                        continue
                    cls = _classify_reach(e)
                    msg = (f"DynamoDB ({cap_key}, table {table}): {cls} "
                           f"({type(e).__name__}: {e}) — not a table/backup verdict "
                           f"(GATE-01); gate SKIPPED")
                    report.reach_errors.append(msg)
                    report._fail(EXIT_REACH)
                    report.gates.append(GateLine("D-76", cap_key, "skip",
                                                 "blocked by reachability"))
                    emit(f"REACHABILITY: {msg}")
                    continue
                if cap_key == "ddb_state":
                    # boot never gate-verifies the state table (it only creates/
                    # uses it); existence is all preflight may honestly assert.
                    report.gates.append(GateLine("T-6", cap_key, "pass",
                                                 f"table {table} exists"))
                    report.capabilities[cap_key] = "exists"
                    continue
                value, fatal, warn = await verify_ddb_table(
                    client, table, ttl_attribute=ttl_attr,
                    pitr_confirmed=pitr_confirmed, required_gsis=tuple(gsis))
                report.capabilities[cap_key] = value
                if fatal:
                    report.gates.append(GateLine("D-76", cap_key, "fail", str(fatal),
                                                 remedy="see detail"))
                    report._fail(EXIT_GATE)
                elif warn:
                    report.gates.append(GateLine("D-76", cap_key, "warn", str(warn)))
                    report.warnings.append(f"{cap_key}: {warn}")
                    if cap_key == "ddb_outbox":
                        outbox_available = True
                else:
                    report.gates.append(GateLine("D-76", cap_key, "pass", str(value)))
                    if cap_key == "ddb_outbox":
                        outbox_available = True
    except Exception as e:
        cls = _classify_reach(e)
        msg = f"DynamoDB client: {cls} ({type(e).__name__}: {e})"
        report.reach_errors.append(msg)
        report._fail(EXIT_REACH)
        emit(f"REACHABILITY: {msg}")
    return outbox_available


async def _probe_mesh(report, emit, timeout, *, targets) -> None:
    """--check-mesh (D-6, opt-in): GET /health only — never a write, never the
    tier-catalog PUT."""
    import httpx
    from .setup import _mesh_url
    async with httpx.AsyncClient(timeout=timeout) as client:
        for svc in targets:
            url = f"{_mesh_url(svc)}/health"
            try:
                resp = await client.get(url)
                report.gates.append(GateLine(
                    "MESH", f"mesh_{svc}", "pass" if resp.status_code < 500 else "warn",
                    f"GET {url} -> {resp.status_code}"))
            except Exception as e:
                report.reach_errors.append(f"mesh {svc} ({url}): {type(e).__name__}: {e}")
                report._fail(EXIT_REACH)
                report.gates.append(GateLine("MESH", f"mesh_{svc}", "fail",
                                             f"unreachable: {type(e).__name__}"))


def _finish(report: PreflightReport, strict: bool, emit) -> None:
    if strict and report.exit_code == EXIT_OK and report.warnings:
        report.exit_code = EXIT_GATE
        report.notes.append("--strict: warnings fail the exit code")
    emit("")
    for g in report.gates:
        line = f"  {g.status.upper():<5} {g.id:<9} {g.name:<26} {g.detail}"
        if g.remedy:
            line += f"\n        remedy: {g.remedy}"
        emit(line)
    verdict = {EXIT_OK: "boot would START",
               EXIT_GATE: "a gate would REFUSE boot",
               EXIT_CONFIG: "config error",
               EXIT_REACH: "declared infrastructure unreachable / not permitted",
               }.get(report.exit_code, "internal error")
    emit(f"\nPREFLIGHT VERDICT: {verdict} (exit {report.exit_code})")
    if not any(g.name == "redis_scripting" and g.status == "skip" for g in report.gates):
        emit("note: preflight wrote nothing except loading the counter's Lua script "
             "into Redis's script cache (idempotent, no data keys — the same load "
             "boot performs; --no-script-load skips it).")


# ---------------------------------------------------------------------------
# T-1 (program board, tooling lane) — `python -m ab0t_quota doctor`.
#
# `preflight` answers "will this boot"; `doctor` grades POSTURE — the class
# today's gates deliberately let through: a Redis with no persistence boots
# fine and loses the outbox on restart; PITR-off passes behind an assertion
# flag. One evaluator set: doctor's boot verdict IS run_preflight's (called,
# not copied — verdict equality is structural), and every posture probe
# reuses the shipped evaluators (read_redis_policy, check_persist_facts,
# verify_ddb_table) or derives from their published capability values.
#
# HONESTY RULE (the programme's thesis): a dimension doctor could not observe
# is graded `not_checked` WITH the reason — never inferred, never "good".
# AccessDenied is a permission answer, never a missing table (D-8).
# ---------------------------------------------------------------------------

POSTURE_SCHEMA = "ab0t-quota/doctor-posture/v1"

GRADE_GOOD = "good"
GRADE_ATTENTION = "attention"
GRADE_RISK = "risk"
GRADE_NOT_CHECKED = "not_checked"
GRADE_INFO = "info"


@dataclass
class PostureFinding:
    id: str
    name: str
    grade: str
    detail: str
    remedy: Optional[str] = None
    checked: bool = True


@dataclass
class DoctorReport:
    preflight: PreflightReport
    findings: List[PostureFinding] = field(default_factory=list)
    side_effects: List[str] = field(default_factory=list)
    exit_code: int = EXIT_OK

    def to_json(self) -> dict:
        """EXTENDS preflight-report/v1: every v1 key is present unchanged; the
        posture section is additive, so a CI consumer of v1 keeps working and
        an auditor gets the graded posture in the same artifact."""
        doc = self.preflight.to_json()
        doc["posture"] = {
            "schema": POSTURE_SCHEMA,
            "side_effects": self.side_effects,
            "findings": [vars(f) for f in self.findings],
            "not_checked": [
                {"name": f.name, "reason": f.detail}
                for f in self.findings if f.grade == GRADE_NOT_CHECKED],
            "verdict": {"exit_code": self.exit_code},
        }
        doc["verdict"]["exit_code"] = self.exit_code
        return doc


def _nc(fid, name, reason) -> PostureFinding:
    return PostureFinding(fid, name, GRADE_NOT_CHECKED, reason, checked=False)


async def _posture_redis(findings: List[PostureFinding], report, config, plan,
                         *, timeout: float) -> None:
    """Persistence + ACL-breadth posture over the DECLARED Redis, using the
    shipped evaluators (read_redis_policy / check_persist_facts / evaluate_*).
    Never writes."""
    from .redis_preflight import (
        check_persist_facts, durability_confirmed_from, evaluate_persist_facts,
        read_redis_policy,
    )
    client, _url = _build_redis_client(plan, timeout)
    try:
        policy, appendonly, save, unavailable, err = await read_redis_policy(client)
        confirmed = durability_confirmed_from(config)
        outbox_here = bool(report.outbox_on_redis)
        if unavailable:
            if confirmed:
                findings.append(PostureFinding(
                    "P-PERSIST", "redis_persistence", GRADE_ATTENTION,
                    f"CONFIG unavailable ({err}) — durability rests on the operator "
                    f"assertion (redis_durability_confirmed). An assertion is a "
                    f"promise, not an observation; nothing here verified that this "
                    f"Redis persists or does not evict.",
                    remedy="if the provider exposes persistence status out-of-band, "
                           "attach that evidence to the audit alongside this report"))
            else:
                findings.append(_nc(
                    "P-PERSIST", "redis_persistence",
                    f"CONFIG unavailable ({err}) and no assertion on record — "
                    f"posture unknown (the boot gate refuses this separately)"))
        else:
            facts = await check_persist_facts(client)
            p_status, p_detail = evaluate_persist_facts(
                aof_enabled=facts.get("aof_enabled"),
                aof_write=facts.get("aof_last_write_status"),
                rdb_bgsave=facts.get("rdb_last_bgsave_status"),
                aof_rewrite=facts.get("aof_last_bgrewrite_status"))
            persisted = (appendonly or "") == "yes" or bool((save or "").strip())
            if p_status == "persist_failing":
                findings.append(PostureFinding(
                    "P-PERSIST", "redis_persistence", GRADE_RISK,
                    f"persistence configured but FAILING: {p_detail}",
                    remedy="free the disk / fix the volume; verify "
                           "aof_last_write_status=ok"))
            elif not persisted and outbox_here:
                findings.append(PostureFinding(
                    "P-PERSIST", "redis_persistence", GRADE_RISK,
                    f"NO persistence (appendonly={appendonly or 'no'}, save="
                    f"{save or 'none'}) and the OUTBOX resolves to this Redis: it "
                    f"boots fine and loses the money outbox on restart"
                    + (" — boot passes only via an operator assertion/allow_ephemeral"
                       if report.exit_code == EXIT_OK else ""),
                    remedy="enable appendonly yes (or RDB save points), or move the "
                           "outbox to DynamoDB"))
            elif not persisted:
                findings.append(PostureFinding(
                    "P-PERSIST", "redis_persistence", GRADE_ATTENTION,
                    f"no persistence (appendonly={appendonly or 'no'}, save="
                    f"{save or 'none'}). The counter tolerates a restart (the "
                    f"reconciler converges it) — but any other state on this Redis "
                    f"does not.",
                    remedy="enable appendonly yes for production"))
            else:
                findings.append(PostureFinding(
                    "P-PERSIST", "redis_persistence", GRADE_GOOD,
                    f"appendonly={appendonly}, save={save or 'none'}; "
                    f"persist facts: {p_detail}"))

        # ACL breadth — introspection may legitimately be denied: that is a
        # PERMISSION answer (often least-privilege working as designed), and
        # doctor must never report confidently over it.
        try:
            user = await client.acl_whoami()
            if isinstance(user, (bytes, bytearray)):
                user = user.decode("utf-8", "replace")
            acl = await client.acl_getuser(user)
            keys = acl.get("keys") if isinstance(acl, dict) else None
            cmds = acl.get("commands") if isinstance(acl, dict) else None
            keys_s = " ".join(keys) if isinstance(keys, (list, tuple)) else str(keys or "")
            cmds_s = str(cmds or "")
            if "~*" in keys_s or "allkeys" in keys_s or "+@all" in cmds_s:
                findings.append(PostureFinding(
                    "P-ACL", "redis_acl_breadth", GRADE_RISK,
                    f"connection user {user!r} is OVER-BROAD (keys={keys_s or '~*'}, "
                    f"commands include +@all) — the library needs only "
                    f"~quota:* +@read +@write +@scripting +ping",
                    remedy="create a scoped user: python -m ab0t_quota provision --emit acl"))
            elif "quota:*" in keys_s:
                findings.append(PostureFinding(
                    "P-ACL", "redis_acl_breadth", GRADE_GOOD,
                    f"connection user {user!r} scoped to {keys_s}"))
            else:
                findings.append(PostureFinding(
                    "P-ACL", "redis_acl_breadth", GRADE_ATTENTION,
                    f"connection user {user!r}: key patterns {keys_s or '(unreported)'} "
                    f"— not the documented ~quota:* scope; review against "
                    f"provision --emit acl"))
        except Exception as e:
            from .redis_preflight import classify_redis_error
            kind = classify_redis_error(e)
            if kind == "acl":
                findings.append(_nc(
                    "P-ACL", "redis_acl_breadth",
                    "ACL introspection DENIED (NOPERM) — a permission answer, not a "
                    "breadth verdict. Denying ACL reads to the runtime user is itself "
                    "consistent with least privilege; verify the ACL out-of-band "
                    "against provision --emit acl"))
            else:
                findings.append(_nc(
                    "P-ACL", "redis_acl_breadth",
                    f"ACL introspection unavailable on this server "
                    f"({type(e).__name__}) — breadth NOT verified; compare the "
                    f"server ACL out-of-band with provision --emit acl"))
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


async def _posture_ddb(findings: List[PostureFinding], report, config, plan,
                       *, timeout: float) -> None:
    """PITR / TTL / retention posture per table, via the SAME evaluator boot
    and preflight use (`verify_ddb_table`) — including the state table, which
    boot only existence-checks: doctor may grade MORE, never judge differently."""
    import asyncio as _aio
    import aioboto3
    from .ddb_preflight import pitr_confirmed_from, verify_ddb_table
    from .provision import TABLE_SPECS, resolve_table_names

    names = resolve_table_names(config)
    pitr_confirmed = pitr_confirmed_from(config)
    session = aioboto3.Session()
    try:
        async with session.client("dynamodb", **_ddb_client_kwargs(plan)) as client:
            for spec in TABLE_SPECS:
                table = names[spec.cap_key]
                fid = f"P-DDB-{spec.cap_key}"
                try:
                    value, fatal, warn = await _aio.wait_for(
                        verify_ddb_table(client, table,
                                         ttl_attribute=spec.ttl_attribute,
                                         pitr_confirmed=pitr_confirmed,
                                         required_gsis=spec.preflight_gsis),
                        timeout * 2)
                except Exception as e:
                    findings.append(_nc(fid, spec.cap_key,
                                        f"table {table}: probe failed "
                                        f"({type(e).__name__}) — posture unknown"))
                    continue
                value_s = str(value)
                if "permission denied" in value_s:
                    findings.append(_nc(
                        fid, spec.cap_key,
                        f"table {table}: DescribeTable DENIED — a permission answer, "
                        f"never a missing table (D-8). Grant "
                        f"dynamodb:DescribeTable/DescribeTimeToLive/"
                        f"DescribeContinuousBackups to let doctor see it."))
                    continue
                if "not found" in value_s:
                    grade = (GRADE_INFO if spec.cap_key == "ddb_handler_ledger"
                             else GRADE_ATTENTION)
                    findings.append(PostureFinding(
                        fid, spec.cap_key, grade,
                        f"table {table} not found"
                        + (" (opt-in feature — fine if the handler ledger is unused)"
                           if spec.cap_key == "ddb_handler_ledger" else
                           " — posture cannot be graded until it exists"),
                        remedy=None if spec.cap_key == "ddb_handler_ledger" else
                               "provision it: python -m ab0t_quota provision --emit terraform"))
                    continue
                if "pitr=asserted" in value_s:
                    findings.append(PostureFinding(
                        fid, spec.cap_key, GRADE_ATTENTION,
                        f"table {table}: PITR ASSERTED by operator — the control "
                        f"plane could not report it. An assertion is a promise, not "
                        f"a backup; on real AWS, verify PITR out-of-band.",
                        remedy="on AWS: aws dynamodb describe-continuous-backups "
                               f"--table-name {table}"))
                elif "WAIVED" in value_s:
                    findings.append(PostureFinding(
                        fid, spec.cap_key, GRADE_RISK,
                        f"table {table}: PITR observed DISABLED and waived by "
                        f"assertion — a money/ledger store with no point-in-time "
                        f"recovery",
                        remedy="enable PITR (provision --emit terraform sets it)"))
                elif fatal:
                    findings.append(PostureFinding(
                        fid, spec.cap_key, GRADE_RISK,
                        f"table {table}: {fatal}", remedy="see detail"))
                elif "ttl=DISABLED" in value_s:
                    findings.append(PostureFinding(
                        fid, spec.cap_key, GRADE_ATTENTION,
                        f"table {table}: TTL disabled — released/settled rows never "
                        f"reap: silent unbounded growth and cost (nothing is lost)",
                        remedy=f"enable TTL on attribute '{spec.ttl_attribute}'"))
                else:
                    findings.append(PostureFinding(
                        fid, spec.cap_key, GRADE_GOOD, f"table {table}: {value_s}"))
    except Exception as e:
        findings.append(_nc("P-DDB", "ddb_posture",
                            f"DynamoDB client unavailable ({type(e).__name__}) — "
                            f"table posture unknown"))


def _posture_static(findings: List[PostureFinding], report, plan) -> None:
    """Offline-derivable posture: encryption in transit, at-rest honesty,
    outbound-target inventory."""
    redis_url = (plan["redis_url"].value or "") if plan else ""
    if redis_url.startswith("rediss://"):
        findings.append(PostureFinding(
            "P-TLS", "encryption_in_transit", GRADE_GOOD,
            "Redis over TLS (rediss://)"))
    elif redis_url:
        host = redis_url.split("://", 1)[-1].split("/")[0].split("@")[-1].split(":")[0]
        local = host in ("localhost", "127.0.0.1", "redis", "quota-redis")
        findings.append(PostureFinding(
            "P-TLS", "encryption_in_transit", GRADE_ATTENTION,
            f"Redis over PLAINTEXT (redis://, host {host})"
            + (" — local/dev host" if local else
               ". Whether that is acceptable depends on the network boundary, "
               "which doctor cannot see; most compliance regimes require TLS "
               "in transit"),
            remedy="use rediss:// (the library supports it unchanged)"))
    endpoint = plan["dynamodb_endpoint"].value if plan else None
    if not endpoint:
        findings.append(PostureFinding(
            "P-TLS-DDB", "ddb_transport", GRADE_GOOD,
            "DynamoDB via the AWS SDK default endpoint (HTTPS)"))
    elif str(endpoint).startswith("https://"):
        findings.append(PostureFinding(
            "P-TLS-DDB", "ddb_transport", GRADE_GOOD, f"DynamoDB via {endpoint} (HTTPS)"))
    else:
        findings.append(PostureFinding(
            "P-TLS-DDB", "ddb_transport", GRADE_ATTENTION,
            f"DynamoDB via {endpoint} (plaintext — allowlisted dev endpoint)"))
    findings.append(_nc(
        "P-REST", "encryption_at_rest",
        "NOT verified: Redis at-rest encryption is not observable over the "
        "protocol; DynamoDB SSE was not probed in this version (AWS encrypts "
        "DynamoDB at rest by default, but this report did not confirm it — "
        "check SSEDescription via DescribeTable for the audit record)"))
    findings.append(_nc(
        "P-IAM", "iam_breadth",
        "NOT verified: IAM policy breadth requires iam:Get*/"
        "SimulatePrincipalPolicy, which the runtime credential need not (and "
        "should not) hold. Compare the attached policy out-of-band with "
        "provision --emit iam. Any AccessDenied above is reported as a "
        "permission answer, never as missing infrastructure (D-8)."))
    if report.outbound_contacts:
        findings.append(PostureFinding(
            "P-OUT", "outbound_targets", GRADE_INFO,
            "; ".join(f"{c['target']} ({c['purpose']})"
                      for c in report.outbound_contacts)))


async def run_doctor(
    config_path: Optional[str] = None,
    *,
    offline: bool = False,
    check_mesh: bool = False,
    strict: bool = False,
    timeout: float = 5.0,
    script_load: bool = True,
    fail_on_risk: bool = False,
    emit: Callable[[str], None] = print,
) -> DoctorReport:
    report = await run_preflight(
        config_path=config_path, offline=offline, check_mesh=check_mesh,
        strict=strict, timeout=timeout, script_load=script_load, emit=emit)
    doc = DoctorReport(preflight=report, exit_code=report.exit_code)
    doc.side_effects = [
        ("doctor performed the boot gates' reads plus ONE server-visible "
         "write: SCRIPT LOAD of the counter's Lua into Redis's script cache "
         "(the same load boot performs; --no-script-load skips it)")
        if script_load and not offline else
        "doctor read only; no SCRIPT LOAD was performed",
        "doctor never creates tables, never writes keys or data, and never "
        "contacts the mesh unless --check-mesh is set (D-6)",
    ]

    findings = doc.findings
    if report.exit_code in (EXIT_CONFIG,):
        findings.append(_nc("P-ALL", "posture",
                            "blocked by a config error — nothing was contacted, "
                            "no posture was observed"))
    elif report.engine_mode == "bridge":
        findings.append(PostureFinding(
            "P-BRIDGE", "bridge_mode", GRADE_INFO,
            "bridge mode: no local Redis/DDB to grade; enforcement posture is "
            "the billing service's (quota enforcement only today — FUTURE-1)"))
        if report.outbound_contacts:
            findings.append(PostureFinding(
                "P-OUT", "outbound_targets", GRADE_INFO,
                "; ".join(f"{c['target']} ({c['purpose']})"
                          for c in report.outbound_contacts)))
    else:
        # re-resolve with the SAME resolver (never a second resolution chain)
        plan = None
        try:
            from .config import load_config
            from .resolve import resolve_dependencies
            config = load_config(config_path)
            plan = resolve_dependencies(config, mode=report.engine_mode)
        except Exception:
            config = None
        if plan is None:
            findings.append(_nc("P-ALL", "posture",
                                "resolution failed after preflight — internal"))
        else:
            _posture_static(findings, report, plan)
            if offline:
                findings.append(_nc(
                    "P-OFFLINE", "live_posture",
                    "--offline: persistence, eviction facts, ACL, PITR and TTL "
                    "were NOT observed (nothing was contacted)"))
            else:
                reach_blocked = report.exit_code == EXIT_REACH
                if reach_blocked and any("Redis" in e or "redis" in e
                                         for e in report.reach_errors):
                    findings.append(_nc(
                        "P-PERSIST", "redis_persistence",
                        "blocked by reachability — not observed"))
                    findings.append(_nc(
                        "P-ACL", "redis_acl_breadth",
                        "blocked by reachability — not observed"))
                else:
                    try:
                        await _posture_redis(findings, report, config, plan,
                                             timeout=timeout)
                    except Exception as e:
                        findings.append(_nc(
                            "P-PERSIST", "redis_persistence",
                            f"posture probe failed ({type(e).__name__}: {e}) — "
                            f"not observed"))
                # D-80 facts, from the evaluator's published capability value
                ev = str(report.capabilities.get("counter_evictions_observed", ""))
                if ev.startswith("evictions_observed"):
                    findings.append(PostureFinding(
                        "P-EVICT", "eviction_facts", GRADE_RISK,
                        f"this Redis has ALREADY evicted keys — the counter may be "
                        f"WRONG RIGHT NOW (an evicted gauge reads low: phantom "
                        f"headroom, over-admission). {ev}",
                        remedy="reconcile the counter (Σ open activations) and set "
                               "maxmemory-policy noeviction"))
                elif ev == "unknown":
                    findings.append(_nc(
                        "P-EVICT", "eviction_facts",
                        "INFO stats unavailable — cannot tell whether this server "
                        "has already evicted (a visibility answer, not a verdict)"))
                elif ev:
                    findings.append(PostureFinding(
                        "P-EVICT", "eviction_facts", GRADE_GOOD,
                        f"no keys evicted on this server ({ev})"))
                try:
                    await _posture_ddb(findings, report, config, plan,
                                       timeout=timeout)
                except Exception as e:
                    findings.append(_nc("P-DDB", "ddb_posture",
                                        f"posture probe failed "
                                        f"({type(e).__name__}) — not observed"))

    risks = [f for f in findings if f.grade == GRADE_RISK]
    if fail_on_risk and doc.exit_code == EXIT_OK and risks:
        doc.exit_code = EXIT_GATE
    _emit_posture(doc, emit, fail_on_risk=fail_on_risk)
    return doc


def _emit_posture(doc: DoctorReport, emit, *, fail_on_risk: bool) -> None:
    emit("")
    emit("DOCTOR POSTURE — grades are remediation advice; the exit code is the "
         "boot verdict" + (" (+ --fail-on-risk)" if fail_on_risk else ""))
    order = {GRADE_RISK: 0, GRADE_ATTENTION: 1, GRADE_NOT_CHECKED: 2,
             GRADE_GOOD: 3, GRADE_INFO: 4}
    for f in sorted(doc.findings, key=lambda f: order.get(f.grade, 9)):
        tag = {GRADE_RISK: "RISK ", GRADE_ATTENTION: "ATTN ",
               GRADE_NOT_CHECKED: "NOT-CHECKED", GRADE_GOOD: "GOOD ",
               GRADE_INFO: "INFO "}[f.grade]
        emit(f"  {tag:<12} {f.name:<24} {f.detail}")
        if f.remedy:
            emit(f"               remedy: {f.remedy}")
    nc = [f for f in doc.findings if f.grade == GRADE_NOT_CHECKED]
    if nc:
        emit(f"  ({len(nc)} dimension(s) NOT checked — the reasons above are part "
             f"of the report: a doctor that reports confidently on what it could "
             f"not see is the failure this tool exists to prevent)")
    emit("SIDE EFFECTS, stated:")
    for s in doc.side_effects:
        emit(f"  * {s}")
    emit(f"\nDOCTOR VERDICT: exit {doc.exit_code}")
