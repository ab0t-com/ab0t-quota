"""F9/P4.1 — the drift metric reaches an HTTP surface, not just Redis.

Ticket 20260810_quota_drift_live_recurrence_permanent_fix.

D-40's law (test_capabilities_consumer_20260710.py's own framing) is: assert
at the CONSUMER (the route), never at the emitter — a metric only Redis can
see is the same "event with no sink" mistake this library has already paid
for once. `RedisCounterMetricDispatcher` (alerts.py) is the "no metrics sink
configured" fallback; this file proves it is actually REACHABLE over HTTP via
`/quota/capabilities`'s `drift_metrics` block — the endpoint every consumer
that calls `setup_quota` already gets for free (no extra sandbox-platform-side
wiring required), and the concrete answer to "expose a counter the platform
can scrape."
"""
from __future__ import annotations

import fakeredis.aioredis
import httpx
import pytest
from fastapi import FastAPI

from ab0t_quota.alerts import DriftAlertManager
from ab0t_quota.setup import _register_capability_routes


async def _client_for(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_capabilities_endpoint_carries_drift_metrics_totals():
    redis = fakeredis.aioredis.FakeRedis()
    try:
        app = FastAPI()
        app.state.quota_capabilities = {}
        _register_capability_routes(app, redis=redis, config={})

        mgr = DriftAlertManager(redis=redis)  # default dispatchers (F9 zero-config)
        await mgr.drift_detected("org-1", "sandbox.concurrent", observed=5, ledger=1,
                                  before=1, after=5, source="provider")
        await mgr.drift_detected("org-2", "sandbox.desktop_sessions", observed=2, ledger=0,
                                  before=0, after=2, source="provider")

        async with await _client_for(app) as client:
            r = await client.get("/quota/capabilities")
        assert r.status_code == 200
        dm = r.json()["drift_metrics"]
        assert dm["drift_detected_total"] == 2
        assert dm["drift_resolved_total"] == 0
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_capabilities_endpoint_lists_currently_sustaining_orgs():
    """The exact set the `quota_drift_sustained` alert has fired for is
    reachable over HTTP too — an operator can see WHICH (org, resource) is
    recurring without grepping logs."""
    redis = fakeredis.aioredis.FakeRedis()
    try:
        app = FastAPI()
        app.state.quota_capabilities = {}
        # threshold=2 via alerts config, mirroring how sandbox-platform's
        # quota-config.json would set `alerts.drift_sustained_threshold`.
        _register_capability_routes(app, redis=redis,
                                    config={"alerts": {"drift_sustained_threshold": 2}})

        mgr = DriftAlertManager(redis=redis, sustained_alert_threshold=2)
        for _ in range(2):
            await mgr.drift_detected("cd790b95", "sandbox.concurrent", observed=5, ledger=0,
                                      before=0, after=5, source="provider")

        async with await _client_for(app) as client:
            r = await client.get("/quota/capabilities")
        dm = r.json()["drift_metrics"]
        assert dm["sustained_threshold"] == 2
        assert {"org_id": "cd790b95", "resource_key": "sandbox.concurrent",
                "consecutive_passes": 2} in dm["currently_sustaining"]
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_capabilities_endpoint_resolving_clears_sustaining_list():
    redis = fakeredis.aioredis.FakeRedis()
    try:
        app = FastAPI()
        app.state.quota_capabilities = {}
        _register_capability_routes(app, redis=redis,
                                    config={"alerts": {"drift_sustained_threshold": 1}})

        mgr = DriftAlertManager(redis=redis, sustained_alert_threshold=1)
        await mgr.drift_detected("org-1", "sandbox.concurrent", observed=5, ledger=0,
                                  before=0, after=5, source="provider")
        await mgr.drift_resolved("org-1", "sandbox.concurrent", value=5.0)

        async with await _client_for(app) as client:
            r = await client.get("/quota/capabilities")
        dm = r.json()["drift_metrics"]
        assert dm["currently_sustaining"] == []
        assert dm["drift_resolved_total"] == 1
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_capabilities_endpoint_without_redis_omits_drift_metrics():
    """Backward-compat: a caller (existing tests, a bridge consumer with no
    Redis of its own) that doesn't pass `redis=` keeps working — the block is
    simply absent, never a crash."""
    app = FastAPI()
    app.state.quota_capabilities = {"reconciler": "on(provider)"}
    _register_capability_routes(app)  # no redis kwarg, matches every pre-existing call site

    async with await _client_for(app) as client:
        r = await client.get("/quota/capabilities")
    assert r.status_code == 200
    assert "drift_metrics" not in r.json()
    assert r.json()["reconciler"] == "on(provider)"
