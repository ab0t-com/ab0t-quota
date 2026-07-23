"""T-6 residue (pack 20260721): table creation is explicit and opt-outable.

(a) `storage.auto_create_tables` default FALSE, enforced at the SETUP call
    sites (D-3) — the primitives keep `create=True` defaults so direct-call
    DDB tests and tooling are untouched.
(b) CreateTable audit: no library setup path reaches table creation without
    the flag; the refusal names the flag (design §8 row 6 — before the D-34
    cascade).
(c) All four self-created tables carry the ManagedBy tags the state table
    already had (ENV-04).
"""
from __future__ import annotations

import json
import logging

import pytest
from fastapi import FastAPI

MANAGED_BY = {"Key": "ManagedBy", "Value": "ab0t-quota-library"}

MINIMAL_CONFIG = {
    "service_name": "test-svc",
    "storage": {"redis_url": "redis://test/0"},
    "tier_provider": {"type": "static", "default_tier": "starter"},
    "alerts": {"enabled": False},
    "enforcement": {"enabled": True},
    "resources": [{"service": "t", "resource_key": "thing.concurrent",
                   "display_name": "T", "counter_type": "gauge", "unit": "t"}],
    "tiers": [{"tier_id": "starter", "display_name": "S", "sort_order": 1,
               "limits": {"thing.concurrent": 5}, "features": []}],
}


class FakeDDB:
    """Async DDB client fake: describe/create/TTL/waiter, per-table state."""

    class exceptions:
        class ResourceNotFoundException(Exception):
            pass

    def __init__(self):
        self.create_calls: list[dict] = []
        self.tables: dict[str, dict] = {}

    async def describe_table(self, TableName):
        if TableName not in self.tables:
            raise self.exceptions.ResourceNotFoundException(TableName)
        gsis = [{"IndexName": g["IndexName"], "IndexStatus": "ACTIVE"}
                for g in self.tables[TableName].get("GlobalSecondaryIndexes", [])]
        return {"Table": {"TableStatus": "ACTIVE", "GlobalSecondaryIndexes": gsis}}

    async def create_table(self, **kw):
        self.create_calls.append(kw)
        self.tables[kw["TableName"]] = kw
        return {}

    async def describe_time_to_live(self, TableName):
        return {"TimeToLiveDescription": {"TimeToLiveStatus": "ENABLED",
                                          "AttributeName": "ttl"}}

    async def update_time_to_live(self, **kw):
        return {}

    async def describe_continuous_backups(self, TableName):
        return {"ContinuousBackupsDescription": {
            "PointInTimeRecoveryDescription": {"PointInTimeRecoveryStatus": "ENABLED"}}}

    def get_waiter(self, name):
        class _W:
            async def wait(self, **kw):
                return None
        return _W()


class _ACtx:
    def __init__(self, obj):
        self._obj = obj

    async def __aenter__(self):
        return self._obj

    async def __aexit__(self, *a):
        return False


class FakeTable:
    def __getattr__(self, name):
        async def _absorb(*a, **kw):
            return {}
        return _absorb


class FakeResource:
    async def Table(self, name):
        return FakeTable()


class FakeSession:
    """Stands in for aioboto3.Session(); hands out the shared FakeDDB client."""

    def __init__(self, client):
        self._client = client

    def client(self, *a, **kw):
        return _ACtx(self._client)

    def resource(self, *a, **kw):
        return _ACtx(FakeResource())


def _write_config(tmp_path, monkeypatch, cfg: dict) -> None:
    p = tmp_path / "quota-config.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.setenv("QUOTA_CONFIG_PATH", str(p))


def _boot(tmp_path, monkeypatch, cfg):
    """Full setup + lifespan with fakeredis and the DDB fakes on every seam
    (app.state.ddb_client for the money stores; aioboto3.Session for the
    state store). Returns the shared FakeDDB."""
    import aioboto3
    import fakeredis.aioredis
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from ab0t_quota import setup_quota

    fake = FakeDDB()
    monkeypatch.setattr(aioboto3, "Session", lambda: FakeSession(fake))
    r = fakeredis.aioredis.FakeRedis()
    _write_config(tmp_path, monkeypatch, cfg)
    app = FastAPI()
    with patch("redis.asyncio.Redis.from_url", side_effect=lambda *a, **kw: r):
        setup_quota(app, enable_paid=False)
        app.state.ddb_client = fake
        with TestClient(app):
            pass
    return fake


def test_no_table_created_without_opt_in(tmp_path, monkeypatch, caplog):
    """ENV-04: with `storage.auto_create_tables` at its DEFAULT (absent =>
    false), a full lifespan against an account with NO tables must reach
    CreateTable ZERO times, and the refusals must name the opt-in flag."""
    with caplog.at_level(logging.WARNING):
        fake = _boot(tmp_path, monkeypatch, MINIMAL_CONFIG)
    created = [c["TableName"] for c in fake.create_calls]
    assert created == [], \
        f"library created table(s) without storage.auto_create_tables: {created}"
    assert "auto_create_tables" in caplog.text, \
        "the missing-table refusal must name storage.auto_create_tables (row 6)"


def test_auto_create_opt_in_reaches_create_table(tmp_path, monkeypatch):
    """Control (proves the audit above can go red): the explicit opt-in still
    self-provisions — state store and activation ledger create through the
    lifespan (the outbox call site needs a wired emitter; covered below)."""
    cfg = dict(MINIMAL_CONFIG)
    cfg["storage"] = {"redis_url": "redis://test/0", "auto_create_tables": True}
    fake = _boot(tmp_path, monkeypatch, cfg)
    created = {c["TableName"] for c in fake.create_calls}
    assert {"ab0t_quota_state", "ab0t_quota_activations"} <= created, \
        f"opt-in must reach the setup-provisioned tables, got {created}"


class _StubEmitter:
    def __init__(self):
        self._billing_disabled = False
        self.stores = []

    def set_outbox_store(self, store):
        self.stores.append(store)

    def disable_billing(self, reason):
        self._billing_disabled = True


@pytest.mark.asyncio
@pytest.mark.parametrize("opt_in,expect_create", [(False, 0), (True, 1)])
async def test_outbox_call_site_honours_auto_create(opt_in, expect_create, caplog):
    """The outbox call site (D-3): default => describe only, refusal names the
    flag; opt-in => CreateTable reached. Exercised directly because the
    lifespan only reaches it with a wired emitter."""
    from ab0t_quota.setup import _resolve_outbox_durability
    app = FastAPI()
    fake = FakeDDB()
    app.state.ddb_client = fake
    storage = {"auto_create_tables": True} if opt_in else {}
    with caplog.at_level(logging.WARNING):
        await _resolve_outbox_durability(
            app, _StubEmitter(), redis=None, config={},
            storage=storage, enable_paid=False)
    assert len(fake.create_calls) == expect_create, \
        f"outbox call site: create_calls={len(fake.create_calls)} opt_in={opt_in}"
    if not opt_in:
        assert "auto_create_tables" in caplog.text


@pytest.mark.asyncio
async def test_direct_ensure_table_still_creates():
    """D-3 placement pin: the primitives keep creating when called DIRECTLY
    (tests/tooling) — policy lives at the setup call sites, not in the
    mechanism."""
    from ab0t_quota.activations import DDBActivationStore
    fake = FakeDDB()
    store = DDBActivationStore(fake, table_name="t_direct")
    await store.ensure_table(gsi_active_timeout_s=5)
    assert [c["TableName"] for c in fake.create_calls] == ["t_direct"]


@pytest.mark.asyncio
async def test_all_self_created_tables_are_tagged():
    """ENV-04(c): every table the library creates carries the ManagedBy tag
    the state table already had — activation ledger, outbox, handler ledger."""
    from ab0t_quota.activations import DDBActivationStore
    from ab0t_quota.billing.outbox import DDBOutboxStore
    from ab0t_quota.handler_ledger import DDBLedgerStore

    for ctor, kwargs in (
        (DDBActivationStore, {"table_name": "t_act"}),
        (DDBOutboxStore, {"table_name": "t_out"}),
        (DDBLedgerStore, {"table_name": "t_led"}),
    ):
        fake = FakeDDB()
        store = ctor(fake, **kwargs)
        await store.ensure_table()
        assert len(fake.create_calls) == 1
        tags = fake.create_calls[0].get("Tags", [])
        assert MANAGED_BY in tags, \
            f"{ctor.__name__} creates its table untagged: Tags={tags}"
