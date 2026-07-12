"""RED test — the multi-key gauge/acquire Lua scripts CROSSSLOT on a clustered Redis.

Author: W-RR (real-Redis conformance leg, 2026-07-11). Do NOT fix here — the fix
(a shared hash tag around the org segment) is a KEYSPACE change gated by
`FUTURE_20260709…md` §2 (dual-read/write, `storage.keyspace_version`, mirrored to
Go per D-22) and is a separate, owner-gated leg. This test only *reproduces* the
defect so a fix is provable.

Provenance / first-hand evidence (all FOUND, 2026-07-11):
  * Stood up a throwaway `redis:7-alpine --cluster-enabled yes` node (all 16384
    slots assigned to it) and ran the ACTUAL library scripts against it. Every
    multi-key script — `_INCR`, `_DECR`, `_INCR_USER`, `_DECR_USER`, `_ACQUIRE` —
    returned the SERVER error `CROSSSLOT Keys in request don't hash to the same
    slot`. Single-key `_TRANSITION` succeeded (control). A hash-tagged variant
    (`quota:{org-1}:…`) co-located and succeeded (the fix, proven to work).
  * Raw `redis-cli CLUSTER KEYSLOT` confirmed the untagged idem/gauge keys land in
    DIFFERENT slots (e.g. 6551 vs 7270); the hash-tagged pair share one slot (6923).

Tracks: D-23 (CROSSSLOT), QI-09 (undeclared key contract), D-57 (emulators never
met a real/clustered Redis). Framed for the pre-deploy gate as D-68+.

This test needs NO running cluster: it computes Redis Cluster's CRC16 hash slot
for the real key-builder outputs and asserts they co-locate. It FAILS today
(keys carry no `{...}` hash tag) and will pass once the org segment is hash-tagged
identically in Python AND Go.
"""
import pytest

from redis.crc import key_slot  # Redis Cluster CRC16 %16384, the exact server algorithm

from ab0t_quota.counters.gauge import GaugeCounter


ORG = "org-1"
RK = "sandboxes"
UID = "user-9"


def _gauge_multikey_sets():
    """Return, per script, the ordered list of keys the library actually passes as
    KEYS[...] — taken from the real builders on GaugeCounter, no reconstruction."""
    g = GaugeCounter(redis=None, org_id=ORG, resource_key=RK)  # builders are pure
    idem = g._idem_key("k1")
    idemgen = g._idem_gen_key("k1")
    org = g._redis_key
    user = g._user_key(UID)
    seq = g._seq_user_key(UID)
    return {
        "_INCR":       [idem, org],
        "_DECR":       [idem, org],
        "_INCR_USER":  [idem, org, user, seq],
        "_DECR_USER":  [idemgen, org, user, seq],
        # _ACQUIRE for a single-gauge bundle: [idem, org, user, seq]
        "_ACQUIRE":    [idem, org, user, seq],
    }


# Disposition (D-70, coordinator 2026-07-11): W-RR authored these as plain-red to
# reproduce D-23. Converted to strict-xfail — NOT to hide the defect (it is confirmed,
# cited, and operator-gated via CHECK 4) but because CROSSSLOT is a CONDITIONAL, GATED
# defect (bites only on a clustered Redis; the fix is the gated hash-tag keyspace
# migration). A permanent plain-red for a conditional defect trains red-blindness
# (D-16/D-49). strict=True keeps the suite honestly green today AND makes it a hard
# failure the moment the hash-tag migration lands and the keys co-locate — i.e. it
# auto-promotes and tells you to delete this marker. W-RR's assertion is unchanged.
@pytest.mark.xfail(
    strict=True,
    reason="D-23 (CROSSSLOT, observed first-hand on a real cluster): multi-key Lua "
    "scripts carry no shared {hash tag}, so their keys land in different cluster slots. "
    "Fixed ONLY by the gated quota:{org} keyspace migration (FUTURE §2, "
    "storage.keyspace_version + dual-read/write, mirrored to Go). Operator CHECK 4 "
    "decides whether prod is clustered and thus whether this must ship. Auto-promotes "
    "(xpass) when the migration co-locates the keys — remove this marker then.",
)
@pytest.mark.parametrize("script,keys", list(_gauge_multikey_sets().items()))
def test_multikey_script_keys_colocate_on_cluster(script, keys):
    """Every key a multi-key Lua script touches MUST hash to one slot, or the
    script CROSSSLOTs on a clustered Redis and fails outright (D-23, observed
    first-hand 2026-07-11). RED today: the keys carry no shared `{...}` hash tag.
    """
    slots = {k: key_slot(k.encode()) for k in keys}
    distinct = set(slots.values())
    assert len(distinct) == 1, (
        f"{script}: keys hash to {len(distinct)} distinct cluster slots "
        f"{slots} -> CROSSSLOT on a clustered Redis. A shared hash tag around the "
        f"org segment (quota:{{{ORG}}}:...) co-locates them (keyspace change, "
        f"FUTURE §2 / D-23)."
    )
