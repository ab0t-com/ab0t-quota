"""Tests for the auth-event registry + receiver + auto-subscribe."""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ab0t_quota import auth_events as ae


class _MockAuth:
    """Replace httpx.AsyncClient with one that routes to our handlers.

    Used as a context manager that monkeypatches httpx.AsyncClient for
    the duration of a test. Records calls + returns canned responses.
    """
    def __init__(self, monkeypatch, responses):
        # responses: dict of (method, url_substring) -> httpx.Response (or callable)
        self._responses = responses
        self.calls = []

        async def _request(client_self, method, url, **kwargs):
            self.calls.append({"method": method, "url": url, **kwargs})
            for (m, sub), resp in self._responses.items():
                if m == method and sub in url:
                    if callable(resp):
                        return resp(method, url, kwargs)
                    return resp
            raise httpx.RequestError(f"unmatched: {method} {url}")

        # Patch the AsyncClient's request() method
        async def _aenter(c): return c
        async def _aexit(c, *a): pass

        class _FakeClient:
            def __init__(self, *args, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def request(self_inner, method, url, **kwargs):
                return await _request(self_inner, method, url, **kwargs)
            async def get(self_inner, url, **kwargs):
                return await _request(self_inner, "GET", url, **kwargs)
            async def post(self_inner, url, **kwargs):
                return await _request(self_inner, "POST", url, **kwargs)

        monkeypatch.setattr(ae.httpx, "AsyncClient", _FakeClient)


@pytest.fixture(autouse=True)
def _reset_registry():
    """Module-level registry must be reset between tests."""
    ae.clear_handlers()
    yield
    ae.clear_handlers()


# ---------------------------------------------------------------------------
# Registry primitives
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_register_handler_basic(self):
        async def h(event):
            pass
        ae.register_handler("auth.user.registered", h)
        assert ae.registered_event_types() == ["auth.user.registered"]

    def test_register_handler_dedupes_by_identity(self):
        async def h(event):
            pass
        ae.register_handler("auth.user.registered", h)
        ae.register_handler("auth.user.registered", h)
        assert ae._HANDLERS["auth.user.registered"] == [h]

    def test_decorator_form(self):
        @ae.on_auth_event("auth.user.login")
        async def my_handler(event):
            pass
        assert "auth.user.login" in ae.registered_event_types()
        assert my_handler in ae._HANDLERS["auth.user.login"]

    def test_decorator_returns_function(self):
        """Decorator must return the wrapped fn so it stays callable."""
        @ae.on_auth_event("x")
        async def fn(event):
            return "result"
        assert callable(fn)

    def test_multiple_handlers_per_event(self):
        async def h1(event): pass
        async def h2(event): pass
        ae.register_handler("auth.user.registered", h1)
        ae.register_handler("auth.user.registered", h2)
        assert ae._HANDLERS["auth.user.registered"] == [h1, h2]

    def test_unregister_handler(self):
        async def h(event): pass
        ae.register_handler("x", h)
        assert ae.unregister_handler("x", h) is True
        assert ae.unregister_handler("x", h) is False  # already gone
        assert ae._HANDLERS["x"] == []

    def test_registered_event_types_only_returns_active(self):
        async def h(event): pass
        ae.register_handler("active", h)
        ae.register_handler("removed", h)
        ae.unregister_handler("removed", h)
        assert "active" in ae.registered_event_types()
        assert "removed" not in ae.registered_event_types()


# ---------------------------------------------------------------------------
# HMAC verification
# ---------------------------------------------------------------------------

class TestHmacVerify:
    SECRET = "test-secret-12345"

    def _sign(self, body: bytes) -> str:
        return hmac.new(self.SECRET.encode(), body, hashlib.sha256).hexdigest()

    def test_valid_signature_passes(self):
        body = b'{"event_type":"x"}'
        sig = self._sign(body)
        assert ae.verify_hmac(body, sig, self.SECRET) is True
        # Also accept sha256= prefix (auth's webhook publisher format)
        assert ae.verify_hmac(body, f"sha256={sig}", self.SECRET) is True

    def test_invalid_signature_fails(self):
        body = b'{"event_type":"x"}'
        bad = "0" * 64
        assert ae.verify_hmac(body, bad, self.SECRET) is False

    def test_no_signature_fails(self):
        assert ae.verify_hmac(b'{}', None, self.SECRET) is False
        assert ae.verify_hmac(b'{}', "", self.SECRET) is False

    def test_empty_secret_fails(self):
        body = b'{}'
        sig = self._sign(body)
        assert ae.verify_hmac(body, sig, "") is False

    def test_tampered_body_fails(self):
        sig = self._sign(b'{"event_type":"x"}')
        assert ae.verify_hmac(b'{"event_type":"y"}', sig, self.SECRET) is False


# ---------------------------------------------------------------------------
# Webhook receiver — dispatch behavior
# ---------------------------------------------------------------------------

class TestWebhookReceiver:
    SECRET = "test-secret-abc"

    def _build_app(self):
        app = FastAPI()
        app.include_router(ae.make_router(webhook_secret=self.SECRET), prefix="/api/quotas")
        return TestClient(app)

    def _sign(self, body: bytes) -> str:
        return hmac.new(self.SECRET.encode(), body, hashlib.sha256).hexdigest()

    def test_no_signature_returns_401(self):
        client = self._build_app()
        r = client.post("/api/quotas/_webhooks/auth", json={"event_type": "x"})
        assert r.status_code == 401

    def test_bad_signature_returns_401(self):
        client = self._build_app()
        r = client.post("/api/quotas/_webhooks/auth", json={"event_type": "x"},
                        headers={"X-Event-Signature": "garbage"})
        assert r.status_code == 401

    def test_invalid_json_returns_400(self):
        client = self._build_app()
        body = b"not json"
        sig = self._sign(body)
        r = client.post("/api/quotas/_webhooks/auth", content=body,
                        headers={"X-Event-Signature": sig, "Content-Type": "application/json"})
        assert r.status_code == 400

    def test_unknown_event_type_returns_ignored(self):
        client = self._build_app()
        body = json.dumps({"event_type": "unknown.event"}).encode()
        sig = self._sign(body)
        r = client.post("/api/quotas/_webhooks/auth", content=body,
                        headers={"X-Event-Signature": sig, "Content-Type": "application/json"})
        assert r.status_code == 200
        assert r.json()["status"] == "ignored"

    def test_dispatches_to_registered_handler(self):
        seen = []

        @ae.on_auth_event("auth.user.registered")
        async def my_handler(event):
            seen.append(event)

        client = self._build_app()
        body = json.dumps({"event_type": "auth.user.registered",
                           "data": {"user_id": "u1", "org_id": "o1"}}).encode()
        sig = self._sign(body)
        r = client.post("/api/quotas/_webhooks/auth", content=body,
                        headers={"X-Event-Signature": sig, "Content-Type": "application/json"})
        assert r.status_code == 200
        assert r.json()["ran"] == 1
        assert len(seen) == 1
        assert seen[0]["data"]["user_id"] == "u1"

    def test_dispatches_to_all_handlers_for_event(self):
        seen = []
        ae.register_handler("e", lambda e: _record(seen, "h1"))  # type: ignore
        # Use proper async handlers
        ae.clear_handlers()

        async def h1(event): seen.append("h1")
        async def h2(event): seen.append("h2")
        ae.register_handler("e", h1)
        ae.register_handler("e", h2)

        client = self._build_app()
        body = json.dumps({"event_type": "e"}).encode()
        sig = self._sign(body)
        r = client.post("/api/quotas/_webhooks/auth", content=body,
                        headers={"X-Event-Signature": sig, "Content-Type": "application/json"})
        assert r.status_code == 200
        assert r.json()["ran"] == 2
        assert seen == ["h1", "h2"]

    def test_handler_exception_does_not_propagate(self):
        """Auth needs a 200 to mark delivered; a buggy handler must not
        bubble up and cause infinite retry."""
        async def boom(event):
            raise RuntimeError("oops")
        async def ok(event):
            pass
        ae.register_handler("e", boom)
        ae.register_handler("e", ok)

        client = self._build_app()
        body = json.dumps({"event_type": "e"}).encode()
        sig = self._sign(body)
        r = client.post("/api/quotas/_webhooks/auth", content=body,
                        headers={"X-Event-Signature": sig, "Content-Type": "application/json"})
        assert r.status_code == 200
        # Only the good handler counts as ran; the bad one's exception is logged.
        assert r.json()["ran"] == 1


def _record(buf, val):
    """Sync helper used in one test only."""
    buf.append(val)


# ---------------------------------------------------------------------------
# subscribe_on_startup — idempotency + env defaults
# ---------------------------------------------------------------------------

class TestSubscribeOnStartup:
    @pytest.mark.asyncio
    async def test_skips_when_no_handlers(self):
        result = await ae.subscribe_on_startup(
            auth_url="https://auth.test", admin_token="t",
            public_url="https://app.test", secret="s",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_skips_when_missing_env(self):
        async def h(e): pass
        ae.register_handler("auth.user.registered", h)
        result = await ae.subscribe_on_startup(
            auth_url="", admin_token="", public_url="", secret="",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_creates_subscription_when_none_exists(self, monkeypatch):
        async def h(e): pass
        ae.register_handler("auth.user.registered", h)
        mock = _MockAuth(monkeypatch, {
            ("GET", "/events/subscriptions"): httpx.Response(200, json={"items": []}),
            ("POST", "/events/subscriptions"): httpx.Response(201, json={"subscription_id": "sub_new"}),
        })
        result = await ae.subscribe_on_startup(
            auth_url="https://auth.test", admin_token="t",
            public_url="https://app.test", secret="s",
        )
        assert result == "sub_new"
        assert any(c["method"] == "POST" for c in mock.calls)

    @pytest.mark.asyncio
    async def test_idempotent_when_subscription_exists(self, monkeypatch):
        async def h(e): pass
        ae.register_handler("auth.user.registered", h)
        existing = {"subscription_id": "sub_old",
                    "endpoint": "https://app.test/api/quotas/_webhooks/auth"}
        mock = _MockAuth(monkeypatch, {
            ("GET", "/events/subscriptions"): httpx.Response(200, json={"items": [existing]}),
            # No POST — if this fires the mock raises (unmatched)
        })
        result = await ae.subscribe_on_startup(
            auth_url="https://auth.test", admin_token="t",
            public_url="https://app.test", secret="s",
        )
        assert result == "sub_old"
        assert all(c["method"] != "POST" for c in mock.calls)

    @pytest.mark.asyncio
    async def test_admin_token_rejected_returns_none(self, monkeypatch):
        async def h(e): pass
        ae.register_handler("auth.user.registered", h)
        _MockAuth(monkeypatch, {
            ("GET", "/events/subscriptions"): httpx.Response(401, json={"detail": "unauthorized"}),
        })
        result = await ae.subscribe_on_startup(
            auth_url="https://auth.test", admin_token="bad",
            public_url="https://app.test", secret="s",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_subscribes_only_to_registered_event_types(self, monkeypatch):
        async def h(e): pass
        ae.register_handler("auth.user.registered", h)
        ae.register_handler("org.created", h)

        captured = {}
        def capture_post(method, url, kwargs):
            captured["body"] = kwargs.get("json")
            return httpx.Response(201, json={"subscription_id": "sub_x"})

        _MockAuth(monkeypatch, {
            ("GET", "/events/subscriptions"): httpx.Response(200, json={"items": []}),
            ("POST", "/events/subscriptions"): capture_post,
        })
        await ae.subscribe_on_startup(
            auth_url="https://auth.test", admin_token="t",
            public_url="https://app.test", secret="s",
        )
        assert set(captured["body"]["event_types"]) == {"auth.user.registered", "org.created"}

    @pytest.mark.asyncio
    async def test_env_defaults(self, monkeypatch):
        async def h(e): pass
        ae.register_handler("auth.user.registered", h)

        monkeypatch.setenv("AB0T_AUTH_AUTH_URL", "https://envauth.test")
        monkeypatch.setenv("AB0T_AUTH_ADMIN_TOKEN", "envtoken")
        monkeypatch.setenv("AB0T_AUTH_WEBHOOK_PUBLIC_URL", "https://envapp.test")
        monkeypatch.setenv("AB0T_AUTH_WEBHOOK_SECRET", "envsecret")

        _MockAuth(monkeypatch, {
            ("GET", "/events/subscriptions"): httpx.Response(200, json={"items": []}),
            ("POST", "/events/subscriptions"): httpx.Response(201, json={"subscription_id": "sub_env"}),
        })
        result = await ae.subscribe_on_startup()  # all kwargs default to env
        assert result == "sub_env"
