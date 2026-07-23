"""T-12 (DOC-04, pack 20260721) — `python -m ab0t_quota preflight`.

Pins the coordinator's non-negotiables: D-4 (name), D-6 (--check-mesh OFF by
default), one evaluator set (verdict-equality with the boot gates), plan
printed before any socket, exit taxonomy separating gate-refusal (1) from
unreachable/credentials (3) from config (2), the SCRIPT LOAD disclosure +
--no-script-load escape, D-10 deprecation call-outs and the D-73 hatch verdict
surfaced.
"""
from __future__ import annotations

import json

import pytest
from unittest.mock import patch

from tests.dnd_harness_20260721 import (
    ContactAttempted, DECOYS, install_no_contact, install_seam_recorders,
)

MINIMAL_CONFIG = {
    "service_name": "test-svc",
    "storage": {"redis_url": "redis://test/0", "persistence_enabled": False},
    "tier_provider": {"type": "static", "default_tier": "starter"},
    "alerts": {"enabled": False},
    "enforcement": {"enabled": True},
    "resources": [{"service": "t", "resource_key": "thing.concurrent",
                   "display_name": "T", "counter_type": "gauge", "unit": "t"}],
    "tiers": [{"tier_id": "starter", "display_name": "S", "sort_order": 1,
               "limits": {"thing.concurrent": 5}, "features": []}],
}


def _write_config(tmp_path, monkeypatch, cfg: dict) -> str:
    p = tmp_path / "quota-config.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.setenv("QUOTA_CONFIG_PATH", str(p))
    return str(p)


class GateRedis:
    """A Redis capability profile the gate evaluators can interrogate."""

    def __init__(self, *, policy="noeviction", version="7.2.0",
                 script_error=None):
        self._policy = policy
        self._version = version
        self._script_error = script_error
        self.script_loads = 0

    async def ping(self):
        return True

    async def config_get(self, key):
        return {"maxmemory-policy": {"maxmemory-policy": self._policy},
                "appendonly": {"appendonly": "yes"},
                "save": {"save": "3600 1"}}[key]

    async def info(self, section=None):
        return {
            "cluster": {"cluster_enabled": "0"},
            "server": {"redis_version": self._version},
            "memory": {"maxmemory": 0, "used_memory": 10_000},
            "stats": {"evicted_keys": 0},
            "persistence": {"aof_enabled": "1", "aof_last_write_status": "ok",
                            "rdb_last_bgsave_status": "ok",
                            "aof_last_bgrewrite_status": "ok"},
        }.get(section, {})

    async def script_load(self, src):
        self.script_loads += 1
        if self._script_error:
            raise Exception(self._script_error)
        return "a" * 40


class UnreachableRedis:
    async def ping(self):
        raise ConnectionError("connection refused")


def _patch_redis(fake):
    return patch("redis.asyncio.Redis.from_url",
                 side_effect=lambda *a, **kw: fake)


async def _run(tmp_path, monkeypatch, *, cfg=None, fake=None, ddb=None, **kw):
    import aioboto3
    from ab0t_quota.preflight import run_preflight
    from tests.test_t6_tables_20260721 import FakeDDB, FakeSession
    _write_config(tmp_path, monkeypatch, cfg or MINIMAL_CONFIG)
    fake_ddb = ddb or FakeDDB()
    monkeypatch.setattr(aioboto3, "Session", lambda: FakeSession(fake_ddb))
    out: list[str] = []
    with _patch_redis(fake or GateRedis()):
        report = await run_preflight(emit=out.append, **kw)
    return report, "\n".join(out)


# --- the command exists under the decided name (D-4) -----------------------

def test_preflight_command_is_wired(tmp_path, monkeypatch, capsys):
    from ab0t_quota.__main__ import main
    _write_config(tmp_path, monkeypatch, MINIMAL_CONFIG)
    try:
        rc = main(["preflight", "--offline"])
    except SystemExit as e:  # argparse rejects an unknown command with exit 2
        pytest.fail(f"`preflight` is not a CLI command (D-4/T-12): {e}")
    assert rc == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "RESOLVED" in out and "OUTBOUND" in out


def test_check_alias_accepted(tmp_path, monkeypatch):
    from ab0t_quota.__main__ import main
    _write_config(tmp_path, monkeypatch, MINIMAL_CONFIG)
    assert main(["check", "--offline"]) == 0


# --- offline: plan + provenance, contacts NOTHING --------------------------

@pytest.mark.asyncio
async def test_offline_contacts_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("REDIS_URL", DECOYS["REDIS_URL"])  # decoy must be inert
    install_no_contact(monkeypatch)
    report, out = await _run(tmp_path, monkeypatch, offline=True)
    assert report.exit_code == 0
    assert "redis_url" in out and "source=" in out, \
        "the resolved plan with provenance must be printed"
    assert DECOYS["REDIS_URL"] not in out, "decoy leaked into the plan"


# --- exit 2: config error, no network --------------------------------------

@pytest.mark.asyncio
async def test_null_redis_url_exits_2_without_contact(tmp_path, monkeypatch):
    monkeypatch.setenv("REDIS_URL", DECOYS["REDIS_URL"])
    monkeypatch.delenv("QUOTA_REDIS_URL", raising=False)
    install_no_contact(monkeypatch)
    cfg = dict(MINIMAL_CONFIG)
    cfg["storage"] = {"redis_url": None}
    report, out = await _run(tmp_path, monkeypatch, cfg=cfg)
    assert report.exit_code == 2
    assert "storage.redis_url" in "".join(report.config_errors)
    assert "no network was contacted" in out.lower()
    # D-10 call-outs surface in the report (generic set, namespaced unset)
    assert any("REDIS_URL" in d for d in report.deprecations), \
        "the D-10 deprecation call-out must surface in the preflight report"


# --- exit taxonomy: 1 (gate) vs 3 (unreachable) — GATE-01's lesson ---------

@pytest.mark.asyncio
async def test_unreachable_is_exit_3_and_never_a_gate_verdict(tmp_path, monkeypatch):
    report, out = await _run(tmp_path, monkeypatch, fake=UnreachableRedis())
    assert report.exit_code == 3
    assert report.reach_errors and "unreachable" in report.reach_errors[0]
    failed = [g for g in report.gates if g.status == "fail"]
    assert failed == [], \
        f"unreachable must SKIP the gates, never fail them: {failed}"
    assert any(g.status == "skip" and g.id == "D-72" for g in report.gates)


@pytest.mark.asyncio
async def test_evicting_policy_is_exit_1_gate_refusal(tmp_path, monkeypatch):
    report, _ = await _run(tmp_path, monkeypatch,
                           fake=GateRedis(policy="allkeys-lru"))
    assert report.exit_code == 1
    d72 = next(g for g in report.gates if g.id == "D-72")
    assert d72.status == "fail"


# --- verdict equality: preflight's judgement IS the boot gate's ------------

@pytest.mark.asyncio
@pytest.mark.parametrize("policy,should_refuse", [
    ("allkeys-lru", True), ("noeviction", False)])
async def test_preflight_verdict_equals_boot_verdict(tmp_path, monkeypatch,
                                                     policy, should_refuse):
    """One evaluator set (§2.5): for the same Redis, boot's gate raise and
    preflight's exit code must correspond 1:1."""
    from fastapi import FastAPI
    from ab0t_quota.setup import _gate_redis_counter_store

    fake = GateRedis(policy=policy)
    cfg = dict(MINIMAL_CONFIG)
    _write_config(tmp_path, monkeypatch, cfg)

    boot_raised = None
    try:
        await _gate_redis_counter_store(FastAPI(), fake, cfg)
    except Exception as e:
        boot_raised = e
    report, _ = await _run(tmp_path, monkeypatch, fake=GateRedis(policy=policy))
    if should_refuse:
        assert boot_raised is not None, "boot must refuse this Redis"
        assert report.exit_code == 1, "preflight must refuse what boot refuses"
    else:
        assert boot_raised is None, f"boot unexpectedly refused: {boot_raised}"
        assert report.exit_code == 0, \
            f"preflight must pass what boot passes: {[vars(g) for g in report.gates if g.status=='fail']}"


@pytest.mark.asyncio
async def test_memory_url_config_error_matches_boot(tmp_path, monkeypatch):
    cfg = dict(MINIMAL_CONFIG)
    cfg["storage"] = {"redis_url": "memory://"}
    report, _ = await _run(tmp_path, monkeypatch, cfg=cfg)
    assert report.exit_code == 2
    assert "QUOTA-CFG-005" in "".join(report.config_errors)


# --- D-6: --check-mesh strictly opt-in -------------------------------------

@pytest.mark.asyncio
async def test_mesh_not_contacted_by_default(tmp_path, monkeypatch):
    rec = install_seam_recorders(monkeypatch)  # httpx/boto3/aioboto3 recorders
    fake = GateRedis()
    report, _ = await _run(tmp_path, monkeypatch, fake=fake)
    httpx_calls = rec.calls_for("httpx")
    assert httpx_calls == [], \
        f"preflight contacted the mesh WITHOUT --check-mesh (D-6): {httpx_calls}"
    assert report.exit_code == 0


@pytest.mark.asyncio
async def test_check_mesh_opt_in_probes(tmp_path, monkeypatch):
    rec = install_seam_recorders(monkeypatch)
    with pytest.raises(ContactAttempted):
        await _run(tmp_path, monkeypatch, fake=GateRedis(), check_mesh=True)
    assert rec.calls_for("httpx"), "--check-mesh must actually probe"


# --- SCRIPT LOAD: disclosed write + escape flag ----------------------------

@pytest.mark.asyncio
async def test_no_script_load_skips_and_discloses(tmp_path, monkeypatch):
    fake = GateRedis()
    report, out = await _run(tmp_path, monkeypatch, fake=fake, script_load=False)
    assert fake.script_loads == 0, "--no-script-load must not touch the script cache"
    d73 = next(g for g in report.gates if g.id == "D-73")
    assert d73.status == "skip" and "boot WILL perform" in d73.detail
    assert report.exit_code == 0


@pytest.mark.asyncio
async def test_script_load_disclosure_in_default_output(tmp_path, monkeypatch):
    fake = GateRedis()
    report, out = await _run(tmp_path, monkeypatch, fake=fake)
    assert fake.script_loads == 1
    assert "script cache" in out, \
        "the one server-visible write must be disclosed in the output"


# --- D-73 hatch verdict surfaces -------------------------------------------

@pytest.mark.asyncio
async def test_d73_hatch_verdict_surfaces(tmp_path, monkeypatch):
    monkeypatch.delenv("AB0T_QUOTA_REDIS_SCRIPTING_CONFIRMED", raising=False)
    cfg = json.loads(json.dumps(MINIMAL_CONFIG))
    cfg["storage"]["redis_scripting_confirmed"] = True
    fake = GateRedis(script_error="ERR unknown command 'script'")
    report, _ = await _run(tmp_path, monkeypatch, cfg=cfg, fake=fake)
    d73 = next(g for g in report.gates if g.id == "D-73")
    assert d73.status == "pass" and "redis_scripting_confirmed" in d73.detail
    assert report.exit_code == 0


# --- bridge mode ------------------------------------------------------------

@pytest.mark.asyncio
async def test_bridge_identity_valid_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("AB0T_MESH_API_KEY", "test-key")
    monkeypatch.setenv("AB0T_SERVICE_NAME", "svc-1")
    install_no_contact(monkeypatch)
    report, out = await _run(
        tmp_path, monkeypatch,
        cfg={"service_name": "svc-1", "engine_mode": "bridge"}, offline=True)
    assert report.exit_code == 0
    assert report.engine_mode == "bridge"
    assert any(g.name == "redis_topology" and "n/a" in g.detail for g in report.gates)


@pytest.mark.asyncio
async def test_bridge_without_key_exits_2(tmp_path, monkeypatch):
    monkeypatch.delenv("AB0T_MESH_API_KEY", raising=False)
    monkeypatch.setenv("AB0T_SERVICE_NAME", "svc-1")
    install_no_contact(monkeypatch)
    report, _ = await _run(
        tmp_path, monkeypatch,
        cfg={"service_name": "svc-1", "engine_mode": "bridge"})
    assert report.exit_code == 2
    assert "AB0T_MESH_API_KEY" in "".join(report.config_errors)


# --- JSON report ------------------------------------------------------------

def test_json_report_schema_and_secret_redaction(tmp_path, monkeypatch, capsys):
    from ab0t_quota.__main__ import main
    cfg = json.loads(json.dumps(MINIMAL_CONFIG))
    monkeypatch.setenv("QUOTA_REDIS_PASSWORD", "SUPER_SECRET_pw_1")
    _write_config(tmp_path, monkeypatch, cfg)
    rc = main(["preflight", "--offline", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["schema"] == "ab0t-quota/preflight-report/v1"
    assert "SUPER_SECRET_pw_1" not in out, "secret leaked into the JSON report"
    row = next(r for r in data["resolved_plan"] if "password" in r["name"])
    assert row["secret"] is True and row["value"] == "(secret: set)"


# --- preflight NEVER creates (§9.2) ----------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("auto_create", [False, True])
async def test_missing_tables_reported_never_created(tmp_path, monkeypatch,
                                                     auto_create):
    """Missing tables are reported per storage.auto_create_tables — WILL
    CREATE (boot) vs pre-create remedy — and preflight itself creates
    NOTHING either way (auto_create_tables legalises BOOT creation only)."""
    from tests.test_t6_tables_20260721 import FakeDDB
    cfg = json.loads(json.dumps(MINIMAL_CONFIG))
    cfg["storage"] = {"redis_url": "redis://test/0",
                      "auto_create_tables": auto_create}  # persistence default ON
    fake_ddb = FakeDDB()
    report, _ = await _run(tmp_path, monkeypatch, cfg=cfg, ddb=fake_ddb)
    assert fake_ddb.create_calls == [], \
        f"preflight CREATED a table (must never): {fake_ddb.create_calls}"
    t6 = [g for g in report.gates if g.id == "T-6"]
    assert t6, "missing tables must be reported"
    if auto_create:
        assert any("WILL CREATE" in g.detail for g in t6)
    else:
        assert any("auto_create_tables" in (g.remedy or "") for g in t6)
    assert report.exit_code == 0, \
        "boot degrades on missing tables (non-paid path) — preflight must not refuse what boot passes"


# --- internal errors are exit 4, blamed on us -------------------------------

def test_internal_error_is_exit_4(tmp_path, monkeypatch, capsys):
    from ab0t_quota.__main__ import main
    _write_config(tmp_path, monkeypatch, MINIMAL_CONFIG)
    with patch("ab0t_quota.preflight.run_preflight",
               side_effect=RuntimeError("boom")):
        rc = main(["preflight"])
    assert rc == 4
    assert "bug in ab0t-quota" in capsys.readouterr().err
