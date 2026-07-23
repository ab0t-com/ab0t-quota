"""K-7 — Python bindings for ST-KEYSPACE-1 and ST-KEYSPACE-2.

ST-KEYSPACE-1: the v2 shape is a byte contract — the builder must reproduce
every row of conformance/keyspace_v2_vectors.json exactly, and the refusal
codes carry the pinned substrings.
ST-KEYSPACE-2: migration semantics — bound by the K-3/K-5 suites
(test_keyspace_dual_write_20260721.py, test_keyspace_migration_20260721.py);
this file pins the clause↔suite mapping so the census cannot go green on a
mention alone. Environment: fakeredis[lua].
"""
import json
from pathlib import Path

import pytest
import fakeredis.aioredis

from ab0t_quota.keyspace import Keyspace, marker_key

REPO = Path(__file__).resolve().parents[1]
VECTORS = json.loads((REPO / "conformance" / "keyspace_v2_vectors.json").read_text())


def _builder(service, version):
    return Keyspace(service=service, version=version)


@pytest.mark.parametrize("vec", VECTORS["vectors"],
                         ids=[f"v{v['version']}-{v['org_id']}" for v in VECTORS["vectors"]])
def test_st_keyspace_1_builder_reproduces_vectors_byte_identically(vec):
    ks = _builder(vec["service"], vec["version"])
    org, rk, uid, period, idem = (vec["org_id"], vec["resource_key"],
                                  vec["user_id"], vec["period"], vec["idempotency_key"])
    got = {
        "gauge": ks.gauge_key(org, rk),
        "gauge_user": ks.user_key(org, rk, uid),
        "gauge_seq_user": ks.seq_user_key(org, rk, uid),
        "idem": ks.idem_key(org, rk, idem),
        "idem_unused": ks.idem_key(org, rk, None),
        "idemgen": ks.idem_gen_key(org, rk, idem),
        "idemgen_unused": ks.idem_gen_key(org, rk, None),
        "acc": ks.acc_key(org, rk, period),
        "rate": ks.rate_key(org, rk),
        "recent": ks.recent_key(org),
    }
    assert got == vec["keys"], "builder diverges from the ST-KEYSPACE-1 vector oracle"
    if vec["hash_tag"]:
        for key in got.values():
            assert "{" + vec["hash_tag"] + "}" in key


def test_st_keyspace_1_marker_key_pinned():
    assert marker_key("sandbox-platform") == VECTORS["marker_key_example"]


def _scenario(sid):
    doc = json.loads((REPO / "conformance" / "scenarios.json").read_text())
    for item in doc["structural_conformance"]:
        if item["id"] == sid:
            return item
    raise AssertionError(f"{sid} not declared in scenarios.json")


@pytest.mark.asyncio
async def test_st_keyspace_1_refusal_substrings_pinned():
    """The declared error_must_contain substrings are what the real refusals
    say — QUOTA-CFG-011 (regression) and QUOTA-CFG-012 (brownfield)."""
    from ab0t_quota.errors import QuotaConfigError
    from ab0t_quota.keyspace_migration import check_boot_keyspace
    item = _scenario("ST-KEYSPACE-1")
    r = fakeredis.aioredis.FakeRedis()
    try:
        await r.set(marker_key("svc-a"),
                    json.dumps({"high_water": "v2-final", "phase": "reaped"}))
        with pytest.raises(QuotaConfigError) as e11:
            await check_boot_keyspace(r, Keyspace(service="svc-a", version=1))
        for frag in item["regression_error_must_contain"]:
            assert frag in str(e11.value), f"CFG-011 refusal lost pinned substring {frag!r}"
        await r.flushall()
        await r.set("quota:org-1:sandbox.concurrent:gauge", "3")
        with pytest.raises(QuotaConfigError) as e12:
            await check_boot_keyspace(r, Keyspace(service="svc-a", version=2))
        for frag in item["brownfield_error_must_contain"]:
            assert frag in str(e12.value), f"CFG-012 refusal lost pinned substring {frag!r}"
    finally:
        await r.flushall()
        await r.aclose()


def test_st_keyspace_1_config_keys_in_strict_schema():
    from ab0t_quota.config_schema import _STORAGE_KEYS
    item = _scenario("ST-KEYSPACE-1")
    for dotted in item["config_keys"]:
        assert dotted.split(".", 1)[1] in _STORAGE_KEYS


def test_st_keyspace_2_clauses_are_bound_to_running_suites():
    """ST-KEYSPACE-2's clauses execute in the K-3/K-5 suites; pin the mapping
    so retiring either suite breaks this binding, not just coverage."""
    item = _scenario("ST-KEYSPACE-2")
    assert len(item["contract"]) == 5
    for fname, needles in {
        "test_keyspace_dual_write_20260721.py": [
            "test_seed_if_absent_never_zero",
            "test_pre_dual_v1_idem_latch_recognised_across_flip",
            "test_dual_read_fallback_after_flip",
        ],
        "test_keyspace_migration_20260721.py": [
            "test_plant_seed_then_add_is_caught",
            "dual window too young",
            "i_confirm_no_other_scope_reads_v1",
        ],
    }.items():
        src = (REPO / "tests" / fname).read_text()
        for needle in needles:
            assert needle in src, f"{fname} no longer binds ST-KEYSPACE-2 ({needle})"
