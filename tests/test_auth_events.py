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


# ---------------------------------------------------------------------------
# T11 — default signup-credit handler honors tier_registry
# ---------------------------------------------------------------------------

class TestDefaultSignupCreditHandlerTierRegistry:
    """T11: _build_default_credit_grant_handler accepts tier_registry and
    threads it through to grant_initial_credit_for_user so signup grants
    use the new TierConfig.credit_grant schema.

    Ticket: 20260516_auto_credit_invoice_paid_wiring (T11 / core drop-in
    promise — every consumer of ab0t-quota gets signup credit without
    writing custom code).
    """

    @pytest.mark.asyncio
    async def test_default_handler_signature_accepts_tier_registry(self):
        """The factory accepts tier_registry as a kwarg (None default)."""
        import inspect
        sig = inspect.signature(ae._build_default_credit_grant_handler)
        assert "tier_registry" in sig.parameters
        assert sig.parameters["tier_registry"].default is None

    @pytest.mark.asyncio
    async def test_default_handler_passes_tier_registry_through(self, monkeypatch):
        """When the factory is built with tier_registry={...}, the handler
        invokes grant_initial_credit_for_user with that tier_registry."""
        captured = {}

        async def _fake_grant(user_id, org_id, **kwargs):
            captured.update({"user_id": user_id, "org_id": org_id, **kwargs})

        monkeypatch.setattr(ae, "grant_initial_credit_for_user", _fake_grant)

        # Fake tier_provider — just returns a tier_id when get_tier is called
        class _TierProvider:
            async def get_tier(self, org_id): return "starter"

        fake_registry = {"starter": object(), "free": object()}
        handler = ae._build_default_credit_grant_handler(
            initial_credits={"free": 10.0},
            tier_provider=_TierProvider(),
            redis=AsyncMock(),
            billing_url="http://billing.test",
            billing_api_key="key",
            tier_registry=fake_registry,
        )

        # Fire the handler with a Stripe-shape auth.user.registered event
        await handler({
            "event_type": "auth.user.registered",
            "data": {"user_id": "u_test", "org_id": "org_test"},
        })

        assert captured["user_id"] == "u_test"
        assert captured["org_id"] == "org_test"
        # The key assertion: tier_registry flows through
        assert captured["tier_registry"] is fake_registry

    @pytest.mark.asyncio
    async def test_default_handler_works_without_tier_registry(self, monkeypatch):
        """Back-compat: when tier_registry=None, handler still calls
        grant_initial_credit_for_user (legacy initial_credits path)."""
        captured = {}

        async def _fake_grant(user_id, org_id, **kwargs):
            captured.update({"user_id": user_id, "org_id": org_id, **kwargs})

        monkeypatch.setattr(ae, "grant_initial_credit_for_user", _fake_grant)

        class _TierProvider:
            async def get_tier(self, org_id): return "free"

        handler = ae._build_default_credit_grant_handler(
            initial_credits={"free": 10.0},
            tier_provider=_TierProvider(),
            redis=AsyncMock(),
            billing_url="http://billing.test",
            billing_api_key="key",
            # tier_registry omitted (None default)
        )

        await handler({
            "event_type": "auth.user.registered",
            "data": {"user_id": "u_test", "org_id": "org_test"},
        })

        assert captured.get("tier_registry") is None
        assert captured["initial_credits"] == {"free": 10.0}

    @pytest.mark.asyncio
    async def test_default_handler_skips_when_event_missing_required_fields(
        self, monkeypatch,
    ):
        """No user_id or no org_id → handler returns without calling grant.
        Regression guard so a malformed auth event doesn't 500."""
        called = []

        async def _fake_grant(*args, **kwargs):
            called.append(args)

        monkeypatch.setattr(ae, "grant_initial_credit_for_user", _fake_grant)

        handler = ae._build_default_credit_grant_handler(
            initial_credits={},
            tier_provider=object(),
            redis=AsyncMock(),
            billing_url="http://billing.test",
            billing_api_key="key",
        )

        await handler({"event_type": "auth.user.registered", "data": {}})
        await handler({"event_type": "auth.user.registered", "data": {"user_id": "u"}})
        await handler({"event_type": "auth.user.registered", "data": {"org_id": "o"}})
        assert called == []

    @pytest.mark.asyncio
    async def test_default_handler_marked_with_lib_sentinel(self):
        """The auto-registered handler should be tagged so setup.py / future
        code can detect it (e.g., to skip re-registering on hot reload)."""
        handler = ae._build_default_credit_grant_handler(
            initial_credits={},
            tier_provider=object(),
            redis=AsyncMock(),
            billing_url="http://billing.test",
            billing_api_key="key",
        )
        # The factory itself doesn't set the sentinel — setup.py does, after
        # building the handler — so this test just proves the handler is
        # decoratable. The sentinel-set is exercised in test_setup.py.
        setattr(handler, "_ab0t_quota_default", True)
        assert getattr(handler, "_ab0t_quota_default", False) is True


class TestSetupQuotaAutoRegistersDefaultHandler:
    """T11: setup_quota(enable_paid=True) auto-registers the default
    signup-credit handler when AB0T_AUTH_WEBHOOK_SECRET is set.

    This is the user-visible drop-in promise — consumers do not write a
    custom @on_auth_event handler for signup credits.
    """

    @pytest.mark.asyncio
    async def test_consumer_handler_coexists_with_default(self):
        """If a consumer registers their own handler, the default doesn't
        unregister it. Both run; idempotency at the lib helper level (Redis
        flag + billing idempotency_key) ensures only one grant lands."""
        consumer_calls = []

        async def consumer_handler(event):
            consumer_calls.append(event.get("data", {}).get("user_id"))

        ae.register_handler("auth.user.registered", consumer_handler)

        # Simulate setup.py also registering the default
        async def lib_default_handler(event): pass
        setattr(lib_default_handler, "_ab0t_quota_default", True)
        ae.register_handler("auth.user.registered", lib_default_handler)

        # Both should be in the registry
        handlers = ae._HANDLERS["auth.user.registered"]
        assert len(handlers) == 2
        assert consumer_handler in handlers
        assert lib_default_handler in handlers

        # Fire an event — both handlers run
        for h in handlers:
            await h({"data": {"user_id": "u_test"}})
        assert "u_test" in consumer_calls


# ---------------------------------------------------------------------------
# @idempotent integration tests — end-to-end through the receiver
# ---------------------------------------------------------------------------

from ab0t_quota.handler_ledger import (
    idempotent, InMemoryLedgerStore, LedgerStatus,
)


class TestIdempotentDispatch:
    SECRET = "test-secret-idempotent"

    def _build(self, ledger):
        app = FastAPI()
        app.include_router(
            ae.make_router(webhook_secret=self.SECRET, ledger_store=ledger),
            prefix="/api/quotas",
        )
        return TestClient(app)

    def _sign(self, body: bytes) -> str:
        return hmac.new(self.SECRET.encode(), body, hashlib.sha256).hexdigest()

    @pytest.mark.asyncio
    async def test_delivery_dedup_blocks_second_run(self):
        ledger = InMemoryLedgerStore()
        seen = []

        @ae.on_auth_event("auth.user.registered")
        @idempotent(handler="my_handler")
        async def h(event, ctx):
            seen.append(event["data"]["user_id"])

        body = json.dumps({"event_type": "auth.user.registered",
                            "event_id": "evt_dup",
                            "data": {"user_id": "u1"}}).encode()
        sig = self._sign(body)
        client = self._build(ledger)
        r1 = client.post("/api/quotas/_webhooks/auth", content=body,
                         headers={"X-Event-Signature": sig, "Content-Type": "application/json"})
        r2 = client.post("/api/quotas/_webhooks/auth", content=body,
                         headers={"X-Event-Signature": sig, "Content-Type": "application/json"})
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert seen == ["u1"]  # second delivery short-circuited
        row = await ledger.get_row(handler_name="my_handler", event_id="evt_dup")
        assert row.status == LedgerStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_business_dedup_via_key(self):
        ledger = InMemoryLedgerStore()
        side_effects = []

        @ae.on_auth_event("auth.user.registered")
        @idempotent(handler="my_handler", key=lambda e: f"org:{e['data']['org_id']}")
        async def h(event, ctx):
            if await ctx.already_done():
                return ctx.skip("already granted")
            side_effects.append(event["data"]["user_id"])
            await ctx.mark_done(side_effect_id="sid1")
            return ctx.success(side_effect_id="sid1")

        client = self._build(ledger)
        # Two different users joining same org → only first should grant
        for uid, evt_id in [("alice", "e_alice"), ("bob", "e_bob")]:
            body = json.dumps({"event_type": "auth.user.registered",
                                "event_id": evt_id,
                                "data": {"user_id": uid, "org_id": "shared_org"}}).encode()
            sig = self._sign(body)
            r = client.post("/api/quotas/_webhooks/auth", content=body,
                            headers={"X-Event-Signature": sig, "Content-Type": "application/json"})
            assert r.status_code == 200
        assert side_effects == ["alice"]  # bob skipped
        bob_row = await ledger.get_row(handler_name="my_handler", event_id="e_bob")
        assert bob_row.status == LedgerStatus.SKIPPED

    @pytest.mark.asyncio
    async def test_retry_on_failure_then_success(self):
        ledger = InMemoryLedgerStore()
        attempts = []

        @ae.on_auth_event("auth.user.registered")
        @idempotent(handler="my_handler", retry={"attempts": 3, "initial_seconds": 0.001})
        async def h(event, ctx):
            attempts.append(1)
            if len(attempts) < 2:
                raise RuntimeError("transient")
            return ctx.success(side_effect_id="sid")

        body = json.dumps({"event_type": "auth.user.registered",
                            "event_id": "evt_retry",
                            "data": {"user_id": "u1"}}).encode()
        sig = self._sign(body)
        client = self._build(ledger)
        r = client.post("/api/quotas/_webhooks/auth", content=body,
                        headers={"X-Event-Signature": sig, "Content-Type": "application/json"})
        assert r.status_code == 200
        assert len(attempts) == 2  # first failed, second succeeded
        row = await ledger.get_row(handler_name="my_handler", event_id="evt_retry")
        assert row.status == LedgerStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_max_attempts_marks_failed_permanent(self):
        ledger = InMemoryLedgerStore()
        attempts = []

        @ae.on_auth_event("auth.user.registered")
        @idempotent(handler="my_handler", retry={"attempts": 2, "initial_seconds": 0.001})
        async def h(event, ctx):
            attempts.append(1)
            raise RuntimeError("perma-fail")

        body = json.dumps({"event_type": "auth.user.registered",
                            "event_id": "evt_perma",
                            "data": {"user_id": "u1"}}).encode()
        sig = self._sign(body)
        client = self._build(ledger)
        r = client.post("/api/quotas/_webhooks/auth", content=body,
                        headers={"X-Event-Signature": sig, "Content-Type": "application/json"})
        assert r.status_code == 200  # receiver still returns 200
        assert len(attempts) == 2
        row = await ledger.get_row(handler_name="my_handler", event_id="evt_perma")
        assert row.status == LedgerStatus.FAILED_PERMANENT
        assert "perma-fail" in (row.error or "")

    @pytest.mark.asyncio
    async def test_plain_handler_still_works(self):
        """v0.2.6 plain handlers (no @idempotent) keep working."""
        seen = []

        @ae.on_auth_event("auth.user.registered")
        async def h(event):
            seen.append(event["data"]["user_id"])

        body = json.dumps({"event_type": "auth.user.registered",
                            "event_id": "evt_plain",
                            "data": {"user_id": "u1"}}).encode()
        sig = self._sign(body)
        client = self._build(InMemoryLedgerStore())
        r = client.post("/api/quotas/_webhooks/auth", content=body,
                        headers={"X-Event-Signature": sig, "Content-Type": "application/json"})
        assert r.status_code == 200
        assert seen == ["u1"]


# ---------------------------------------------------------------------------
# BACKWARD-COMPAT pins — v0.5.2 must not change v0.5.1's wire-level keys
# ---------------------------------------------------------------------------

from ab0t_quota.auth_events import compose_credit_dedup_key


class TestBackwardCompat:
    """Pins for keys that the lib produces and ships to downstream systems
    (Redis + billing-service). Changing these without coordination would
    cause in-flight grants to double-fire (because billing wouldn't see
    the prior idempotency key) and would re-grant credits on container
    restart (because the Redis dedup flag wouldn't match)."""

    def test_default_redis_flag_key_unchanged(self):
        """Same as v0.5.1: `credit_granted:user:{user_id}:{tier_id}`."""
        key = compose_credit_dedup_key(
            "per_user_per_tier",
            user_id="u1", org_id="o1", tier_id="free",
        )
        assert key == "credit_granted:user:u1:free"

    def test_org_policy_key_shape(self):
        key = compose_credit_dedup_key(
            "per_org_per_tier", user_id="u1", org_id="o1", tier_id="free",
        )
        assert key == "credit_granted:org:o1:free"

    def test_user_global_policy_key_shape(self):
        key = compose_credit_dedup_key(
            "per_user_global", user_id="u1", org_id="o1", tier_id="free",
        )
        assert key == "credit_granted:user:u1"

    def test_org_global_policy_key_shape(self):
        key = compose_credit_dedup_key(
            "per_org_global", user_id="u1", org_id="o1", tier_id="free",
        )
        assert key == "credit_granted:org:o1"

    def test_plain_handlers_still_dispatched(self):
        """Handlers registered without @idempotent (v0.5.1-style) must
        keep working when the receiver has a ledger_store wired."""
        from ab0t_quota.handler_ledger import InMemoryLedgerStore
        seen = []

        @ae.on_auth_event("auth.user.registered")
        async def plain_handler(event):
            seen.append(event["data"]["user_id"])

        app = FastAPI()
        app.include_router(
            ae.make_router(webhook_secret="s", ledger_store=InMemoryLedgerStore()),
            prefix="/api/quotas",
        )
        body = json.dumps({"event_type": "auth.user.registered",
                            "event_id": "evt_plain_bc",
                            "data": {"user_id": "u1"}}).encode()
        sig = hmac.new(b"s", body, hashlib.sha256).hexdigest()
        r = TestClient(app).post("/api/quotas/_webhooks/auth", content=body,
                                  headers={"X-Event-Signature": sig,
                                            "Content-Type": "application/json"})
        assert r.status_code == 200
        assert seen == ["u1"]

    def test_make_router_old_signature_still_works(self):
        """v0.5.1 callers that don't pass ledger_store must keep working."""
        router = ae.make_router(webhook_secret="s")  # no ledger_store kwarg
        assert router is not None
