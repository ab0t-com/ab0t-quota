"""HTTP clients for the billing and payment mesh services.

These clients handle auth (X-API-Key), error extraction, timeouts,
and the specific URL patterns each service expects. They are used
internally by the router factory — consumers don't need to touch them.

Return types match the models in billing/models.py exactly.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Optional
from uuid import uuid4

import httpx

from .models import (
    RecordUsageRequest,
    UsageMetadata,
    BillingBalanceResponse,
    BillingTransactionsResponse,
    BillingUsageRecordsResponse,
    BillingUsageSummaryResponse,
    CancelSubscriptionResponse,
    CheckoutSessionResponse,
    CheckoutInitResponse,
    CheckoutVerifyResponse,
    InvoicesResponse,
    PaymentMethodDeleteResponse,
    PaymentMethodSetDefaultResponse,
    PaymentMethodsResponse,
    PlansResponse,
    PortalSessionResponse,
    PromotionalCreditResponse,
    SubscriptionsResponse,
    TierChangeResponse,
    WebhookResponse,
)

logger = logging.getLogger("ab0t_quota.billing")


# =========================================================================
# Error types
# =========================================================================

class PaymentServiceError(Exception):
    """Structured error from the payment service."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Payment service error ({status_code}): {detail}")


class BillingServiceError(Exception):
    """Structured error from the billing service."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Billing service error ({status_code}): {detail}")


# =========================================================================
# Shared helpers
# =========================================================================

def _extract_detail(response: httpx.Response) -> str:
    """Extract error detail from an upstream response."""
    try:
        body = response.json()
        if isinstance(body, dict):
            detail = body.get("detail") or body.get("message") or body.get("error")
            if isinstance(detail, str) and detail.strip():
                return detail.strip()
    except Exception:
        pass
    text = (response.text or "").strip()
    return text[:500] if text else f"HTTP {response.status_code}"


def _api_key_headers(api_key: str) -> dict[str, str]:
    """Build auth headers from an opaque API key."""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        if "." in api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            headers["X-API-Key"] = api_key
    return headers


# =========================================================================
# Payment Service Client
# =========================================================================

class PaymentServiceClient:
    """Async HTTP client for the payment service (port 8005).

    Handles: plans, checkout, portal, subscriptions, invoices, payment
    methods, setup intents, and webhook forwarding.
    """

    def __init__(self, base_url: str, api_key: str, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.client = httpx.AsyncClient(timeout=timeout)

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}{path}"
        try:
            response = await self.client.request(
                method, url, headers=_api_key_headers(self.api_key), **kwargs,
            )
        except httpx.ConnectError as e:
            logger.warning("payment_unreachable url=%s error=%s", url, str(e))
            raise PaymentServiceError(503, "Payment service unreachable")
        except httpx.TimeoutException:
            logger.warning("payment_timeout url=%s", url)
            raise PaymentServiceError(504, "Payment service timeout")

        if response.status_code >= 400:
            detail = _extract_detail(response)
            logger.warning("payment_error url=%s status=%s detail=%s", url, response.status_code, detail)
            raise PaymentServiceError(response.status_code, detail)

        return response.json()

    async def close(self) -> None:
        await self.client.aclose()

    # --- Plans ---

    async def get_plans(self, org_id: str, provider_org: Optional[str] = None) -> PlansResponse:
        params: dict[str, str] = {"include_prices": "true"}
        if provider_org:
            params["provider_org"] = provider_org
        data = await self._request("GET", f"/checkout/{org_id}/plans", params=params)
        return PlansResponse.model_validate(data)

    # --- Checkout ---

    async def init_checkout(self) -> CheckoutInitResponse:
        data = await self._request("POST", "/checkout/init")
        return CheckoutInitResponse.model_validate(data)

    async def create_checkout_session(
        self, org_id: str, plan_id: str,
        success_url: str, cancel_url: str,
        customer_email: Optional[str] = None,
        session_token: Optional[str] = None,
        fingerprint: Optional[str] = None,
    ) -> CheckoutSessionResponse:
        body: dict[str, str] = {"success_url": success_url, "cancel_url": cancel_url}
        if customer_email:
            body["customer_email"] = customer_email
        if session_token:
            body["session_token"] = session_token
        if fingerprint:
            body["fingerprint"] = fingerprint
        data = await self._request("POST", f"/checkout/{org_id}/plan/{plan_id}", json=body)
        return CheckoutSessionResponse.model_validate(data)

    async def create_topup_session(
        self, org_id: str, amount: float,
        success_url: str, cancel_url: str,
    ) -> CheckoutSessionResponse:
        data = await self._request("POST", f"/checkout/{org_id}/session", json={
            "amount": amount,
            "currency": "usd",
            "description": f"Balance top-up ${amount:.2f}",
            "mode": "payment",
            "success_url": success_url,
            "cancel_url": cancel_url,
            "metadata": {"type": "account_funding", "org_id": org_id},
        })
        return CheckoutSessionResponse.model_validate(data)

    async def verify_checkout_session(
        self, session_id: str, process_if_complete: bool = True,
        verification_token: Optional[str] = None,
    ) -> CheckoutVerifyResponse:
        # Payment service now requires `verification_token` when
        # process_if_complete=True (returns 403 "Verification token is
        # required for session processing" otherwise). The token is minted
        # by `create_checkout_session` and is hashed into the Stripe session's
        # metadata; the caller is expected to stash the plaintext token and
        # replay it here.
        params: dict[str, str] = {"process_if_complete": str(process_if_complete).lower()}
        if verification_token:
            params["verification_token"] = verification_token
        data = await self._request(
            "GET", f"/checkout/sessions/{session_id}/verify",
            params=params,
        )
        return CheckoutVerifyResponse.model_validate(data)

    # --- Portal ---

    async def create_portal_session(self, org_id: str, return_url: str) -> PortalSessionResponse:
        data = await self._request("POST", f"/portal/{org_id}/session", json={"return_url": return_url})
        return PortalSessionResponse.model_validate(data)

    # --- Subscriptions ---

    async def get_subscriptions(self, org_id: str) -> SubscriptionsResponse:
        data = await self._request("GET", f"/subscriptions/{org_id}")
        return SubscriptionsResponse.model_validate(data)

    async def cancel_subscription(self, org_id: str, subscription_id: str) -> CancelSubscriptionResponse:
        data = await self._request("DELETE", f"/subscriptions/{org_id}/{subscription_id}")
        return CancelSubscriptionResponse.model_validate(data)

    # --- Invoices ---

    async def get_invoices(self, org_id: str, limit: int = 10, offset: int = 0) -> InvoicesResponse:
        data = await self._request("GET", f"/invoices/{org_id}/", params={"limit": limit, "offset": offset})
        return InvoicesResponse.model_validate(data)

    async def get_invoice_pdf_url(self, org_id: str, invoice_id: str) -> str:
        # Payment's actual download endpoint (the legacy /pdf path doesn't exist)
        url = f"{self.base_url}/invoices/v2/invoices/{org_id}/{invoice_id}/download"
        try:
            response = await self.client.request(
                "GET", url, headers=_api_key_headers(self.api_key), follow_redirects=False,
            )
        except httpx.ConnectError:
            raise PaymentServiceError(503, "Payment service unreachable")
        except httpx.TimeoutException:
            raise PaymentServiceError(504, "Payment service timeout")
        if response.status_code in (301, 302, 307, 308):
            return response.headers.get("location", "")
        if response.status_code >= 400:
            raise PaymentServiceError(response.status_code, _extract_detail(response))
        try:
            body = response.json()
            return body.get("url") or body.get("pdf_url") or ""
        except Exception:
            raise PaymentServiceError(502, "Unexpected invoice PDF response")

    # --- Payment Methods ---

    async def get_payment_methods(self, org_id: str) -> PaymentMethodsResponse:
        data = await self._request("GET", f"/payment-methods/{org_id}")
        return PaymentMethodsResponse.model_validate(data)

    async def set_default_method(self, org_id: str, method_id: str) -> PaymentMethodSetDefaultResponse:
        data = await self._request("PUT", f"/payment-methods/{org_id}/{method_id}/default")
        return PaymentMethodSetDefaultResponse.model_validate(data)

    async def remove_method(self, org_id: str, method_id: str) -> PaymentMethodDeleteResponse:
        data = await self._request("DELETE", f"/payment-methods/{org_id}/{method_id}")
        return PaymentMethodDeleteResponse.model_validate(data)

    # --- Webhook forwarding ---

    async def forward_webhook(self, body: bytes, stripe_signature: str) -> WebhookResponse:
        url = f"{self.base_url}/webhooks/stripe"
        headers = {"Content-Type": "application/json", "Stripe-Signature": stripe_signature}
        try:
            response = await self.client.request("POST", url, content=body, headers=headers)
        except httpx.ConnectError:
            raise PaymentServiceError(503, "Payment service unreachable")
        except httpx.TimeoutException:
            raise PaymentServiceError(504, "Payment service timeout")
        if response.status_code >= 400:
            raise PaymentServiceError(response.status_code, _extract_detail(response))
        data = response.json()
        return WebhookResponse.model_validate(data)


# =========================================================================
# Billing Service Client
# =========================================================================

class BillingServiceClient:
    """Async HTTP client for the billing service (port 8002).

    Handles: balance, usage, transactions, tier management.
    """

    def __init__(self, base_url: str, api_key: str, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.client = httpx.AsyncClient(timeout=timeout)

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}{path}"
        try:
            response = await self.client.request(
                method, url, headers=_api_key_headers(self.api_key), **kwargs,
            )
        except httpx.ConnectError as e:
            logger.warning("billing_unreachable url=%s error=%s", url, str(e))
            raise BillingServiceError(503, "Billing service unreachable")
        except httpx.TimeoutException:
            logger.warning("billing_timeout url=%s", url)
            raise BillingServiceError(504, "Billing service timeout")

        if response.status_code >= 400:
            detail = _extract_detail(response)
            logger.warning("billing_error url=%s status=%s detail=%s", url, response.status_code, detail)
            raise BillingServiceError(response.status_code, detail)

        return response.json()

    async def close(self) -> None:
        await self.client.aclose()

    async def get_balance(self, org_id: str) -> BillingBalanceResponse:
        data = await self._request("GET", f"/billing/{org_id}/balance")
        return BillingBalanceResponse.model_validate(data)

    async def get_usage_summary(self, org_id: str) -> BillingUsageSummaryResponse:
        # Billing's actual path: /billing/usage/{org}/summary (not /billing/{org}/usage/summary)
        data = await self._request("GET", f"/billing/usage/{org_id}/summary")
        return BillingUsageSummaryResponse.model_validate(data)

    async def get_usage_records(self, org_id: str, limit: int = 20, offset: int = 0) -> BillingUsageRecordsResponse:
        # Billing's actual path: /billing/usage/{org}/records
        data = await self._request("GET", f"/billing/usage/{org_id}/records", params={"limit": limit, "offset": offset})
        return BillingUsageRecordsResponse.model_validate(data)

    async def get_transactions(self, org_id: str, limit: int = 20, offset: int = 0) -> BillingTransactionsResponse:
        # No trailing slash — billing returns 307 then the proxy can't decode it
        data = await self._request("GET", f"/billing/{org_id}/transactions", params={"limit": limit, "offset": offset})
        return BillingTransactionsResponse.model_validate(data)

    async def set_tier(self, org_id: str, tier_id: str, reason: str = "checkout_complete") -> TierChangeResponse:
        data = await self._request("PUT", f"/billing/{org_id}/tier", json={"tier_id": tier_id, "reason": reason})
        return TierChangeResponse.model_validate(data)

    async def record_usage(self, payload: "RecordUsageRequest | dict") -> dict:
        """Record a usage event in billing. Accepts a typed RecordUsageRequest
        (the typed path) or a raw dict (the legacy back-compat path). org_id is
        read from the body and used in the URL so callers don't pass it twice.

        Billing's contract: POST /billing/usage/{org_id}/ with body =
        RecordUsageRequest (org_id, user_id, tool_id, session_id, request_id,
        resource_type, reservation_id, cost, platform_fee, metadata). NOTE:
        billing has NO top-level `action` field — `action` lives in metadata,
        and billing IGNORES unknown top-level fields (metadata is the only
        propagating open channel). Omitting cost/platform_fee is the PRICED
        path (MINIMUM_USAGE_COST + balance debit); for metering rows use
        record_resource_usage which forces cost="0"/platform_fee="0".

        A RecordUsageRequest is dumped with exclude_none=True. A raw dict is
        passed through UNCHANGED (legacy back-compat — deprecated; migrate to
        the typed path / record_resource_usage). Legacy dicts are NOT validated
        through the forbid model so existing callers that send extra top-level
        keys do not start failing.
        """
        if isinstance(payload, RecordUsageRequest):
            body = payload.model_dump(exclude_none=True)
        else:
            body = payload
        org_id = body.get("org_id")
        if not org_id:
            raise BillingServiceError(400, "record_usage payload missing org_id")
        try:
            return await self._request("POST", f"/billing/usage/{org_id}/", json=body)
        except BillingServiceError:
            # Best-effort: usage recording failures must not crash the
            # caller's primary path. Log + return.
            logger.warning("record_usage failed for org=%s", org_id)
            return {}

    async def record_resource_usage(
        self, org_id: str, user_id: str, *,
        resource_type: str = "compute",
        session_id: str = "",
        reservation_id: Optional[str] = None,
        request_id: Optional[str] = None,
        tool_id: str = "sandbox-platform",
        metadata: Optional[dict] = None,
        cost: str = "0",
        platform_fee: str = "0",
        compute_time: float = 0.0,
    ) -> dict:
        """Record a metering/analytics usage row for an infra resource.

        This is the METERING path, NOT the money path. The actual charge is the
        reserve -> commit proration done by billing's lifecycle consumer at
        stop/delete. To avoid double-charging / cost fabrication, this row MUST
        send cost="0" + platform_fee="0" + reservation_id (see ticket
        20260615_inter_service_contract_drift, WORKFLOW_FINDINGS section 2).
        These defaults are baked in here so a metering caller can never enter
        billing's priced branch (MINIMUM_USAGE_COST floor + balance debit).

        resource_type is an OPEN string (public mesh). action + descriptive
        dimensions go in the metadata channel via UsageMetadata; never put a
        session token in session_id. Best-effort: returns {} on billing error.
        """
        req = RecordUsageRequest(
            org_id=org_id, user_id=user_id, tool_id=tool_id,
            session_id=session_id,
            request_id=request_id or f"sbx-{uuid4().hex[:12]}",
            resource_type=resource_type,
            reservation_id=reservation_id,
            cost=cost, platform_fee=platform_fee,
            compute_time=compute_time,
            metadata=(metadata if isinstance(metadata, dict) else {}),
        )
        return await self.record_usage(req)

    async def settle_activation(
        self, org_id: str, *,
        settlement_key: str,
        observation: "SettlementObservation",
        reservation_id: Optional[str] = None,
        usage_record_id: Optional[str] = None,
    ) -> dict:
        """Settle usage that can no longer be committed. **D-12 — the revenue-loss close.**

        `POST /billing/{org_id}/settle`. Billing's `/commit` is state-guarded on a LIVE Redis
        reservation hash: past the reservation window it 404s and the revenue is **lost
        forever**. This endpoint is the durable, activation-scoped path — it needs no live
        hash, so a settlement arriving arbitrarily late still lands, in the correct
        three-bucket spend order.

        Ticket: billing/output/tickets/20260712_revenue_chain_integrity (billing's half:
        TASK B-1 / W-D12). This method is the CALLER — without it, billing's settlement
        endpoint is a mechanism nobody invokes, and the money is still gone (D-64).

        ⚠️ `settlement_key` is the ONLY thing standing between a retry and a double-charge.
        Billing dedups it with a **DynamoDB conditional write that has NO TTL** — the dedup is
        durable and *eternal*, which is precisely the point (a 24h Redis window cannot dedup a
        settlement that arrives on day 3). Two consequences, both load-bearing:

          1. **Reusing a key across two genuinely different settlements silently refuses the
             second.** Uniqueness is a contract, not a nicety.
          2. **Pass `reservation_id`** — the same key billing's OWN SQS lifecycle consumer
             uses (`lifecycle_consumer.py:425`: `settlement_key=reservation_id`). This is what
             makes the two settlement paths dedup **against each other**: if this library
             settles an event and the SNS copy of that same event is later delivered to
             billing's consumer, billing's DynamoDB condition refuses the second. Keying on
             anything else (e.g. `activation_id`) would open a **double-charge** — two keys,
             one usage, two debits.

        ⚠️ **THIS SENDS THE INPUTS, NOT A COST (B-D13).**
        ----------------------------------------------------
        We send what we **OBSERVED** — when it started, when it stopped, the rate we were
        quoted — and **billing computes what it costs** with the one law it owns
        (`app/core/proration.py::price_usage`).

        This method used to send a pre-computed `actual_cost`, and that single field pushed the
        cost law across the mesh boundary: this library carried a **port of billing's
        proration**, as did the Go library. Three implementations of one money law
        (**D-35**). They were guarded by a frozen vector table — but *a copy kept in sync is
        still a copy*, and it stays equal only until the day it doesn't, silently.

          **A caller that cannot compute a cost cannot compute it wrong.**

        The library's proration is **archived**, not synchronised (`ab0t_quota/.archive/`).
        **Do not reintroduce arithmetic here.** In particular: if `hourly_rate` is missing, do
        NOT invent one. Billing prices a rate-less runtime at **ZERO and ALERTS**
        (`settle_missing_hourly_rate`) — a fabricated price is worse than no price, and that
        policy lives in exactly one place. We do not duplicate it and we do not compete with it.

        Idempotent. A replay returns HTTP 200 with `replayed=true` and the ORIGINAL payload;
        no money moves.

        Raises `BillingServiceError`. The status codes are billing's real contract, read from
        its handler (`app/api/billing.py` → `app/core/reservation.py::settle_activation`):
          * **409** — "not eligible for settlement": `/commit` already took this money, or the
            reservation is still live and `/commit` should be used. **The books are correct.**
            This is NOT an error and NOT a revenue loss — the caller must treat it as success.
          * **400** — invalid lifetime (`stopped_at < started_at`) or a negative computed cost
            (reachable only via a negative rate/fee — a caller sending garbage).
          * **404** — no billing account for this org.
          * **5xx / 503 / 504** — transient. The settlement did **not** land, or landed and the
            response was lost. **Retry** — never void, never assume. The durable key makes the
            retry safe in both readings.
        """
        body = {
            "settlement_key": settlement_key,
            **observation.to_settlement_payload(),
        }
        # B-D14, not recreated: send absence as ABSENCE. `to_settlement_payload()` OMITS a money
        # key it has no value for rather than sending an explicit `null` — because the bug we
        # just killed was an always-present key whose value was sometimes `None`, against a
        # `.get(k, default)` on the other side that only defaults on an ABSENT key.
        if reservation_id is not None:
            body["reservation_id"] = reservation_id
        if usage_record_id is not None:
            body["usage_record_id"] = usage_record_id

        return await self._request("POST", f"/billing/{org_id}/settle", json=body)

    async def apply_promotional_credit(
        self, org_id: str, amount: float,
        reason: str = "initial_credit",
        idempotency_key: Optional[str] = None,
    ) -> PromotionalCreditResponse:
        """Apply promotional/trial credits to an org's billing account.

        Idempotent — the billing service deduplicates on idempotency_key.
        Server enforces per-request and lifetime caps.
        """
        data = await self._request("POST", f"/billing/{org_id}/promotional-credit", json={
            "amount": amount,
            "reason": reason,
            "idempotency_key": idempotency_key or f"{org_id}:{reason}",
        })
        return PromotionalCreditResponse.model_validate(data)

    async def reset_subscription_credit(
        self, org_id: str,
        expected_source_tier: str,
        idempotency_key: str,
        reason: str = "downgrade_reset",
    ) -> dict:
        """Zero `subscription_credit` after a downgrade, with safety check.

        The endpoint compares the stored `subscription_credit_source_tier`
        against `expected_source_tier`. Mismatch → 409 (the credit came
        from a different subscription; don't wipe). Idempotent on
        `idempotency_key`.

        See ticket 20260516_paid_plan_balance_model_gap (Phase 3.2).
        """
        return await self._request(
            "POST", f"/billing/{org_id}/reset-subscription-credit",
            json={
                "expected_source_tier": expected_source_tier,
                "idempotency_key": idempotency_key,
                "reason": reason,
            },
        )

    async def apply_credit_grant(
        self, org_id: str, amount: float,
        destination: str,
        lifecycle: str,
        idempotency_key: str,
        rollover_max: Optional[float] = None,
        reason: str = "subscription_credit_grant",
        source: Optional[str] = None,
        source_tier: Optional[str] = None,
    ) -> dict:
        """Apply a credit grant per a consumer-declared lifecycle policy.

        Used by the subscription-invoice webhook handler to land
        subscription-bundled credits on an org's billing account per
        the consumer's TierConfig.credit_grant policy.

        Args:
          org_id: target org.
          amount: grant amount (positive).
          destination: "balance" | "credit_balance" | "subscription_credit".
          lifecycle: "persistent" | "use_it_or_lose_it" |
                     "rollover_unlimited" | "rollover_capped".
          rollover_max: required iff lifecycle == "rollover_capped".
          idempotency_key: typically the source invoice ID, so Stripe
                           webhook redelivery is naturally idempotent.
          reason: audit-log reason tag.

        Returns the billing-service response payload (UpdateBalanceResponse
        shape) on success. Raises BillingServiceError on non-2xx.
        """
        body: dict = {
            "amount": amount,
            "destination": destination,
            "lifecycle": lifecycle,
            "idempotency_key": idempotency_key,
            "reason": reason,
        }
        if rollover_max is not None:
            body["rollover_max"] = rollover_max
        if source is not None:
            body["source"] = source
        if source_tier is not None:
            body["source_tier"] = source_tier
        return await self._request(
            "POST", f"/billing/{org_id}/apply-credit-grant", json=body,
        )

    # --- Reservation lifecycle ---

    async def reserve_funds(
        self, org_id: str, user_id: str, estimated_cost: str,
        tool_id: str = "default", session_id: str = "",
        operation_type: str = "api_call", metadata: Optional[dict] = None,
    ) -> Optional[str]:
        """Reserve funds before provisioning. Returns reservation_id or None on 402.

        operation_type default aligns with billing's ReservationRequest default
        (billing/output/app/models/billing.py:218 -> "api_call"). The client
        always sends operation_type on the wire, so billing never applies its
        own default; matching the defaults keeps callers that rely on the
        default sending the value billing's contract would have chosen (it also
        feeds billing's fallback idempotency fingerprint). Pass an explicit
        value (e.g. the resource_type) to categorise the reservation.
        """
        try:
            data = await self._request("POST", f"/billing/{org_id}/reserve", json={
                "org_id": org_id, "user_id": user_id, "tool_id": tool_id,
                "estimated_cost": str(estimated_cost),
                "session_id": session_id, "operation_type": operation_type,
                "metadata": metadata or {},
            })
            return data.get("reservation_id")
        except BillingServiceError as e:
            if e.status_code == 402:
                return None
            raise

    async def commit_reservation(
        self, org_id: str, reservation_id: str,
        actual_usage: Optional[dict] = None,
    ) -> bool:
        """Commit a reservation after successful provisioning."""
        try:
            await self._request("POST", f"/billing/{org_id}/commit", json={
                "reservation_id": reservation_id,
                "actual_usage": actual_usage or {},
            })
            return True
        except BillingServiceError:
            return False

    async def refund_reservation(
        self, org_id: str, reservation_id: str, reason: str = "cancelled",
    ) -> bool:
        """Refund a reservation (launch failure, cancellation)."""
        try:
            await self._request("POST", f"/billing/{org_id}/refund", json={
                "reservation_id": reservation_id,
                "reason": reason,
            })
            return True
        except BillingServiceError:
            return False
