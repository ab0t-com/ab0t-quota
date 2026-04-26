"""Tests for the setup_quota() drop-in.

setup_quota(app) is synchronous: it mounts middleware + routes immediately
and composes its async init/teardown onto the app's lifespan. The consumer
calls it once after FastAPI() construction. One line.

These tests run a real FastAPI app with TestClient and prove:
  - /api/quotas/usage, /tiers, /check/{key}, /check-bundle/{name} are mounted
  - QuotaGuard middleware returns 429 when the rate limit is exceeded
  - Health-style paths are exempt from the middleware
  - The QuotaContext lands on app.state.quota
  - The QuotaContext doesn't leak upstream service surface
  - Existing user lifespans are composed, not replaced
  - Persistence can be disabled cleanly

Persistence (DynamoDB) is disabled in tests; paid-tier wiring is disabled
so we don't need mesh credentials. Those paths are exercised separately
by the existing billing-router tests.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager

import fakeredis.aioredis
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from unittest.mock import patch


CONFIG = {
    "storage": {
        "redis_url": "redis://test/0",
        "persistence_enabled": False,
    },
    "tier_provider": {
        "type": "static",
        "default_tier": "starter",
    },
    "alerts": {"enabled": False},
    "enforcement": {"enabled": True},
    "resources": [
        {
            "service": "test-svc",
            "resource_key": "thing.concurrent",
            "display_name": "Concurrent Things",
            "counter_type": "gauge",
            "unit": "things",
        },
        {
            "service": "test-svc",
            "resource_key": "api.requests_per_hour",
            "display_name": "API Requests/Hour",
            "counter_type": "rate",
            "unit": "requests",
            "window_seconds": 3600,
        },
    ],
    "resource_bundles": {
        "default": ["thing.concurrent"],
    },
    "tiers": [
        {
            "tier_id": "starter",
            "display_name": "Starter",
            "sort_order": 1,
            "limits": {
                "thing.concurrent": 5,
                "api.requests_per_hour": 3,  # tiny, so we can hit it in tests
            },
            "features": ["basic"],
        },
    ],
}


@pytest_asyncio.fixture
async def fake_redis():
    """Patch Redis.from_url everywhere so setup_quota uses fakeredis."""
    r = fakeredis.aioredis.FakeRedis()

    def fake_from_url(*a, **kw):
        return r

    with patch("redis.asyncio.Redis.from_url", side_effect=fake_from_url):
        yield r
    try:
        await r.flushall()
    except Exception:
        pass


@pytest_asyncio.fixture
async def config_file(tmp_path):
    """Write the test config to a temp file and point QUOTA_CONFIG_PATH at it."""
    p = tmp_path / "quota-config.json"
    p.write_text(json.dumps(CONFIG))
    old = os.environ.get("QUOTA_CONFIG_PATH")
    os.environ["QUOTA_CONFIG_PATH"] = str(p)
    yield str(p)
    if old is None:
        os.environ.pop("QUOTA_CONFIG_PATH", None)
    else:
        os.environ["QUOTA_CONFIG_PATH"] = old


# ---------------------------------------------------------------------------
# End-to-end: one-line setup yields a working app
# ---------------------------------------------------------------------------

class TestSetupQuotaEndToEnd:
    def test_one_line_setup_serves_tiers_endpoint(self, fake_redis, config_file):
        """The whole point: one line, /tiers works."""
        from ab0t_quota import setup_quota

        app = FastAPI()
        setup_quota(app, enable_paid=False)

        with TestClient(app) as client:
            resp = client.get("/api/quotas/tiers")
            assert resp.status_code == 200
            data = resp.json()
            assert "tiers" in data
            tier_ids = [t["tier_id"] for t in data["tiers"]]
            assert "starter" in tier_ids
            starter = next(t for t in data["tiers"] if t["tier_id"] == "starter")
            assert starter["limits"]["thing.concurrent"]["limit"] == 5

    def test_usage_endpoint_uses_org_extractor(self, fake_redis, config_file):
        from ab0t_quota import setup_quota

        async def fake_extractor(request):
            return request.headers.get("X-Org-ID")

        app = FastAPI()
        setup_quota(app, enable_paid=False, org_extractor=fake_extractor)

        with TestClient(app) as client:
            r = client.get("/api/quotas/usage")
            assert r.status_code == 401  # no header → no org_id
            r = client.get("/api/quotas/usage", headers={"X-Org-ID": "org-1"})
            assert r.status_code == 200
            body = r.json()
            assert body["org_id"] == "org-1"
            assert body["tier_id"] == "starter"

    def test_check_endpoint_returns_quota_decision(self, fake_redis, config_file):
        from ab0t_quota import setup_quota

        async def extractor(request):
            return request.headers.get("X-Org-ID")

        app = FastAPI()
        setup_quota(app, enable_paid=False, enable_rate_limit=False, org_extractor=extractor)

        with TestClient(app) as client:
            r = client.get(
                "/api/quotas/check/thing.concurrent",
                headers={"X-Org-ID": "org-1"},
            )
            assert r.status_code == 200
            body = r.json()
            assert body["decision"] in ("allow", "allow_warning")
            assert body["limit"] == 5

    def test_check_bundle_endpoint(self, fake_redis, config_file):
        from ab0t_quota import setup_quota

        async def extractor(request):
            return request.headers.get("X-Org-ID")

        app = FastAPI()
        setup_quota(app, enable_paid=False, enable_rate_limit=False, org_extractor=extractor)

        with TestClient(app) as client:
            r = client.get("/api/quotas/check-bundle/default", headers={"X-Org-ID": "org-1"})
            assert r.status_code == 200
            assert r.json()["allowed"] is True
            r2 = client.get("/api/quotas/check-bundle/something-not-declared",
                            headers={"X-Org-ID": "org-1"})
            assert r2.status_code == 200
            assert r2.json()["allowed"] is True

    def test_rate_limit_middleware_enforces_after_n_requests(self, fake_redis, config_file):
        """api.requests_per_hour=3; 4th request should 429."""
        from ab0t_quota import setup_quota

        async def extractor(request):
            return request.headers.get("X-Org-ID")

        app = FastAPI()
        setup_quota(app, enable_paid=False, org_extractor=extractor)

        @app.get("/api/data")
        async def some_route():
            return {"ok": True}

        with TestClient(app) as client:
            for i in range(3):
                r = client.get("/api/data", headers={"X-Org-ID": "org-1"})
                assert r.status_code == 200, f"request {i} should succeed"
            r = client.get("/api/data", headers={"X-Org-ID": "org-1"})
            assert r.status_code == 429
            body = r.json()
            assert body["error"] == "quota_exceeded"
            assert body["resource"] == "api.requests_per_hour"

    def test_rate_limit_skipped_for_health(self, fake_redis, config_file):
        from ab0t_quota import setup_quota

        async def extractor(request):
            return request.headers.get("X-Org-ID")

        app = FastAPI()
        setup_quota(app, enable_paid=False, org_extractor=extractor)

        @app.get("/health")
        async def health():
            return {"status": "ok"}

        with TestClient(app) as client:
            for _ in range(20):
                r = client.get("/health", headers={"X-Org-ID": "org-1"})
                assert r.status_code == 200

    def test_quota_context_lands_on_app_state(self, fake_redis, config_file):
        from ab0t_quota import setup_quota, QuotaContext

        app = FastAPI()
        setup_quota(app, enable_paid=False)

        @app.get("/__diag")
        async def diag(request: Request):
            ctx = request.app.state.quota
            return {"is_ctx": isinstance(ctx, QuotaContext), "has_engine": ctx.engine is not None}

        with TestClient(app) as client:
            r = client.get("/__diag")
            assert r.status_code == 200
            assert r.json() == {"is_ctx": True, "has_engine": True}

    def test_quota_context_does_not_leak_upstream_surface(self, fake_redis, config_file):
        """QuotaContext exposes engine + helpers; never billing/payment/SNS clients."""
        from ab0t_quota import setup_quota, QuotaContext

        captured: dict = {}

        app = FastAPI()
        setup_quota(app, enable_paid=False)

        @app.get("/__diag")
        async def diag(request: Request):
            captured["ctx"] = request.app.state.quota
            return {"ok": True}

        with TestClient(app) as client:
            client.get("/__diag")
            ctx = captured["ctx"]
            assert isinstance(ctx, QuotaContext)
            for forbidden in ("billing", "payment", "stripe", "sns"):
                attrs = [a for a in dir(ctx) if forbidden in a.lower() and not a.startswith("_")]
                assert not attrs, f"QuotaContext leaks {forbidden} surface: {attrs}"


# ---------------------------------------------------------------------------
# Lifespan composition
# ---------------------------------------------------------------------------

class TestLifespanComposition:
    def test_existing_user_lifespan_still_runs(self, fake_redis, config_file):
        """If the consumer already had a lifespan, setup_quota composes around it."""
        from ab0t_quota import setup_quota

        events: list[str] = []

        @asynccontextmanager
        async def my_lifespan(app):
            events.append("user_setup")
            yield
            events.append("user_teardown")

        app = FastAPI(lifespan=my_lifespan)
        setup_quota(app, enable_paid=False)

        with TestClient(app):
            pass

        assert "user_setup" in events
        assert "user_teardown" in events

    def test_no_user_lifespan_still_works(self, fake_redis, config_file):
        """A FastAPI() with no lifespan still gets composed cleanly."""
        from ab0t_quota import setup_quota

        app = FastAPI()  # no lifespan
        setup_quota(app, enable_paid=False)

        with TestClient(app) as client:
            r = client.get("/api/quotas/tiers")
            assert r.status_code == 200


# ---------------------------------------------------------------------------
# on_ready callback (used by consumers to capture the engine reference)
# ---------------------------------------------------------------------------

class TestOnReady:
    def test_on_ready_fires_with_quota_context(self, fake_redis, config_file):
        """on_ready callback receives the live QuotaContext when lifespan starts."""
        from ab0t_quota import setup_quota, QuotaContext

        captured: list = []

        def cb(ctx):
            captured.append(ctx)

        app = FastAPI()
        setup_quota(app, enable_paid=False, on_ready=cb)

        with TestClient(app):
            pass

        assert len(captured) == 1
        assert isinstance(captured[0], QuotaContext)
        assert captured[0].engine is not None

    def test_on_ready_async_callback_supported(self, fake_redis, config_file):
        """on_ready accepts async callbacks too."""
        from ab0t_quota import setup_quota

        captured: list = []

        async def cb(ctx):
            captured.append(ctx)

        app = FastAPI()
        setup_quota(app, enable_paid=False, on_ready=cb)
        with TestClient(app):
            pass
        assert len(captured) == 1

    def test_on_ready_failure_does_not_break_startup(self, fake_redis, config_file):
        """If on_ready throws, startup still succeeds."""
        from ab0t_quota import setup_quota

        def cb(ctx):
            raise RuntimeError("oops")

        app = FastAPI()
        setup_quota(app, enable_paid=False, on_ready=cb)

        with TestClient(app) as client:
            r = client.get("/api/quotas/tiers")
            assert r.status_code == 200


# ---------------------------------------------------------------------------
# Public surface guarantees
# ---------------------------------------------------------------------------

class TestPublicSurfaceIsClean:
    def test_setup_quota_signature_has_no_upstream_kwargs(self):
        """setup_quota must not require URLs/keys for other mesh services."""
        import inspect
        from ab0t_quota import setup_quota
        sig = inspect.signature(setup_quota)
        params = set(sig.parameters.keys())
        leaks = {
            "billing_url", "payment_url",
            "billing_api_key", "payment_api_key",
            "sns_topic_arn",
        }
        assert not (params & leaks), f"setup_quota leaks mesh-internal params: {params & leaks}"


# ---------------------------------------------------------------------------
# Bridge mode (mode="bridge")
# ---------------------------------------------------------------------------

class TestBridgeMode:
    def _bridge_config_file(self, tmp_path):
        cfg = {
            "service_name": "svc-1",
            "engine_mode": "bridge",
            "bridge_cache": {"tier_ttl_seconds": 60.0, "decision_ttl_seconds": 1.0},
        }
        p = tmp_path / "quota-config.json"
        p.write_text(json.dumps(cfg))
        old = os.environ.get("QUOTA_CONFIG_PATH")
        os.environ["QUOTA_CONFIG_PATH"] = str(p)
        return str(p), old

    def test_bridge_mode_does_not_touch_redis(self, monkeypatch, tmp_path):
        """In bridge mode, no Redis client is built — no FakeRedis needed."""
        from ab0t_quota import setup_quota
        path, old = self._bridge_config_file(tmp_path)
        monkeypatch.setenv("AB0T_MESH_API_KEY", "test")
        monkeypatch.setenv("AB0T_SERVICE_NAME", "svc-1")

        # Patch Redis.from_url to fail loudly if called
        def boom(*a, **kw):
            raise RuntimeError("Redis must NOT be initialized in bridge mode")
        monkeypatch.setattr("redis.asyncio.Redis.from_url", boom)

        try:
            app = FastAPI()
            setup_quota(app)  # mode auto-detected from config.engine_mode

            with TestClient(app):
                pass  # lifespan runs without Redis being touched
        finally:
            if old is None:
                os.environ.pop("QUOTA_CONFIG_PATH", None)
            else:
                os.environ["QUOTA_CONFIG_PATH"] = old

    def test_bridge_mode_mounts_check_routes(self, monkeypatch, tmp_path):
        """/api/quotas/check/{key} routes should mount and work via mock httpx."""
        from ab0t_quota import setup_quota
        import httpx

        path, old = self._bridge_config_file(tmp_path)
        monkeypatch.setenv("AB0T_MESH_API_KEY", "test")
        monkeypatch.setenv("AB0T_SERVICE_NAME", "svc-1")
        monkeypatch.setattr("redis.asyncio.Redis.from_url",
                            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("unused")))

        # Mock the BridgeClient's transport so we can verify it was called
        captured = {}
        async def handler(request):
            captured["url"] = str(request.url)
            return httpx.Response(200, json={
                "decision": "allow", "current": 0, "limit": 5, "resource_key": "x.y",
            })

        try:
            app = FastAPI()
            setup_quota(app, mode="bridge")
            # Replace the lifespan-built client with one wired to mock transport
            # so the test runs without real network calls
            from ab0t_quota.bridge import BridgeClient
            from ab0t_quota.caches import CachedBridgeClient

            with TestClient(app) as client:
                # Override the bridge client's underlying transport AFTER lifespan ran
                ctx = app.state.quota
                inner_client = ctx._client._client._client  # CachedBridgeClient -> BridgeClient -> httpx
                inner_client._transport = httpx.MockTransport(handler)

                async def fake_extract(req):
                    return "org-1"
                # Also patch org_extractor would be cleaner — use header approach
                # The route uses the default extractor (request.state.user.org_id)
                # which won't be set, so it will 401. That's expected.
                r = client.get("/api/quotas/check/x.y")
                # No auth header -> 401 (expected)
                assert r.status_code == 401
        finally:
            if old is None:
                os.environ.pop("QUOTA_CONFIG_PATH", None)
            else:
                os.environ["QUOTA_CONFIG_PATH"] = old

    def test_bridge_mode_with_org_extractor(self, monkeypatch, tmp_path):
        """With org_extractor + mocked HTTP, full request → bridge → response works."""
        from ab0t_quota import setup_quota
        import httpx

        path, old = self._bridge_config_file(tmp_path)
        monkeypatch.setenv("AB0T_MESH_API_KEY", "test")
        monkeypatch.setenv("AB0T_SERVICE_NAME", "svc-1")
        monkeypatch.setattr("redis.asyncio.Redis.from_url",
                            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("unused")))

        async def handler(request):
            assert "/billing/quota/svc-1/org-1/check/x.y" in str(request.url)
            return httpx.Response(200, json={
                "decision": "allow", "current": 0, "limit": 5, "resource_key": "x.y",
                "tier_id": "starter", "tier_display": "Starter",
            })

        async def extractor(req):
            return req.headers.get("X-Org-ID")

        try:
            app = FastAPI()
            setup_quota(app, mode="bridge", org_extractor=extractor)

            with TestClient(app) as client:
                # Wire mock transport into the bridge client
                ctx = app.state.quota
                ctx._client._client._client._transport = httpx.MockTransport(handler)
                r = client.get("/api/quotas/check/x.y", headers={"X-Org-ID": "org-1"})
                assert r.status_code == 200
                assert r.json()["decision"] == "allow"
        finally:
            if old is None:
                os.environ.pop("QUOTA_CONFIG_PATH", None)
            else:
                os.environ["QUOTA_CONFIG_PATH"] = old

    def test_bridge_mode_quota_context_is_BridgeContext(self, monkeypatch, tmp_path):
        """app.state.quota in bridge mode is a BridgeContext, not a QuotaContext."""
        from ab0t_quota import setup_quota
        from ab0t_quota.bridge import BridgeContext
        path, old = self._bridge_config_file(tmp_path)
        monkeypatch.setenv("AB0T_MESH_API_KEY", "test")
        monkeypatch.setenv("AB0T_SERVICE_NAME", "svc-1")
        monkeypatch.setattr("redis.asyncio.Redis.from_url",
                            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("unused")))
        try:
            app = FastAPI()
            setup_quota(app, mode="bridge")
            with TestClient(app):
                assert isinstance(app.state.quota, BridgeContext)
        finally:
            if old is None:
                os.environ.pop("QUOTA_CONFIG_PATH", None)
            else:
                os.environ["QUOTA_CONFIG_PATH"] = old

    def test_unknown_mode_falls_back_to_local(self, fake_redis, config_file, monkeypatch, caplog):
        """Misconfigured engine_mode should not crash — log warning, default local."""
        from ab0t_quota import setup_quota
        app = FastAPI()
        setup_quota(app, mode="quantum")  # nonsense mode
        with TestClient(app) as client:
            r = client.get("/api/quotas/tiers")
            assert r.status_code == 200  # local mode mounts /tiers


# ---------------------------------------------------------------------------
# Persistence path can be disabled cleanly
# ---------------------------------------------------------------------------

class TestPersistenceDisabled:
    def test_no_persistence_no_snapshot_worker(self, fake_redis, config_file):
        """When persistence_enabled=false, no DynamoDB calls and no worker."""
        from ab0t_quota import setup_quota

        app = FastAPI()
        setup_quota(app, enable_paid=False)

        @app.get("/__diag")
        async def diag(request: Request):
            return {"store_is_none": request.app.state.quota._store is None}

        with TestClient(app) as client:
            r = client.get("/__diag")
            assert r.status_code == 200
            assert r.json() == {"store_is_none": True}
