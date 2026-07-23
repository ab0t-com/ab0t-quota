"""T-1 (program board, tooling lane; ticket 20260721_setup_and_doctor_verbs)
— `python -m ab0t_quota doctor` + ST-CLI-1 (Python binding half).

Pins:
  * the verb exists under the DECIDED name (`doctor`, ticket §6.1);
  * ONE evaluator set — doctor's boot verdict IS run_preflight's (verdict
    equality with boot, the never-drift rule), posture never changes the
    exit code unless --fail-on-risk;
  * `--json` EXTENDS preflight-report/v1 (superset; posture section carries
    ab0t-quota/doctor-posture/v1);
  * posture catches what the gates deliberately wave through (D-14 planted
    offenders): persistence-off behind a durability assertion (the outbox
    lost on restart), PITR behind an assertion flag, already-evicted keys;
  * HONESTY: a dimension doctor could not observe is `not_checked` WITH the
    reason — introspection denied is a permission answer, never a verdict
    (D-8) — proven in BOTH directions (planted over-broad ACL goes RED;
    denied ACL introspection does NOT).
"""
from __future__ import annotations

import json

import pytest
from unittest.mock import patch

from tests.dnd_harness_20260721 import DECOYS, install_no_contact
from tests.test_t12_preflight_20260721 import (
    GateRedis, MINIMAL_CONFIG, UnreachableRedis, _patch_redis, _write_config,
)
from tests.test_t6_tables_20260721 import FakeDDB, FakeSession


def _seed_tables(fake_ddb: FakeDDB, names=None) -> None:
    """Seed the four registry tables into the fake, GSIs from the ONE registry."""
    from ab0t_quota.provision import TABLE_SPECS, resolve_table_names
    names = names or resolve_table_names(None)
    for spec in TABLE_SPECS:
        fake_ddb.tables[names[spec.cap_key]] = {
            "TableName": names[spec.cap_key],
            "GlobalSecondaryIndexes": [{"IndexName": g.name} for g in spec.gsis],
        }


class PersistOffRedis(GateRedis):
    """Boots green (policy noeviction) but persistence is OFF — the exact
    posture the ticket names: 'boots fine and loses the outbox on restart'."""

    async def config_get(self, key):
        return {"maxmemory-policy": {"maxmemory-policy": self._policy},
                "appendonly": {"appendonly": "no"},
                "save": {"save": ""}}[key]

    async def info(self, section=None):
        base = await super().info(section)
        if section == "persistence":
            return {"aof_enabled": "0", "aof_last_write_status": "ok",
                    "rdb_last_bgsave_status": "ok",
                    "aof_last_bgrewrite_status": "ok"}
        return base


class EvictedKeysRedis(GateRedis):
    async def info(self, section=None):
        base = await super().info(section)
        if section == "stats":
            return {"evicted_keys": 120}
        return base


class BroadAclRedis(GateRedis):
    async def acl_whoami(self):
        return "default"

    async def acl_getuser(self, user):
        return {"keys": ["~*"], "commands": "+@all"}


class ScopedAclRedis(GateRedis):
    async def acl_whoami(self):
        return "ab0t-quota"

    async def acl_getuser(self, user):
        return {"keys": ["~quota:*"], "commands": "-@all +@read +@write +@scripting +ping"}


class NopermAclRedis(GateRedis):
    async def acl_whoami(self):
        import redis.exceptions as rex
        raise rex.NoPermissionError("NOPERM this user has no permissions to run the 'acl' command")


async def _run_doctor(tmp_path, monkeypatch, *, cfg=None, fake=None, ddb=None,
                      seed=True, **kw):
    import aioboto3
    from ab0t_quota.preflight import run_doctor
    _write_config(tmp_path, monkeypatch, cfg or MINIMAL_CONFIG)
    fake_ddb = ddb or FakeDDB()
    if seed and ddb is None:
        _seed_tables(fake_ddb)
    monkeypatch.setattr(aioboto3, "Session", lambda: FakeSession(fake_ddb))
    out: list[str] = []
    with _patch_redis(fake or GateRedis()):
        doc = await run_doctor(emit=out.append, **kw)
    return doc, fake_ddb, "\n".join(out)


def _finding(doc, name):
    return next((f for f in doc.findings if f.name == name), None)


# --- the verb exists under the decided name (ticket §6.1) --------------------

def test_doctor_command_is_wired(tmp_path, monkeypatch, capsys):
    from ab0t_quota.__main__ import main
    _write_config(tmp_path, monkeypatch, MINIMAL_CONFIG)
    try:
        rc = main(["doctor", "--offline"])
    except SystemExit as e:
        pytest.fail(f"`doctor` is not a CLI command (ticket §6.1): {e}")
    assert rc == 0, capsys.readouterr().out
    assert "DOCTOR POSTURE" in capsys.readouterr().out


# --- ONE evaluator set: doctor's exit IS the boot/preflight verdict ---------

@pytest.mark.asyncio
@pytest.mark.parametrize("policy,expected_exit", [
    ("allkeys-lru", 1), ("noeviction", 0)])
async def test_doctor_verdict_equals_preflight_and_boot(tmp_path, monkeypatch,
                                                        policy, expected_exit):
    from fastapi import FastAPI
    from ab0t_quota.setup import _gate_redis_counter_store

    boot_raised = None
    try:
        await _gate_redis_counter_store(FastAPI(), GateRedis(policy=policy),
                                        dict(MINIMAL_CONFIG))
    except Exception as e:
        boot_raised = e
    doc, _, _ = await _run_doctor(tmp_path, monkeypatch,
                                  fake=GateRedis(policy=policy))
    assert doc.exit_code == expected_exit, \
        "doctor must exit with the boot verdict (one evaluator set)"
    assert (boot_raised is not None) == (expected_exit == 1), \
        "the parametrized expectation drifted from boot's actual gate"
    assert doc.preflight.exit_code == expected_exit


@pytest.mark.asyncio
async def test_doctor_unreachable_is_exit_3_never_a_posture_verdict(tmp_path, monkeypatch):
    doc, _, _ = await _run_doctor(tmp_path, monkeypatch, fake=UnreachableRedis())
    assert doc.exit_code == 3
    persist = _finding(doc, "redis_persistence")
    assert persist is not None and persist.grade == "not_checked", \
        "an unreachable Redis must yield NOT-CHECKED posture, never a grade"
    assert "reachability" in persist.detail


# --- planted offenders: green gates, red posture (D-14) ---------------------

@pytest.mark.asyncio
async def test_persistence_off_outbox_on_redis_boots_green_doctor_risk(tmp_path, monkeypatch):
    """The ticket's headline case: durability ASSERTED, persistence observed
    OFF, outbox on this Redis — preflight exit 0, doctor RISK."""
    cfg = json.loads(json.dumps(MINIMAL_CONFIG))
    cfg["storage"]["persistence_enabled"] = False
    cfg["outbox"] = {"store": "redis", "redis_durability_confirmed": True}
    cfg["activations"] = {"store": "redis"}
    doc, _, _ = await _run_doctor(tmp_path, monkeypatch, cfg=cfg,
                                  fake=PersistOffRedis())
    assert doc.exit_code == 0, \
        f"control: boot passes on the assertion ({[g for g in doc.preflight.gates if g.status=='fail']})"
    persist = _finding(doc, "redis_persistence")
    assert persist is not None and persist.grade == "risk", \
        "doctor must grade RISK: no persistence + outbox on this Redis"
    assert "loses the money outbox on restart" in persist.detail


@pytest.mark.asyncio
async def test_persistence_off_outbox_elsewhere_is_attention_not_risk(tmp_path, monkeypatch):
    """Negative control for the instrument: same persistence-off Redis with
    the outbox on DDB must NOT read as a money risk (the counter heals)."""
    doc, _, _ = await _run_doctor(tmp_path, monkeypatch, fake=PersistOffRedis())
    persist = _finding(doc, "redis_persistence")
    assert persist is not None and persist.grade == "attention", persist
    assert doc.exit_code == 0


@pytest.mark.asyncio
async def test_pitr_asserted_passes_gates_but_doctor_flags_the_promise(tmp_path, monkeypatch):
    class NoPitrDDB(FakeDDB):
        async def describe_continuous_backups(self, TableName):
            raise Exception("UnknownOperationException")
    cfg = json.loads(json.dumps(MINIMAL_CONFIG))
    cfg["storage"] = {"redis_url": "redis://test/0", "ddb_pitr_confirmed": True}
    fake_ddb = NoPitrDDB()
    _seed_tables(fake_ddb)
    doc, _, _ = await _run_doctor(tmp_path, monkeypatch, cfg=cfg, ddb=fake_ddb)
    assert doc.exit_code == 0, "control: the gate passes on the assertion"
    flagged = [f for f in doc.findings
               if f.grade == "attention" and "promise, not" in f.detail]
    assert flagged, \
        "doctor must flag PITR-by-assertion: an assertion is a promise, not a backup"


@pytest.mark.asyncio
async def test_already_evicted_keys_is_a_risk_counter_wrong_now(tmp_path, monkeypatch):
    doc, _, _ = await _run_doctor(tmp_path, monkeypatch, fake=EvictedKeysRedis())
    assert doc.exit_code == 0, "control: D-80 degrades, boot still starts"
    ev = _finding(doc, "eviction_facts")
    assert ev is not None and ev.grade == "risk"
    assert "WRONG RIGHT NOW" in ev.detail
    assert ev.remedy and "reconcile" in ev.remedy.lower()


# --- ACL breadth: honest in BOTH directions ---------------------------------

@pytest.mark.asyncio
async def test_overbroad_acl_goes_red(tmp_path, monkeypatch):
    doc, _, _ = await _run_doctor(tmp_path, monkeypatch, fake=BroadAclRedis())
    acl = _finding(doc, "redis_acl_breadth")
    assert acl is not None and acl.grade == "risk", acl
    assert "OVER-BROAD" in acl.detail


@pytest.mark.asyncio
async def test_scoped_acl_is_good(tmp_path, monkeypatch):
    doc, _, _ = await _run_doctor(tmp_path, monkeypatch, fake=ScopedAclRedis())
    acl = _finding(doc, "redis_acl_breadth")
    assert acl is not None and acl.grade == "good", acl


@pytest.mark.asyncio
async def test_denied_acl_introspection_is_a_permission_answer_never_a_verdict(tmp_path, monkeypatch):
    """D-8: AccessDenied/NOPERM is a permission answer. A doctor that grades
    (either way) what it could not see is the failure this programme exists
    to eliminate."""
    doc, _, _ = await _run_doctor(tmp_path, monkeypatch, fake=NopermAclRedis())
    acl = _finding(doc, "redis_acl_breadth")
    assert acl is not None and acl.grade == "not_checked", acl
    assert "permission answer" in acl.detail


@pytest.mark.asyncio
async def test_no_acl_support_is_not_checked_not_good(tmp_path, monkeypatch):
    doc, _, _ = await _run_doctor(tmp_path, monkeypatch, fake=GateRedis())
    acl = _finding(doc, "redis_acl_breadth")
    assert acl is not None and acl.grade == "not_checked", acl


@pytest.mark.asyncio
async def test_ddb_describe_denied_is_permission_answer_never_missing_table(tmp_path, monkeypatch):
    class DeniedDDB(FakeDDB):
        async def describe_table(self, TableName):
            raise Exception("AccessDeniedException: not authorized")
    doc, _, _ = await _run_doctor(tmp_path, monkeypatch, ddb=DeniedDDB())
    denied = [f for f in doc.findings if "permission answer" in f.detail
              and f.grade == "not_checked" and f.name.startswith("ddb")]
    assert denied, "DDB AccessDenied must be a not_checked permission answer (D-8)"
    assert not any("not found" in f.detail for f in denied)


# --- --json extends preflight-report/v1 -------------------------------------

@pytest.mark.asyncio
async def test_json_is_a_superset_of_preflight_report_v1(tmp_path, monkeypatch):
    from ab0t_quota.preflight import run_preflight
    import aioboto3
    doc, _, _ = await _run_doctor(tmp_path, monkeypatch, fake=GateRedis())
    d = doc.to_json()
    assert d["schema"] == "ab0t-quota/preflight-report/v1", \
        "doctor --json must EXTEND v1, not replace it (CI consumers pin it)"
    for key in ("library", "config", "resolved_plan", "outbound_contacts",
                "gates", "capabilities", "verdict"):
        assert key in d, f"v1 key {key} missing — extension broke the base schema"
    posture = d["posture"]
    assert posture["schema"] == "ab0t-quota/doctor-posture/v1"
    assert posture["findings"], "an auditor gets graded findings"
    assert posture["side_effects"], "doctor must state its own side effects"
    assert isinstance(posture["not_checked"], list)
    assert d["verdict"]["exit_code"] == doc.exit_code


def test_json_cli_output_parses(tmp_path, monkeypatch, capsys):
    from ab0t_quota.__main__ import main
    _write_config(tmp_path, monkeypatch, MINIMAL_CONFIG)
    rc = main(["doctor", "--offline", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0 and data["posture"]["schema"] == "ab0t-quota/doctor-posture/v1"


# --- --fail-on-risk ----------------------------------------------------------

@pytest.mark.asyncio
async def test_fail_on_risk_promotes_risk_to_exit_1(tmp_path, monkeypatch):
    doc, _, _ = await _run_doctor(tmp_path, monkeypatch, fake=EvictedKeysRedis(),
                                  fail_on_risk=True)
    assert doc.exit_code == 1
    doc2, _, _ = await _run_doctor(tmp_path, monkeypatch, fake=GateRedis(),
                                   fail_on_risk=True)
    assert doc2.exit_code == 0, "no risk => fail-on-risk must not fire"


# --- doctor mutates nothing; offline contacts nothing -----------------------

@pytest.mark.asyncio
async def test_doctor_never_creates_tables(tmp_path, monkeypatch):
    fake_ddb = FakeDDB()  # all four tables MISSING
    doc, _, _ = await _run_doctor(tmp_path, monkeypatch, ddb=fake_ddb)
    assert fake_ddb.create_calls == [], \
        f"doctor CREATED a table: {fake_ddb.create_calls}"


@pytest.mark.asyncio
async def test_offline_doctor_contacts_nothing_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("REDIS_URL", DECOYS["REDIS_URL"])
    install_no_contact(monkeypatch)
    from ab0t_quota.preflight import run_doctor
    _write_config(tmp_path, monkeypatch, MINIMAL_CONFIG)
    out: list[str] = []
    doc = await run_doctor(offline=True, emit=out.append)
    assert doc.exit_code == 0
    live = _finding(doc, "live_posture")
    assert live is not None and live.grade == "not_checked"
    assert "NOT observed" in live.detail


# --- the not-checked ledger is part of the human report ---------------------

@pytest.mark.asyncio
async def test_human_output_carries_the_not_checked_ledger(tmp_path, monkeypatch):
    doc, _, out = await _run_doctor(tmp_path, monkeypatch, fake=GateRedis())
    assert "NOT checked" in out
    assert "SIDE EFFECTS, stated:" in out


# --- ST-CLI-1 (Python binding half): verb names, exit taxonomy, schema ------

def test_st_cli_1_python_binding(tmp_path, monkeypatch, capsys):
    """ST-CLI-1: pins the decided verb triad + exit taxonomy + report schema
    against conformance/scenarios.json (the Go half:
    ab0t-quota-go/cmd/quotactl/st_cli_1_binding_test.go)."""
    import subprocess  # noqa: F401  (documentation only)
    from pathlib import Path
    doc = json.loads((Path(__file__).resolve().parents[1] /
                      "conformance" / "scenarios.json").read_text())
    item = {i["id"]: i for i in doc["structural_conformance"]}.get("ST-CLI-1")
    assert item is not None, "ST-CLI-1 must be declared in scenarios.json"
    # verbs pinned by the scenario exist in this runtime's CLI
    from ab0t_quota.__main__ import main
    _write_config(tmp_path, monkeypatch, MINIMAL_CONFIG)
    for verb in item["python_verbs"]:
        argv = {"preflight": [verb, "--offline"],
                "doctor": [verb, "--offline"],
                "provision": [verb, "--emit", "acl"]}[verb]
        try:
            rc = main(argv)
        except SystemExit as e:
            pytest.fail(f"pinned verb {verb!r} missing from the Python CLI: {e}")
        assert rc == 0, f"pinned verb {verb!r} failed: rc={rc}"
        capsys.readouterr()
    # exit taxonomy pinned
    from ab0t_quota import preflight as pf
    taxonomy = {int(k): v for k, v in item["exit_taxonomy"].items()}
    assert taxonomy[pf.EXIT_OK].startswith("ok")
    assert taxonomy[pf.EXIT_GATE].startswith("gate")
    assert taxonomy[pf.EXIT_CONFIG].startswith("config")
    assert taxonomy[pf.EXIT_REACH].startswith("unreachable")
    assert taxonomy[pf.EXIT_INTERNAL].startswith("internal")
    # report schemas pinned
    assert item["report_schema"] == pf.REPORT_SCHEMA
    assert item["posture_schema"] == pf.POSTURE_SCHEMA
    # the honest asymmetry is DECLARED, not papered over (D-8)
    assert "side_effects" in item and "go" in item["side_effects"] \
        and "python" in item["side_effects"]
    assert "Setup" in item["side_effects"]["go"], \
        "the scenario must state Go's doctor runs full Setup (cannot claim read-only)"
