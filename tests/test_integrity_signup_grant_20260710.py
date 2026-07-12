"""P1.4 / QC-04 / D-13 (library half) — signup-grant failure + durability
semantics (ticket 20260709).

RED-BY-DESIGN (until P1.4). Two verified defects in
`grant_initial_credit_for_user`:

  1. HTTP 400 is treated as SUCCESS: the Redis dedup flag is set when billing
     returns 200 OR 400 (auth_events.py `if resp.status_code in (200, 400)`), so
     a validation error permanently SUPPRESSES the user's credit. A corrected
     redelivery is then silently skipped by the flag. D-13: 400 must be a
     failure — never mark the grant granted.

  2. The dedup flag has a 30-day TTL even for `*_global` ("one credit EVER")
     policies (`ex=86400*30`). After 30 days — or a Redis flush — the guard
     evaporates and a redelivery re-grants. D-13: for global policies the guard
     must be DURABLE (no TTL).

Billing's own durable dedup (Contract B) is the other half and lives in billing's
ticket; this suite only pins the LIBRARY half.
"""
from __future__ import annotations

from types import SimpleNamespace

import fakeredis.aioredis
import pytest
import pytest_asyncio

from ab0t_quota import auth_events
from ab0t_quota.auth_events import compose_credit_dedup_key, grant_initial_credit_for_user


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.flushall()
    await r.aclose()


class _Resp:
    def __init__(self, status): self.status_code = status; self.text = "{}"


class _FakeClient:
    """Stand-in for httpx.AsyncClient that returns a fixed status."""
    def __init__(self, status): self._status = status
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, *a, **k): return _Resp(self._status)


def _patch_billing(monkeypatch, status):
    monkeypatch.setattr(auth_events.httpx, "AsyncClient", lambda **kw: _FakeClient(status))


class _Provider:
    def __init__(self, tier): self._tier = tier
    async def get_tier(self, org_id, **kw): return self._tier


@pytest.mark.asyncio
async def test_http_400_does_not_mark_grant_granted(redis, monkeypatch):
    """Billing returns 400. The dedup flag must NOT be set, so a corrected
    redelivery can still grant. RED today: 400 sets the flag (grant suppressed)."""
    _patch_billing(monkeypatch, 400)
    await grant_initial_credit_for_user(
        "user-1", "org-1",
        initial_credits={"free": 5.0},
        tier_provider=_Provider("free"),
        redis=redis,
        billing_url="http://billing.local",
        billing_api_key="k",
    )
    flag_key = compose_credit_dedup_key("per_user_per_tier", user_id="user-1", org_id="org-1", tier_id="free")
    assert await redis.get(flag_key) is None, (
        "QC-04/D-13: a 400 response set the dedup flag — the grant is permanently "
        "suppressed even though billing never applied it. 400 must be a failure."
    )


@pytest.mark.asyncio
async def test_global_policy_flag_has_no_ttl(redis, monkeypatch):
    """A `per_user_global` ('one credit EVER') grant that succeeds must set a
    DURABLE flag (no TTL). RED today: the flag carries a 30-day TTL, so it
    evaporates and a later redelivery re-grants (double credit)."""
    _patch_billing(monkeypatch, 200)
    tier_grant = SimpleNamespace(
        trigger=SimpleNamespace(value="signup"),
        amount_per_period=5.0,
        destination=SimpleNamespace(value="credit_balance"),
        lifecycle=SimpleNamespace(value="persistent"),
        dedup="per_user_global",
        rollover_max_periods=None,
    )
    tier = SimpleNamespace(credit_grant=tier_grant)
    await grant_initial_credit_for_user(
        "user-1", "org-1",
        initial_credits={},
        tier_provider=_Provider("pro"),
        redis=redis,
        billing_url="http://billing.local",
        billing_api_key="k",
        tier_registry={"pro": tier},
    )
    flag_key = compose_credit_dedup_key("per_user_global", user_id="user-1", org_id="org-1", tier_id="pro")
    assert await redis.get(flag_key) is not None, "precondition: the grant flag was set on 200"
    ttl = await redis.ttl(flag_key)
    assert ttl == -1, (
        f"QC-04/D-13: the 'one credit EVER' flag has TTL={ttl}s (not -1/no-expiry). "
        f"It will evaporate and let a later redelivery re-grant — double credit."
    )


class _CountingClient:
    """httpx.AsyncClient stand-in that counts POSTs and returns 200."""
    posts = {"n": 0}
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, *a, **k):
        _CountingClient.posts["n"] += 1
        return _Resp(200)


@pytest.mark.asyncio
async def test_durable_ledger_dedup_prevents_regrant_after_redis_flush(redis, monkeypatch):
    """Claim R2 / D-13 — the grant must route through the durable @idempotent
    ledger, not just a volatile Redis flag. After a Redis flush (or the flag's
    TTL expiring, or a failover) a redelivery must STILL be deduped by the
    durable ledger — otherwise the customer is credited twice.

    RED today: the grant only consults the Redis flag; flush it and the second
    call re-POSTs to billing (double grant). GREEN target: the ledger's durable
    business-dedup (no TTL) stops the second POST.
    """
    auth_events._reset_fallback_ledger_store()   # clean shared-fallback ledger
    _CountingClient.posts["n"] = 0
    monkeypatch.setattr(auth_events.httpx, "AsyncClient", lambda **kw: _CountingClient())

    call = dict(
        initial_credits={"free": 5.0},
        tier_provider=_Provider("free"),
        redis=redis,
        billing_url="http://billing.local",
        billing_api_key="k",
    )
    await grant_initial_credit_for_user("u-dd", "o-dd", **call)
    assert _CountingClient.posts["n"] == 1, "precondition: first grant POSTed once"

    # The volatile fast-path flag is lost (flush / >30d TTL / Redis failover).
    await redis.flushall()

    await grant_initial_credit_for_user("u-dd", "o-dd", **call)
    assert _CountingClient.posts["n"] == 1, (
        "R2/D-13: the grant re-POSTed to billing after the Redis flag was lost — "
        "the durable ledger dedup did not hold. A redelivery double-credits."
    )
