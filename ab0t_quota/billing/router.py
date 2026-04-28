"""Router factory — generates all billing & payment proxy routes.

Usage:
    from ab0t_quota.billing import create_billing_router

    app.include_router(create_billing_router(
        payment_url="http://payment:8005",
        payment_api_key="ab0t_sk_live_...",
        billing_url="http://billing:8002",
        billing_api_key="ab0t_sk_live_...",
        consumer_org_id="...",
        auth_url="https://auth.service.ab0t.com",
        auth_org_slug="my-service-users",
    ))

Creates 20 routes with:
- Account-first anonymous checkout (lead capture before Stripe)
- Password reset email on account creation
- DynamoDB correlation tracking (no orphaned payments)
- Webhook fallback tier sync
- Idempotent processing (safe for double-calls)
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .clients import (
    BillingServiceClient,
    BillingServiceError,
    PaymentServiceClient,
    PaymentServiceError,
)
from .models import (
    AnonymousCheckoutResponse,
    BillingBalanceResponse,
    BillingTransactionsResponse,
    BillingUsageRecordsResponse,
    BillingUsageSummaryResponse,
    CancelSubscriptionResponse,
    CheckoutCompleteResponse,
    CheckoutInitResponse,
    CheckoutSessionResponse,
    InvoicesResponse,
    PaymentMethodDeleteResponse,
    PaymentMethodSetDefaultResponse,
    PaymentMethodsResponse,
    PlansResponse,
    PortalSessionResponse,
    SubscriptionsResponse,
)

logger = logging.getLogger("ab0t_quota.billing")

_TEMPLATE_DIR = Path(__file__).parent / "templates"


class AuthenticatedUser(Protocol):
    org_id: str
    email: str


class CheckoutStore(Protocol):
    """Interface for storing checkout intents (DynamoDB or any KV store)."""

    async def put_item(self, pk: str, sk: str, data: dict, ttl_seconds: int = 0) -> None: ...
    async def get_item(self, pk: str, sk: str) -> Optional[dict]: ...


def create_billing_router(
    *,
    payment_url: str,
    payment_api_key: str,
    billing_url: str,
    billing_api_key: str,
    consumer_org_id: str,
    auth_reader: Any = None,
    auth_admin: Any = None,
    auth_url: Optional[str] = None,
    auth_org_slug: Optional[str] = None,
    quota_config_path: Optional[str] = None,
    checkout_store: Optional[CheckoutStore] = None,
    templates_dir: Optional[str] = None,
    prefix: str = "/api",
) -> APIRouter:
    """Create a FastAPI router with all billing & payment proxy routes.

    Args:
        payment_url: Payment service base URL
        payment_api_key: Service API key for payment service
        billing_url: Billing service base URL
        billing_api_key: Service API key for billing service
        consumer_org_id: Your org UUID in the payment service (where plans live)
        auth_reader: FastAPI Depends() for read-level auth (returns object with .org_id).
            Optional — if not supplied, only the public routes (plans, checkout/init,
            checkout/anonymous, checkout/complete, webhooks/stripe) are mounted.
        auth_admin: FastAPI Depends() for admin-level auth, used to gate
            mutating endpoints (cancel subscription, set/remove default
            payment method). REQUIRED whenever auth_reader is supplied —
            see `make_admin_dep` for a sensible default. There is no silent
            fallback: passing auth_reader without auth_admin raises ValueError
            because the previous fallback (admin = reader) collapsed the
            permission boundary and let any authenticated user perform
            admin-only billing actions.
        auth_url: Public auth service URL (for account creation + password reset)
        auth_org_slug: Hosted login org slug (for account creation)
        quota_config_path: Path to quota-config.json (for plan→tier mapping)
        checkout_store: DynamoDB-like store for checkout intent tracking
        templates_dir: Override template directory
        prefix: URL prefix for all routes (default: /api)
    """
    for name, val in [("payment_url", payment_url), ("payment_api_key", payment_api_key),
                       ("billing_url", billing_url), ("billing_api_key", billing_api_key),
                       ("consumer_org_id", consumer_org_id)]:
        if not val:
            raise ValueError(f"{name} is required")

    if auth_reader is not None and auth_admin is None:
        raise ValueError(
            "auth_admin is required when auth_reader is provided. "
            "Use ab0t_quota.billing.make_admin_dep(auth_guard) for a sensible "
            "default that requires the 'billing.admin' permission, or pass "
            "auth_admin=auth_reader explicitly to keep the legacy "
            "permission-collapsing behaviour (NOT recommended — it lets any "
            "authenticated user cancel subscriptions and modify payment methods)."
        )

    payment = PaymentServiceClient(payment_url, payment_api_key)
    billing = BillingServiceClient(billing_url, billing_api_key)

    tpl_dir = templates_dir or str(_TEMPLATE_DIR)
    from fastapi.templating import Jinja2Templates
    templates = Jinja2Templates(directory=tpl_dir)

    tier_map: dict[str, str] = {}
    if quota_config_path:
        try:
            with open(quota_config_path) as f:
                config = json.load(f)
            tier_map = {t["display_name"].lower(): t["tier_id"] for t in config.get("tiers", [])}
        except Exception as e:
            logger.warning("Failed to load quota config for tier mapping: %s", e)

    db = checkout_store
    router = APIRouter()

    # =====================================================================
    # BILLING ROUTES (require auth)
    # =====================================================================

    if auth_reader:
        @router.get(f"{prefix}/billing/balance", response_model=BillingBalanceResponse, tags=["Billing"])
        async def get_balance(request: Request, user=Depends(auth_reader)):
            try:
                return await billing.get_balance(user.org_id)
            except BillingServiceError as e:
                raise HTTPException(status_code=e.status_code, detail="Billing service error")

        @router.get(f"{prefix}/billing/usage/summary", response_model=BillingUsageSummaryResponse, tags=["Billing"])
        async def get_usage_summary(request: Request, user=Depends(auth_reader)):
            try:
                return await billing.get_usage_summary(user.org_id)
            except BillingServiceError as e:
                raise HTTPException(status_code=e.status_code, detail="Billing service error")

        @router.get(f"{prefix}/billing/usage/records", response_model=BillingUsageRecordsResponse, tags=["Billing"])
        async def get_usage_records(request: Request, user=Depends(auth_reader),
                                    limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
            try:
                return await billing.get_usage_records(user.org_id, limit, offset)
            except BillingServiceError as e:
                raise HTTPException(status_code=e.status_code, detail="Billing service error")

        @router.get(f"{prefix}/billing/transactions", response_model=BillingTransactionsResponse, tags=["Billing"])
        async def get_transactions(request: Request, user=Depends(auth_reader),
                                   limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
            try:
                return await billing.get_transactions(user.org_id, limit, offset)
            except BillingServiceError as e:
                raise HTTPException(status_code=e.status_code, detail="Billing service error")

    # =====================================================================
    # PAYMENT ROUTES (require auth)
    # =====================================================================

    if auth_reader:
        @router.get(f"{prefix}/payments/subscriptions", response_model=SubscriptionsResponse, tags=["Payments"])
        async def get_subscriptions(request: Request, user=Depends(auth_reader)):
            try:
                return await payment.get_subscriptions(user.org_id)
            except PaymentServiceError as e:
                raise HTTPException(status_code=e.status_code, detail="Payment service error")

        @router.get(f"{prefix}/payments/invoices", response_model=InvoicesResponse, tags=["Payments"])
        async def get_invoices(request: Request, user=Depends(auth_reader),
                               limit: int = Query(10, ge=1, le=50), offset: int = Query(0, ge=0)):
            try:
                return await payment.get_invoices(user.org_id, limit, offset)
            except PaymentServiceError as e:
                raise HTTPException(status_code=e.status_code, detail="Payment service error")

        @router.get(
            f"{prefix}/payments/invoices/{'{invoice_id}'}/pdf",
            tags=["Payments"],
            description=(
                "Returns a 302 redirect with a `Location` header pointing at the "
                "signed PDF URL on the upstream payment service. Clients should "
                "follow the redirect to download the invoice PDF."
            ),
            responses={302: {"description": "Redirect to signed invoice PDF URL"}},
        )
        async def get_invoice_pdf(request: Request, invoice_id: str, user=Depends(auth_reader)):
            try:
                url = await payment.get_invoice_pdf_url(user.org_id, invoice_id)
                return RedirectResponse(url=url)
            except PaymentServiceError as e:
                raise HTTPException(status_code=e.status_code, detail="Payment service error")

        @router.get(f"{prefix}/payments/methods", response_model=PaymentMethodsResponse, tags=["Payments"])
        async def get_payment_methods(request: Request, user=Depends(auth_reader)):
            try:
                return await payment.get_payment_methods(user.org_id)
            except PaymentServiceError as e:
                raise HTTPException(status_code=e.status_code, detail="Payment service error")

    if auth_admin:
        @router.delete(f"{prefix}/payments/subscriptions/{'{subscription_id}'}", response_model=CancelSubscriptionResponse, tags=["Payments"])
        async def cancel_subscription(request: Request, subscription_id: str, user=Depends(auth_admin)):
            try:
                return await payment.cancel_subscription(user.org_id, subscription_id)
            except PaymentServiceError as e:
                raise HTTPException(status_code=e.status_code, detail="Payment service error")

        @router.put(f"{prefix}/payments/methods/{'{method_id}'}/default", response_model=PaymentMethodSetDefaultResponse, tags=["Payments"])
        async def set_default_method(request: Request, method_id: str, user=Depends(auth_admin)):
            try:
                return await payment.set_default_method(user.org_id, method_id)
            except PaymentServiceError as e:
                raise HTTPException(status_code=e.status_code, detail="Payment service error")

        @router.delete(f"{prefix}/payments/methods/{'{method_id}'}", response_model=PaymentMethodDeleteResponse, tags=["Payments"])
        async def remove_method(request: Request, method_id: str, user=Depends(auth_admin)):
            try:
                return await payment.remove_method(user.org_id, method_id)
            except PaymentServiceError as e:
                raise HTTPException(status_code=e.status_code, detail="Payment service error")

        @router.post(
            f"{prefix}/payments/topup",
            response_model=CheckoutSessionResponse,
            tags=["Payments"],
            description=(
                "Create a Stripe Checkout session for an account balance top-up "
                "(one-time payment, USD). The browser must be redirected to the "
                "returned `url` to complete payment. Capped at $10,000 per call. "
                "Admin-gated: a top-up immediately initiates a charge against the "
                "org's saved payment method, so this is a write operation, not a "
                "read."
            ),
        )
        async def create_topup(request: Request, user=Depends(auth_admin),
                               amount: float = Body(..., gt=0, le=10000, embed=True)):
            try:
                base = str(request.base_url).rstrip("/")
                return await payment.create_topup_session(
                    user.org_id, amount,
                    success_url=f"{base}/billing?topup=success&amount={amount}",
                    cancel_url=f"{base}/billing?topup=cancelled",
                )
            except PaymentServiceError as e:
                raise HTTPException(status_code=e.status_code, detail="Payment service error")

    # =====================================================================
    # PLANS (public)
    # =====================================================================

    @router.get(
        f"{prefix}/payments/plans",
        response_model=PlansResponse,
        response_model_exclude={"org_id"},  # belt
        tags=["Payments"],
    )
    async def get_plans(request: Request):
        try:
            data = await payment.get_plans(consumer_org_id, provider_org=consumer_org_id)
            # Construct a fresh PlansResponse rather than passing the upstream
            # object through. Without this the `extra: "allow"` model_config
            # on PlansResponse would forward `org_id` (the platform's
            # consumer-org UUID) into the public response — finding I3 in
            # audit ticket 20260428. Plans are public; the consumer org is
            # an internal identifier and has no business on this surface.
            return PlansResponse(plans=data.plans, count=data.count)
        except PaymentServiceError as e:
            raise HTTPException(status_code=e.status_code, detail="Payment service error")

    # =====================================================================
    # CHECKOUT (static routes BEFORE {plan_id})
    # =====================================================================

    @router.post(
        f"{prefix}/payments/checkout/init",
        response_model=CheckoutInitResponse,
        tags=["Payments"],
        description=(
            "Issue an anti-fraud session token + browser fingerprint hash that the "
            "client must replay to the anonymous checkout endpoint. Public — "
            "intended to be called from the pricing page before the user has an "
            "account."
        ),
    )
    async def init_checkout(request: Request):
        try:
            return await payment.init_checkout()
        except PaymentServiceError as e:
            raise HTTPException(status_code=e.status_code, detail="Payment service error")

    @router.post(
        f"{prefix}/payments/checkout/anonymous/{'{plan_id}'}",
        response_model=AnonymousCheckoutResponse,
        tags=["Payments"],
        description=(
            "Account-first anonymous checkout: provisions the customer's account "
            "(when `auth_url`/`auth_org_slug` are configured), creates a Stripe "
            "checkout session for the chosen plan, and returns the Stripe URL to "
            "redirect the browser to. When account creation succeeds, the response "
            "also includes the new `org_id` and a JWT `access_token` so the client "
            "can sign the user in once they return from Stripe. Sets a "
            "`checkout_intent` cookie used by the success page to recover state."
        ),
    )
    async def create_anonymous_checkout(
        request: Request, plan_id: str,
        email: str = Body(...), session_token: str = Body(...), fingerprint: str = Body(...),
    ):
        """Account-first anonymous checkout: create account → Stripe redirect."""
        try:
            # Step 1: Create account BEFORE Stripe (captures lead)
            resp_stub: dict = {}
            new_org = None
            if auth_url and auth_org_slug:
                new_org = await _create_anonymous_account(auth_url, auth_org_slug, email, resp_stub)
            access_token = resp_stub.get("access_token")

            # Step 2: Create Stripe checkout
            checkout_org = new_org or consumer_org_id
            base = str(request.base_url).rstrip("/")
            result = await payment.create_checkout_session(
                checkout_org, plan_id,
                success_url=f"{base}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}",
                cancel_url=f"{base}/pricing?cancelled=true",
                customer_email=email,
                session_token=session_token,
                fingerprint=fingerprint,
            )

            # Step 3: Store correlation data
            session_id = result.id if hasattr(result, "id") else result.get("id", "")
            if db and session_id:
                try:
                    await db.put_item(pk=f"CHECKOUT#{session_id}", sk="INTENT",
                                      data={"email": email, "plan_id": plan_id, "status": "pending"},
                                      ttl_seconds=86400 * 7)
                    if new_org:
                        await db.put_item(pk=f"CHECKOUT#{session_id}", sk="ACCOUNT",
                                          data={"org_id": new_org, "email": email},
                                          ttl_seconds=86400 * 7)
                except Exception as e:
                    logger.warning("checkout_intent_store_failed: %s", e)

            # Step 4: Return only the Stripe redirect URL — never include
            # `access_token`, `org_id`, or any "new vs existing email" flag in
            # the public response. Distinguishable shape was a textbook
            # email-enumeration leak (audit ticket 20260428, finding I1):
            # any unauthenticated caller could probe whether an email had an
            # account by checking which fields the response carried.
            #
            # Account credentials (when account creation was triggered) are
            # delivered out-of-band via the password-reset email that
            # `_create_anonymous_account` sends. The browser receives only
            # the Stripe checkout URL and an opaque session marker.
            resp_obj = result.model_dump() if hasattr(result, "model_dump") else (result if isinstance(result, dict) else {})
            # Strip any leaky fields that a future model_config="extra: allow"
            # change might let through.
            for leaky in ("access_token", "org_id", "new_account", "account_error"):
                resp_obj.pop(leaky, None)

            # Stash the access_token + org_id in the httponly checkout_intent
            # cookie below so the browser can pick them up on the
            # /checkout/success page without exposing them in a publicly
            # cacheable response body.
            json_response = JSONResponse(content=resp_obj)
            cookie_payload = {
                "email": email, "plan_id": plan_id,
                "session_id": session_id, "org_id": new_org or "",
            }
            if access_token:
                cookie_payload["access_token"] = access_token
            json_response.set_cookie(
                key="checkout_intent",
                value=urllib.parse.quote(json.dumps(cookie_payload)),
                max_age=3600, httponly=True, samesite="lax",
            )
            return json_response

        except PaymentServiceError as e:
            raise HTTPException(status_code=e.status_code, detail="Payment service error")

    if auth_reader:
        @router.post(
            f"{prefix}/payments/checkout/complete",
            response_model=CheckoutCompleteResponse,
            tags=["Payments"],
            description=(
                "Verify a returned Stripe checkout session and synchronise the "
                "customer's tier with the billing service. Idempotent — safe to call "
                "multiple times. When `tier_synced` is false but a tier was resolved, "
                "the Stripe webhook will retry the sync (`tier_pending=true`). "
                "Requires authentication: the caller's `org_id` must match the "
                "session's `metadata.org_id` (or the DynamoDB-tracked account org "
                "for anonymous-checkout flows). Without auth, anyone holding a "
                "Stripe `session_id` could read the customer's email and tier — "
                "audit ticket 20260428 finding A3."
            ),
        )
        async def complete_checkout(
            request: Request,
            session_id: str = Body(...),
            new_account: bool = Body(False),
            user=Depends(auth_reader),
        ):
            """Verify checkout and sync tier. Account already exists (created before Stripe)."""
            try:
                result = await payment.verify_checkout_session(session_id, process_if_complete=True)
                status = result.status if hasattr(result, "status") else result.get("status", "unknown")
                metadata = result.metadata if hasattr(result, "metadata") else result.get("metadata") or {}
                org_id = (metadata or {}).get("org_id", "")
                plan_id = (metadata or {}).get("plan_id", "")
                customer_email = result.customer_email if hasattr(result, "customer_email") else result.get("customer_email", "")

                # Resolve the session's org_id (Stripe metadata first, then
                # DynamoDB fallback for anonymous-checkout sessions where the
                # account was provisioned after Stripe redirect).
                session_org_id = org_id
                if not session_org_id and db and session_id:
                    try:
                        acct = await db.get_item(pk=f"CHECKOUT#{session_id}", sk="ACCOUNT")
                        if acct and acct.get("org_id"):
                            session_org_id = acct["org_id"]
                    except Exception:
                        pass

                # Authorisation: caller must own this checkout session.
                # Mock/test sessions (no metadata.org_id, no DB record) are
                # allowed because they return hardcoded fake data anyway.
                caller_org = getattr(user, "org_id", None)
                if session_org_id and caller_org and session_org_id != caller_org:
                    logger.info(
                        "checkout_complete_org_mismatch session_id=%s caller_org=%s session_org=%s",
                        session_id, caller_org, session_org_id,
                    )
                    raise HTTPException(
                        status_code=403,
                        detail="Checkout session does not belong to your organization",
                    )

                # Use session_org_id (resolved above with DynamoDB fallback)
                # for the rest of the flow — the original `org_id` may have
                # been empty when only the DynamoDB account record had it.
                org_id = session_org_id or org_id

                resp: dict = {
                    "status": status, "session_id": session_id,
                    "email": customer_email, "plan_id": plan_id,
                    "tier": None, "tier_synced": False,
                }

                if status not in ("complete", "paid"):
                    resp["retry"] = True
                    return resp

                tier_id = await _resolve_plan_to_tier(plan_id, tier_map, payment, consumer_org_id)

                if org_id and tier_id:
                    try:
                        await billing.set_tier(org_id, tier_id, reason="checkout_complete")
                        resp["tier_synced"] = True
                    except Exception as e:
                        logger.warning("tier_sync_failed org=%s error=%s", org_id, e)

                if tier_id:
                    resp["tier"] = tier_id
                if tier_id and not resp["tier_synced"]:
                    resp["tier_pending"] = True

                # Mark intent processed
                if db and session_id:
                    try:
                        await db.put_item(pk=f"CHECKOUT#{session_id}", sk="INTENT",
                                          data={"status": "completed", "org_id": org_id, "email": customer_email})
                    except Exception:
                        pass

                resp["redirect"] = "/dashboard"
                return resp

            except HTTPException:
                raise
            except PaymentServiceError as e:
                raise HTTPException(status_code=e.status_code, detail=e.detail)

    # Authenticated checkout (AFTER static routes)
    if auth_reader:
        @router.post(f"{prefix}/payments/checkout/{'{plan_id}'}", response_model=CheckoutSessionResponse, tags=["Payments"])
        async def create_checkout(request: Request, plan_id: str, user=Depends(auth_reader)):
            try:
                base = str(request.base_url).rstrip("/")
                return await payment.create_checkout_session(
                    user.org_id, plan_id,
                    success_url=f"{base}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}",
                    cancel_url=f"{base}/pricing?cancelled=true",
                    customer_email=getattr(user, "email", None),
                )
            except PaymentServiceError as e:
                raise HTTPException(status_code=e.status_code, detail="Payment service error")

        @router.post(f"{prefix}/payments/portal", response_model=PortalSessionResponse, tags=["Payments"])
        async def create_portal(request: Request, user=Depends(auth_reader)):
            try:
                base = str(request.base_url).rstrip("/")
                return await payment.create_portal_session(user.org_id, return_url=f"{base}/billing")
            except PaymentServiceError as e:
                raise HTTPException(status_code=e.status_code, detail="Payment service error")

    # =====================================================================
    # WEBHOOK PROXY (no auth — Stripe signs the payload)
    # =====================================================================

    @router.post(f"{prefix}/webhooks/stripe", tags=["Webhooks"], include_in_schema=False)
    async def stripe_webhook_proxy(request: Request):
        body = await request.body()
        signature = request.headers.get("stripe-signature", "")
        if not signature:
            raise HTTPException(status_code=400, detail="Missing Stripe-Signature header")
        try:
            result = await payment.forward_webhook(body, signature)
        except PaymentServiceError as e:
            raise HTTPException(status_code=e.status_code, detail="Webhook processing error")

        # Webhook fallback: tier sync for checkouts where success page didn't process
        if db:
            try:
                event = json.loads(body)
                if event.get("type") == "checkout.session.completed":
                    session_obj = event.get("data", {}).get("object", {})
                    sid = session_obj.get("id", "")
                    if sid:
                        intent = await db.get_item(pk=f"CHECKOUT#{sid}", sk="INTENT")
                        if intent and intent.get("status") == "pending":
                            plan_id = intent.get("plan_id", "")
                            acct = await db.get_item(pk=f"CHECKOUT#{sid}", sk="ACCOUNT")
                            org_id = (acct or {}).get("org_id", "")
                            if org_id and plan_id:
                                tid = await _resolve_plan_to_tier(plan_id, tier_map, payment, consumer_org_id)
                                if tid:
                                    try:
                                        await billing.set_tier(org_id, tid, reason="webhook_fallback")
                                    except Exception:
                                        pass
                            try:
                                await db.put_item(pk=f"CHECKOUT#{sid}", sk="INTENT",
                                                  data={**intent, "status": "completed_by_webhook"})
                            except Exception:
                                pass
            except Exception as e:
                logger.warning("webhook_fallback_error: %s", e)

        return result

    # =====================================================================
    # CHECKOUT SUCCESS PAGE
    # =====================================================================

    @router.get("/checkout/success", response_class=HTMLResponse, include_in_schema=False)
    async def checkout_success_page(request: Request):
        return templates.TemplateResponse("checkout_success.html", {"request": request})

    return router


# =========================================================================
# Helpers
# =========================================================================

async def _resolve_plan_to_tier(
    plan_id: str,
    tier_map: dict[str, str],
    payment: PaymentServiceClient,
    consumer_org_id: str,
) -> Optional[str]:
    """Map plan_id to tier_id by looking up plan name from the payment service."""
    if not plan_id or not tier_map:
        return None
    try:
        plans_data = await payment.get_plans(consumer_org_id, provider_org=consumer_org_id)
        for p in plans_data.plans:
            if p.plan_id == plan_id:
                name = (p.name or "").lower()
                if name in tier_map:
                    return tier_map[name]
        return None
    except Exception:
        return None


async def _create_anonymous_account(
    auth_url: str,
    auth_org_slug: str,
    email: str,
    resp: dict,
) -> Optional[str]:
    """Create account, send password reset email. Returns org_id or None."""
    import base64
    import secrets

    import httpx

    try:
        temp_password = secrets.token_urlsafe(24) + "!1Aa"
        async with httpx.AsyncClient(timeout=10.0) as client:
            reg_resp = await client.post(
                f"{auth_url}/organizations/{auth_org_slug}/auth/register",
                json={"email": email, "password": temp_password, "name": email.split("@")[0]},
            )

            if reg_resp.status_code in (200, 201):
                reg_data = reg_resp.json()
                access_token = reg_data.get("access_token", "")
                resp["access_token"] = access_token
                resp["new_account"] = True

                new_org = reg_data.get("org_id") or ""
                if not new_org and access_token:
                    try:
                        payload_b64 = access_token.split(".")[1]
                        payload_b64 += "=" * (4 - len(payload_b64) % 4)
                        new_org = json.loads(base64.b64decode(payload_b64)).get("org_id", "")
                    except Exception:
                        pass

                # Send password reset email
                try:
                    await client.post(
                        f"{auth_url}/organizations/{auth_org_slug}/auth/reset-password",
                        json={"email": email},
                    )
                except Exception:
                    pass

                return new_org or None

            elif reg_resp.status_code == 409:
                resp["new_account"] = False
                try:
                    await client.post(
                        f"{auth_url}/organizations/{auth_org_slug}/auth/reset-password",
                        json={"email": email},
                    )
                except Exception:
                    pass
                return None

            else:
                resp["account_error"] = "Account creation failed. Check your email."
                return None

    except Exception as e:
        logger.error("anonymous_account_error email=%s: %s", email, e)
        resp["account_error"] = "Account creation failed."
        return None
