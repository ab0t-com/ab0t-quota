"""K-1/K-2 — keyspace versioning seam + v2 shape + co-slot law.

Spec: tickets/20260721_keyspace_versioning/SPEC_keyspace_versioning_20260721.md
RED-first (board rows K-1, K-2). The v1 xfail in
test_cluster_crossslot_keyslot_20260711.py is NOT relaxed — v1 keys stay
untagged and cluster-unsafe; this file adds the v2 positive law (spec §9-V4).
"""
import pytest
from redis.crc import key_slot

SVC = "sandbox-platform"
ORG = "org-1"
RK = "sandbox.concurrent"
UID = "user-9"


def _ks(version=2, dual=False, service=SVC):
    from ab0t_quota.keyspace import Keyspace
    return Keyspace(service=service, version=version, dual_write=dual)


# ---------------------------------------------------------------- K-1: seam

def test_v1_shapes_bit_identical_to_today():
    """Keyspace() default (v1, no dual) reproduces today's key strings exactly."""
    from ab0t_quota.keyspace import Keyspace
    ks = Keyspace()
    assert ks.version == 1 and ks.dual_write is False
    assert ks.gauge_key(ORG, RK) == f"quota:{ORG}:{RK}:gauge"
    assert ks.user_key(ORG, RK, UID) == f"quota:{ORG}:{RK}:gauge:user:{UID}"
    assert ks.seq_user_key(ORG, RK, UID) == f"quota:{ORG}:{RK}:gauge:seq:user:{UID}"
    assert ks.idem_key(ORG, RK, "k1") == f"quota:{ORG}:{RK}:idem:k1"
    assert ks.idem_key(ORG, RK, None) == f"quota:{ORG}:{RK}:idem:__unused__"
    assert ks.idem_gen_key(ORG, RK, "k1") == f"quota:{ORG}:{RK}:idemgen:k1"
    assert ks.acc_key(ORG, RK, "2026-07") == f"quota:{ORG}:{RK}:acc:2026-07"
    assert ks.rate_key(ORG, RK) == f"quota:{ORG}:{RK}:rate"
    assert ks.recent_key(ORG) == f"quota:reconcile:recent:{ORG}"


def test_v2_shapes_match_spec_2_1():
    """Exact v2 family strings (spec §2.1); the hash tag braces are literal."""
    ks = _ks()
    tag = "{" + f"{SVC}/{ORG}" + "}"
    assert ks.gauge_key(ORG, RK) == f"quota:v2:{tag}:{RK}:gauge"
    assert ks.user_key(ORG, RK, UID) == f"quota:v2:{tag}:{RK}:gauge:user:{UID}"
    assert ks.seq_user_key(ORG, RK, UID) == f"quota:v2:{tag}:{RK}:gauge:seq:user:{UID}"
    assert ks.idem_key(ORG, RK, "k1") == f"quota:v2:{tag}:{RK}:idem:k1"
    assert ks.idem_key(ORG, RK, None) == f"quota:v2:{tag}:{RK}:idem:__unused__"
    assert ks.idem_gen_key(ORG, RK, "k1") == f"quota:v2:{tag}:{RK}:idemgen:k1"
    assert ks.acc_key(ORG, RK, "2026-07") == f"quota:v2:{tag}:{RK}:acc:2026-07"
    assert ks.rate_key(ORG, RK) == f"quota:v2:{tag}:{RK}:rate"
    assert ks.recent_key(ORG) == f"quota:v2:{tag}:reconcile:recent"


def test_legal_states_and_refusals():
    """Four legal states (spec §3.1); v2/dual require a service scope."""
    from ab0t_quota.keyspace import Keyspace, KeyspaceConfigError
    Keyspace()                                        # (1,false)
    _ks(version=1, dual=True)                         # (1,true)
    _ks(version=2, dual=True)                         # (2,true)
    _ks(version=2, dual=False)                        # (2,false)
    with pytest.raises(KeyspaceConfigError):
        Keyspace(version=3)
    with pytest.raises(KeyspaceConfigError):
        Keyspace(service=None, version=2)             # v2 needs the svc segment
    with pytest.raises(KeyspaceConfigError):
        Keyspace(service=None, version=1, dual_write=True)  # dual writes v2 keys


def test_marker_key_shape():
    from ab0t_quota.keyspace import marker_key
    assert marker_key(SVC) == f"quota:keyspace:meta:{SVC}"


# ------------------------------------------------------- K-2: charset guard

@pytest.mark.parametrize("bad", ["a{b", "a}b", "a/b", "a:b", "v2", "v13", ""])
def test_charset_guard_refuses_bad_org(bad):
    """Orgs containing {,},/,: or matching ^v[0-9]+$ are refused loudly at the
    boundary (spec §2.3/§2.4) — never silently mangled into a key."""
    from ab0t_quota.keyspace import KeyspaceScopeError
    ks = _ks()
    with pytest.raises(KeyspaceScopeError):
        ks.gauge_key(bad, RK)


@pytest.mark.parametrize("bad", ["a{b", "a}b", "a/b", "a:b", ""])
def test_charset_guard_refuses_bad_service(bad):
    from ab0t_quota.keyspace import Keyspace, KeyspaceScopeError
    with pytest.raises(KeyspaceScopeError):
        Keyspace(service=bad, version=2)


def test_charset_guard_negative_control():
    """D-14 control: a clean org id passes — the guard can distinguish."""
    ks = _ks()
    assert ks.gauge_key("0b7e-uuid-ok", RK)


# ---------------------------------------------------------- K-2: co-slot law

def test_v2_all_counter_keys_one_org_coslot():
    """Every v2 key of one (svc,org) — the widest _ACQUIRE bundle included —
    hashes to ONE cluster slot (spec §2.3, V4). This is the positive law the
    v1 strict-xfail documents the absence of."""
    ks = _ks()
    keys = [
        ks.gauge_key(ORG, RK), ks.user_key(ORG, RK, UID),
        ks.seq_user_key(ORG, RK, UID), ks.idem_key(ORG, RK, "k1"),
        ks.idem_gen_key(ORG, RK, "k1"), ks.acc_key(ORG, RK, "2026-07"),
        ks.rate_key(ORG, RK), ks.recent_key(ORG),
        # _ACQUIRE across a second bundle resource: same (svc,org) tag
        ks.gauge_key(ORG, "sandbox.gpu"), ks.user_key(ORG, "sandbox.gpu", UID),
        ks.seq_user_key(ORG, "sandbox.gpu", UID),
    ]
    slots = {k: key_slot(k.encode()) for k in keys}
    assert len(set(slots.values())) == 1, f"CROSSSLOT within one (svc,org): {slots}"


def test_v2_coslot_negative_control():
    """D-14 control: keys for a DIFFERENT (svc,org) do land elsewhere for this
    pair (chosen so slots differ) — proving the assertion above can fail."""
    a = _ks().gauge_key(ORG, RK)
    b = _ks(service="other-svc").gauge_key("org-2", RK)
    assert key_slot(a.encode()) != key_slot(b.encode())


# --------------------------------------------- K-1: strict schema new keys

@pytest.mark.parametrize("storage", [
    {"keyspace_version": 1, "keyspace_dual_write": False},
    # K-9 (D-KS-9): declared migration states are now CONSUMED by setup_quota
    # (local/byo_redis) — legal at the schema; bridge refuses in setup itself.
    {"keyspace_version": 2},
    {"keyspace_version": 1, "keyspace_dual_write": True},
    {"keyspace_version": 2, "keyspace_dual_write": True},
])
def test_config_schema_accepts_keyspace_keys(storage):
    from ab0t_quota.config_schema import validate_config
    validate_config({"storage": storage})


@pytest.mark.parametrize("storage", [
    {"keyspace_version": "2"},          # wrong type
    {"keyspace_version": 3},            # not a defined version
    {"keyspace_version": True},         # bool is not an int here
    {"keyspace_dual_write": "yes"},     # wrong type
])
def test_config_schema_refuses_bad_keyspace_values(storage):
    from ab0t_quota.config_schema import validate_config
    from ab0t_quota.errors import QuotaConfigError
    with pytest.raises(QuotaConfigError):
        validate_config({"storage": storage})
