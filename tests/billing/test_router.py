"""Tests for the billing router factory.

Uses FastAPI TestClient to verify route generation, auth enforcement,
and request handling without hitting real services.
"""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from ab0t_quota.billing import create_billing_router


async def mock_auth_reader(request: Request):
    """Simulated auth dependency returning a user with org_id."""
    class User:
        org_id = "test-org-123"
        email = "test@example.com"
    return User()


async def mock_auth_admin(request: Request):
    return await mock_auth_reader(request)


def _create_app(**kwargs) -> FastAPI:
    """Create a test FastAPI app with the billing router mounted."""
    app = FastAPI()
    router = create_billing_router(**kwargs)
    app.include_router(router)
    return app


class TestRouterCreation:
    def test_minimal_config_creates_public_routes(self):
        """Without auth deps, only public routes are created."""
        app = _create_app(
            payment_url="http://test:8005",
            payment_api_key="key",
            billing_url="http://test:8002",
            billing_api_key="key",
            consumer_org_id="org-1",
        )
        paths = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/api/payments/plans" in paths
        assert "/api/payments/checkout/init" in paths
        assert "/api/webhooks/stripe" in paths
        assert "/checkout/success" in paths
        # Auth routes should NOT be present
        assert "/api/billing/balance" not in paths
        assert "/api/payments/subscriptions" not in paths

    def test_full_config_creates_all_routes(self):
        """With auth deps, all 20 routes are created."""
        app = _create_app(
            payment_url="http://test:8005",
            payment_api_key="key",
            billing_url="http://test:8002",
            billing_api_key="key",
            consumer_org_id="org-1",
            auth_reader=mock_auth_reader,
            auth_admin=mock_auth_admin,
        )
        paths = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/api/billing/balance" in paths
        assert "/api/billing/transactions" in paths
        assert "/api/payments/subscriptions" in paths
        assert "/api/payments/methods" in paths
        assert "/api/payments/portal" in paths
        assert "/api/payments/topup" in paths
        assert "/api/payments/checkout/{plan_id}" in paths

    def test_custom_prefix(self):
        """Prefix changes all route paths."""
        app = _create_app(
            payment_url="http://test:8005",
            payment_api_key="key",
            billing_url="http://test:8002",
            billing_api_key="key",
            consumer_org_id="org-1",
            prefix="/v2",
        )
        paths = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/v2/payments/plans" in paths
        assert "/v2/webhooks/stripe" in paths

    def test_missing_payment_url_raises(self):
        with pytest.raises(ValueError, match="payment_url"):
            create_billing_router(
                payment_url="",
                payment_api_key="key",
                billing_url="http://test:8002",
                billing_api_key="key",
                consumer_org_id="org-1",
            )

    def test_missing_consumer_org_raises(self):
        with pytest.raises(ValueError, match="consumer_org_id"):
            create_billing_router(
                payment_url="http://test:8005",
                payment_api_key="key",
                billing_url="http://test:8002",
                billing_api_key="key",
                consumer_org_id="",
            )


class TestPublicRoutes:
    @pytest.fixture
    def client(self):
        app = _create_app(
            payment_url="http://test:8005",
            payment_api_key="key",
            billing_url="http://test:8002",
            billing_api_key="key",
            consumer_org_id="org-1",
        )
        return TestClient(app)

    def test_checkout_success_page_route_registered(self):
        """Checkout success page route must be registered."""
        app = _create_app(
            payment_url="http://test:8005",
            payment_api_key="key",
            billing_url="http://test:8002",
            billing_api_key="key",
            consumer_org_id="org-1",
        )
        paths = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/checkout/success" in paths

    def test_webhook_requires_signature(self, client):
        """Webhook route must reject requests without Stripe-Signature."""
        response = client.post(
            "/api/webhooks/stripe",
            json={"type": "test"},
        )
        assert response.status_code == 400
        assert "Stripe-Signature" in response.json()["detail"]

    def test_checkout_complete_validates_input(self):
        """Checkout complete must reject missing session_id.

        /checkout/complete became auth-required in audit ticket 20260428
        (was previously open — see security note in router.py docstring).
        Build a separate client with auth deps so the route is mounted at
        all, then assert input validation still fires.
        """
        app = _create_app(
            payment_url="http://test:8005",
            payment_api_key="key",
            billing_url="http://test:8002",
            billing_api_key="key",
            consumer_org_id="org-1",
            auth_reader=mock_auth_reader,
            auth_admin=mock_auth_admin,
        )
        response = TestClient(app).post(
            "/api/payments/checkout/complete",
            json={},
        )
        assert response.status_code == 422


class TestRouteOrdering:
    """Verify that static routes (init, complete, anonymous) are registered
    before the {plan_id} catch-all to prevent path conflicts."""

    @pytest.fixture
    def app(self):
        # auth_admin required when auth_reader is provided — strict mode
        # introduced by audit ticket 20260428 to stop the silent permission
        # collapse where any authenticated user could hit admin routes.
        return _create_app(
            payment_url="http://test:8005",
            payment_api_key="key",
            billing_url="http://test:8002",
            billing_api_key="key",
            consumer_org_id="org-1",
            auth_reader=mock_auth_reader,
            auth_admin=mock_auth_admin,
        )

    def test_init_not_caught_by_plan_id(self, app):
        paths = [r.path for r in app.routes if hasattr(r, "path")]
        init_idx = next(i for i, p in enumerate(paths) if "checkout/init" in p)
        plan_idx = next(i for i, p in enumerate(paths) if "{plan_id}" in p)
        assert init_idx < plan_idx, "checkout/init must be before {plan_id}"

    def test_complete_not_caught_by_plan_id(self, app):
        # Filter to only checkout routes to avoid noise from FastAPI defaults
        checkout_paths = [
            r.path for r in app.routes
            if hasattr(r, "path") and "checkout" in r.path
        ]
        complete_idx = next(i for i, p in enumerate(checkout_paths) if "complete" in p)
        plan_idx = next(
            (i for i, p in enumerate(checkout_paths) if "{plan_id}" in p and "anonymous" not in p),
            len(checkout_paths),  # if not found (no auth → no {plan_id} route)
        )
        assert complete_idx < plan_idx, "checkout/complete must be before {plan_id}"

    def test_anonymous_not_caught_by_plan_id(self, app):
        paths = [r.path for r in app.routes if hasattr(r, "path")]
        anon_idx = next(i for i, p in enumerate(paths) if "anonymous" in p)
        plan_idx = next(i for i, p in enumerate(paths) if "checkout/{plan_id}" in p and "anonymous" not in p)
        assert anon_idx < plan_idx, "anonymous/{plan_id} must be before {plan_id}"


class TestResolveIdToTier:
    """T0e — multi-ID-space, drift-resistant plan/price → tier resolver.

    Priority order: explicit consumer maps → plan metadata.tier_id →
    Stripe price→plan match + metadata → display-name fallback (logs WARNING).

    Ticket: 20260516_auto_credit_invoice_paid_wiring (T0e / AC0e / AC16).
    """

    def _fake_plans_response(self, plans_list):
        """Build a minimal PlansResponse with the given PlanItem-ish dicts."""
        from ab0t_quota.billing.models import PlanItem, PlanPrice, PlansResponse
        plans = []
        for p in plans_list:
            prices = [
                PlanPrice(price_id=pr["price_id"], stripe_price_id=pr.get("stripe_price_id"))
                for pr in p.get("prices", [])
            ]
            kwargs = {"plan_id": p["plan_id"], "name": p.get("name", ""), "prices": prices}
            if "metadata" in p:
                kwargs["metadata"] = p["metadata"]  # PlanItem extra=allow accepts it
            plans.append(PlanItem(**kwargs))
        return PlansResponse(plans=plans, count=len(plans))

    def _fake_payment_client(self, plans_response):
        """Async-mock payment client whose get_plans returns the canned response."""
        class _Payment:
            async def get_plans(self, *args, **kwargs):
                return plans_response
        return _Payment()

    @pytest.mark.asyncio
    async def test_priority_1_explicit_plan_to_tier_map_wins(self):
        """Explicit map in quota-config.json's billing_integration takes
        precedence over EVERYTHING. No payment-service round-trip needed."""
        from ab0t_quota.billing.router import _resolve_id_to_tier
        # Payment-service intentionally not called — pass a client that would
        # raise if hit, to prove the explicit map short-circuits the lookup.
        class _Fail:
            async def get_plans(self, *args, **kwargs):
                raise AssertionError("Should not call payment-service when explicit map hits")

        tid = await _resolve_id_to_tier(
            "plan-uuid-starter-monthly",
            tier_map={},
            payment=_Fail(),
            consumer_org_id="org-test",
            explicit_plan_to_tier={"plan-uuid-starter-monthly": "starter"},
        )
        assert tid == "starter"

    @pytest.mark.asyncio
    async def test_priority_1_explicit_stripe_price_to_tier_map_wins(self):
        """Same priority as plan_to_tier — Stripe price IDs in the explicit
        map resolve without hitting payment-service."""
        from ab0t_quota.billing.router import _resolve_id_to_tier
        class _Fail:
            async def get_plans(self, *args, **kwargs):
                raise AssertionError("Should not call payment-service")

        tid = await _resolve_id_to_tier(
            "price_1234abcd",
            tier_map={},
            payment=_Fail(),
            consumer_org_id="org-test",
            explicit_stripe_price_to_tier={"price_1234abcd": "pro"},
        )
        assert tid == "pro"

    @pytest.mark.asyncio
    async def test_priority_2_plan_metadata_tier_id(self):
        """When seed_plans.sh seeds metadata={tier_id: 'starter'}, the resolver
        reads it. No display-name dependency."""
        from ab0t_quota.billing.router import _resolve_id_to_tier
        payment = self._fake_payment_client(self._fake_plans_response([
            {"plan_id": "plan-abc", "name": "Anything Renamed",
             "metadata": {"tier_id": "starter"}},
        ]))
        tid = await _resolve_id_to_tier(
            "plan-abc",
            tier_map={"anything renamed": "starter"},  # works either way
            payment=payment,
            consumer_org_id="org-test",
        )
        assert tid == "starter"

    @pytest.mark.asyncio
    async def test_priority_3_stripe_price_id_resolves_via_parent_plan_metadata(self):
        """Stripe price ID from subscription items → find parent plan →
        read metadata.tier_id. The T3 downgrade-reset path uses this."""
        from ab0t_quota.billing.router import _resolve_id_to_tier
        payment = self._fake_payment_client(self._fake_plans_response([
            {"plan_id": "plan-pro", "name": "Pro Plan",
             "metadata": {"tier_id": "pro"},
             "prices": [{"price_id": "price-internal", "stripe_price_id": "price_stripe_xyz"}]},
        ]))
        tid = await _resolve_id_to_tier(
            "price_stripe_xyz",  # the Stripe price ID from subscription.items
            tier_map={},
            payment=payment,
            consumer_org_id="org-test",
        )
        assert tid == "pro"

    @pytest.mark.asyncio
    async def test_priority_4_display_name_fallback_with_warning(self, caplog):
        """Last-resort: plan_id matches but no metadata.tier_id → display
        name → tier_map. Logs a WARNING so drift is visible."""
        import logging
        from ab0t_quota.billing.router import _resolve_id_to_tier
        payment = self._fake_payment_client(self._fake_plans_response([
            {"plan_id": "plan-legacy", "name": "Starter"},  # no metadata
        ]))
        with caplog.at_level(logging.WARNING, logger="ab0t_quota.billing"):
            tid = await _resolve_id_to_tier(
                "plan-legacy",
                tier_map={"starter": "starter"},
                payment=payment,
                consumer_org_id="org-test",
            )
        assert tid == "starter"
        assert any("display-name fallback" in r.message for r in caplog.records), \
            "WARNING should fire so display-name drift is visible to operators"

    @pytest.mark.asyncio
    async def test_unresolved_identifier_returns_none(self):
        """Identifier doesn't match anything → None (don't silently default
        to 'free' or any other tier). T2 dispatch reads None as
        skipped_no_tier and logs accordingly."""
        from ab0t_quota.billing.router import _resolve_id_to_tier
        payment = self._fake_payment_client(self._fake_plans_response([
            {"plan_id": "plan-other", "name": "Other"},
        ]))
        tid = await _resolve_id_to_tier(
            "totally-unknown-id",
            tier_map={"other": "other_tier"},
            payment=payment,
            consumer_org_id="org-test",
        )
        assert tid is None

    @pytest.mark.asyncio
    async def test_empty_identifier_returns_none(self):
        """Defensive: empty identifier → None without any lookup."""
        from ab0t_quota.billing.router import _resolve_id_to_tier
        class _Fail:
            async def get_plans(self, *args, **kwargs):
                raise AssertionError("Should not call payment-service")

        tid = await _resolve_id_to_tier(
            "", tier_map={}, payment=_Fail(), consumer_org_id="org-test",
        )
        assert tid is None

    @pytest.mark.asyncio
    async def test_back_compat_shim_delegates_to_new_resolver(self):
        """_resolve_plan_to_tier (old name, kept for existing call sites)
        delegates to _resolve_id_to_tier with the same priorities."""
        from ab0t_quota.billing.router import _resolve_plan_to_tier
        class _Fail:
            async def get_plans(self, *args, **kwargs):
                raise AssertionError("Should hit explicit map first")

        tid = await _resolve_plan_to_tier(
            "plan-x",
            tier_map={"x": "tier_x"},
            payment=_Fail(),
            consumer_org_id="org-test",
            explicit_plan_to_tier={"plan-x": "starter"},
        )
        assert tid == "starter"


class TestStripeWebhookProxySignatureVerify:
    """T1 — Stripe signature verification in the lib proxy.

    AC1: bad sig → 400; missing header → 400; valid sig → 200; unset secret → 500
    AC14: forward to payment-service works after verify (covered in T2 once
          dispatch lands; this test class focuses on T1 sig-verify alone).

    Ticket: 20260516_auto_credit_invoice_paid_wiring (T1).
    """

    @pytest.fixture
    def webhook_secret(self):
        # A 32-byte hex secret; matches what Stripe would generate
        return "whsec_test_" + "a" * 64

    @pytest.fixture
    def signed_event_payload(self, webhook_secret, monkeypatch):
        """Build a real Stripe-signed payload + header. We don't mock Stripe's
        own SDK because the verification logic IS what we're testing."""
        import stripe, time, hmac, hashlib, json as _json
        payload = _json.dumps({
            "id": "evt_test_t1",
            "object": "event",
            "type": "checkout.session.completed",
            "data": {"object": {"id": "cs_test_t1"}},
        }).encode()
        ts = int(time.time())
        signed_payload = f"{ts}.{payload.decode()}".encode()
        sig = hmac.new(webhook_secret.encode(), signed_payload, hashlib.sha256).hexdigest()
        header = f"t={ts},v1={sig}"
        return payload, header

    def _mount_proxy(self, monkeypatch, webhook_secret, payment_forward_raises=None):
        """Mount the proxy and stub the payment forward call."""
        monkeypatch.setenv("AB0T_QUOTA_STRIPE_WEBHOOK_SECRET", webhook_secret)

        # Stub PaymentServiceClient.forward_webhook to either succeed or raise
        from ab0t_quota.billing import clients as _clients

        async def _fake_forward(self, body, signature):
            if payment_forward_raises is not None:
                raise payment_forward_raises
            return {"ok": True}

        monkeypatch.setattr(_clients.PaymentServiceClient, "forward_webhook",
                            _fake_forward, raising=True)

        app = _create_app(
            payment_url="http://test:8005",
            payment_api_key="key",
            billing_url="http://test:8002",
            billing_api_key="key",
            consumer_org_id="org-1",
        )
        return TestClient(app)

    def test_missing_signature_header_returns_400(self, webhook_secret, monkeypatch):
        client = self._mount_proxy(monkeypatch, webhook_secret)
        r = client.post("/api/webhooks/stripe", content=b'{"type":"test"}')
        assert r.status_code == 400
        assert "Missing Stripe-Signature" in r.json()["detail"]

    def test_unset_secret_returns_500(self, monkeypatch):
        """When neither AB0T_QUOTA_STRIPE_WEBHOOK_SECRET nor STRIPE_WEBHOOK_SECRET
        is set, the proxy must 500 (Stripe will retry; ops fixes config)."""
        monkeypatch.delenv("AB0T_QUOTA_STRIPE_WEBHOOK_SECRET", raising=False)
        monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
        app = _create_app(
            payment_url="http://test:8005",
            payment_api_key="key",
            billing_url="http://test:8002",
            billing_api_key="key",
            consumer_org_id="org-1",
        )
        client = TestClient(app)
        r = client.post(
            "/api/webhooks/stripe",
            content=b'{"type":"test"}',
            headers={"Stripe-Signature": "t=1234,v1=abc"},
        )
        assert r.status_code == 500
        assert "Server config error" in r.json()["detail"]

    def test_invalid_signature_returns_400(self, webhook_secret, monkeypatch):
        """Tampered signature → 400 (Stripe stops retrying; ops investigates)."""
        client = self._mount_proxy(monkeypatch, webhook_secret)
        r = client.post(
            "/api/webhooks/stripe",
            content=b'{"id":"evt_x","type":"checkout.session.completed","data":{"object":{}}}',
            headers={"Stripe-Signature": "t=1234,v1=deadbeef"},
        )
        assert r.status_code == 400
        assert "Invalid signature" in r.json()["detail"]

    def test_valid_signature_passes_through(
        self, webhook_secret, signed_event_payload, monkeypatch,
    ):
        """A correctly-signed event verifies and forwards successfully."""
        client = self._mount_proxy(monkeypatch, webhook_secret)
        payload, header = signed_event_payload
        r = client.post(
            "/api/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": header},
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True}

    def test_generic_fallback_secret_no_longer_read(
            self, webhook_secret, signed_event_payload, monkeypatch):
        """REPLACES test_fallback_secret_env_var (T-4/ENV-05, pack 20260721):
        the old test encoded the DEFECT — the generic STRIPE_WEBHOOK_SECRET
        (the payment service's convention) verifying this route's webhooks.
        The generic name is no longer read: a request signed with it must be
        refused as UNCONFIGURED. Companion contract test:
        tests/test_phase2_ambient_20260721.py::test_stripe_secret_not_harvested."""
        monkeypatch.delenv("AB0T_QUOTA_STRIPE_WEBHOOK_SECRET", raising=False)
        monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", webhook_secret)

        from ab0t_quota.billing import clients as _clients
        async def _fake_forward(self, body, signature):
            return {"ok": True}
        monkeypatch.setattr(_clients.PaymentServiceClient, "forward_webhook",
                            _fake_forward, raising=True)

        app = _create_app(
            payment_url="http://test:8005",
            payment_api_key="key",
            billing_url="http://test:8002",
            billing_api_key="key",
            consumer_org_id="org-1",
        )
        client = TestClient(app)
        payload, header = signed_event_payload
        r = client.post(
            "/api/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": header},
        )
        assert r.status_code == 500, r.text
        assert "config error" in r.text.lower(), \
            "generic-secret-signed webhook must be refused as unconfigured"

    def test_namespaced_env_var_takes_precedence(
        self, webhook_secret, signed_event_payload, monkeypatch,
    ):
        """When both are set, AB0T_QUOTA_STRIPE_WEBHOOK_SECRET wins.
        We set STRIPE_WEBHOOK_SECRET to a WRONG value and confirm verify
        still succeeds against the namespaced one."""
        monkeypatch.setenv("AB0T_QUOTA_STRIPE_WEBHOOK_SECRET", webhook_secret)
        monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_wrong_secret_should_be_ignored")

        from ab0t_quota.billing import clients as _clients
        async def _fake_forward(self, body, signature):
            return {"ok": True}
        monkeypatch.setattr(_clients.PaymentServiceClient, "forward_webhook",
                            _fake_forward, raising=True)

        app = _create_app(
            payment_url="http://test:8005",
            payment_api_key="key",
            billing_url="http://test:8002",
            billing_api_key="key",
            consumer_org_id="org-1",
        )
        client = TestClient(app)
        payload, header = signed_event_payload
        r = client.post(
            "/api/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": header},
        )
        assert r.status_code == 200, r.text

    def test_payment_service_forward_error_returns_503(
        self, webhook_secret, signed_event_payload, monkeypatch,
    ):
        """When payment-service forwarding fails (e.g. transient 5xx, or
        4xx during T0d rollout window), we return 503 so Stripe retries.
        The grant work (T2) is idempotent — retry is safe + necessary so
        payment-service eventually updates its invoice rows."""
        from ab0t_quota.billing.clients import PaymentServiceError
        client = self._mount_proxy(
            monkeypatch, webhook_secret,
            payment_forward_raises=PaymentServiceError(status_code=500, detail="forward broke"),
        )
        payload, header = signed_event_payload
        r = client.post(
            "/api/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": header},
        )
        assert r.status_code == 503
        assert "will retry" in r.json()["detail"]


class TestStripeWebhookProxyInvoicePaidDispatch:
    """T2 — dispatch invoice.payment_succeeded to handle_subscription_invoice_paid().

    AC2-AC6 + AC15 + AC10 covered here via real HMAC-signed events,
    real Stripe SDK verification, and an in-process stubbed BillingServiceClient.

    Ticket: 20260516_auto_credit_invoice_paid_wiring (T2).
    """

    @pytest.fixture
    def webhook_secret(self):
        return "whsec_test_t2_" + "b" * 60

    def _sign(self, payload_bytes, secret):
        import time, hmac, hashlib
        ts = int(time.time())
        signed = f"{ts}.{payload_bytes.decode()}".encode()
        sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        return f"t={ts},v1={sig}"

    def _invoice_paid_event(self, *, org_id="org-test", plan_id="plan-starter",
                            invoice_id="in_test_t2",
                            event_type="invoice.payment_succeeded"):
        """Build a Stripe-shape paid-invoice event payload.

        Mirrors what Stripe sends after Phase 2.1's subscription_data.metadata
        propagation lands org_id + plan_id on invoice.metadata.

        event_type defaults to "invoice.payment_succeeded" for back-compat with
        existing tests. Pass "invoice.paid" to simulate the newer Stripe API
        version event for the same business outcome (X2, ticket
        20260518_post_upgrade_credit_and_ux_propagation).
        """
        import json as _json
        return _json.dumps({
            "id": f"evt_{invoice_id}",
            "object": "event",
            "type": event_type,
            "data": {
                "object": {
                    "id": invoice_id,
                    "object": "invoice",
                    "amount_paid": 2900,  # cents — irrelevant for dispatch test
                    "subscription": "sub_test_t2",
                    "metadata": {"org_id": org_id, "plan_id": plan_id},
                }
            },
        }).encode()

    def _build_app(self, monkeypatch, webhook_secret, *, tier_registry,
                   apply_grant_response, apply_grant_raises=None,
                   payment_forward_raises=None):
        """Mount the proxy with stubbed BillingServiceClient + PaymentServiceClient
        for in-process testing.

        - tier_registry: dict passed to create_billing_router(tier_registry=...)
        - apply_grant_response: dict returned by stubbed BillingServiceClient.apply_credit_grant
        - apply_grant_raises: exception to raise from apply_credit_grant (transient/permanent test)
        - payment_forward_raises: exception from forward_webhook (T1 503 test)
        """
        monkeypatch.setenv("AB0T_QUOTA_STRIPE_WEBHOOK_SECRET", webhook_secret)

        from ab0t_quota.billing import clients as _clients

        # Capture apply_credit_grant calls for assertion
        captured = {"apply_credit_grant_calls": []}

        async def _fake_apply_grant(self, **kwargs):
            captured["apply_credit_grant_calls"].append(kwargs)
            if apply_grant_raises is not None:
                raise apply_grant_raises
            return apply_grant_response
        monkeypatch.setattr(_clients.BillingServiceClient, "apply_credit_grant",
                            _fake_apply_grant, raising=True)

        async def _fake_forward(self, body, signature):
            if payment_forward_raises is not None:
                raise payment_forward_raises
            return {"ok": True}
        monkeypatch.setattr(_clients.PaymentServiceClient, "forward_webhook",
                            _fake_forward, raising=True)

        # Provide a payment client that resolves plan-starter → starter via
        # explicit map (set up below); get_plans stubbed not to be called.
        async def _fake_get_plans(self, *args, **kwargs):
            raise AssertionError("get_plans should not be called when explicit map hits")
        monkeypatch.setattr(_clients.PaymentServiceClient, "get_plans",
                            _fake_get_plans, raising=True)

        # Write a temporary quota-config.json with the explicit plan→tier map
        # so the resolver short-circuits without hitting payment-service.
        import json as _json
        import tempfile, os as _os
        config_dir = tempfile.mkdtemp(prefix="qc_t2_")
        config_path = _os.path.join(config_dir, "quota-config.json")
        with open(config_path, "w") as f:
            _json.dump({
                "tiers": [{"tier_id": "starter", "display_name": "Starter", "limits": {}}],
                "billing_integration": {
                    "plan_to_tier": {"plan-starter": "starter"},
                },
            }, f)

        app = _create_app(
            payment_url="http://test:8005",
            payment_api_key="key",
            billing_url="http://test:8002",
            billing_api_key="key",
            consumer_org_id="org-consumer",
            quota_config_path=config_path,
            tier_registry=tier_registry,
        )
        return TestClient(app), captured

    def _starter_tier_with_grant(self):
        """A TierConfig that declares subscription_with_credits + use_it_or_lose_it."""
        from ab0t_quota.models.core import (
            TierConfig, BillingModel, Price, BillingPeriod,
            CreditGrant, CreditTrigger, CreditLifecycle, CreditDestination,
        )
        from decimal import Decimal
        return TierConfig(
            tier_id="starter",
            display_name="Starter",
            billing_model=BillingModel.SUBSCRIPTION_WITH_CREDITS,
            price=Price(amount_per_period=Decimal("29.00"), currency="USD",
                        period=BillingPeriod.MONTH),
            credit_grant=CreditGrant(
                trigger=CreditTrigger.SUBSCRIPTION_INVOICE_PAID,
                amount_per_period=Decimal("29.00"),
                currency="USD",
                lifecycle=CreditLifecycle.USE_IT_OR_LOSE_IT,
                destination=CreditDestination.SUBSCRIPTION_CREDIT,
                reset_on_downgrade=True,
                reset_on_upgrade=False,
            ),
        )

    def test_applied_path_calls_grant_and_returns_200(self, webhook_secret, monkeypatch):
        """The happy path: subscription_with_credits tier → grant fires →
        billing /apply-credit-grant called with correct destination + lifecycle.
        Proxy returns 200; payment-service forward succeeds."""
        registry = {"starter": self._starter_tier_with_grant()}
        client, captured = self._build_app(
            monkeypatch, webhook_secret,
            tier_registry=registry,
            apply_grant_response={"new_balance": "29.00", "applied": True},
        )
        payload = self._invoice_paid_event()
        r = client.post(
            "/api/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": self._sign(payload, webhook_secret)},
        )
        assert r.status_code == 200, r.text
        # Grant was called exactly once
        assert len(captured["apply_credit_grant_calls"]) == 1
        call = captured["apply_credit_grant_calls"][0]
        # Correct routing: destination = subscription_credit, lifecycle = use_it_or_lose_it
        assert call["org_id"] == "org-test"
        assert call["destination"] == "subscription_credit"
        assert call["lifecycle"] == "use_it_or_lose_it"
        assert call["source"] == "in_test_t2"
        assert call["source_tier"] == "starter"
        # Idempotency key follows the documented contract
        assert call["idempotency_key"] == "invoice:in_test_t2:credit_grant"

    # ------------------------------------------------------------------
    # X2 — both invoice.payment_succeeded AND invoice.paid must dispatch.
    # Ticket: 20260518_post_upgrade_credit_and_ux_propagation.
    #
    # Stripe emits both event types for the same business outcome (an
    # invoice was paid in full). The lib MUST accept both — older Stripe
    # API versions emit invoice.payment_succeeded; newer versions emit
    # invoice.paid; some accounts emit both. The idempotency key is
    # invoice-id-based so billing-service dedupes regardless of how many
    # event types arrive for one invoice.
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("event_type", [
        "invoice.payment_succeeded",
        "invoice.paid",
    ])
    def test_dispatch_fires_for_both_paid_invoice_event_types(
        self, event_type, webhook_secret, monkeypatch
    ):
        """X2 — both invoice.payment_succeeded and invoice.paid trigger
        the subscription_credit grant. Regression guard: a refactor that
        narrows the match condition (e.g. drops one event type) breaks
        the credit-grant path silently in production. This test fails
        loudly the moment that happens.
        """
        registry = {"starter": self._starter_tier_with_grant()}
        client, captured = self._build_app(
            monkeypatch, webhook_secret,
            tier_registry=registry,
            apply_grant_response={"new_balance": "29.00", "applied": True},
        )
        payload = self._invoice_paid_event(event_type=event_type)
        r = client.post(
            "/api/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": self._sign(payload, webhook_secret)},
        )
        assert r.status_code == 200, r.text
        assert len(captured["apply_credit_grant_calls"]) == 1, (
            f"Expected exactly one grant call for {event_type}; "
            f"got {len(captured['apply_credit_grant_calls'])}. "
            f"If zero: dispatch match condition was narrowed."
        )
        call = captured["apply_credit_grant_calls"][0]
        # Routing must be identical regardless of event type
        assert call["org_id"] == "org-test"
        assert call["destination"] == "subscription_credit"
        assert call["lifecycle"] == "use_it_or_lose_it"
        assert call["source"] == "in_test_t2"
        assert call["source_tier"] == "starter"
        # Idempotency key is invoice-id-based, NOT event-type-based.
        # This is the dedup contract that makes both events safe to receive.
        assert call["idempotency_key"] == "invoice:in_test_t2:credit_grant"

    def test_idempotency_key_stable_across_event_types_for_same_invoice(
        self, webhook_secret, monkeypatch
    ):
        """X2 — security/payments invariant: if Stripe emits BOTH event
        types for the same paid invoice (some accounts do), the lib calls
        billing-service twice with the SAME idempotency key. Billing-
        service's existing dedup-by-idempotency-key behavior then ensures
        only one actual grant lands. This test validates the lib half of
        that contract; billing-service has its own dedup tests.
        """
        registry = {"starter": self._starter_tier_with_grant()}
        client, captured = self._build_app(
            monkeypatch, webhook_secret,
            tier_registry=registry,
            apply_grant_response={"new_balance": "29.00", "applied": True},
        )

        # POST both event types for the SAME invoice
        for et in ("invoice.payment_succeeded", "invoice.paid"):
            payload = self._invoice_paid_event(event_type=et)
            r = client.post(
                "/api/webhooks/stripe",
                content=payload,
                headers={"Stripe-Signature": self._sign(payload, webhook_secret)},
            )
            assert r.status_code == 200, r.text

        # Lib called billing exactly twice (no internal dedup at lib layer)
        assert len(captured["apply_credit_grant_calls"]) == 2
        # Both calls used the same idempotency_key — the dedup contract
        # billing-service relies on to deduplicate
        keys = [c["idempotency_key"] for c in captured["apply_credit_grant_calls"]]
        assert keys[0] == keys[1] == "invoice:in_test_t2:credit_grant", (
            f"Idempotency keys diverged across event types: {keys}. "
            f"This breaks billing-service dedup; same invoice would be "
            f"credited twice in production."
        )

    def test_invoice_paid_with_invalid_signature_still_rejected(
        self, webhook_secret, monkeypatch
    ):
        """Security: adding invoice.paid to the dispatch match MUST NOT
        weaken signature verification. A bad signature on an invoice.paid
        event must still be rejected at the route's verifier (router.py
        :703-710) BEFORE dispatch logic runs. No auth-bypass via newer
        event type.
        """
        registry = {"starter": self._starter_tier_with_grant()}
        client, captured = self._build_app(
            monkeypatch, webhook_secret,
            tier_registry=registry,
            apply_grant_response={"should_not_be_called": True},
        )
        payload = self._invoice_paid_event(event_type="invoice.paid")
        # Deliberately-bad signature
        r = client.post(
            "/api/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": "t=0,v1=deadbeef"},
        )
        assert r.status_code == 400, r.text
        assert "signature" in r.json()["detail"].lower()
        # Dispatch did NOT run — billing was never called
        assert len(captured["apply_credit_grant_calls"]) == 0

    def test_skipped_no_grant_when_tier_is_capacity_only(self, webhook_secret, monkeypatch):
        """A capacity_only tier (no credit_grant) → handler returns
        skipped_no_grant; NO billing call; proxy still returns 200."""
        from ab0t_quota.models.core import TierConfig, BillingModel
        registry = {
            "starter": TierConfig(
                tier_id="starter", display_name="Starter",
                billing_model=BillingModel.CAPACITY_ONLY,
            ),
        }
        client, captured = self._build_app(
            monkeypatch, webhook_secret,
            tier_registry=registry,
            apply_grant_response={"should_not_be_called": True},
        )
        payload = self._invoice_paid_event()
        r = client.post(
            "/api/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": self._sign(payload, webhook_secret)},
        )
        assert r.status_code == 200, r.text
        # NO grant call
        assert len(captured["apply_credit_grant_calls"]) == 0

    def test_skipped_no_metadata_when_invoice_lacks_org_id(self, webhook_secret, monkeypatch):
        """Pre-Phase-2.1 subscriptions (or non-subscription invoices) have
        no org_id in invoice.metadata → handler returns skipped_no_metadata;
        NO billing call; proxy returns 200."""
        registry = {"starter": self._starter_tier_with_grant()}
        client, captured = self._build_app(
            monkeypatch, webhook_secret,
            tier_registry=registry,
            apply_grant_response={"should_not_be_called": True},
        )
        # Build invoice without metadata.org_id
        import json as _json
        payload = _json.dumps({
            "id": "evt_no_meta",
            "object": "event",
            "type": "invoice.payment_succeeded",
            "data": {"object": {"id": "in_no_meta", "object": "invoice",
                                 "amount_paid": 2900, "metadata": {}}},
        }).encode()
        r = client.post(
            "/api/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": self._sign(payload, webhook_secret)},
        )
        assert r.status_code == 200, r.text
        assert len(captured["apply_credit_grant_calls"]) == 0

    def test_deferred_transient_returns_503(self, webhook_secret, monkeypatch):
        """billing-service returns 503 (transient) → handler returns
        deferred_transient → proxy returns 503 so Stripe retries.
        Idempotency on invoice:{id}:credit_grant makes the retry safe.

        AC15.
        """
        from ab0t_quota.billing.clients import BillingServiceError
        registry = {"starter": self._starter_tier_with_grant()}
        client, captured = self._build_app(
            monkeypatch, webhook_secret,
            tier_registry=registry,
            apply_grant_response=None,
            apply_grant_raises=BillingServiceError(status_code=503, detail="upstream timeout"),
        )
        payload = self._invoice_paid_event()
        r = client.post(
            "/api/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": self._sign(payload, webhook_secret)},
        )
        assert r.status_code == 503
        assert "will retry" in r.json()["detail"]
        # Grant was attempted (and failed)
        assert len(captured["apply_credit_grant_calls"]) == 1

    def test_failed_permanent_returns_200_with_error_log(self, webhook_secret, monkeypatch, caplog):
        """billing-service returns 400 (permanent) → handler returns
        failed_permanent → proxy returns 200 (don't retry-storm) but logs
        ERROR for paging."""
        import logging
        from ab0t_quota.billing.clients import BillingServiceError
        registry = {"starter": self._starter_tier_with_grant()}
        client, captured = self._build_app(
            monkeypatch, webhook_secret,
            tier_registry=registry,
            apply_grant_response=None,
            apply_grant_raises=BillingServiceError(status_code=400, detail="bad request"),
        )
        payload = self._invoice_paid_event()
        with caplog.at_level(logging.ERROR, logger="ab0t_quota.billing"):
            r = client.post(
                "/api/webhooks/stripe",
                content=payload,
                headers={"Stripe-Signature": self._sign(payload, webhook_secret)},
            )
        assert r.status_code == 200, r.text
        # Grant was attempted (and failed permanently)
        assert len(captured["apply_credit_grant_calls"]) == 1
        # ERROR-level log emitted for paging
        assert any("invoice_paid_failed_permanent_PAGE" in rec.message
                   for rec in caplog.records), \
            "Permanent failure must page via ERROR-level log"

    def test_idempotency_replay_safe(self, webhook_secret, monkeypatch):
        """Same invoice delivered twice → handler is called twice but uses
        the same idempotency_key both times. billing-service dedups on the
        key, so the credit lands exactly once at the ledger level.

        Here we just verify the lib helper passes the same key both times —
        the billing-service-side dedup is tested in test_credit_grant_flows.

        AC6.
        """
        registry = {"starter": self._starter_tier_with_grant()}
        client, captured = self._build_app(
            monkeypatch, webhook_secret,
            tier_registry=registry,
            apply_grant_response={"applied": True},
        )
        payload = self._invoice_paid_event(invoice_id="in_replay_t2")
        signature = self._sign(payload, webhook_secret)
        # Send twice (Stripe redelivery simulation)
        for _ in range(2):
            r = client.post(
                "/api/webhooks/stripe",
                content=payload,
                headers={"Stripe-Signature": signature},
            )
            assert r.status_code == 200
        # Both calls used the SAME idempotency_key
        keys = [c["idempotency_key"] for c in captured["apply_credit_grant_calls"]]
        assert len(keys) == 2
        assert keys[0] == keys[1] == "invoice:in_replay_t2:credit_grant"

    def test_non_invoice_event_passes_through_without_dispatch(self, webhook_secret, monkeypatch):
        """Events of other types (e.g. payment_intent.succeeded) skip the
        T2 dispatch entirely and just forward to payment-service."""
        registry = {"starter": self._starter_tier_with_grant()}
        client, captured = self._build_app(
            monkeypatch, webhook_secret,
            tier_registry=registry,
            apply_grant_response={"should_not_be_called": True},
        )
        import json as _json
        payload = _json.dumps({
            "id": "evt_pi", "object": "event",
            "type": "payment_intent.succeeded",
            "data": {"object": {"id": "pi_test"}},
        }).encode()
        r = client.post(
            "/api/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": self._sign(payload, webhook_secret)},
        )
        assert r.status_code == 200, r.text
        # NO grant call — wrong event type
        assert len(captured["apply_credit_grant_calls"]) == 0


class TestStripeShapeExtractors:
    """T3 — module-level helpers for pulling fields out of Stripe webhook payloads.

    These run against real Stripe-shaped nested objects (not simplified dicts)
    because Stripe's `previous_attributes` is partial and easy to mis-parse.

    Ticket: 20260516_auto_credit_invoice_paid_wiring (T3 / AC18).
    """

    def test_extract_price_id_from_subscription(self):
        from ab0t_quota.billing.router import _extract_subscription_price_id
        sub = {
            "id": "sub_test",
            "items": {
                "object": "list",
                "data": [{"id": "si_1", "price": {"id": "price_starter_monthly"}}],
            },
            "metadata": {"org_id": "org-test"},
        }
        assert _extract_subscription_price_id(sub) == "price_starter_monthly"

    def test_extract_price_id_returns_none_when_items_missing(self):
        from ab0t_quota.billing.router import _extract_subscription_price_id
        assert _extract_subscription_price_id(None) is None
        assert _extract_subscription_price_id({}) is None
        assert _extract_subscription_price_id({"items": {}}) is None
        assert _extract_subscription_price_id({"items": {"data": []}}) is None
        assert _extract_subscription_price_id({"items": {"data": [{}]}}) is None
        # price exists but no id
        assert _extract_subscription_price_id(
            {"items": {"data": [{"price": {}}]}}
        ) is None

    def test_extract_previous_price_id_full_items_shape(self):
        """When Stripe DOES send the full items shape in previous_attributes
        (as it does on a price change)."""
        from ab0t_quota.billing.router import _extract_previous_subscription_price_id
        previous = {
            "items": {
                "object": "list",
                "data": [{"id": "si_1", "price": {"id": "price_starter_OLD"}}],
            },
        }
        assert _extract_previous_subscription_price_id(previous) == "price_starter_OLD"

    def test_extract_previous_price_id_missing_when_items_didnt_change(self):
        """If items didn't change, previous_attributes has no `items` key.
        The extractor must treat that as 'not a tier change' (None)."""
        from ab0t_quota.billing.router import _extract_previous_subscription_price_id
        # Subscription metadata changed but not items
        assert _extract_previous_subscription_price_id(
            {"metadata": {"some_key": "old_value"}}
        ) is None
        assert _extract_previous_subscription_price_id({}) is None
        assert _extract_previous_subscription_price_id(None) is None

    def test_extract_org_id_from_subscription_metadata(self):
        from ab0t_quota.billing.router import _extract_org_id_from_subscription
        assert _extract_org_id_from_subscription(
            {"metadata": {"org_id": "org-123", "plan_id": "plan-x"}}
        ) == "org-123"

    def test_extract_org_id_returns_none_when_missing(self):
        from ab0t_quota.billing.router import _extract_org_id_from_subscription
        assert _extract_org_id_from_subscription(None) is None
        assert _extract_org_id_from_subscription({}) is None
        assert _extract_org_id_from_subscription({"metadata": {}}) is None
        # metadata is not a dict (defensive)
        assert _extract_org_id_from_subscription({"metadata": "not-a-dict"}) is None


class TestStripeWebhookProxySubscriptionDispatch:
    """T3 — dispatch customer.subscription.{updated,deleted} for downgrade reset.

    AC7-AC9 + AC17. Real HMAC + real Stripe SDK + in-process stubbed
    BillingServiceClient.reset_subscription_credit.

    Ticket: 20260516_auto_credit_invoice_paid_wiring (T3).
    """

    @pytest.fixture
    def webhook_secret(self):
        return "whsec_test_t3_" + "c" * 60

    def _sign(self, payload_bytes, secret):
        import time, hmac, hashlib
        ts = int(time.time())
        signed = f"{ts}.{payload_bytes.decode()}".encode()
        sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        return f"t={ts},v1={sig}"

    def _subscription_updated_event(
        self, *, org_id="org-test", old_price="price_old", new_price="price_new",
        event_id="evt_sub_updated_t3", sub_id="sub_test_t3",
    ):
        """Real-shape customer.subscription.updated event with price change."""
        import json as _json
        return _json.dumps({
            "id": event_id,
            "object": "event",
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": sub_id,
                    "object": "subscription",
                    "status": "active",
                    "items": {
                        "object": "list",
                        "data": [{"id": "si_1", "price": {"id": new_price}}],
                    },
                    "metadata": {"org_id": org_id},
                },
                "previous_attributes": {
                    "items": {
                        "object": "list",
                        "data": [{"id": "si_1", "price": {"id": old_price}}],
                    },
                },
            },
        }).encode()

    def _subscription_deleted_event(
        self, *, org_id="org-test", last_price="price_pro",
        event_id="evt_sub_deleted_t3", sub_id="sub_test_t3_del",
    ):
        """Real-shape customer.subscription.deleted event."""
        import json as _json
        return _json.dumps({
            "id": event_id,
            "object": "event",
            "type": "customer.subscription.deleted",
            "data": {
                "object": {
                    "id": sub_id,
                    "object": "subscription",
                    "status": "canceled",
                    "items": {
                        "object": "list",
                        "data": [{"id": "si_1", "price": {"id": last_price}}],
                    },
                    "metadata": {"org_id": org_id},
                },
            },
        }).encode()

    def _build_app(self, monkeypatch, webhook_secret, *, tier_registry,
                   reset_response=None, reset_raises=None, stripe_price_map=None):
        """Mount the proxy with stubbed BillingServiceClient.reset_subscription_credit."""
        monkeypatch.setenv("AB0T_QUOTA_STRIPE_WEBHOOK_SECRET", webhook_secret)

        from ab0t_quota.billing import clients as _clients

        captured = {"reset_calls": []}

        async def _fake_reset(self, **kwargs):
            captured["reset_calls"].append(kwargs)
            if reset_raises is not None:
                raise reset_raises
            return reset_response or {"new_balance": "0.00"}
        monkeypatch.setattr(_clients.BillingServiceClient, "reset_subscription_credit",
                            _fake_reset, raising=True)

        async def _fake_forward(self, body, signature):
            return {"ok": True}
        monkeypatch.setattr(_clients.PaymentServiceClient, "forward_webhook",
                            _fake_forward, raising=True)

        # Resolver should short-circuit via explicit stripe_price_to_tier map.
        # get_plans should never be called.
        async def _fake_get_plans(self, *args, **kwargs):
            raise AssertionError("get_plans should not be called")
        monkeypatch.setattr(_clients.PaymentServiceClient, "get_plans",
                            _fake_get_plans, raising=True)

        import json as _json, tempfile, os as _os
        config_dir = tempfile.mkdtemp(prefix="qc_t3_")
        config_path = _os.path.join(config_dir, "quota-config.json")
        with open(config_path, "w") as f:
            _json.dump({
                "tiers": [
                    {"tier_id": "free", "display_name": "Free", "limits": {}},
                    {"tier_id": "starter", "display_name": "Starter", "limits": {}},
                    {"tier_id": "pro", "display_name": "Pro", "limits": {}},
                ],
                "billing_integration": {
                    "stripe_price_to_tier": stripe_price_map or {},
                },
            }, f)

        app = _create_app(
            payment_url="http://test:8005",
            payment_api_key="key",
            billing_url="http://test:8002",
            billing_api_key="key",
            consumer_org_id="org-consumer",
            quota_config_path=config_path,
            tier_registry=tier_registry,
        )
        return TestClient(app), captured

    def _make_tiers(self):
        """Three-tier registry: free (sort 0), starter (sort 1), pro (sort 2)
        with subscription_with_credits + use_it_or_lose_it on paid tiers."""
        from ab0t_quota.models.core import (
            TierConfig, BillingModel, Price, BillingPeriod,
            CreditGrant, CreditTrigger, CreditLifecycle, CreditDestination,
        )
        from decimal import Decimal
        def paid(tid, sort_order, amount):
            return TierConfig(
                tier_id=tid, display_name=tid.title(), sort_order=sort_order,
                billing_model=BillingModel.SUBSCRIPTION_WITH_CREDITS,
                price=Price(amount_per_period=Decimal(amount), currency="USD",
                            period=BillingPeriod.MONTH),
                credit_grant=CreditGrant(
                    trigger=CreditTrigger.SUBSCRIPTION_INVOICE_PAID,
                    amount_per_period=Decimal(amount), currency="USD",
                    lifecycle=CreditLifecycle.USE_IT_OR_LOSE_IT,
                    destination=CreditDestination.SUBSCRIPTION_CREDIT,
                    reset_on_downgrade=True, reset_on_upgrade=False,
                ),
            )
        return {
            "free":    TierConfig(tier_id="free",    display_name="Free",
                                  sort_order=0, billing_model=BillingModel.CAPACITY_ONLY),
            "starter": paid("starter", 1, "10.00"),
            "pro":     paid("pro",     2, "50.00"),
        }

    def test_downgrade_pro_to_starter_resets(self, webhook_secret, monkeypatch):
        """Pro → Starter via Customer Portal: reset_subscription_credit fires
        with expected_source_tier=pro. AC7."""
        registry = self._make_tiers()
        client, captured = self._build_app(
            monkeypatch, webhook_secret,
            tier_registry=registry,
            stripe_price_map={"price_pro": "pro", "price_starter": "starter"},
            reset_response={"applied": True, "amount": "50.00"},
        )
        payload = self._subscription_updated_event(
            old_price="price_pro", new_price="price_starter",
        )
        r = client.post(
            "/api/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": self._sign(payload, webhook_secret)},
        )
        assert r.status_code == 200, r.text
        assert len(captured["reset_calls"]) == 1
        call = captured["reset_calls"][0]
        assert call["org_id"] == "org-test"
        assert call["expected_source_tier"] == "pro"
        assert call["idempotency_key"]  # event_id-keyed for redelivery dedup

    def test_upgrade_starter_to_pro_does_not_reset(self, webhook_secret, monkeypatch):
        """Starter → Pro (upgrade): helper detects it's NOT a downgrade
        (sort_order comparison) → skips reset. AC8."""
        registry = self._make_tiers()
        client, captured = self._build_app(
            monkeypatch, webhook_secret,
            tier_registry=registry,
            stripe_price_map={"price_starter": "starter", "price_pro": "pro"},
        )
        payload = self._subscription_updated_event(
            old_price="price_starter", new_price="price_pro",
        )
        r = client.post(
            "/api/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": self._sign(payload, webhook_secret)},
        )
        assert r.status_code == 200, r.text
        # No reset call — upgrade is not a downgrade
        assert len(captured["reset_calls"]) == 0

    def test_subscription_deleted_resets_to_free(self, webhook_secret, monkeypatch):
        """Full cancellation: subscription.deleted → treat new_tier=free →
        reset fires if old tier had reset_on_downgrade=True. AC17."""
        registry = self._make_tiers()
        client, captured = self._build_app(
            monkeypatch, webhook_secret,
            tier_registry=registry,
            stripe_price_map={"price_pro": "pro"},
            reset_response={"applied": True},
        )
        payload = self._subscription_deleted_event(last_price="price_pro")
        r = client.post(
            "/api/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": self._sign(payload, webhook_secret)},
        )
        assert r.status_code == 200, r.text
        assert len(captured["reset_calls"]) == 1
        call = captured["reset_calls"][0]
        assert call["expected_source_tier"] == "pro"

    def test_no_price_change_no_reset(self, webhook_secret, monkeypatch):
        """subscription.updated where items didn't change (e.g. metadata-only
        update) → previous_attributes.items is missing → no reset call."""
        registry = self._make_tiers()
        client, captured = self._build_app(
            monkeypatch, webhook_secret,
            tier_registry=registry,
            stripe_price_map={"price_starter": "starter"},
        )
        # Event with no items in previous_attributes
        import json as _json
        payload = _json.dumps({
            "id": "evt_no_change", "object": "event",
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_no_change",
                    "items": {"data": [{"price": {"id": "price_starter"}}]},
                    "metadata": {"org_id": "org-test"},
                },
                "previous_attributes": {"metadata": {"updated_field": "old"}},
            },
        }).encode()
        r = client.post(
            "/api/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": self._sign(payload, webhook_secret)},
        )
        assert r.status_code == 200, r.text
        assert len(captured["reset_calls"]) == 0

    def test_multi_sub_safety_check_409_skipped(self, webhook_secret, monkeypatch):
        """billing returns 409 (source_tier mismatch — credit belongs to a
        DIFFERENT subscription in a multi-sub org) → handler returns
        skipped_safety_check → proxy returns 200 (expected behavior). AC9."""
        from ab0t_quota.billing.clients import BillingServiceError
        registry = self._make_tiers()
        client, captured = self._build_app(
            monkeypatch, webhook_secret,
            tier_registry=registry,
            stripe_price_map={"price_pro": "pro", "price_starter": "starter"},
            reset_raises=BillingServiceError(status_code=409,
                                              detail="source_tier_mismatch"),
        )
        payload = self._subscription_updated_event(
            old_price="price_pro", new_price="price_starter",
        )
        r = client.post(
            "/api/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": self._sign(payload, webhook_secret)},
        )
        # Reset was attempted (409 came back); proxy still returns 200 because
        # the lib helper converts 409 to skipped_safety_check (not an error).
        assert r.status_code == 200, r.text
        assert len(captured["reset_calls"]) == 1

    def test_transient_billing_failure_returns_503(self, webhook_secret, monkeypatch):
        """billing 5xx during reset → proxy returns 503 → Stripe retries."""
        from ab0t_quota.billing.clients import BillingServiceError
        registry = self._make_tiers()
        client, captured = self._build_app(
            monkeypatch, webhook_secret,
            tier_registry=registry,
            stripe_price_map={"price_pro": "pro", "price_starter": "starter"},
            reset_raises=BillingServiceError(status_code=503, detail="timeout"),
        )
        payload = self._subscription_updated_event(
            old_price="price_pro", new_price="price_starter",
        )
        r = client.post(
            "/api/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": self._sign(payload, webhook_secret)},
        )
        assert r.status_code == 503

    def test_no_org_id_skips_silently(self, webhook_secret, monkeypatch):
        """Subscription without metadata.org_id (legacy / unrelated sub) →
        skip silently; no reset call; 200."""
        registry = self._make_tiers()
        client, captured = self._build_app(
            monkeypatch, webhook_secret,
            tier_registry=registry,
            stripe_price_map={"price_pro": "pro"},
        )
        import json as _json
        payload = _json.dumps({
            "id": "evt_no_org", "object": "event",
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_no_org",
                    "items": {"data": [{"price": {"id": "price_new"}}]},
                    "metadata": {},  # no org_id
                },
                "previous_attributes": {
                    "items": {"data": [{"price": {"id": "price_pro"}}]},
                },
            },
        }).encode()
        r = client.post(
            "/api/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": self._sign(payload, webhook_secret)},
        )
        assert r.status_code == 200, r.text
        assert len(captured["reset_calls"]) == 0
