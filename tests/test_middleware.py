"""2.1.5 — Middleware tests."""

import pytest
import pytest_asyncio
import fakeredis.aioredis
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.testclient import TestClient
from fastapi import FastAPI

from ab0t_quota.engine import QuotaEngine
from ab0t_quota.middleware import QuotaGuard
from ab0t_quota.models.core import ResourceDef, CounterType, TierConfig, TierLimits
from ab0t_quota.registry import ResourceRegistry
from ab0t_quota.providers import StaticTierProvider
from ab0t_quota.models.requests import QuotaIncrementRequest


TIERS = {
    "free": TierConfig(
        tier_id="free", display_name="Free", sort_order=0,
        features=set(),
        limits={"api.requests": TierLimits(limit=3)},  # very low for testing
    ),
}

RESOURCES = [
    ResourceDef(
        service="test", resource_key="api.requests",
        display_name="API Requests/Hr", counter_type=CounterType.RATE,
        unit="requests", window_seconds=3600,
    ),
]


def _build_app(redis, enabled=True, fail_open=False, fail_open_error_threshold=0):
    registry = ResourceRegistry()
    registry.register(*RESOURCES)
    provider = StaticTierProvider(default_tier="free")
    engine = QuotaEngine(redis=redis, tier_provider=provider, registry=registry, tiers=TIERS)

    app = FastAPI()

    async def org_extractor(request):
        return "org-test"

    app.add_middleware(
        QuotaGuard,
        engine=engine,
        resource_key="api.requests",
        org_extractor=org_extractor,
        enabled=enabled,
        fail_open=fail_open,
        fail_open_error_threshold=fail_open_error_threshold,
    )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/data")
    async def data():
        return {"data": "hello"}

    return app, engine


class TestQuotaGuard:
    def test_exempt_paths_skip_check(self):
        redis = fakeredis.aioredis.FakeRedis()
        app, _ = _build_app(redis)
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        # No X-Quota-Limit header on exempt paths
        assert "X-Quota-Limit" not in resp.headers

    def test_allowed_adds_headers(self):
        redis = fakeredis.aioredis.FakeRedis()
        app, _ = _build_app(redis)
        client = TestClient(app)
        resp = client.get("/api/data")
        assert resp.status_code == 200
        assert resp.headers.get("X-Quota-Limit") == "3"

    def test_denied_returns_429(self):
        redis = fakeredis.aioredis.FakeRedis()
        app, _ = _build_app(redis)
        client = TestClient(app)
        # Exhaust the limit (3 requests)
        for _ in range(3):
            resp = client.get("/api/data")
            assert resp.status_code == 200
        # 4th request should be denied
        resp = client.get("/api/data")
        assert resp.status_code == 429
        body = resp.json()
        assert body["error"] == "quota_exceeded"
        assert body["resource"] == "api.requests"
        assert "Retry-After" in resp.headers

    def test_disabled_skips_all(self):
        redis = fakeredis.aioredis.FakeRedis()
        app, _ = _build_app(redis, enabled=False)
        client = TestClient(app)
        # Should work even beyond the limit
        for _ in range(10):
            resp = client.get("/api/data")
            assert resp.status_code == 200


class TestFailMode:
    """M1: Middleware fail-closed by default, configurable fail-open with threshold."""

    def test_default_is_fail_closed(self):
        """Default (fail_open=False): engine error returns 503."""
        redis = fakeredis.aioredis.FakeRedis()
        app, engine = _build_app(redis, fail_open=False)
        with patch.object(engine, "check", side_effect=RuntimeError("Redis down")):
            client = TestClient(app)
            resp = client.get("/api/data")
            assert resp.status_code == 503
            assert resp.json()["error"] == "quota_service_unavailable"

    def test_fail_open_allows_on_error(self):
        """fail_open=True: engine error passes request through."""
        redis = fakeredis.aioredis.FakeRedis()
        app, engine = _build_app(redis, fail_open=True)
        with patch.object(engine, "check", side_effect=RuntimeError("Redis down")):
            client = TestClient(app)
            resp = client.get("/api/data")
            assert resp.status_code == 200

    def test_fail_open_with_threshold_switches_to_closed(self):
        """fail_open=True + threshold=3: after 3 errors, returns 503."""
        redis = fakeredis.aioredis.FakeRedis()
        app, engine = _build_app(redis, fail_open=True, fail_open_error_threshold=3)
        with patch.object(engine, "check", side_effect=RuntimeError("Redis down")):
            client = TestClient(app)
            # First 2 errors: fail open (200)
            assert client.get("/api/data").status_code == 200
            assert client.get("/api/data").status_code == 200
            # 3rd error: threshold reached, fail closed (503)
            assert client.get("/api/data").status_code == 503

    def test_error_counter_resets_on_success(self):
        """Consecutive error counter resets when a check succeeds."""
        redis = fakeredis.aioredis.FakeRedis()
        app, engine = _build_app(redis, fail_open=True, fail_open_error_threshold=3)
        original_check = engine.check
        call_count = 0

        async def flaky_check(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise RuntimeError("Redis down")
            return await original_check(*args, **kwargs)

        with patch.object(engine, "check", side_effect=flaky_check):
            client = TestClient(app)
            # 2 errors (fail open)
            assert client.get("/api/data").status_code == 200
            assert client.get("/api/data").status_code == 200
            # Success — resets counter
            assert client.get("/api/data").status_code == 200

    def test_fail_closed_returns_503_not_500(self):
        """Fail-closed returns structured 503, not an unhandled 500."""
        redis = fakeredis.aioredis.FakeRedis()
        app, engine = _build_app(redis, fail_open=False)
        with patch.object(engine, "check", side_effect=Exception("unexpected")):
            client = TestClient(app)
            resp = client.get("/api/data")
            assert resp.status_code == 503
            body = resp.json()
            assert "error" in body
            assert "detail" in body
