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
import os
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..models.core import TierConfig
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
    tier_registry: Optional[dict[str, TierConfig]] = None,
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
        tier_registry: Loaded TierConfig dict keyed by tier_id, passed in from
            setup_quota's load_tiers() result. Required for the Stripe webhook
            proxy's invoice.payment_succeeded dispatch (T2/T3 in ticket
            20260516_auto_credit_invoice_paid_wiring) — without it, the
            dispatch handlers can't read the consumer's billing_model /
            credit_grant / lifecycle / destination policy. Logged as WARNING
            at startup if not provided; the Stripe dispatch then falls back
            to "all events skipped" rather than silently mis-granting.
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
    # T0e — explicit ID→tier maps from quota-config.json's optional
    # `billing_integration` block. Both maps are flat dicts:
    #   {
    #     "billing_integration": {
    #       "plan_to_tier":       {"<payment-plan-uuid>": "starter", ...},
    #       "stripe_price_to_tier": {"price_<stripe-id>":   "starter", ...}
    #     }
    #   }
    # These let consumers pin the mapping in one place instead of relying
    # on payment-plan display-name equality with tier display-name. Read
    # once at router creation; passed to _resolve_id_to_tier at call sites.
    explicit_plan_to_tier: dict[str, str] = {}
    explicit_stripe_price_to_tier: dict[str, str] = {}
    if quota_config_path:
        try:
            with open(quota_config_path) as f:
                config = json.load(f)
            # TODO(public-mesh-ga): Keep display-name tier maps as legacy
            # fallback only; public consumers need stable plan/price IDs or
            # metadata mappings as the source of truth. Backlink:
            # /home/ubuntu/infra/infra/code/resource/output/sandbox-platform/tickets/20260516_auto_credit_invoice_paid_wiring/codex_report_20260516_235326_llm_judge_public_mesh_billing_quota.md
            tier_map = {t["display_name"].lower(): t["tier_id"] for t in config.get("tiers", [])}
            bi = config.get("billing_integration") or {}
            explicit_plan_to_tier = dict(bi.get("plan_to_tier") or {})
            explicit_stripe_price_to_tier = dict(bi.get("stripe_price_to_tier") or {})
            if explicit_plan_to_tier or explicit_stripe_price_to_tier:
                logger.info(
                    "create_billing_router: loaded explicit ID→tier maps "
                    "(plan_to_tier=%d entries, stripe_price_to_tier=%d entries)",
                    len(explicit_plan_to_tier), len(explicit_stripe_price_to_tier),
                )
        except Exception as e:
            logger.warning("Failed to load quota config for tier mapping: %s", e)

    # T0b — capture tier_registry from caller for the Stripe webhook proxy's
    # invoice.payment_succeeded dispatch (T2/T3). Warn loudly at startup if
    # missing so misconfiguration is visible immediately rather than at the
    # first paid invoice. Ticket: 20260516_auto_credit_invoice_paid_wiring.
    if tier_registry is None:
        logger.warning(
            "create_billing_router: tier_registry not provided. The Stripe "
            "webhook proxy will mount, but invoice.payment_succeeded dispatch "
            "(T2) cannot fire credit grants without the consumer's tier "
            "policy. Pass tier_registry=load_tiers(config) from your "
            "setup_quota wiring."
        )
    else:
        logger.info(
            "create_billing_router: tier_registry attached with %d tiers (%s)",
            len(tier_registry),
            ", ".join(sorted(tier_registry.keys())),
        )

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

            # Step 3: Store correlation data. Capture the plaintext
            # verification_token so /complete can replay it back to /verify
            # (payment service 403s otherwise — see clients.py
            # verify_checkout_session).
            session_id = result.id if hasattr(result, "id") else result.get("id", "")
            verification_token = (
                result.verification_token
                if hasattr(result, "verification_token")
                else (result.get("verification_token") if isinstance(result, dict) else None)
            )
            if db and session_id:
                try:
                    intent_data: dict = {"email": email, "plan_id": plan_id, "status": "pending"}
                    if verification_token:
                        intent_data["verification_token"] = verification_token
                    await db.put_item(pk=f"CHECKOUT#{session_id}", sk="INTENT",
                                      data=intent_data,
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
            for leaky in ("access_token", "org_id", "new_account", "account_error", "verification_token"):
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
                # Pull the plaintext `verification_token` stashed at
                # create-session time. Payment service requires it whenever
                # process_if_complete=True (raises 403 otherwise). Missing-OK
                # for mock / legacy sessions — the upstream then returns 403
                # only if the session has a stored hash that mandates a token.
                verification_token: Optional[str] = None
                if db and session_id:
                    try:
                        intent = await db.get_item(pk=f"CHECKOUT#{session_id}", sk="INTENT")
                        if intent:
                            verification_token = intent.get("verification_token") or None
                    except Exception as e:
                        logger.warning("checkout_verification_token_read_failed: %s", e)
                result = await payment.verify_checkout_session(
                    session_id, process_if_complete=True,
                    verification_token=verification_token,
                )
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
                result = await payment.create_checkout_session(
                    user.org_id, plan_id,
                    success_url=f"{base}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}",
                    cancel_url=f"{base}/pricing?cancelled=true",
                    customer_email=getattr(user, "email", None),
                )
                # Payment service now mints a `verification_token` for each
                # checkout session. The success-page → complete-checkout flow
                # must replay it back to /verify or the call 403s. Stash the
                # plaintext token in our CHECKOUT#{sid} INTENT record so
                # `complete_checkout` (same browser, may be a different
                # request) can read it. Strip from the response so the token
                # never reaches the browser — replay must originate from the
                # server side.
                session_id = result.id if hasattr(result, "id") else (result.get("id") if isinstance(result, dict) else "")
                verification_token = (
                    result.verification_token
                    if hasattr(result, "verification_token")
                    else (result.get("verification_token") if isinstance(result, dict) else None)
                )
                if db and session_id and verification_token:
                    try:
                        await db.put_item(
                            pk=f"CHECKOUT#{session_id}", sk="INTENT",
                            data={
                                "org_id": user.org_id, "plan_id": plan_id,
                                "user_id": getattr(user, "user_id", None),
                                "verification_token": verification_token,
                                "status": "pending",
                            },
                            ttl_seconds=86400 * 7,
                        )
                    except Exception as e:
                        logger.warning("checkout_verification_token_store_failed: %s", e)
                if hasattr(result, "verification_token"):
                    try:
                        result.verification_token = None
                    except Exception:
                        pass
                return result
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

        # T1 — local Stripe signature verification.
        # Ticket: 20260516_auto_credit_invoice_paid_wiring.
        #
        # After §8 Step 6 cutover, Stripe Dashboard points at this endpoint
        # directly. The proxy must verify locally before processing or
        # forwarding to payment-service. The forwarded request reaches
        # payment-service with the SAME Stripe-Signature header; payment-
        # service's multi-secret support (T0d) accepts our endpoint's secret
        # alongside its legacy secret, so the forward verifies cleanly.
        #
        # Env var is namespaced (AB0T_QUOTA_STRIPE_WEBHOOK_SECRET) to avoid
        # collision with payment-service's same-named STRIPE_WEBHOOK_SECRET
        # when both services run in the same compose/k8s namespace. Falls
        # back to STRIPE_WEBHOOK_SECRET for local dev convenience.
        import stripe as _stripe  # local import: 'stripe' is in the [billing] extra (T0c)

        secret = (
            os.getenv("AB0T_QUOTA_STRIPE_WEBHOOK_SECRET")
            or os.getenv("STRIPE_WEBHOOK_SECRET", "")
        )
        # TODO(public-mesh-ga): Support a comma-separated
        # AB0T_QUOTA_STRIPE_WEBHOOK_SECRETS list for endpoint-secret
        # rotation, mirroring payment-service cutover behavior. Backlink:
        # /home/ubuntu/infra/infra/code/resource/output/sandbox-platform/tickets/20260516_auto_credit_invoice_paid_wiring/codex_report_20260516_235326_llm_judge_public_mesh_billing_quota.md
        if not secret:
            logger.error(
                "AB0T_QUOTA_STRIPE_WEBHOOK_SECRET unset (and STRIPE_WEBHOOK_SECRET "
                "fallback also empty) — cannot verify Stripe signature. Configure "
                "this in your sandbox-platform/.env.production before Stripe "
                "Dashboard cuts over to this endpoint."
            )
            raise HTTPException(status_code=500, detail="Server config error")

        try:
            event = _stripe.Webhook.construct_event(body, signature, secret)
        except _stripe.error.SignatureVerificationError:
            logger.warning(
                "stripe_signature_invalid — request rejected. Likely cause: "
                "AB0T_QUOTA_STRIPE_WEBHOOK_SECRET doesn't match the secret "
                "Stripe Dashboard generated for this endpoint."
            )
            raise HTTPException(status_code=400, detail="Invalid signature")
        except ValueError as e:
            logger.warning("stripe_payload_invalid: %s", e)
            raise HTTPException(status_code=400, detail="Invalid payload")

        # T1 success: `event` is verified. T2 dispatches invoice.paid below,
        # T3 dispatches customer.subscription.{updated,deleted}. All dispatch
        # runs on the verified event BEFORE the forward to payment-service.

        # T2 — dispatch invoice.payment_succeeded to handle_subscription_invoice_paid.
        # Ticket: 20260516_auto_credit_invoice_paid_wiring.
        #
        # The library helper takes consumer-supplied tier_registry +
        # plan_to_tier resolver (both in this closure from T0b/T0e) and
        # routes the credit grant to billing-service per the tier's declared
        # billing_model + credit_grant policy.
        #
        # Status → HTTP response decision matrix (per ticket §6 T2):
        #   applied / skipped_*  → 200 (continue to forward)
        #   deferred_transient   → 503 (Stripe retries; grant is idempotent
        #                               on invoice:{id}:credit_grant so the
        #                               eventual successful retry won't double-credit)
        #   failed_permanent     → 200 + ERROR log (don't retry-storm a
        #                               permanent failure; surface for paging)
        event_type = event.get("type", "")

        if event_type == "invoice.payment_succeeded":
            from .subscription_credit import handle_subscription_invoice_paid

            # Adapter: the lib helper expects Callable[[str], Awaitable[Optional[str]]].
            # Our T0e resolver takes more kwargs; close over them here.
            async def _resolve(identifier: str) -> Optional[str]:
                return await _resolve_id_to_tier(
                    identifier,
                    tier_map=tier_map,
                    payment=payment,
                    consumer_org_id=consumer_org_id,
                    explicit_plan_to_tier=explicit_plan_to_tier,
                    explicit_stripe_price_to_tier=explicit_stripe_price_to_tier,
                )

            invoice = event.get("data", {}).get("object", {}) or {}
            try:
                dispatch_result = await handle_subscription_invoice_paid(
                    invoice=invoice,
                    tier_registry=tier_registry or {},
                    plan_to_tier=_resolve,
                    billing_client=billing,
                )
            except Exception as e:
                # Unexpected exception in dispatch (not a clean status return).
                # Don't swallow — return 500 so Stripe retries. Log fully.
                logger.exception(
                    "invoice_paid_dispatch_unexpected_exception event_id=%s err=%s",
                    event.get("id"), e,
                )
                raise HTTPException(status_code=500, detail="Dispatch error")

            status = dispatch_result.get("status")
            # stdlib-compatible positional format (NOT structlog kwargs per
            # ticket Report 1 H6 — module uses stdlib logging).
            logger.info(
                "invoice_paid_dispatch_result status=%s event_id=%s "
                "invoice_id=%s org_id=%s tier_id=%s",
                status, event.get("id"),
                dispatch_result.get("invoice_id"),
                dispatch_result.get("org_id"),
                dispatch_result.get("tier_id"),
            )

            if status == "deferred_transient":
                # Stripe will retry. Grant idempotency key prevents double-credit.
                raise HTTPException(
                    status_code=503,
                    detail="Transient billing failure; will retry",
                )
            if status == "failed_permanent":
                # Log at ERROR for paging; respond 200 so Stripe doesn't
                # retry-storm a permanent failure that won't self-resolve.
                logger.error(
                    "invoice_paid_failed_permanent_PAGE status_code=%s event_id=%s "
                    "invoice_id=%s org_id=%s tier_id=%s",
                    dispatch_result.get("status_code"),
                    event.get("id"),
                    dispatch_result.get("invoice_id"),
                    dispatch_result.get("org_id"),
                    dispatch_result.get("tier_id"),
                )
            # All other statuses (applied, skipped_*) fall through to forward.

        # T3 — dispatch customer.subscription.{updated,deleted} for downgrade reset.
        # Ticket: 20260516_auto_credit_invoice_paid_wiring.
        #
        # .updated: a tier change is signalled by price_id changing on the
        #   subscription items. Stripe sends the new subscription object in
        #   data.object and the OLD attributes in data.previous_attributes.
        #   Library helper reset_subscription_credit_on_tier_change handles
        #   the actual decision (downgrade detection via sort_order, the
        #   reset_on_downgrade policy check, and the source-tier 409 safety).
        # .deleted: full cancellation. payment-service's _sync_subscription_tier
        #   sets billing tier=free; we mirror that here by treating
        #   new_tier_id="free" and letting the helper decide whether to reset.
        #
        # Status → HTTP same matrix as T2.
        elif event_type in ("customer.subscription.updated",
                            "customer.subscription.deleted"):
            from .subscription_credit import reset_subscription_credit_on_tier_change

            sub = event.get("data", {}).get("object", {}) or {}
            previous = event.get("data", {}).get("previous_attributes", {}) or {}
            org_id = _extract_org_id_from_subscription(sub)

            # Resolve old/new price IDs by event type
            if event_type == "customer.subscription.deleted":
                # On delete: subscription.items still carries the last price.
                old_price_id = _extract_subscription_price_id(sub)
                # TODO(public-mesh-ga): Replace hardcoded cancellation target
                # with the consumer catalog's configured default/free tier.
                # Backlink:
                # /home/ubuntu/infra/infra/code/resource/output/sandbox-platform/tickets/20260516_auto_credit_invoice_paid_wiring/codex_report_20260516_235326_llm_judge_public_mesh_billing_quota.md
                new_tier_id = "free"  # mirror payment-service's _sync_subscription_tier
                new_price_id = None
            else:
                # On update: previous_attributes carries the old items shape
                # (partial — only fields that changed). Current sub has the new.
                new_price_id = _extract_subscription_price_id(sub)
                old_price_id = _extract_previous_subscription_price_id(previous)
                new_tier_id = None  # resolved below if we have a new price

            # Short-circuit when there's nothing to act on
            if not org_id or not old_price_id:
                logger.info(
                    "subscription_update_skip event_id=%s reason=no_org_or_no_old_price "
                    "(not a tier change, or missing metadata)",
                    event.get("id"),
                )
            else:
                # Adapter — same shape as T2's _resolve, scoped to this branch.
                async def _resolve(identifier: str) -> Optional[str]:
                    return await _resolve_id_to_tier(
                        identifier,
                        tier_map=tier_map,
                        payment=payment,
                        consumer_org_id=consumer_org_id,
                        explicit_plan_to_tier=explicit_plan_to_tier,
                        explicit_stripe_price_to_tier=explicit_stripe_price_to_tier,
                    )

                old_tier_id = await _resolve(old_price_id)
                if event_type == "customer.subscription.updated" and new_price_id:
                    new_tier_id = await _resolve(new_price_id)

                if not old_tier_id or not new_tier_id:
                    logger.info(
                        "subscription_update_skip event_id=%s reason=unresolved_tier "
                        "old_price=%s old_tier=%s new_price=%s new_tier=%s",
                        event.get("id"), old_price_id, old_tier_id, new_price_id, new_tier_id,
                    )
                else:
                    try:
                        reset_result = await reset_subscription_credit_on_tier_change(
                            org_id=org_id,
                            old_tier_id=old_tier_id,
                            new_tier_id=new_tier_id,
                            tier_registry=tier_registry or {},
                            billing_client=billing,
                            tier_change_event_id=event.get("id"),
                        )
                    except Exception as e:
                        logger.exception(
                            "subscription_update_dispatch_unexpected_exception "
                            "event_id=%s err=%s",
                            event.get("id"), e,
                        )
                        raise HTTPException(status_code=500, detail="Dispatch error")

                    reset_status = reset_result.get("status")
                    logger.info(
                        "subscription_update_dispatch_result event_type=%s status=%s "
                        "event_id=%s org_id=%s old_tier=%s new_tier=%s",
                        event_type, reset_status, event.get("id"),
                        org_id, old_tier_id, new_tier_id,
                    )

                    if reset_status == "deferred_transient":
                        raise HTTPException(
                            status_code=503,
                            detail="Transient billing failure; will retry",
                        )
                    if reset_status == "failed":
                        logger.error(
                            "subscription_update_failed_PAGE status_code=%s "
                            "event_id=%s org_id=%s old_tier=%s new_tier=%s",
                            reset_result.get("status_code"),
                            event.get("id"), org_id, old_tier_id, new_tier_id,
                        )
                    # All other statuses (reset, skipped_*) fall through to forward.

        # Forward to payment-service. payment-service accepts the same
        # signature thanks to T0d's multi-secret support.
        try:
            result = await payment.forward_webhook(body, signature)
        except PaymentServiceError as e:
            # Forward failures (including 4xx from payment-service) bubble
            # back to Stripe as 503 so Stripe redelivers. The grant work T2
            # does is idempotent on invoice:{id}:credit_grant, and payment-
            # service dedups on Stripe event_id, so retry is safe.
            logger.warning(
                "payment_service_forward_failed status_code=%d event_id=%s",
                getattr(e, "status_code", 0), event.get("id"),
            )
            raise HTTPException(status_code=503, detail="Forward failed; will retry")

        # Webhook fallback: tier sync for checkouts where success page didn't process.
        # Uses the verified event dict from T1 instead of re-parsing body.
        if db:
            try:
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
                                # T0e — pass the explicit maps loaded from
                                # quota-config.json's billing_integration block
                                # so the webhook-fallback path benefits from
                                # consumer-pinned mappings too (not just T2/T3).
                                tid = await _resolve_plan_to_tier(
                                    plan_id, tier_map, payment, consumer_org_id,
                                    explicit_plan_to_tier=explicit_plan_to_tier,
                                    explicit_stripe_price_to_tier=explicit_stripe_price_to_tier,
                                )
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

# ---------------------------------------------------------------------------
# Stripe-shape extraction helpers — used by T3 dispatch.
# Kept module-level for unit testing against real Stripe-shaped fixtures.
# Tolerant of partial / missing nested structure (Stripe's
# previous_attributes only carries CHANGED fields, not the whole sub).
# ---------------------------------------------------------------------------

def _extract_subscription_price_id(sub):
    """Pull the current price_id from a Stripe subscription object.

    Shape: sub.items.data[0].price.id
    Returns None if any link in the chain is missing.

    Ticket: 20260516_auto_credit_invoice_paid_wiring (T3).
    """
    if not sub:
        return None
    items = (sub.get("items") or {}).get("data") or []
    if not items:
        return None
    price = items[0].get("price") or {}
    pid = price.get("id")
    return pid if isinstance(pid, str) else None


def _extract_previous_subscription_price_id(previous):
    """Pull the OLD price_id from a Stripe customer.subscription.updated event's
    `previous_attributes`.

    Caveat: `previous_attributes` is partial — Stripe only sends fields that
    changed. If items didn't change, this returns None and the caller should
    treat the event as a non-tier-change (no reset).

    Same nested shape as the current-price extractor.

    Ticket: 20260516_auto_credit_invoice_paid_wiring (T3).
    """
    if not previous:
        return None
    items = (previous.get("items") or {}).get("data") or []
    if not items:
        return None
    price = items[0].get("price") or {}
    pid = price.get("id")
    return pid if isinstance(pid, str) else None


def _extract_org_id_from_subscription(sub):
    """Pull org_id from a Stripe subscription's metadata.

    Set by payment-service's checkout flow (Phase 2.1 of parent ticket).
    Pre-2.1 subscriptions don't have it; caller treats None as "skip".

    Ticket: 20260516_auto_credit_invoice_paid_wiring (T3).
    """
    if not sub:
        return None
    md = sub.get("metadata") or {}
    org_id = md.get("org_id") if isinstance(md, dict) else None
    return org_id if isinstance(org_id, str) and org_id else None


async def _resolve_id_to_tier(
    identifier: str,
    *,
    tier_map: dict[str, str],
    payment: PaymentServiceClient,
    consumer_org_id: str,
    explicit_plan_to_tier: Optional[dict[str, str]] = None,
    explicit_stripe_price_to_tier: Optional[dict[str, str]] = None,
) -> Optional[str]:
    """Stable, multi-ID-space resolver.

    `identifier` can be any of:
      - payment-service plan UUID (from invoice.metadata.plan_id, Phase 2.1 propagation)
      - Stripe price ID (from customer.subscription.updated items[].price.id)
      - Stripe plan ID (legacy; rare today)

    Resolution priority (most-stable first):
      1. Explicit consumer-declared map in quota-config.json's
         `billing_integration.plan_to_tier` / `stripe_price_to_tier` —
         a flat dict from the consumer's config, never depends on
         payment-service responses or display-name drift.
      2. Payment plan `metadata.tier_id` — set at seed time when the
         consumer's seed_plans.sh includes `metadata = {"tier_id": ...}`.
         Stable across renames.
      3. Match `identifier` against any price's `stripe_price_id` to
         find the parent plan, then resolve as in priority 2.
      4. Display-name match (legacy fallback): map plan.name.lower() →
         tier_id via tier_map. Logs a WARNING because display-name
         drift will silently break this — fix by adding the mapping
         to step 1 or to seed metadata for step 2.

    Returns None if no path resolves. Logs at the matching level so
    operators can see WHICH path resolved (debug/warning).

    Ticket: 20260516_auto_credit_invoice_paid_wiring (T0e).
    """
    if not identifier:
        return None

    explicit_plan_to_tier = explicit_plan_to_tier or {}
    explicit_stripe_price_to_tier = explicit_stripe_price_to_tier or {}

    # Priority 1: explicit consumer maps — both ID spaces in one shot.
    if identifier in explicit_plan_to_tier:
        logger.debug("resolve_id_to_tier identifier=%s via explicit_plan_to_tier", identifier)
        return explicit_plan_to_tier[identifier]
    if identifier in explicit_stripe_price_to_tier:
        logger.debug("resolve_id_to_tier identifier=%s via explicit_stripe_price_to_tier", identifier)
        return explicit_stripe_price_to_tier[identifier]

    # Priorities 2-4 need to query the payment service.
    try:
        plans_data = await payment.get_plans(consumer_org_id, provider_org=consumer_org_id)
    except Exception as e:
        logger.warning("resolve_id_to_tier: get_plans failed: %s", e)
        return None

    # Priority 2: direct plan_id match → check metadata.tier_id
    for p in plans_data.plans:
        if p.plan_id != identifier:
            continue
        # PlanItem.model_config allows extras; metadata flows through if
        # payment-service returns it. Read defensively.
        meta = getattr(p, "metadata", None) or {}
        if isinstance(meta, dict) and meta.get("tier_id"):
            tid = meta["tier_id"]
            logger.debug("resolve_id_to_tier identifier=%s via plan.metadata.tier_id=%s",
                         identifier, tid)
            return tid
        # No metadata.tier_id on this plan — fall through to priority 4
        # (display-name) for THIS plan only.
        break

    # Priority 3: Stripe price ID → find parent plan → check metadata.tier_id
    for p in plans_data.plans:
        prices = getattr(p, "prices", None) or []
        for price in prices:
            sp_id = getattr(price, "stripe_price_id", None)
            if sp_id and sp_id == identifier:
                meta = getattr(p, "metadata", None) or {}
                if isinstance(meta, dict) and meta.get("tier_id"):
                    tid = meta["tier_id"]
                    logger.debug(
                        "resolve_id_to_tier identifier=%s via price.stripe_price_id → "
                        "plan.metadata.tier_id=%s",
                        identifier, tid,
                    )
                    return tid
                # Found the plan but no metadata.tier_id — fall through to
                # priority 4 with the parent plan's display name.
                name = (p.name or "").lower()
                if name in tier_map:
                    tid = tier_map[name]
                    # TODO(public-mesh-ga): Make this fallback dev-only or
                    # emit an operator error once plan metadata/config maps
                    # are standard; display names are not stable IDs.
                    # Backlink:
                    # /home/ubuntu/infra/infra/code/resource/output/sandbox-platform/tickets/20260516_auto_credit_invoice_paid_wiring/codex_report_20260516_235326_llm_judge_public_mesh_billing_quota.md
                    logger.warning(
                        "resolve_id_to_tier identifier=%s resolved via plan display-name "
                        "fallback (Stripe price %s → plan %r → tier %s). Pin this in "
                        "quota-config.json's billing_integration.stripe_price_to_tier "
                        "to avoid drift.",
                        identifier, sp_id, p.name, tid,
                    )
                    return tid

    # Priority 4 (final): plan_id matches but no metadata, use display name.
    for p in plans_data.plans:
        if p.plan_id == identifier:
            name = (p.name or "").lower()
            if name in tier_map:
                tid = tier_map[name]
                # TODO(public-mesh-ga): Make this fallback dev-only or
                # emit an operator error once plan metadata/config maps
                # are standard; display names are not stable IDs.
                # Backlink:
                # /home/ubuntu/infra/infra/code/resource/output/sandbox-platform/tickets/20260516_auto_credit_invoice_paid_wiring/codex_report_20260516_235326_llm_judge_public_mesh_billing_quota.md
                logger.warning(
                    "resolve_id_to_tier identifier=%s resolved via plan display-name "
                    "fallback (%r → %s). Pin this in quota-config.json's "
                    "billing_integration.plan_to_tier to avoid drift.",
                    identifier, p.name, tid,
                )
                return tid

    logger.info(
        "resolve_id_to_tier identifier=%s did not resolve via any path "
        "(checked %d plans).", identifier, len(plans_data.plans),
    )
    return None


async def _resolve_plan_to_tier(
    plan_id: str,
    tier_map: dict[str, str],
    payment: PaymentServiceClient,
    consumer_org_id: str,
    explicit_plan_to_tier: Optional[dict[str, str]] = None,
    explicit_stripe_price_to_tier: Optional[dict[str, str]] = None,
) -> Optional[str]:
    """Back-compat shim. Delegates to _resolve_id_to_tier.

    Existing call sites that only have a payment plan_id continue to work.
    New call sites (T2/T3 invoice/subscription dispatch) should call
    _resolve_id_to_tier directly to express that the identifier may be
    either a plan_id or a Stripe price_id.
    """
    # TODO(public-mesh-ga): Do not require the legacy display-name tier_map
    # when explicit plan/price maps are present; explicit maps should be
    # sufficient for production resolution. Backlink:
    # /home/ubuntu/infra/infra/code/resource/output/sandbox-platform/tickets/20260516_auto_credit_invoice_paid_wiring/codex_report_20260516_235326_llm_judge_public_mesh_billing_quota.md
    if not plan_id or not tier_map:
        return None
    return await _resolve_id_to_tier(
        plan_id,
        tier_map=tier_map,
        payment=payment,
        consumer_org_id=consumer_org_id,
        explicit_plan_to_tier=explicit_plan_to_tier,
        explicit_stripe_price_to_tier=explicit_stripe_price_to_tier,
    )


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
