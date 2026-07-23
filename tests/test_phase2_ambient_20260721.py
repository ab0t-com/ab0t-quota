"""Phase 2 — the rest of the ambient surface (T-4, T-5, T-7, T-19, T-20) plus
the two Gate-B verifier pins (QUOTA-CFG-005, deprecated-env warning).

RED-first per design_test_harness_20260721.md §1 (Rule A: failures land on
assertions, never on imports). Reuses tests/dnd_harness_20260721.py.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from pathlib import Path

import pytest
from fastapi import FastAPI

from tests.dnd_harness_20260721 import (
    DECOYS,
    ContactAttempted,
    install_no_contact,
    install_pollution,
    install_seam_recorders,
)

REPO = Path(__file__).resolve().parents[1]

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


# ---------------------------------------------------------------------------
# Verifier pins (behaviour shipped in claim 2; pinned permanently here)
# ---------------------------------------------------------------------------

def test_memory_url_is_rejected_with_bridge_pointer(tmp_path, monkeypatch):
    """PIN QUOTA-CFG-005 (D-5(b)): Python refuses `memory://` and points at
    bridge mode — never a silent in-memory counter."""
    from ab0t_quota import setup_quota
    cfg = dict(MINIMAL_CONFIG); cfg["storage"] = {"redis_url": "memory://"}
    _write_config(tmp_path, monkeypatch, cfg)
    with pytest.raises(Exception) as ei:
        setup_quota(FastAPI())
    assert type(ei.value).__name__ == "QuotaConfigError"
    assert "QUOTA-CFG-005" in str(ei.value) and "bridge" in str(ei.value)


def test_deprecated_env_source_warns(tmp_path, monkeypatch, caplog):
    """PIN the TRANSITION tier: the library's own legacy DYNAMODB_ENDPOINT is
    honoured but warns loudly, naming the namespaced replacement."""
    from ab0t_quota import setup_quota
    monkeypatch.delenv("QUOTA_DYNAMODB_ENDPOINT", raising=False)
    monkeypatch.setenv("DYNAMODB_ENDPOINT", "http://localhost:8000")
    _write_config(tmp_path, monkeypatch, MINIMAL_CONFIG)
    with caplog.at_level(logging.WARNING):
        setup_quota(FastAPI(), enable_paid=False)
    assert any("DEPRECATED" in r.message and "DYNAMODB_ENDPOINT" in r.message
               for r in caplog.records), "legacy source must warn, not pass silently"


# ---------------------------------------------------------------------------
# T-4 — money-path secrets (ENV-05 / ENV-06)
# ---------------------------------------------------------------------------

def _stripe_sig(secret: str, body: bytes) -> str:
    ts = str(int(time.time()))
    mac = hmac.new(secret.encode(), f"{ts}.{body.decode()}".encode(), hashlib.sha256)
    return f"t={ts},v1={mac.hexdigest()}"


def test_stripe_secret_not_harvested(monkeypatch):
    """ENV-05: with the namespaced secret UNSET and only the generic decoy
    around, a webhook signed with the DECOY secret must NOT verify — the route
    must refuse as UNCONFIGURED (pre-verification), reaching for nothing. The
    socket guard (not the constructor seams) is the negative-contact layer here
    because the router legitimately constructs its clients at mount time."""
    from fastapi.testclient import TestClient
    from ab0t_quota.billing.router import create_billing_router

    monkeypatch.delenv("AB0T_QUOTA_STRIPE_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", DECOYS["STRIPE_WEBHOOK_SECRET"])
    install_no_contact(monkeypatch)

    app = FastAPI()
    app.include_router(create_billing_router(
        payment_url="http://payment.test", payment_api_key="k",
        billing_url="http://billing.test", billing_api_key="k",
        consumer_org_id="org-1"))
    client = TestClient(app, raise_server_exceptions=False)

    body = json.dumps({"id": "evt_1", "type": "invoice.paid",
                       "data": {"object": {}}}).encode()
    resp = client.post("/api/webhooks/stripe", content=body,
                       headers={"stripe-signature": _stripe_sig(DECOYS["STRIPE_WEBHOOK_SECRET"], body)})
    assert resp.status_code == 500 and "config error" in resp.text.lower(), \
        ("decoy-signed webhook must be refused as UNCONFIGURED (the generic secret "
         f"is not a declaration), got {resp.status_code}: {resp.text[:200]}")


def test_sns_generic_name_is_deprecation_not_harvest(monkeypatch, caplog):
    """ENV-06: the generic SNS_LIFECYCLE_TOPIC_ARN is our OWN legacy name — it
    still resolves (documented transition) but must WARN naming the namespaced
    replacement; the namespaced name beats it."""
    from ab0t_quota.billing.lifecycle import LifecycleEmitter
    monkeypatch.delenv("AB0T_MESH_SNS_LIFECYCLE_TOPIC_ARN", raising=False)
    monkeypatch.setenv("SNS_LIFECYCLE_TOPIC_ARN", DECOYS["SNS_LIFECYCLE_TOPIC_ARN"])
    with caplog.at_level(logging.WARNING):
        em = LifecycleEmitter(outbox_enabled=False)
    assert em._topic_arn == DECOYS["SNS_LIFECYCLE_TOPIC_ARN"]  # transition, not purge
    assert any("DEPRECATED" in r.message and "AB0T_MESH_SNS_LIFECYCLE_TOPIC_ARN" in r.message
               for r in caplog.records), "legacy SNS name must warn on use"

    caplog.clear()
    monkeypatch.setenv("AB0T_MESH_SNS_LIFECYCLE_TOPIC_ARN", "arn:aws:sns:r:1:namespaced")
    em2 = LifecycleEmitter(outbox_enabled=False)
    assert em2._topic_arn == "arn:aws:sns:r:1:namespaced"  # namespaced wins


def test_generic_stripe_var_alone_logs_startup_error(tmp_path, monkeypatch, caplog):
    """Migration row 3, D-10 SIGNED (option 1): generic set + namespaced unset
    is called out LOUDLY at setup time — the silent-off trap is named, not
    merely documented. (Was strict-xfail while D-10 was unsigned.)"""
    from ab0t_quota import setup_quota
    monkeypatch.delenv("AB0T_QUOTA_STRIPE_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("AB0T_QUOTA_SUPPRESS_DEPRECATION_ERRORS", raising=False)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", DECOYS["STRIPE_WEBHOOK_SECRET"])
    _write_config(tmp_path, monkeypatch, MINIMAL_CONFIG)
    with caplog.at_level(logging.ERROR):
        setup_quota(FastAPI(), enable_paid=False)
    hits = [r for r in caplog.records
            if "STRIPE_WEBHOOK_SECRET" in r.message and "AB0T_QUOTA_STRIPE_WEBHOOK_SECRET" in r.message]
    assert hits, "generic-set/namespaced-unset must produce a startup ERROR naming both vars"
    assert any(r.levelno == logging.ERROR for r in hits), "the call-out must be ERROR level"
    # D-10 binding condition: presence only — the decoy VALUE must never be logged
    assert DECOYS["STRIPE_WEBHOOK_SECRET"] not in caplog.text, \
        "presence-only check leaked the generic var's value into the log"


def test_d10_suppress_lever_downgrades_error_to_warning(tmp_path, monkeypatch, caplog):
    """D-10's lever: AB0T_QUOTA_SUPPRESS_DEPRECATION_ERRORS=true turns the hard
    error into a warning for an operator mid-migration."""
    from ab0t_quota import setup_quota
    monkeypatch.delenv("AB0T_QUOTA_STRIPE_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", DECOYS["STRIPE_WEBHOOK_SECRET"])
    monkeypatch.setenv("AB0T_QUOTA_SUPPRESS_DEPRECATION_ERRORS", "true")
    _write_config(tmp_path, monkeypatch, MINIMAL_CONFIG)
    with caplog.at_level(logging.WARNING):
        setup_quota(FastAPI(), enable_paid=False)
    hits = [r for r in caplog.records
            if "STRIPE_WEBHOOK_SECRET" in r.message and "AB0T_QUOTA_STRIPE_WEBHOOK_SECRET" in r.message]
    assert hits, "suppressed call-out must still WARN, never fall silent"
    assert all(r.levelno == logging.WARNING for r in hits), \
        "with the lever set the call-out must be WARNING, not ERROR"


def test_d10_transition_sns_presence_warns_in_bridge_mode(tmp_path, monkeypatch, caplog):
    """D-10 transition tier: legacy SNS_LIFECYCLE_TOPIC_ARN set + namespaced
    unset warns at startup even in BRIDGE mode, where the plan is empty so the
    resolver's on-use warning can never fire — the presence check is the only
    voice a bridge consumer gets."""
    from ab0t_quota import setup_quota
    monkeypatch.delenv("AB0T_MESH_SNS_LIFECYCLE_TOPIC_ARN", raising=False)
    monkeypatch.setenv("SNS_LIFECYCLE_TOPIC_ARN", DECOYS["SNS_LIFECYCLE_TOPIC_ARN"])
    monkeypatch.setenv("AB0T_MESH_API_KEY", "test-key")
    monkeypatch.setenv("AB0T_SERVICE_NAME", "svc-1")
    _write_config(tmp_path, monkeypatch,
                  {"service_name": "svc-1", "engine_mode": "bridge"})
    with caplog.at_level(logging.WARNING):
        setup_quota(FastAPI())
    assert any("DEPRECATED" in r.message and "SNS_LIFECYCLE_TOPIC_ARN" in r.message
               and "AB0T_MESH_SNS_LIFECYCLE_TOPIC_ARN" in r.message
               for r in caplog.records), \
        "transition-tier presence must warn at startup, naming the replacement"


# ---------------------------------------------------------------------------
# T-5 — outbound inventory + offline mode (ENV-07/08/11)
# ---------------------------------------------------------------------------

def test_startup_logs_every_outbound_target(tmp_path, monkeypatch, caplog):
    from ab0t_quota import setup_quota
    _write_config(tmp_path, monkeypatch, MINIMAL_CONFIG)
    with caplog.at_level(logging.INFO):
        setup_quota(FastAPI(), enable_paid=False)
    text = caplog.text
    assert "OUTBOUND TARGETS" in text, "startup must inventory outbound targets"
    assert "billing" in text and "payment" in text


def test_offline_mode_contacts_nothing(tmp_path, monkeypatch, caplog):
    """AB0T_QUOTA_OFFLINE=true: a full lifespan boots and NOTHING outbound is
    even constructed — no aioboto3 session (state-store persistence AND
    activation/outbox self-provision), no httpx (catalog PUT / paid clients),
    no boto3 (SNS). Gate-C re-gate condition: persistence_enabled is left at
    its DEFAULT (True) — the earlier version set it False, which configured
    the proof around the state-store hole the Gate C verifier found."""
    import fakeredis.aioredis
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from ab0t_quota import setup_quota

    rec = install_seam_recorders(monkeypatch)  # will re-patch redis below
    r = fakeredis.aioredis.FakeRedis()
    monkeypatch.setenv("AB0T_QUOTA_OFFLINE", "true")
    monkeypatch.setenv("AB0T_MESH_API_KEY", "test-key")  # would enable the PUT
    cfg = dict(MINIMAL_CONFIG)
    cfg["storage"] = {"redis_url": "redis://test/0"}  # persistence_enabled DEFAULT
    _write_config(tmp_path, monkeypatch, cfg)

    app = FastAPI()
    with patch("redis.asyncio.Redis.from_url", side_effect=lambda *a, **kw: r):
        setup_quota(app)  # enable_paid default True — the surface offline must gate
        with TestClient(app):
            pass
    outbound = [c for c in rec.all_calls if c[0] != "redis"]
    assert outbound == [], f"offline mode reached for infrastructure: {outbound}"
    assert "offline" in caplog.text.lower()


# ---------------------------------------------------------------------------
# T-7 — the CLI shares the resolver (ENV-10)
# ---------------------------------------------------------------------------

def test_cli_refuses_without_declared_store(monkeypatch, capsys):
    """`events` with NO declared store must exit non-zero naming the settings —
    never a silent in-memory run that reads and writes nothing. A generic
    REDIS_URL decoy must not count as a declaration."""
    import ab0t_quota.handler_ledger as hl
    from ab0t_quota.__main__ import main

    for name in ("AB0T_QUOTA_DDB_TABLE", "QUOTA_REDIS_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REDIS_URL", DECOYS["REDIS_URL"])

    constructed = []
    real = hl.InMemoryLedgerStore

    class SpyStore(real):
        def __init__(self, *a, **kw):
            constructed.append(1)
            super().__init__(*a, **kw)

    monkeypatch.setattr(hl, "InMemoryLedgerStore", SpyStore)
    try:
        rc, exc = main(["events", "--user-id", "u1"]), None
    except SystemExit as e:
        rc, exc = e.code, None
    except Exception as e:  # today: the DECOY redis is dialled and explodes
        rc, exc = None, e
    err = capsys.readouterr().err
    assert exc is None, \
        f"CLI used an undeclared store instead of refusing cleanly: {exc!r}"
    assert rc not in (0, None), \
        "an undeclared store must be a non-zero exit, not a silent in-memory run"
    assert "QUOTA_REDIS_URL" in err or "AB0T_QUOTA_DDB_TABLE" in err
    assert constructed == [], "silent InMemoryLedgerStore fallback still constructed"


# ---------------------------------------------------------------------------
# T-20 — bridge mode hard-requires its mesh identity
# ---------------------------------------------------------------------------

def test_bridge_without_mesh_key_is_config_error(tmp_path, monkeypatch):
    from ab0t_quota import setup_quota
    cfg = {"service_name": "svc-1", "engine_mode": "bridge"}
    _write_config(tmp_path, monkeypatch, cfg)
    monkeypatch.delenv("AB0T_MESH_API_KEY", raising=False)
    monkeypatch.setenv("AB0T_SERVICE_NAME", "svc-1")
    with pytest.raises(Exception) as ei:
        setup_quota(FastAPI())
    assert type(ei.value).__name__ == "QuotaConfigError", \
        f"bridge without a mesh key must refuse at setup, got {type(ei.value).__name__}"
    assert "AB0T_MESH_API_KEY" in str(ei.value)


def test_bridge_with_key_still_boots(tmp_path, monkeypatch):
    """Control: the declared bridge path is unaffected (test_setup.py's shape)."""
    from ab0t_quota import setup_quota
    cfg = {"service_name": "svc-1", "engine_mode": "bridge"}
    _write_config(tmp_path, monkeypatch, cfg)
    monkeypatch.setenv("AB0T_MESH_API_KEY", "test-key")
    monkeypatch.setenv("AB0T_SERVICE_NAME", "svc-1")
    setup_quota(FastAPI())  # must not raise


# ---------------------------------------------------------------------------
# T-19 — config schema validation
# ---------------------------------------------------------------------------

def test_config_schema_rejects_unknown_storage_key(tmp_path, monkeypatch):
    """A typo'd storage key must be a config error, not a silently-ignored
    no-op (a typo must never silently change enforcement — D-14/D-48)."""
    from ab0t_quota import load_config
    cfg = dict(MINIMAL_CONFIG)
    cfg["storage"] = {"redis_url": "redis://test/0", "redis_ur": "redis://typo/0"}
    _write_config(tmp_path, monkeypatch, cfg)
    with pytest.raises(Exception) as ei:
        load_config()
    assert type(ei.value).__name__ == "QuotaConfigError", \
        f"unknown storage key must be a QuotaConfigError, got {type(ei.value).__name__}"
    assert "redis_ur" in str(ei.value)


def test_config_schema_rejects_wrong_type(tmp_path, monkeypatch):
    from ab0t_quota import load_config
    cfg = dict(MINIMAL_CONFIG)
    cfg["storage"] = {"redis_url": 6379}
    _write_config(tmp_path, monkeypatch, cfg)
    with pytest.raises(Exception) as ei:
        load_config()
    assert type(ei.value).__name__ == "QuotaConfigError"
    assert "redis_url" in str(ei.value)


def test_schema_matches_example_config(tmp_path, monkeypatch):
    """The example's `$schema` pointer must be TRUE: the committed schema file
    exists, matches the models, and the example config validates."""
    schema_path = REPO / "quota-config-schema.json"
    assert schema_path.exists(), "committed quota-config-schema.json must exist (T-19)"
    import importlib.util
    assert importlib.util.find_spec("ab0t_quota.config_schema") is not None, \
        "fix must ship ab0t_quota.config_schema"
    from ab0t_quota.config_schema import generate_schema, validate_config
    assert json.loads(schema_path.read_text()) == generate_schema(), \
        "committed schema drifted from the models — regenerate and commit"
    example = json.loads((REPO / "quota-config.example.json").read_text())
    validate_config(example)  # must not raise
    # and the frozen consumer config validates too:
    consumer = json.loads((Path(__file__).parent / "data" /
                           "consumer_sandbox_platform_quota_config_20260721.json").read_text())
    validate_config(consumer)


# fixtures ------------------------------------------------------------------

@pytest.fixture
def seam_recorders(monkeypatch):
    return install_seam_recorders(monkeypatch)


def test_custom_redis_key_prefix_is_refused(tmp_path, monkeypatch):
    """CHANGELOG 0.6.x announced: custom storage.redis_key_prefix is no longer
    allowed (forks the keyspace, breaks cross-runtime sharing; Go refuses at
    boot). Python's keyspace is hard-fixed to `quota:` yet the schema silently
    ACCEPTED any value — an announced contract nobody enforced (Gate E's
    cross-lane finding). The default "quota" stays valid."""
    from ab0t_quota import load_config
    cfg = dict(MINIMAL_CONFIG)
    cfg["storage"] = {"redis_url": "redis://test/0", "redis_key_prefix": "custom"}
    _write_config(tmp_path, monkeypatch, cfg)
    with pytest.raises(Exception) as ei:
        load_config()
    assert type(ei.value).__name__ == "QuotaConfigError"
    assert "redis_key_prefix" in str(ei.value) and "quota" in str(ei.value)

    cfg["storage"] = {"redis_url": "redis://test/0", "redis_key_prefix": "quota"}
    _write_config(tmp_path, monkeypatch, cfg)
    load_config()  # the default value remains valid
