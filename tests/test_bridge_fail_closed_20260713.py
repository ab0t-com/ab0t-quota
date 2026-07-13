"""D5 (ticket 20260712_payment_credit_calls_404) — bridge fails CLOSED by default.

A billing outage must NEVER admit unbilled usage (never lose a tenant's usage/
payment). Bridge outage fallbacks previously returned decision="allow" (fail-open).
Now: fail-CLOSED (deny) by default, opt-in fail-open via AB0T_QUOTA_BRIDGE_FAIL_OPEN.
The guard raises 429 on decision=="deny", so this default blocks unbilled usage.
"""
import os

import httpx
import pytest

from ab0t_quota import bridge


def _clear(monkeypatch):
    monkeypatch.delenv("AB0T_QUOTA_BRIDGE_FAIL_OPEN", raising=False)


def test_network_error_fails_CLOSED_by_default(monkeypatch):
    _clear(monkeypatch)
    r = bridge._network_error_result("sandbox.concurrent", "conn refused")
    assert r["decision"] == "deny", "bridge network error must DENY (never admit unbilled usage) by default"


def test_billing_error_fails_CLOSED_by_default(monkeypatch):
    _clear(monkeypatch)
    resp = httpx.Response(503, text="billing down")
    r = bridge.BridgeClient._parse(resp, op="check")
    assert r["decision"] == "deny", "bridge billing error must DENY by default"


@pytest.mark.parametrize("val", ["true", "1", "yes", "on", "TRUE"])
def test_opt_in_fail_open(monkeypatch, val):
    monkeypatch.setenv("AB0T_QUOTA_BRIDGE_FAIL_OPEN", val)
    r = bridge._network_error_result("x", "err")
    assert r["decision"] == "allow", "explicit AB0T_QUOTA_BRIDGE_FAIL_OPEN opts into availability-over-billing"
    resp = httpx.Response(503, text="down")
    assert bridge.BridgeClient._parse(resp, op="check")["decision"] == "allow"


def test_helper_default_is_false(monkeypatch):
    _clear(monkeypatch)
    assert bridge._bridge_fail_open() is False
