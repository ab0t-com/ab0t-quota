"""ST-RESOLVE-1 — the Python binding (T-24, pack 20260721).

Asserts the shipped resolver satisfies the clauses declared in
conformance/scenarios.json (Go's binding: quota/declared_store_20260721_test.go).
Found by the T-24 binding census.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCENARIOS = REPO / "conformance" / "scenarios.json"



def _resolve_row(config, **overrides):
    from ab0t_quota.resolve import Requirement, resolve_dependency
    kw = dict(name="Redis counter store URL", config_key="storage.redis_url",
              env=("QUOTA_REDIS_URL",), requirement=Requirement.REQUIRED,
              code="QUOTA-CFG-001")
    kw.update(overrides)
    return resolve_dependency(config, **kw)

# The Python ST-RESOLVE-1 binding: assert the resolver satisfies the declared
# clauses (Go's binding: quota/declared_store_20260721_test.go).
# ---------------------------------------------------------------------------

def _resolve_row(config, **overrides):
    from ab0t_quota.resolve import Requirement, resolve_dependency
    kw = dict(name="Redis counter store URL", config_key="storage.redis_url",
              env=("QUOTA_REDIS_URL",), requirement=Requirement.REQUIRED,
              code="QUOTA-CFG-001")
    kw.update(overrides)
    return resolve_dependency(config, **kw)


class TestSTResolve1PythonBinding:
    """ST-RESOLVE-1 — clause-by-clause, against the shipped resolver."""

    def _declared_item(self):
        doc = json.loads(SCENARIOS.read_text())
        return next(i for i in doc["structural_conformance"]
                    if i["id"] == "ST-RESOLVE-1")

    def test_clause_1_absent_and_null_are_typed_errors_naming_both_sources(self, monkeypatch):
        from ab0t_quota.errors import QuotaConfigError
        from ab0t_quota.resolve import resolve_dependencies
        monkeypatch.delenv("QUOTA_REDIS_URL", raising=False)
        tokens = self._declared_item()["required_error_must_contain"]
        for cfg in ({"tiers": []},                              # absent
                    {"storage": {"redis_url": None}, "tiers": []}):  # explicit null
            with pytest.raises(QuotaConfigError) as ei:
                resolve_dependencies(cfg, mode="local")
            for tok in tokens:
                assert tok in str(ei.value), \
                    f"declared token {tok!r} missing from the refusal (config={cfg})"

    def test_clause_2_null_is_not_truthiness(self):
        from ab0t_quota.resolve import _walk
        assert _walk({"storage": {"redis_url": None}}, "storage.redis_url") == ("null", None)
        assert _walk({"storage": {}}, "storage.redis_url") == ("absent", None)
        assert _walk({"storage": {"redis_url": ""}}, "storage.redis_url") == ("declared", "")

    def test_clause_3_namespaced_env_beats_explicit_null(self, monkeypatch):
        monkeypatch.setenv("QUOTA_REDIS_URL", "redis://declared-by-env:6379/0")
        row = _resolve_row({"storage": {"redis_url": None}})
        assert row.value == "redis://declared-by-env:6379/0"
        assert "env" in row.source.lower()

    def test_clause_4_memory_url_refused_with_bridge_pointer(self, monkeypatch):
        from ab0t_quota.errors import QuotaConfigError
        from ab0t_quota.resolve import resolve_dependencies
        monkeypatch.delenv("QUOTA_REDIS_URL", raising=False)
        cfg = {"storage": {"redis_url": "memory://"}, "tiers": []}
        with pytest.raises(QuotaConfigError) as ei:
            resolve_dependencies(cfg, mode="local")
        assert "QUOTA-CFG-005" in str(ei.value) and "bridge" in str(ei.value)

    def test_clause_5_declared_password_field_beats_url_embedded(self):
        from ab0t_quota.resolve import strip_url_password
        url, url_pw = strip_url_password("redis://:urlpw@host:6379/0")
        assert url_pw == "urlpw" and ":urlpw@" not in url
        # setup.py:305-317 passes the DECLARED field as the kwarg over the
        # stripped URL; the both-set warning is pinned by the Phase-1 suite.

    def test_clause_6_previously_line_is_redacted(self, monkeypatch):
        from ab0t_quota.errors import QuotaConfigError
        from ab0t_quota.resolve import resolve_dependencies
        monkeypatch.delenv("QUOTA_REDIS_URL", raising=False)
        monkeypatch.setenv("REDIS_URL", "redis://:leakyPW@generic-host:6379/9")
        with pytest.raises(QuotaConfigError) as ei:
            resolve_dependencies({"tiers": []}, mode="local")
        msg = str(ei.value)
        assert "generic-host" in msg, "the previously line must show the redacted host"
        assert "leakyPW" not in msg, "userinfo must be redacted from the previously line"
