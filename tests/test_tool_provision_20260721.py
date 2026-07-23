"""T-2/T-3 (program board, tooling lane; ticket 20260721_setup_and_doctor_verbs)
— `python -m ab0t_quota provision`.

Pins:
  * artifacts are generated FROM the enforcing registry (drift binding: the
    registry is EXECUTED against the four ensure_table sites and compared);
  * preflight's DDB phase reads the SAME registry (planted-offender proof);
  * the conformance verifier can go RED (D-14: one planted offender per
    artifact kind);
  * `--local` composes its docker command from the registry, verifies with
    `verify_redis_invariants` (the boot evaluator), and never runs cloud;
  * IAM create-path actions are OPT-IN (D-3's rule in artifact form).
"""
from __future__ import annotations

import json

import pytest

import ab0t_quota.provision as prov
from tests.test_t6_tables_20260721 import FakeDDB, FakeSession, _write_config
from tests.test_t12_preflight_20260721 import MINIMAL_CONFIG


# --- registry sanity + planted offenders on the registry itself -------------

def test_registry_self_check_passes_as_shipped():
    prov._self_check()


def test_self_check_goes_red_on_planted_evicting_policy(monkeypatch):
    """D-14: the instrument must be seen to fail. Plant an offender in the
    registry and prove emission refuses."""
    monkeypatch.setattr(prov, "REQUIRED_MAXMEMORY_POLICY", "allkeys-lru")
    with pytest.raises(AssertionError, match="REFUSES"):
        prov.emit("compose")


def test_self_check_goes_red_on_below_floor_image(monkeypatch):
    monkeypatch.setattr(prov, "REDIS_IMAGE", "redis:5-alpine")
    with pytest.raises(AssertionError, match="floor"):
        prov.emit("compose")


# --- every emitted artifact conforms to the enforcing constants -------------

@pytest.mark.parametrize("kind", prov.EMIT_KINDS)
def test_emitted_artifact_conforms(kind):
    text = prov.emit(kind)
    assert prov.check_artifact_conformance(kind, text) == []
    assert "GENERATED" in text and "enforcing" in text, \
        "the artifact must name its provenance (generated from the registry)"


def test_compose_pins_the_gate_contract():
    text = prov.emit("compose")
    assert prov.REQUIRED_MAXMEMORY_POLICY in text
    assert prov.REQUIRED_MAXMEMORY_POLICY not in prov.EVICTING_POLICIES, \
        "registry emits a policy the D-72 gate refuses"
    assert "--appendonly" in text and '"yes"' in text
    assert "QUOTA_REDIS_URL=" in text, "the declaration line is the product"


def test_terraform_renders_every_registry_table_gsi_ttl_pitr():
    text = prov.emit("terraform")
    for spec in prov.TABLE_SPECS:
        assert spec.default_name in text
        for g in spec.gsis:
            assert g.name in text and g.hash_key in text
        assert f'attribute_name = "{spec.ttl_attribute}"' in text
    assert "point_in_time_recovery" in text
    assert "CREATES NOTHING" in text, "emit-and-let-them-apply must be stated"


def test_acl_is_least_privilege():
    text = prov.emit("acl")
    assert f"~{prov.KEYSPACE_PATTERN}" in text
    for rule in prov.ACL_REQUIRED_RULES:
        assert rule in text
    assert "+@all" not in text and "~* " not in text


def test_iam_create_actions_are_opt_in():
    base = prov.emit("iam")
    assert "dynamodb:CreateTable" not in base, \
        "create-path IAM must be opt-in (D-3 in artifact form)"
    for a in prov.IAM_RUNTIME_ACTIONS:
        assert a in base
    with_create = prov.emit("iam", include_create=True)
    for a in prov.IAM_CREATE_ACTIONS:
        assert a in with_create
    # the policy body parses as JSON
    body = base[base.index("{"):]
    doc = json.loads(body)
    assert doc["Statement"][0]["Sid"] == "Ab0tQuotaRuntime"


def test_declared_table_names_override_defaults():
    cfg = {"storage": {"dynamodb_table": "custom_state"},
           "outbox": {"ddb_table": "custom_outbox"}}
    text = prov.emit("iam", cfg)
    assert "custom_state" in text and "custom_outbox" in text
    assert "table/ab0t_quota_state" not in text
    assert "table/ab0t_quota_outbox" not in text


# --- the conformance verifier can go RED (D-14, per artifact kind) ----------

@pytest.mark.parametrize("kind,plant", [
    ("compose", lambda t: t.replace(prov.REQUIRED_MAXMEMORY_POLICY, "allkeys-lru")),
    ("acl", lambda t: t.replace(f"~{prov.KEYSPACE_PATTERN}", "~*")),
    ("terraform", lambda t: t.replace("ab0t_quota_outbox", "wrong_table")),
    ("iam", lambda t: t.replace("dynamodb:DescribeContinuousBackups", "dynamodb:Nothing")),
])
def test_verifier_goes_red_on_planted_offender(kind, plant):
    text = prov.emit(kind)
    broken = plant(text)
    assert broken != text, "the plant did not change the artifact (dead control)"
    assert prov.check_artifact_conformance(kind, broken) != [], \
        f"planted {kind} offender was NOT detected — the verifier cannot fail"


# --- drift binding: the registry IS the enforcing create-schema -------------

@pytest.mark.asyncio
async def test_registry_matches_the_executed_ensure_table_sites():
    """EXECUTE the four enforcing create sites against a recording fake and
    compare with TABLE_SPECS — change a schema without the registry (or vice
    versa) and this goes red. This is what 'generated from the enforcing
    registry' means, made falsifiable."""
    from ab0t_quota.activations import DDBActivationStore
    from ab0t_quota.billing.outbox import DDBOutboxStore
    from ab0t_quota.handler_ledger import DDBLedgerStore
    from ab0t_quota.persistence import QuotaStore

    fake = FakeDDB()
    await DDBOutboxStore(fake, table_name="ab0t_quota_outbox").ensure_table()
    await DDBActivationStore(fake, table_name="ab0t_quota_activations").ensure_table()
    await DDBLedgerStore(fake, table_name="ab0t_quota_handler_ledger").ensure_table()
    store = QuotaStore(table_name="ab0t_quota_state")
    await store.initialize(session=FakeSession(fake), create=True)

    created = {c["TableName"]: c for c in fake.create_calls}
    specs = {t.default_name: t for t in prov.TABLE_SPECS}
    assert set(created) == set(specs), \
        f"registry tables {set(specs)} != executed create sites {set(created)}"
    for name, spec in specs.items():
        call = created[name]
        assert call["KeySchema"] == [
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"}], name
        want_gsis = {
            g.name: {"hash": g.hash_key, "range": g.range_key,
                     "proj": g.projection}
            for g in spec.gsis}
        got_gsis = {
            g["IndexName"]: {"hash": g["KeySchema"][0]["AttributeName"],
                             "range": g["KeySchema"][1]["AttributeName"],
                             "proj": g["Projection"]["ProjectionType"]}
            for g in call["GlobalSecondaryIndexes"]}
        assert got_gsis == want_gsis, f"{name}: GSI drift between registry and code"
        want_attrs = {"PK": "S", "SK": "S"}
        for g in spec.gsis:
            want_attrs[g.hash_key] = g.hash_type
            want_attrs[g.range_key] = g.range_type
        got_attrs = {a["AttributeName"]: a["AttributeType"]
                     for a in call["AttributeDefinitions"]}
        assert got_attrs == want_attrs, f"{name}: attribute drift"


@pytest.mark.asyncio
async def test_preflight_ddb_checks_read_the_same_registry(tmp_path, monkeypatch):
    """Planted offender: rename a registry table and preflight must follow —
    proving preflight and the emitters share ONE source."""
    import aioboto3
    import dataclasses
    from ab0t_quota.preflight import run_preflight

    planted = tuple(
        dataclasses.replace(s, default_name="planted_outbox_name")
        if s.cap_key == "ddb_outbox" else s
        for s in prov.TABLE_SPECS)
    monkeypatch.setattr(prov, "TABLE_SPECS", planted)
    _write_config(tmp_path, monkeypatch, MINIMAL_CONFIG)
    fake_ddb = FakeDDB()
    monkeypatch.setattr(aioboto3, "Session", lambda: FakeSession(fake_ddb))
    from tests.test_t12_preflight_20260721 import GateRedis, _patch_redis
    out: list[str] = []
    with _patch_redis(GateRedis()):
        report = await run_preflight(emit=out.append)
    assert any("planted_outbox_name" in (g.detail or "") for g in report.gates), \
        "preflight did not follow the registry — it has its own table list (drift)"


# --- --local (T-3) ----------------------------------------------------------

def test_local_docker_command_derives_from_registry():
    cmd = prov.local_docker_command(port=7001, name="x")
    joined = " ".join(cmd)
    assert "--maxmemory-policy" in joined and prov.REQUIRED_MAXMEMORY_POLICY in joined
    assert "--appendonly" in joined and "yes" in joined
    assert "7001:6379" in joined
    assert prov.REDIS_IMAGE in joined
    assert cmd[0] == "docker", "local means Docker — never a cloud CLI"


def test_local_dry_run_runs_nothing(capsys):
    calls = []
    rc = prov.run_local(dry_run=True, _runner=lambda c: calls.append(c),
                        emit_line=print)
    assert rc == 0
    assert calls == [], "--dry-run must not execute anything"
    out = capsys.readouterr().out
    assert "docker run" in out and prov.REQUIRED_MAXMEMORY_POLICY in out
    assert "side effect statement" in out


def test_local_starts_and_verifies_with_the_boot_evaluator(capsys):
    class R:
        def __init__(self, stdout="", rc=0):
            self.stdout, self.returncode, self.stderr = stdout, rc, ""
    cmds = []

    def runner(c):
        cmds.append(c)
        return R()
    rc = prov.run_local(_runner=runner, _verifier=lambda: (True, []),
                        emit_line=print)
    assert rc == 0
    assert any("run" in c for c in cmds[1:2]) or len(cmds) >= 2, cmds
    out = capsys.readouterr().out
    assert "verify_redis_invariants" in out, \
        "the verification must name the boot evaluator (one judgement)"
    assert "QUOTA_REDIS_URL=" in out


def test_local_nonconforming_container_is_exit_1(capsys):
    class R:
        stdout, returncode, stderr = "", 0, ""
    rc = prov.run_local(
        _runner=lambda c: R(),
        _verifier=lambda: (False, ["counter_eviction_policy: allkeys-lru"]),
        emit_line=print)
    assert rc == 1
    assert "NONCONFORMING" in capsys.readouterr().out


def test_local_without_docker_is_exit_3_with_manual_command(capsys):
    def runner(c):
        raise FileNotFoundError("docker")
    rc = prov.run_local(_runner=runner, emit_line=print)
    assert rc == 3
    assert "docker run" in capsys.readouterr().out, \
        "no docker => print the equivalent manual command"


# --- CLI wiring -------------------------------------------------------------

def test_provision_command_is_wired(capsys, monkeypatch):
    from ab0t_quota.__main__ import main
    monkeypatch.delenv("QUOTA_CONFIG_PATH", raising=False)
    try:
        rc = main(["provision", "--emit", "compose"])
    except SystemExit as e:
        pytest.fail(f"`provision` is not a CLI command (ticket §6.1): {e}")
    assert rc == 0
    out = capsys.readouterr().out
    assert prov.REQUIRED_MAXMEMORY_POLICY in out


def test_provision_without_mode_is_exit_2(capsys, monkeypatch):
    from ab0t_quota.__main__ import main
    monkeypatch.delenv("QUOTA_CONFIG_PATH", raising=False)
    assert main(["provision"]) == 2


def test_provision_setup_is_not_a_verb(monkeypatch):
    """Ticket §6.1: `provision`, NOT `setup` — setup_quota() is the library's
    own entry point and the collision lands in the docs. Pin the rejection."""
    from ab0t_quota.__main__ import main
    with pytest.raises(SystemExit):
        main(["setup", "--emit", "compose"])
