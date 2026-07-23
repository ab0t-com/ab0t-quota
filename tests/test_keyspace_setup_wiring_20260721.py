"""K-9 — `setup_quota` keyspace wiring (board K-9; keyspace spec §3).

The F-1/D-23 mechanism (K-1…K-7) existed but was reachable only via
QuotaEngine(keyspace=…); `setup_quota` never consumed the declared
`storage.keyspace_version` / `keyspace_dual_write`, and `check_boot_keyspace`
(QUOTA-CFG-011/012) was never called at boot. This suite is the wiring's
permanent control:

  W1  the (1,false) DEFAULT is byte-identical to pre-wiring behaviour —
      exact v1 key strings, no v2 keys, no marker (the regression that matters)
  W2  keyspace_version=2 (greenfield) boots and writes v2-shape keys
  W3  (1,true) dual boots and maintains BOTH shapes
  W4  brownfield: v1 keys + config v2 + no completed migration ⇒ QUOTA-CFG-012
      refusal AT BOOT, through setup_quota
  W5  regression: marker v2-final + config v1 ⇒ QUOTA-CFG-011 refusal at boot
  W6  negative control: with check_boot_keyspace stubbed out, the W4 world
      boots — proving the refusal is attributable to the boot-guard wiring
      (a wired-but-guard-dropped build turns W4 red)
  W7  the T-1 resolver carries the rows (declared config / documented default;
      an ambient generic env var changes nothing)
  W8  capabilities + app.state expose the active keyspace and migration phase
      (the state preflight/doctor read — TOOL lane consumes, never re-derives)
  W9  bridge mode still REFUSES a declared keyspace state (D-KS-8: bridge does
      not consume it; lifting the schema refusal must not open a silent no-op)
  W10 seed_redis receives the wired keyspace

Environment: fakeredis[lua], no real infra.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from unittest.mock import patch

pytestmark = pytest.mark.asyncio

ORG = "org1"
RK = "thing.concurrent"
SVC = "test-svc"

BASE_CONFIG = {
    "service_name": SVC,
    "storage": {"redis_url": "redis://test/0", "persistence_enabled": False,
                "connect_retry_seconds": 0},
    "tier_provider": {"type": "static", "default_tier": "starter"},
    "alerts": {"enabled": False},
    "enforcement": {"enabled": True},
    "resources": [{"service": "t", "resource_key": RK,
                   "display_name": "T", "counter_type": "gauge", "unit": "t"}],
    "tiers": [{"tier_id": "starter", "display_name": "S", "sort_order": 1,
               "limits": {RK: 5}, "features": []}],
}


def _cfg(**storage_extra):
    cfg = json.loads(json.dumps(BASE_CONFIG))
    cfg["storage"].update(storage_extra)
    return cfg


def _write_config(tmp_path, monkeypatch, cfg):
    p = tmp_path / "quota-config.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.setenv("QUOTA_CONFIG_PATH", str(p))


def _fresh_redis():
    import fakeredis.aioredis
    return fakeredis.aioredis.FakeRedis()


def _setup(tmp_path, monkeypatch, cfg, r):
    """setup_quota against a supplied fakeredis; returns the app (lifespan
    NOT yet run — callers drive it so refusals raise on their own frame)."""
    from ab0t_quota import setup_quota
    _write_config(tmp_path, monkeypatch, cfg)
    app = FastAPI()
    with patch("redis.asyncio.Redis.from_url", side_effect=lambda *a, **kw: r):
        setup_quota(app, enable_paid=False)
    return app


async def _booted(app):
    return app.router.lifespan_context(app)


async def _quota_keys(r):
    keys = set()
    async for k in r.scan_iter(match="quota:*", count=500):
        keys.add(k.decode() if isinstance(k, bytes) else k)
    return keys


async def _drive_acquire(app):
    """One acquire touching the full key family: gauge, user, seq, idem."""
    ctx = app.state.quota
    res = await ctx.engine.acquire(
        ORG, resource_key=RK, user_id="u1", idempotency_key="k1")
    assert res.admitted, f"acquire refused: {res}"
    return res


# ---------------------------------------------------------------------------
# W1 — the v1 default is byte-identical (THE regression control)
# ---------------------------------------------------------------------------

async def test_w1_default_boot_is_v1_byte_identical(tmp_path, monkeypatch):
    """A consumer who upgrades and changes nothing gets EXACTLY the pre-change
    keyspace: the frozen v1 byte-oracle below, no v2 key, no marker, no dual."""
    r = _fresh_redis()
    app = _setup(tmp_path, monkeypatch, _cfg(), r)
    async with await _booted(app):
        await _drive_acquire(app)
        ks = app.state.quota.engine._keyspace
        assert ks.version == 1 and ks.dual_write is False
    keys = await _quota_keys(r)
    # Frozen v1 oracle — these exact bytes, nothing else in quota:*.
    expected = {
        f"quota:{ORG}:{RK}:gauge",
        f"quota:{ORG}:{RK}:gauge:user:u1",
        f"quota:{ORG}:{RK}:gauge:seq:user:u1",
        f"quota:{ORG}:{RK}:idem:k1",
    }
    assert keys == expected, f"v1 default keyspace changed: {sorted(keys)}"
    assert not any(k.startswith("quota:v2:") for k in keys)
    assert not any(k.startswith("quota:keyspace:meta:") for k in keys), \
        "the v1 default must not write a migration marker"


# ---------------------------------------------------------------------------
# W2/W3 — declared states are consumed by setup (the K-9 wiring itself)
# ---------------------------------------------------------------------------

async def test_w2_keyspace_v2_config_boots_greenfield_and_writes_v2(tmp_path, monkeypatch):
    r = _fresh_redis()
    app = _setup(tmp_path, monkeypatch, _cfg(keyspace_version=2), r)
    async with await _booted(app):
        ks = app.state.quota.engine._keyspace
        assert (ks.version, ks.dual_write, ks.service) == (2, False, SVC), \
            f"setup did not consume the declared keyspace: {ks}"
        await _drive_acquire(app)
    keys = await _quota_keys(r)
    assert f"quota:v2:{{{SVC}/{ORG}}}:{RK}:gauge" in keys, \
        f"v2 boot wrote no v2 gauge key: {sorted(keys)}"
    assert not any(k.startswith(f"quota:{ORG}:") for k in keys), \
        f"v2 (dual off) boot must not write v1 keys: {sorted(keys)}"


async def test_w3_dual_write_config_maintains_both_shapes(tmp_path, monkeypatch):
    r = _fresh_redis()
    app = _setup(tmp_path, monkeypatch,
                 _cfg(keyspace_version=1, keyspace_dual_write=True), r)
    async with await _booted(app):
        ks = app.state.quota.engine._keyspace
        assert (ks.version, ks.dual_write) == (1, True)
        await _drive_acquire(app)
    keys = await _quota_keys(r)
    v1_gauge = f"quota:{ORG}:{RK}:gauge"
    v2_gauge = f"quota:v2:{{{SVC}/{ORG}}}:{RK}:gauge"
    assert v1_gauge in keys and v2_gauge in keys, \
        f"dual boot must maintain BOTH shapes, got {sorted(keys)}"
    assert await r.get(v1_gauge) == await r.get(v2_gauge), \
        "dual-written shapes diverged on one acquire"


# ---------------------------------------------------------------------------
# W4/W5 — the boot guards fire THROUGH setup (QUOTA-CFG-011/012)
# ---------------------------------------------------------------------------

async def test_w4_brownfield_v2_with_live_v1_keys_refuses_cfg012(tmp_path, monkeypatch):
    from ab0t_quota.errors import QuotaConfigError
    r = _fresh_redis()
    await r.set(f"quota:{ORG}:{RK}:gauge", "3")  # live v1 counter, no marker
    app = _setup(tmp_path, monkeypatch, _cfg(keyspace_version=2), r)
    with pytest.raises(QuotaConfigError) as ei:
        async with await _booted(app):
            pass
    assert "QUOTA-CFG-012" in str(ei.value), \
        f"brownfield v2 boot must refuse with QUOTA-CFG-012, got: {ei.value}"


async def test_w5_v1_config_against_completed_migration_refuses_cfg011(tmp_path, monkeypatch):
    from ab0t_quota.errors import QuotaConfigError
    from ab0t_quota.keyspace import marker_key
    r = _fresh_redis()
    await r.set(marker_key(SVC), json.dumps(
        {"high_water": "v2-final", "phase": "reaped"}))
    app = _setup(tmp_path, monkeypatch, _cfg(), r)  # default (1,false)
    with pytest.raises(QuotaConfigError) as ei:
        async with await _booted(app):
            pass
    assert "QUOTA-CFG-011" in str(ei.value), \
        f"v1 boot over a reaped keyspace must refuse with QUOTA-CFG-011, got: {ei.value}"


async def test_w6_negative_control_guard_stub_lets_w4_world_boot(tmp_path, monkeypatch):
    """Attribution control: stub the guard and the W4 world boots — so the W4
    refusal comes from the check_boot_keyspace wiring, and a build that drops
    that call is caught by W4, not silently green."""
    import ab0t_quota.keyspace_migration as ksm

    async def _no_guard(redis, keyspace):
        return None

    monkeypatch.setattr(ksm, "check_boot_keyspace", _no_guard)
    r = _fresh_redis()
    await r.set(f"quota:{ORG}:{RK}:gauge", "3")
    app = _setup(tmp_path, monkeypatch, _cfg(keyspace_version=2), r)
    async with await _booted(app):
        pass  # boots ONLY because the guard was stubbed


# ---------------------------------------------------------------------------
# W7 — the rows ride the T-1 resolver; nothing ambient
# ---------------------------------------------------------------------------

async def test_w7_resolver_rows_declared_vs_default_and_never_ambient(monkeypatch):
    from ab0t_quota.resolve import Provenance, resolve_dependencies
    # An ambient generic name must never be read (declared, not discovered).
    monkeypatch.setenv("KEYSPACE_VERSION", "2")
    monkeypatch.setenv("QUOTA_KEYSPACE_VERSION", "2")  # not a documented source either
    base = {"storage": {"redis_url": "redis://x/0"}, "tiers": []}
    plan = resolve_dependencies(base, mode="local")
    assert plan["keyspace_version"].value == 1
    assert plan["keyspace_version"].provenance is Provenance.DEFAULT
    assert plan["keyspace_dual_write"].value is False

    declared = {"storage": {"redis_url": "redis://x/0", "keyspace_version": 2,
                            "keyspace_dual_write": True}, "tiers": []}
    plan2 = resolve_dependencies(declared, mode="local")
    assert plan2["keyspace_version"].value == 2
    assert plan2["keyspace_version"].provenance is Provenance.CONFIG
    assert plan2["keyspace_dual_write"].value is True


# ---------------------------------------------------------------------------
# W8 — operators can SEE the active shape (capabilities + readable state)
# ---------------------------------------------------------------------------

async def test_w8_capabilities_and_state_expose_keyspace_and_phase(tmp_path, monkeypatch):
    from ab0t_quota.keyspace import marker_key
    r = _fresh_redis()
    await r.set(marker_key(SVC), json.dumps(
        {"high_water": "dual", "phase": "dual", "dual_since": 1.0}))
    app = _setup(tmp_path, monkeypatch,
                 _cfg(keyspace_version=1, keyspace_dual_write=True), r)
    async with await _booted(app):
        caps = app.state.quota_capabilities
        assert "keyspace" in caps, f"capabilities missing keyspace: {caps}"
        assert "v1" in caps["keyspace"] and "dual" in caps["keyspace"], caps["keyspace"]
        state = app.state.quota_keyspace_state
        assert state["version"] == 1 and state["dual_write"] is True
        assert state["service"] == SVC
        assert state["migration_phase"] == "dual", state


async def test_w8b_default_capabilities_report_v1_no_phase(tmp_path, monkeypatch):
    r = _fresh_redis()
    app = _setup(tmp_path, monkeypatch, _cfg(), r)
    async with await _booted(app):
        caps = app.state.quota_capabilities
        assert "keyspace" in caps and "v1" in caps["keyspace"], caps.get("keyspace")
        assert app.state.quota_keyspace_state["migration_phase"] == "none"


# ---------------------------------------------------------------------------
# W9 — bridge mode does NOT consume the keyspace state: keep refusing (D-KS-8)
# ---------------------------------------------------------------------------

async def test_w9_bridge_mode_refuses_declared_keyspace_state(tmp_path, monkeypatch):
    from ab0t_quota import setup_quota
    from ab0t_quota.errors import QuotaConfigError
    cfg = _cfg(keyspace_version=2)
    cfg["engine_mode"] = "bridge"
    _write_config(tmp_path, monkeypatch, cfg)
    monkeypatch.setenv("AB0T_MESH_API_KEY", "k")
    monkeypatch.setenv("AB0T_CONSUMER_ORG_ID", "o")
    app = FastAPI()
    with pytest.raises(QuotaConfigError) as ei:
        setup_quota(app, enable_paid=False)
    msg = str(ei.value)
    assert "bridge" in msg and "keyspace" in msg, \
        f"bridge + declared keyspace must refuse naming the gap, got: {msg}"


# ---------------------------------------------------------------------------
# W10 — the persistence seed path receives the wired keyspace
# ---------------------------------------------------------------------------

async def test_w10_seed_redis_receives_wired_keyspace(tmp_path, monkeypatch):
    import ab0t_quota.setup as setup_mod

    calls = {}

    class _StubStore:
        def __init__(self, **kw):
            pass

        async def initialize(self, create=False):
            return None

        async def get_override(self, org_id, resource_key):
            return None

        async def seed_redis(self, redis, registry, activation_store=None,
                             keyspace=None):
            calls["keyspace"] = keyspace
            return 0

        def start_sync_worker(self, redis, registry, interval_seconds=300):
            return None

        async def close(self):
            return None

    monkeypatch.setattr(setup_mod, "QuotaStore", _StubStore)
    r = _fresh_redis()
    app = _setup(tmp_path, monkeypatch,
                 _cfg(keyspace_version=2, persistence_enabled=True), r)
    async with await _booted(app):
        pass
    ks = calls.get("keyspace")
    assert ks is not None, "seed_redis was not given the wired keyspace"
    assert ks.version == 2 and ks.service == SVC, f"wrong keyspace to seed: {ks}"
