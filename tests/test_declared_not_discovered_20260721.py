"""DECLARED, NOT DISCOVERED — T-1/T-2/T-3 contract tests (pack 20260721).

RED-first per design_test_harness_20260721.md §1: every test here was written
BEFORE the resolver existed and fails on its ASSERTION against 0.6.2 behaviour
(proof-of-red blocks in the pack work_log). Rule A: no module-scope import of
any fix-side symbol — typed-error identity is asserted by __name__ and via
importlib, so a missing symbol is an assertion failure, not a collection error.

Seam registry (Rule B) — every GREEN asserts a PAIR (public-entry behaviour AND
old-seam recorder empty); no test asserts by calling the new resolver directly:

  seam                          | what the fix does to it        | GREEN asserts instead
  ------------------------------|--------------------------------|--------------------------------
  redis.asyncio.Redis.from_url  | unreached in undeclared cases  | QuotaConfigError + recorder empty
  boto3.client / aioboto3 / httpx| unreached before config errors| recorder empty
  engine `tiers or DEFAULT_TIERS`| deleted (explicit tiers)      | QuotaEngine(tiers=None) raises
  load_tiers DEFAULT_TIERS return| deleted (raises)              | setup_quota raises naming `tiers`
  load_config missing -> {}      | deleted (raises)              | load_config raises naming the path
"""
from __future__ import annotations

import ast
import importlib.util
import json
import logging
import os
import re
import re
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

CONSUMER_CONFIG = Path(__file__).parent / "data" / "consumer_sandbox_platform_quota_config_20260721.json"
# The fixture above is a self-contained test example: a full, realistic config of
# the shape that produced the incident. It is deliberately NOT tied to any
# checkout outside this repo — a library's test suite must not reach into a
# consumer's directory layout, and it must not go red because a consumer edited
# their own config. (Both happened: the old sync check hard-pathed
# `../../../resource/output/<consumer>/quota-config.json`, so the suite depended
# on a private repo being present at a fixed relative path AND on that consumer
# never changing it.)

LIB_DIR = Path(__file__).resolve().parents[1] / "ab0t_quota"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def polluted_env(monkeypatch):
    return install_pollution(monkeypatch)


@pytest.fixture
def seam_recorders(monkeypatch):
    return install_seam_recorders(monkeypatch)


@pytest.fixture
def no_contact(monkeypatch):
    return install_no_contact(monkeypatch)


def _write_config(tmp_path, monkeypatch, cfg: dict) -> str:
    p = tmp_path / "quota-config.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.setenv("QUOTA_CONFIG_PATH", str(p))
    return str(p)


def _consumer_config() -> dict:
    return json.loads(CONSUMER_CONFIG.read_text())


def _consumer_config_at_incident() -> dict:
    """The consumer's config **in the shape that caused the incident**: every
    real field of theirs, with `storage.redis_url` forced back to `null`.

    Why this exists (2026-07-24): the gates below originally read the consumer's
    live config directly, which silently assumed the consumer would stay broken.
    When they fixed it — declaring `${QUOTA_REDIS_URL:-…}` instead of `null` —
    three gates went red even though the library was correct. **A regression test
    that depends on a customer remaining broken stops testing the defect the
    moment they recover, which is exactly when you most want it still armed.**

    So: keep sourcing every other field from their real config (that is what
    made these gates trustworthy), and pin only the one field the incident was
    about. The byte-for-byte sync check against their live file stays separate
    and unchanged — it tracks drift; this pins the defect.
    """
    cfg = _consumer_config()
    cfg.setdefault("storage", {})["redis_url"] = None
    return cfg


MINIMAL_TIERS = [{
    "tier_id": "free", "display_name": "Free", "sort_order": 1,
    "limits": {"thing.concurrent": 5}, "features": [],
}]


# ---------------------------------------------------------------------------
# T-1 / T-2 — the headline and the ENV-01/02 family
# ---------------------------------------------------------------------------

def test_consumer_config_fixture_has_the_incident_shape():
    """The fixture must keep the structural properties the gates below rely on.

    Replaces the old byte-for-byte sync check against a consumer's live file
    (design §2.4). That check tied this suite to a directory outside the repo and
    went red when the consumer FIXED their config — i.e. it asserted that someone
    else stayed broken. What the gates actually need is the *shape*, which is
    what this pins.
    """
    cfg = _consumer_config()
    assert isinstance(cfg.get("storage"), dict), "fixture must declare a storage block"
    assert cfg.get("tiers"), "fixture must declare tiers (the gates load them)"
    assert cfg.get("resources"), "fixture must declare resources"
    # The incident shape is applied by _consumer_config_at_incident(), not baked
    # into the fixture — so the fixture stays a valid, bootable example.
    assert _consumer_config_at_incident()["storage"]["redis_url"] is None


def test_typed_error_is_exported():
    """Rule A form: a missing fix-side symbol is an assertion failure."""
    spec = importlib.util.find_spec("ab0t_quota.errors")
    assert spec is not None, "fix must ship ab0t_quota.errors"
    mod = importlib.import_module("ab0t_quota.errors")
    assert hasattr(mod, "QuotaConfigError"), "fix must export QuotaConfigError"


def test_null_redis_url_is_a_config_error(tmp_path, monkeypatch, polluted_env,
                                          no_contact, seam_recorders, caplog):
    """THE HEADLINE GATE (Gate B): the affected consumer's exact config, in an
    environment polluted with generic decoys, produces ONE config error naming
    storage.redis_url — and contacts NOTHING."""
    from ab0t_quota import setup_quota
    _write_config(tmp_path, monkeypatch, _consumer_config_at_incident())

    app = FastAPI()
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(Exception) as ei:
            setup_quota(app)  # consumer defaults — not enable_paid=False
    msg = str(ei.value)
    # (1) ONE error, the right one, correctly aimed:
    assert type(ei.value).__name__ == "QuotaConfigError", \
        f"expected QuotaConfigError, got {type(ei.value).__name__}: {msg[:300]}"
    assert "storage.redis_url" in msg
    assert "QUOTA_REDIS_URL" in msg               # remedy names the namespaced env var
    assert not isinstance(ei.value, ContactAttempted)
    assert "cluster" not in msg.lower()           # never the GATE-01 misdiagnosis
    # (2) contacted NOTHING:
    assert seam_recorders.all_calls == []
    # (3) the decoy password leaked nowhere — not even into the error text:
    assert DECOYS["REDIS_PASSWORD"] not in msg
    polluted_env.assert_no_decoy_leaked(
        recorded=seam_recorders.all_calls, caplog_text=caplog.text, app=app)
    # (4) rule C — the harness did the work it claims:
    assert polluted_env.decoy_count == 6
    assert seam_recorders.installed == {"redis", "boto3", "aioboto3", "httpx"}
    assert no_contact.layers == {"connect", "getaddrinfo", "create_connection"}


def test_absent_redis_url_is_a_config_error_not_localhost(tmp_path, monkeypatch,
                                                          no_contact, seam_recorders):
    """ENV-01's second branch: key ABSENT (not null), no env at all — must be
    the same config error, never the invented redis://localhost:6379/0."""
    from ab0t_quota import setup_quota
    cfg = _consumer_config()
    del cfg["storage"]["redis_url"]
    for name in ("QUOTA_REDIS_URL", "REDIS_URL", "REDIS_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    _write_config(tmp_path, monkeypatch, cfg)

    app = FastAPI()
    with pytest.raises(Exception) as ei:
        setup_quota(app)
    assert type(ei.value).__name__ == "QuotaConfigError", \
        f"expected QuotaConfigError, got {type(ei.value).__name__}: {str(ei.value)[:300]}"
    assert "storage.redis_url" in str(ei.value)
    assert seam_recorders.all_calls == [], \
        f"a client was constructed on an undeclared store: {seam_recorders.all_calls}"


def test_password_not_harvested_from_generic_env(tmp_path, monkeypatch, polluted_env,
                                                 no_contact, seam_recorders):
    """ENV-02: with the URL declared via namespaced env (which BEATS explicit
    null — the decided precedence) and only the generic REDIS_PASSWORD around,
    the connection must carry NO password."""
    from ab0t_quota import setup_quota
    monkeypatch.setenv("QUOTA_REDIS_URL", "redis://declared-host.test:6379/0")
    _write_config(tmp_path, monkeypatch, _consumer_config_at_incident())  # redis_url: null

    app = FastAPI()
    with pytest.raises(Exception):
        setup_quota(app)  # recorder raises at from_url — the recorded call is the evidence
    calls = seam_recorders.calls_for("redis")
    assert len(calls) == 1, f"expected exactly one from_url construction, got {calls}"
    _, args, kwargs = calls[0]
    assert args and args[0] == "redis://declared-host.test:6379/0", \
        "env-beats-null precedence: the declared namespaced URL must win"
    assert kwargs.get("password") in (None, ""), \
        f"generic REDIS_PASSWORD was harvested into the connection: {kwargs.get('password')!r}"
    assert DECOYS["REDIS_PASSWORD"] not in repr(calls)


def test_declared_url_with_declared_password_precedence(tmp_path, monkeypatch,
                                                        no_contact, caplog):
    """D-5(a): the separately-declared storage.redis_password BEATS the
    URL-embedded one — asserted on the EFFECTIVE connection kwargs (the real
    from_url merge), not on our own call arguments."""
    import redis.asyncio as _ra
    from ab0t_quota import setup_quota

    effective = {}
    real_from_url = _ra.Redis.from_url.__func__

    def spy(cls, url, **kwargs):
        client = real_from_url(cls, url, **kwargs)
        effective["url"] = url
        effective["password"] = client.connection_pool.connection_kwargs.get("password")
        return client

    monkeypatch.setattr(_ra.Redis, "from_url", classmethod(spy))
    for name in ("QUOTA_REDIS_URL", "REDIS_URL", "QUOTA_REDIS_PASSWORD", "REDIS_PASSWORD"):
        monkeypatch.delenv(name, raising=False)

    cfg = _consumer_config()
    cfg["storage"]["redis_url"] = "redis://:urlpass@declared-host.test:6379/0"
    cfg["storage"]["redis_password"] = "fieldpass"
    cfg["storage"]["persistence_enabled"] = False
    cfg["tier_provider"] = {"type": "static", "default_tier": "free"}
    _write_config(tmp_path, monkeypatch, cfg)

    app = FastAPI()
    with caplog.at_level(logging.WARNING):
        setup_quota(app, enable_paid=False)  # sync phase only; lazy client, no I/O
    assert effective.get("password") == "fieldpass", \
        f"URL-embedded password won over the declared field: {effective!r}"
    # the mismatch is exactly ENV-02's assembled-pair hazard — it must be loud:
    assert any("password" in r.message.lower() for r in caplog.records), \
        "both-set-and-differing must log a warning naming the two sources"


def test_required_loops_agrees_with_resolved_store(monkeypatch):
    """ENV-09: the health contract must read the SAME resolution the connection
    was built from. A generic REDIS_URL decoy must not flip the requirement."""
    from ab0t_quota.setup import required_money_loops
    monkeypatch.delenv("QUOTA_REDIS_URL", raising=False)
    monkeypatch.setenv("REDIS_URL", DECOYS["REDIS_URL"])

    # undeclared store + generic decoy: the decoy must NOT create a requirement
    req = required_money_loops({"storage": {"redis_url": None}}, enable_paid=False)
    assert "preflight_reverification" not in req, \
        "generic REDIS_URL was harvested into the health contract (ENV-09)"

    # declared store: requirement present (the control that proves the test can pass)
    req = required_money_loops({"storage": {"redis_url": "redis://x/0"}}, enable_paid=False)
    assert "preflight_reverification" in req


# ---------------------------------------------------------------------------
# T-3 — never invent a tier catalog (ENV-03 / ENV-12 / ENV-13 / ENV-15)
# ---------------------------------------------------------------------------

def test_missing_config_file_is_fatal(tmp_path, monkeypatch):
    """ENV-03(c): a bad QUOTA_CONFIG_PATH must be a config error, not {}+warn."""
    from ab0t_quota import load_config
    monkeypatch.setenv("QUOTA_CONFIG_PATH", str(tmp_path / "nope" / "quota-config.json"))
    with pytest.raises(Exception) as ei:
        load_config()
    assert type(ei.value).__name__ == "QuotaConfigError", \
        f"missing config file must be fatal; got {type(ei.value).__name__}"
    assert "QUOTA_CONFIG_PATH" in str(ei.value) or "quota-config.json" in str(ei.value)


def test_no_config_file_found_is_fatal(tmp_path, monkeypatch):
    """ENV-03(c), search-path branch: no file anywhere ⇒ error, not silent {}."""
    import ab0t_quota.config as cfgmod
    from ab0t_quota import load_config
    monkeypatch.delenv("QUOTA_CONFIG_PATH", raising=False)
    monkeypatch.setattr(cfgmod, "CONFIG_SEARCH_PATHS",
                        [str(tmp_path / "absent-a.json"), str(tmp_path / "absent-b.json")])
    with pytest.raises(Exception) as ei:
        load_config()
    assert type(ei.value).__name__ == "QuotaConfigError"


def test_missing_tiers_key_is_fatal(tmp_path, monkeypatch, no_contact):
    """ENV-13: a PRESENT, VALID config with no `tiers` key must refuse — the
    operator believes their file is authoritative; DEFAULT_TIERS must not bind."""
    from ab0t_quota import setup_quota
    cfg = _consumer_config()
    del cfg["tiers"]
    cfg["storage"]["redis_url"] = "redis://declared-host.test:6379/0"
    cfg["storage"]["persistence_enabled"] = False
    cfg["tier_provider"] = {"type": "static", "default_tier": "free"}
    for name in ("QUOTA_REDIS_URL", "REDIS_URL"):
        monkeypatch.delenv(name, raising=False)
    _write_config(tmp_path, monkeypatch, cfg)

    app = FastAPI()
    with pytest.raises(Exception) as ei:
        setup_quota(app, enable_paid=False)
    assert type(ei.value).__name__ == "QuotaConfigError", \
        f"tier-less config must refuse, got {type(ei.value).__name__}"
    assert "tiers" in str(ei.value)


def test_engine_requires_explicit_tiers():
    """ENV-12: the second invented-catalog path — QuotaEngine(tiers=None) must
    raise; an explicit empty catalog ({}) is honoured (O3 ruling)."""
    import fakeredis.aioredis
    from ab0t_quota.engine import QuotaEngine
    from ab0t_quota.providers import StaticTierProvider
    from ab0t_quota.registry import ResourceRegistry

    r = fakeredis.aioredis.FakeRedis()
    provider = StaticTierProvider({"org-1": "free"})
    with pytest.raises(Exception) as ei:
        QuotaEngine(redis=r, tier_provider=provider, registry=ResourceRegistry(), tiers=None)
    assert type(ei.value).__name__ == "QuotaConfigError", \
        f"tiers=None must raise QuotaConfigError, got {type(ei.value).__name__}"
    assert "tiers" in str(ei.value)

    # explicit empty catalog is a DECLARATION and must be honoured, not "improved"
    QuotaEngine(redis=r, tier_provider=provider, registry=ResourceRegistry(), tiers={})


def test_partial_config_never_publishes_invented_catalog(tmp_path, monkeypatch,
                                                         no_contact, seam_recorders):
    """ENV-15: service_name present, tiers absent — the config error must fire
    in the SYNC phase and nothing may construct an httpx client (the catalog
    PUT to production billing)."""
    from ab0t_quota import setup_quota
    cfg = _consumer_config()
    del cfg["tiers"]
    cfg["storage"]["redis_url"] = "redis://declared-host.test:6379/0"
    cfg["storage"]["persistence_enabled"] = False
    cfg["tier_provider"] = {"type": "static", "default_tier": "free"}
    assert cfg.get("service_name")  # the ENV-15 amplifier is present
    monkeypatch.setenv("AB0T_MESH_API_KEY", "test-key")
    for name in ("QUOTA_REDIS_URL", "REDIS_URL"):
        monkeypatch.delenv(name, raising=False)
    _write_config(tmp_path, monkeypatch, cfg)

    app = FastAPI()
    with pytest.raises(Exception) as ei:
        setup_quota(app)  # enable_paid default True — the dangerous variant
    assert type(ei.value).__name__ == "QuotaConfigError"
    assert seam_recorders.calls_for("httpx") == [], \
        "an invented catalog reached an outbound client construction"


def test_default_tiers_unreachable_census():
    """AST census (grep is line-granular and blind to reference-vs-definition):
    DEFAULT_TIERS may be referenced only from tiers.py (definition) and
    __init__.py (the documented explicit opt-in export)."""
    allowed = {"tiers.py", "__init__.py"}
    offenders = []
    for path in sorted(LIB_DIR.rglob("*.py")):
        rel = path.relative_to(LIB_DIR).as_posix()
        if rel in allowed:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "DEFAULT_TIERS":
                offenders.append(f"{rel}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom) and any(
                    a.name == "DEFAULT_TIERS" for a in (node.names or [])):
                offenders.append(f"{rel}:{node.lineno} (import)")
            elif isinstance(node, ast.Attribute) and node.attr == "DEFAULT_TIERS":
                offenders.append(f"{rel}:{node.lineno} (attr)")
    assert offenders == [], \
        f"DEFAULT_TIERS reachable from production code: {offenders}"


# ---------------------------------------------------------------------------
# GATE-04 (structural, via T-1) — an undeclared store never reaches a gate
# ---------------------------------------------------------------------------

def test_undeclared_store_never_reaches_gates(tmp_path, monkeypatch, seam_recorders):
    """The gate family must be UNREACHABLE without a declared store (the
    refusal precedes it), and REACHED with one (count >= 1 — guards a vacuous
    green). Declared leg runs in test_setup.py's fakeredis pattern already —
    here we assert the gate recorder stays empty on the undeclared leg."""
    import ab0t_quota.setup as setup_mod
    from ab0t_quota import setup_quota

    gate_calls = []
    real_gate = setup_mod._gate_redis_topology

    async def recording_gate(app, redis, config):
        gate_calls.append("topology")
        return await real_gate(app, redis, config)

    monkeypatch.setattr(setup_mod, "_gate_redis_topology", recording_gate)
    for name in ("QUOTA_REDIS_URL", "REDIS_URL"):
        monkeypatch.delenv(name, raising=False)
    _write_config(tmp_path, monkeypatch, _consumer_config_at_incident())  # redis_url: null

    app = FastAPI()
    with pytest.raises(Exception) as ei:
        setup_quota(app)
        # if setup returns (today's behaviour) the lifespan must also be driven,
        # else the gate never runs and this red would be vacuous:
        from fastapi.testclient import TestClient
        with TestClient(app):
            pass
    assert type(ei.value).__name__ == "QuotaConfigError", \
        f"undeclared store must be a config refusal, got {type(ei.value).__name__}"
    assert gate_calls == [], "a gate interrogated infrastructure nobody declared"
    assert seam_recorders.all_calls == []


# ---------------------------------------------------------------------------
# D-CK-5 (ticket 20260722_end_customer_experience_defects) — THE PERMANENT
# CONTROL: "the config is king; everything else is an example, and examples
# never leak."
#
# Extends the DEFAULT_TIERS census above from ONE forbidden symbol to the
# CLASS it belongs to: no consumer-specific IDENTIFIER — resource key, tier
# name, or service name — may appear in library LOGIC. Examples, docs and
# opt-in exports are fine WHERE THEY ARE; reachability from production code is
# the test.
#
# This is the control that would have caught `messages.py`'s ACTION_HINTS (a
# copy table keyed on sandbox-platform's resource keys) and UPGRADE_TIER_MAP
# (a fixed free -> Starter -> Pro -> Enterprise ladder). Both shipped behind
# TODOs admitting them, dated 2026-05-16, and survived because every example
# and test AGREED with the hardcode.
# ---------------------------------------------------------------------------

#: Resource keys look like `<domain>.<name>` — ResourceDef.resource_key's own
#: pattern. Any such literal in library logic names SOMEBODY's resource.
_RESOURCE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]{2,}$")
#: Service names as they appear in this mesh.
_SERVICE_NAME_RE = re.compile(r"^[a-z][a-z0-9]*-(platform|service)$")
_FILE_LIKE_RE = re.compile(r"\.(html|json|py|md|txt|js|css|lua|yaml|yml)$")

#: (file, namespace) -> why THAT file may say `<namespace>.something`.
#: The pairing is the point: `resource.started` is a mesh auth EVENT in
#: auth_events, and a resource KEY in messages.py. Same shape, opposite
#: verdicts — so the allowlist is scoped to the file that owns the vocabulary,
#: never global. SHRINK-ONLY (D-14 rule 3): every row is a finding you have
#: agreed to look at again.
_LIBRARY_VOCABULARY: dict[str, set[str]] = {
    # config-document paths, named by the resolver / provisioner / migrator
    "resolve.py": {"storage", "outbox", "auth"},
    "setup.py": {"storage", "auth"},
    "provision.py": {"storage", "outbox", "activations"},
    "keyspace_migration.py": {"storage"},
    # mesh permission scopes, mesh auth-EVENT types, Stripe protocol events
    "billing/auth_helpers.py": {"billing", "costs"},
    "billing/router.py": {"invoice"},
    "billing/config.py": {"resource"},
    "billing/lifecycle.py": {"resource"},
}

#: OPEN leaks of this exact class, recorded rather than hidden. The census is
#: GREEN with these and RED with anything new — so a known finding cannot be
#: used to silence an unknown one. Each names its owner.
KNOWN_LEAKS: dict[str, str] = {
    # FIXED 2026-07-22 by Lane BILL2 while this control was being written:
    # `billing/clients.py`'s `tool_id: str = "sandbox-platform"` default is
    # gone (`_resolve_tool_id`), and this census went RED on the stale row —
    # which is the register behaving correctly in the direction nobody tests.
    "setup.py: resource key 'api.requests_per_hour'":
        "`setup_quota(rate_limit_resource=...)` defaults to one consumer's "
        "resource key. Documented + overridable, and loud rather than silent "
        "(an undeclared resource is not enforced), but it is still the library "
        "naming a key only config should supply. Successor: default to None "
        "and require the consumer to name their own rate-limit resource.",
    "middleware.py: resource key 'api.requests_per_hour'":
        "same default, at the middleware's own signature. Same successor.",
}


def _documentation_string_ids(tree: ast.AST) -> set[int]:
    """String literals that are PROSE, not values.

    Docstrings and `Field(description=...)` may legitimately SAY
    "sandbox.concurrent" as an example — that is the principle working, not
    failing. A literal anywhere else is a value the library computes with.
    """
    out = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)) and body and \
                isinstance(body[0], ast.Expr) and \
                isinstance(body[0].value, ast.Constant) and \
                isinstance(body[0].value.value, str):
            out.add(id(body[0].value))
        if isinstance(node, ast.Call):
            # documentation-by-contract keywords
            for kw in node.keywords:
                if kw.arg in ("description", "remedy", "docs_anchor", "help",
                              "state", "summary", "title", "example") and \
                        isinstance(kw.value, ast.Constant):
                    out.add(id(kw.value))
            # logger names are module paths, not identifiers of anyone's world
            fn = node.func
            if getattr(fn, "attr", None) == "getLogger" or \
                    getattr(fn, "id", None) == "getLogger":
                for a in node.args:
                    if isinstance(a, ast.Constant):
                        out.add(id(a))
    return out


def consumer_identifier_violations(source: str, filename: str = "<mem>") -> list[str]:
    """Consumer-specific identifiers reachable from library logic.

    STATED LIMITS (D-14 rule 4) — what this instrument CANNOT see:
      1. An identifier assembled at runtime (`f"{domain}.concurrent"`, or
         concatenation through variables) is invisible: only literal string
         constants are censused. The behavioural suites cover those forms.
      2. A tier name reached through a documented, overridable knob default
         (`default_tier="free"` on the providers) is out of field of view —
         the detector requires the literal to be indexed INTO a tier catalog.
         That failure is loud (an unknown tier is refused at the engine), not
         silent. Recorded so it is a decision, not an oversight.
      3. Prose is exempt by construction. An example inside a docstring or a
         `Field(description=...)` never trips this — that IS the principle:
         examples are fine where they are.
      4. It guards THIS library only. A consumer of the library can still
         import what this forbids — billing's `service.py` imports
         `DEFAULT_TIERS` today (ticket §5c-1, D-CK-4). That boundary needs its
         own control inside the consuming service; a census cannot chase a
         symbol across a repository line.
      5. `_LIBRARY_VOCABULARY` is scoped per FILE, so moving protocol-shaped
         copy into a new module reddens it — deliberately: that move is when
         a leak usually happens.
    """
    tree = ast.parse(source)
    allowed = _LIBRARY_VOCABULARY.get(filename, set())
    doc_ids = _documentation_string_ids(tree)
    out = []

    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in doc_ids:
            continue
        value = node.value
        if _FILE_LIKE_RE.search(value):
            continue
        if _RESOURCE_KEY_RE.match(value):
            if value.split(".")[0] in allowed:
                continue
            out.append(f"{filename}:{node.lineno} resource key {value!r}")
        elif _SERVICE_NAME_RE.match(value):
            out.append(f"{filename}:{node.lineno} service name {value!r}")

    # A tier CATALOG indexed by a literal tier name: the UPGRADE_TIER_MAP
    # shape, and engine.py's old `self._tiers.get(tier_id, self._tiers.get("free"))`.
    for node in ast.walk(tree):
        target = key = None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "get" and node.args:
            target, key = node.func.value, node.args[0]
        elif isinstance(node, ast.Subscript):
            target, key = node.value, node.slice
        if target is None or not isinstance(key, ast.Constant) \
                or not isinstance(key.value, str):
            continue
        name = (getattr(target, "attr", None) or getattr(target, "id", "") or "")
        if re.search(r"(^|_)tiers$|tier_map|tier_ladder|tier_catalog",
                     name, re.IGNORECASE) and key.value.isidentifier():
            out.append(f"{filename}:{node.lineno} tier name {key.value!r} "
                       f"indexed into {name}")
    return sorted(set(out))


def _census_library() -> list[str]:
    offenders = []
    for path in sorted(LIB_DIR.rglob("*.py")):
        rel = path.relative_to(LIB_DIR).as_posix()
        if rel in ("registry.py", "tiers.py"):
            # Opt-in exports, proven unreachable from library logic — SANDBOX_RESOURCES
            # (ticket §5c: "EXAMPLE, fine") and DEFAULT_TIERS (census above).
            continue
        offenders += consumer_identifier_violations(path.read_text(), rel)
    return offenders


def test_no_consumer_identifier_in_library_logic():
    """D-CK-5: the principle, made testable."""
    unknown = [o for o in _census_library()
               if not any(k.split(": ", 1)[0] == o.split(":")[0]
                          and k.split(": ", 1)[1] in o for k in KNOWN_LEAKS)]
    assert unknown == [], (
        "consumer-specific identifiers reachable from library logic — the "
        "config is king, and these name ONE consumer's world:\n  "
        + "\n  ".join(unknown))


def test_known_leaks_are_still_real_and_shrink_only():
    """D-14 rule 3: a recorded finding is a finding. It may be FIXED (this test
    tells you to delete the row) but never quietly extended."""
    census = _census_library()
    for leak in KNOWN_LEAKS:
        f, what = leak.split(": ", 1)
        assert any(o.startswith(f + ":") and what in o for o in census), \
            f"KNOWN_LEAKS row is stale — fixed? delete it: {leak}"
    assert len(KNOWN_LEAKS) == 2, \
        "the recorded-leak register grew; a new leak must be fixed, not recorded"


def test_library_vocabulary_exemptions_shrink_only():
    for rel in _LIBRARY_VOCABULARY:
        assert (LIB_DIR / rel).exists(), f"stale vocabulary exemption: {rel}"
    assert sum(len(v) for v in _LIBRARY_VOCABULARY.values()) == 14, \
        "the per-file vocabulary allowlist changed size — justify it explicitly"


def test_every_vocabulary_exemption_is_load_bearing():
    """An allowlist entry that nothing needs is dead weight that silently
    widens the census's blind spot. Removing ANY single namespace must turn
    the sweep red — so the list is exactly as wide as the facts require."""
    for rel, namespaces in _LIBRARY_VOCABULARY.items():
        for ns in namespaces:
            _LIBRARY_VOCABULARY[rel] = namespaces - {ns}
            try:
                still_clean = not any(
                    o.startswith(rel + ":") for o in
                    consumer_identifier_violations(
                        (LIB_DIR / rel).read_text(), rel))
            finally:
                _LIBRARY_VOCABULARY[rel] = namespaces
            assert not still_clean, \
                f"unnecessary exemption {rel} -> {ns!r}: remove it"


@pytest.mark.parametrize("plant,label", [
    ('HINTS = {"sandbox.concurrent": "Stop an existing sandbox."}\n',
     "the ACTION_HINTS form — a copy table keyed on a resource key"),
    ('def f(rk):\n    if rk == "sandbox.gpu_instances":\n        return 1\n',
     "a resource key as a branch condition"),
    ('LADDER = {"free": {"_default": "Starter"}}\nx = tiers.get("free")\n',
     "the UPGRADE_TIER_MAP form — a tier catalog indexed by a tier name"),
    ('DEFAULT_SERVICE = "acme-platform"\n',
     "a consumer service name as a value"),
    ('def g(t):\n    return self._tiers.get(t, self._tiers.get("free"))\n',
     "engine.py's old lowest-tier fallback"),
    ('COPY = {"resource.cpu_cores": "Scale down an allocation."}\n',
     "a resource key whose namespace is protocol vocabulary ELSEWHERE"),
])
def test_consumer_id_census_catches_planted_offenders(plant, label):
    """D-14 rule 2: plants span FORMS, not instances."""
    assert consumer_identifier_violations(plant, "planted.py") != [], \
        f"census blind to {label}"


def test_planted_offender_in_a_real_logic_path_reddens_the_sweep():
    """D-14 rule 1: the sweep itself must be shown RED-capable, not just the
    detector. Plants `sandbox.concurrent` into messages.py — the exact file
    and the exact form this control exists for — in memory."""
    src = (LIB_DIR / "messages.py").read_text()
    planted = src.replace(
        "DEFAULT_TEMPLATES = Templates()",
        'DEFAULT_TEMPLATES = Templates()\n_HINTS = {"sandbox.concurrent": "Stop a sandbox."}')
    assert planted != src, "plant did not apply — anchor moved"
    assert consumer_identifier_violations(planted, "messages.py") != [], \
        "a sandbox resource key planted in messages.py did not redden the census"
    # and the real file is clean
    assert consumer_identifier_violations(src, "messages.py") == []


@pytest.mark.parametrize("clean", [
    '"""Example: sandbox.concurrent is a gauge."""\n',
    'x = Field(default=None, description="e.g. sandbox.concurrent")\n',
    'KEY = "quota:v2:"\nURL = "https://billing.service.ab0t.com"\n',
    'cfg.get("tiers")\nd["resources"]\n',
    'log = logging.getLogger("ab0t_quota.messages")\n',
    'tier_cfg.get("default_tier")\n',
])
def test_consumer_id_census_does_not_flag_prose_or_protocol(clean):
    """Negative control: prose, protocol strings, logger names and generic
    config keys are NOT consumer identifiers. A census that reddens on those
    is unusable, and an unusable census gets silenced."""
    assert consumer_identifier_violations(clean, "clean.py") == []
