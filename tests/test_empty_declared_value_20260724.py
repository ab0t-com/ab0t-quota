"""A declared-but-EMPTY config string is 'unset', never 'declared as empty'.

Found 2026-07-24 while wiring a consumer to `"redis_url": "${QUOTA_REDIS_URL}"`.

`config.py` resolves an unset `${QUOTA_VAR}` with no inline default to `""`, and
`resolve._env_lookup` already documents "Empty string == unset (matches
`${VAR:-default}` interpolation semantics)". But `_walk` returned
`("declared", "")`, so the two halves of the same resolver disagreed: the config
side accepted the empty string as a real declaration and handed it to the redis
constructor.

The observable symptom was `ValueError: Redis URL must specify one of the
following schemes (redis://, rediss://, unix://)` — a raw redis-py parse error
instead of a typed QUOTA-CFG error naming `storage.redis_url`. That is precisely
the unhelpful-diagnosis class the declared-not-discovered programme exists to
remove, and it fires in the most likely real-world case: the env var simply not
being set in one environment.

Scope: STRINGS only. Decision O3 — an explicit empty *collection* is a real
declaration (`tiers: []` means "zero tiers") — is deliberately left intact and is
covered by its own tests.
"""

import json
import os
import pathlib
import tempfile

import pytest
from fastapi import FastAPI

MIN_TIERS = [{"tier_id": "free", "display_name": "Free", "sort_order": 1,
              "limits": {}, "features": []}]


def _write(cfg, monkeypatch):
    d = tempfile.mkdtemp()
    p = pathlib.Path(d) / "quota-config.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.setenv("QUOTA_CONFIG_PATH", str(p))
    return str(p)


@pytest.fixture
def clean_env(monkeypatch):
    for k in ("QUOTA_REDIS_URL", "REDIS_URL", "REDIS_PASSWORD"):
        monkeypatch.delenv(k, raising=False)


@pytest.mark.parametrize("empty", ["", "   "])
def test_empty_declared_redis_url_is_a_typed_config_error(empty, monkeypatch, clean_env):
    """RED pre-fix: raised redis-py's ValueError about URL schemes."""
    from ab0t_quota import setup_quota
    _write({"service_name": "t", "engine_mode": "byo_redis",
            "storage": {"redis_url": empty}, "tiers": MIN_TIERS}, monkeypatch)

    with pytest.raises(Exception) as ei:
        setup_quota(FastAPI())

    msg = str(ei.value)
    assert type(ei.value).__name__ == "QuotaConfigError", \
        f"expected QuotaConfigError, got {type(ei.value).__name__}: {msg[:200]}"
    assert "storage.redis_url" in msg
    assert "QUOTA_REDIS_URL" in msg          # remedy names the namespaced env var
    assert "scheme" not in msg.lower()       # never redis-py's parse error


def test_uninterpolated_env_reference_behaves_as_unset(monkeypatch, clean_env):
    """The real-world shape: `${QUOTA_REDIS_URL}` declared, variable not set.

    config.py interpolates it to "", which must read as unset — not as a
    declaration of the empty string.
    """
    from ab0t_quota import setup_quota
    _write({"service_name": "t", "engine_mode": "byo_redis",
            "storage": {"redis_url": "${QUOTA_REDIS_URL}"}, "tiers": MIN_TIERS},
           monkeypatch)

    with pytest.raises(Exception) as ei:
        setup_quota(FastAPI())
    assert type(ei.value).__name__ == "QuotaConfigError"
    assert "storage.redis_url" in str(ei.value)


def test_env_still_wins_over_an_empty_declaration(monkeypatch, clean_env):
    """Empty config value must fall THROUGH to the namespaced env var, not
    short-circuit the precedence chain."""
    from ab0t_quota.resolve import resolve_dependencies
    monkeypatch.setenv("QUOTA_REDIS_URL", "redis://declared-host:6379/4")
    r = resolve_dependencies({"storage": {"redis_url": ""}, "tiers": MIN_TIERS})["redis_url"]
    assert r.value == "redis://declared-host:6379/4"
    assert r.source == "env:QUOTA_REDIS_URL"


def test_a_real_declared_value_is_untouched(monkeypatch, clean_env):
    """Negative control: the fix must not alter normal resolution."""
    from ab0t_quota.resolve import resolve_dependencies
    r = resolve_dependencies({"storage": {"redis_url": "redis://h:6379/4"}, "tiers": MIN_TIERS})["redis_url"]
    assert r.value == "redis://h:6379/4"
    assert r.source == "config:storage.redis_url"


# ---------------------------------------------------------------------------
# D-10 false positive: a password declared INSIDE the URL is still declared
# ---------------------------------------------------------------------------

def test_url_embedded_password_silences_the_generic_redis_password_error(monkeypatch, caplog):
    """The affected consumer's exact shape: password embedded in the declared
    URL (`redis://:pw@host:6379/4`) AND a generic REDIS_PASSWORD in the env.

    Nothing is harvested and nothing is lost, so the D-10 line must not fire at
    ERROR. RED pre-fix: logged 'the Redis password is no longer harvested'.
    """
    import logging as _logging
    from ab0t_quota.resolve import check_deprecated_generic_env

    monkeypatch.setenv("REDIS_PASSWORD", "unused-generic")
    monkeypatch.delenv("QUOTA_REDIS_PASSWORD", raising=False)
    cfg = {"storage": {"redis_url": "redis://:realpw@shared-redis:6379/4"}}

    with caplog.at_level(_logging.DEBUG):
        check_deprecated_generic_env(cfg)

    errors = [r for r in caplog.records if r.levelno >= _logging.ERROR]
    assert not errors, f"expected no ERROR, got: {[r.getMessage()[:120] for r in errors]}"
    assert "realpw" not in caplog.text          # never leak the password


def test_generic_redis_password_still_errors_when_url_has_none(monkeypatch, caplog):
    """Negative control — the D-10 protection itself must stay armed."""
    import logging as _logging
    from ab0t_quota.resolve import check_deprecated_generic_env

    monkeypatch.setenv("REDIS_PASSWORD", "generic")
    monkeypatch.delenv("QUOTA_REDIS_PASSWORD", raising=False)
    monkeypatch.delenv("QUOTA_REDIS_URL", raising=False)

    with caplog.at_level(_logging.DEBUG):
        check_deprecated_generic_env({"storage": {"redis_url": "redis://host:6379/4"}})

    assert [r for r in caplog.records if r.levelno >= _logging.ERROR], \
        "D-10 must still fire when the URL carries no password"
