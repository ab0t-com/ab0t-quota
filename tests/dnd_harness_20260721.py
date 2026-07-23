"""Shared harness for the declared-not-discovered suite (pack 20260721, T-1..T-3).

Not a test file. Provides:
  * ContactAttempted — raised by every interception layer.
  * DECOYS + decoy token list — the environment-pollution values.
  * PollutedEnv / install_pollution — decoy env + namespaced-twin clearing + leak-scan.
  * SeamRecorders / install_seam_recorders — record-then-raise client-constructor seams.
  * NoContactGuard / install_no_contact — socket-level guard (connect/getaddrinfo/
    create_connection) + uvloop refusal.

Design source: tickets/20260721_shared_lib_declared_not_discovered/
design_test_harness_20260721.md §2 (interception layers), §3 (pollution fixture).
Seam-patching traps honoured: boto3/aioboto3 are function-local imports in the
library, so we patch the module attributes (`boto3.client`, `aioboto3.Session`)
never `ab0t_quota.<mod>.boto3`; redis is patched on the CLASSMETHOD
`redis.asyncio.Redis.from_url` (the function form delegates to it — R-2).
"""
from __future__ import annotations

import asyncio
import os
import socket


class ContactAttempted(AssertionError):
    """An interception layer saw the library reach for infrastructure."""


# Values are syntactically valid for their slot and carry greppable tokens
# ("decoy" / "c4a1") so the leak-scan catches them at the USE site.
DECOYS = {
    "REDIS_URL":               "redis://decoy-redis.invalid:6399/9",
    "REDIS_PASSWORD":          "DECOY_PASSWORD_c4a1",
    "STRIPE_WEBHOOK_SECRET":   "whsec_DECOY_c4a1",
    "SNS_LIFECYCLE_TOPIC_ARN": "arn:aws:sns:decoy-region-9:000000000000:decoy-c4a1",
    "AUTH_SERVICE_URL":        "https://decoy-auth.invalid",
    # Carve-out (design §3): AWS_REGION is SDK contract. It is set as a decoy but
    # asserted differently — it must never appear in a library-RESOLVED declared
    # value; under the headline test it must never be reached at all.
    "AWS_REGION":              "decoy-region-9",
}

DECOY_TOKENS = (
    "decoy-redis.invalid", "DECOY_PASSWORD_c4a1", "whsec_DECOY_c4a1",
    "decoy-c4a1", "decoy-auth.invalid", "decoy-region-9",
)

# Namespaced twins that must be CLEARED so the generic name alone is on offer.
# Deliberately NOT cleared: the two suite-wide fakeredis assertions from
# tests/conftest.py (AB0T_QUOTA_REDIS_CLUSTER_CONFIRMED_DISABLED,
# AB0T_QUOTA_REDIS_DURABILITY_CONFIRMED) — documented operator assertions.
NAMESPACED_TWINS = (
    "QUOTA_REDIS_URL", "QUOTA_REDIS_PASSWORD",
    "AB0T_QUOTA_STRIPE_WEBHOOK_SECRET", "AB0T_MESH_SNS_LIFECYCLE_TOPIC_ARN",
    "AB0T_AUTH_AUTH_URL", "QUOTA_DYNAMODB_ENDPOINT", "DYNAMODB_ENDPOINT",
    "AB0T_MESH_API_KEY", "AB0T_SERVICE_NAME", "QUOTA_CONFIG_PATH",
)


class PollutedEnv:
    def __init__(self, decoys: dict):
        # Rule C: the fixture asserts the work it claims (NC-7 proves this can fail).
        assert len(decoys) == 6, f"pollution fixture expects 6 decoys, got {len(decoys)}"
        self.decoys = dict(decoys)
        self.decoy_count = len(decoys)

    def assert_no_decoy_leaked(self, *, recorded=(), caplog_text: str = "", app=None):
        """Sweep recorder args, caplog text, and app.state reprs for decoy tokens."""
        surfaces = []
        for call in recorded:
            surfaces.append(("seam-recorder", repr(call)))
        if caplog_text:
            surfaces.append(("caplog", caplog_text))
        if app is not None:
            state = getattr(getattr(app, "state", None), "_state", {}) or {}
            for key, val in state.items():
                surfaces.append((f"app.state.{key}", repr(val)))
        for token in DECOY_TOKENS:
            for where, text in surfaces:
                assert token not in text, f"decoy {token!r} leaked into {where}: {text[:200]}"


def install_pollution(monkeypatch, *, decoys: dict = DECOYS) -> PollutedEnv:
    for tok in DECOY_TOKENS:
        for k, v in os.environ.items():
            assert tok not in v, f"decoy token {tok!r} pre-exists in env {k} — dirty environment"
    for name, value in decoys.items():
        monkeypatch.setenv(name, value)
    for name in NAMESPACED_TWINS:
        monkeypatch.delenv(name, raising=False)
    return PollutedEnv(decoys)


class SeamRecorders:
    def __init__(self):
        self.all_calls: list[tuple[str, tuple, dict]] = []
        self.installed: set[str] = set()

    def record_and_raise(self, seam: str):
        def _hook(*args, **kwargs):
            self.all_calls.append((seam, args, kwargs))
            raise ContactAttempted(
                f"{seam} constructor reached with args={args!r} kwargs={kwargs!r}")
        return _hook

    def calls_for(self, seam: str):
        return [c for c in self.all_calls if c[0] == seam]


def install_seam_recorders(monkeypatch) -> SeamRecorders:
    import aioboto3
    import boto3
    import httpx
    import redis.asyncio

    rec = SeamRecorders()
    monkeypatch.setattr(redis.asyncio.Redis, "from_url",
                        classmethod(lambda cls, *a, **kw: rec.record_and_raise("redis")(*a, **kw)))
    monkeypatch.setattr(aioboto3, "Session", rec.record_and_raise("aioboto3"))
    monkeypatch.setattr(boto3, "client", rec.record_and_raise("boto3"))
    monkeypatch.setattr(httpx, "AsyncClient", rec.record_and_raise("httpx"))
    rec.installed = {"redis", "boto3", "aioboto3", "httpx"}
    return rec


class NoContactGuard:
    def __init__(self):
        self.layers = {"connect", "getaddrinfo", "create_connection"}


def install_no_contact(monkeypatch) -> NoContactGuard:
    # uvloop connects in C and would bypass the patched Python-level methods:
    # its presence makes this guard's silence meaningless, so refuse it loudly.
    loop_mod = ""
    try:
        loop_mod = type(asyncio.get_event_loop_policy()).__module__
    except Exception:
        pass
    assert "uvloop" not in loop_mod, "no_contact guard is void under uvloop"

    def _no_connect(self, addr):
        raise ContactAttempted(f"socket.connect -> {addr!r}")

    def _no_gai(host, *a, **kw):
        raise ContactAttempted(f"socket.getaddrinfo -> {host!r}")

    def _no_create(*a, **kw):
        raise ContactAttempted(f"socket.create_connection -> {a!r}")

    monkeypatch.setattr(socket.socket, "connect", _no_connect)
    monkeypatch.setattr(socket, "getaddrinfo", _no_gai)
    monkeypatch.setattr(socket, "create_connection", _no_create)
    return NoContactGuard()
