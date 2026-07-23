"""T-21 (ENV-17, pack 20260721) — D-12 SIGNED option 1: bridge tier reads
stop inventing `"free"` on billing failure.

`get_tier`/`usage` route outages through the SAME fail-open/closed switch every
other bridge op uses (0.6.2's `AB0T_QUOTA_BRIDGE_FAIL_OPEN`, default closed):
closed => a typed BridgeUnavailableError naming the outage (the dependent check
denies loudly); open => allow, tier reported UNKNOWN — never `"free"`. Non-200
gains the log it previously lacked (it was silent even for 401/403 — E-74
second read).
"""
from __future__ import annotations

import logging

import httpx
import pytest


def _client_with_handler(handler):
    from ab0t_quota.bridge import BridgeClient
    bc = BridgeClient(
        base_url="https://billing.service.ab0t.com",
        api_key="ab0t_sk_test",
        service_name="svc-1",
    )
    bc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                   headers={"X-API-Key": "ab0t_sk_test"})
    return bc


@pytest.fixture
def fail_closed(monkeypatch):
    monkeypatch.delenv("AB0T_QUOTA_BRIDGE_FAIL_OPEN", raising=False)


@pytest.fixture
def fail_open(monkeypatch):
    monkeypatch.setenv("AB0T_QUOTA_BRIDGE_FAIL_OPEN", "true")


async def _network_error_handler(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("billing unreachable")


async def _forbidden_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(403, json={"detail": "bad mesh key"})


# --- D-12 control 1: never invents free -----------------------------------

@pytest.mark.asyncio
async def test_bridge_tier_outage_never_invents_free(fail_closed):
    """Default (closed): a billing outage must raise a TYPED unavailable error
    naming the outage — never return a tier the operator did not assign."""
    from ab0t_quota import bridge
    client = _client_with_handler(_network_error_handler)
    err_type = getattr(bridge, "BridgeUnavailableError", None)
    assert err_type is not None, \
        "D-12 requires a typed bridge-unavailable signal (bridge.BridgeUnavailableError)"
    with pytest.raises(err_type) as ei:
        await client.get_tier("org-1")
    msg = str(ei.value).lower()
    assert "free" not in msg and ("unreachable" in msg or "unavailable" in msg or "outage" in msg)
    await client.close()


# --- D-12 control 2: non-200 is logged (it was silent, incl. 401/403) -----

@pytest.mark.asyncio
async def test_bridge_tier_non200_is_logged_and_never_free(fail_closed, caplog):
    from ab0t_quota import bridge
    client = _client_with_handler(_forbidden_handler)
    err_type = getattr(bridge, "BridgeUnavailableError", RuntimeError)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(err_type):
            await client.get_tier("org-1")
    assert any("403" in r.message for r in caplog.records), \
        "a non-200 tier read must LOG the status it previously swallowed silently"
    await client.close()


# --- D-12 control 3: fail-open reports UNKNOWN, never free ----------------

@pytest.mark.asyncio
async def test_bridge_tier_fail_open_reports_unknown_never_free(fail_open, caplog):
    from ab0t_quota import bridge
    client = _client_with_handler(_network_error_handler)
    with caplog.at_level(logging.ERROR):
        tier = await client.get_tier("org-1")
    assert tier != "free", \
        "fail-open must never report the invented cheapest tier"
    assert tier == getattr(bridge, "TIER_UNKNOWN", "unknown")
    assert "OPEN" in caplog.text, "the fallback choice must be logged loudly"
    await client.close()


# --- usage(): same law ----------------------------------------------------

@pytest.mark.asyncio
async def test_bridge_usage_outage_never_invents_free_closed(fail_closed):
    from ab0t_quota import bridge
    client = _client_with_handler(_network_error_handler)
    err_type = getattr(bridge, "BridgeUnavailableError", None)
    assert err_type is not None
    with pytest.raises(err_type):
        await client.usage("org-1")
    await client.close()


@pytest.mark.asyncio
async def test_bridge_usage_fail_open_reports_unknown(fail_open):
    from ab0t_quota import bridge
    client = _client_with_handler(_network_error_handler)
    out = await client.usage("org-1")
    assert out.get("tier_id") != "free" and out.get("tier_display") != "Free", \
        f"usage() still invents the free pair on outage: {out}"
    assert out.get("error"), "the unavailable result must carry the error"
    await client.close()


# --- the check-path error result reports no invented tier either ----------

def test_check_error_result_reports_no_invented_tier(fail_closed):
    from ab0t_quota import bridge
    r = bridge._network_error_result("thing.concurrent", "conn refused")
    assert r["decision"] == "deny"  # 0.6.2 behavior unchanged (pin)
    assert r.get("tier_id") != "free" and r.get("tier_display") != "Free", \
        f"the check outage result still reports the invented free pair: {r}"


# --- control: the declared 200 path is untouched --------------------------

@pytest.mark.asyncio
async def test_get_tier_200_path_unchanged(fail_closed):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tier_id": "pro"})
    client = _client_with_handler(handler)
    assert await client.get_tier("org-1") == "pro"
    await client.close()
