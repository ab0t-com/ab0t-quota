"""HTTP clients for the billing and payment mesh services.

These clients handle auth (X-API-Key), error extraction, timeouts,
and the specific URL patterns each service expects. They are used
internally by the router factory — consumers don't need to touch them.

Return types match the models in billing/models.py exactly.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from .models import (
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
    ) -> CheckoutVerifyResponse:
        data = await self._request(
            "GET", f"/checkout/sessions/{session_id}/verify",
            params={"process_if_complete": str(process_if_complete).lower()},
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
        url = f"{self.base_url}/invoices/{org_id}/{invoice_id}/pdf"
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
        data = await self._request("GET", f"/billing/{org_id}/usage/summary")
        return BillingUsageSummaryResponse.model_validate(data)

    async def get_usage_records(self, org_id: str, limit: int = 20, offset: int = 0) -> BillingUsageRecordsResponse:
        data = await self._request("GET", f"/billing/{org_id}/usage/", params={"limit": limit, "offset": offset})
        return BillingUsageRecordsResponse.model_validate(data)

    async def get_transactions(self, org_id: str, limit: int = 20, offset: int = 0) -> BillingTransactionsResponse:
        data = await self._request("GET", f"/billing/{org_id}/transactions/", params={"limit": limit, "offset": offset})
        return BillingTransactionsResponse.model_validate(data)

    async def set_tier(self, org_id: str, tier_id: str, reason: str = "checkout_complete") -> TierChangeResponse:
        data = await self._request("PUT", f"/billing/{org_id}/tier", json={"tier_id": tier_id, "reason": reason})
        return TierChangeResponse.model_validate(data)

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

    # --- Reservation lifecycle ---

    async def reserve_funds(
        self, org_id: str, user_id: str, estimated_cost: str,
        tool_id: str = "default", session_id: str = "",
        operation_type: str = "compute", metadata: Optional[dict] = None,
    ) -> Optional[str]:
        """Reserve funds before provisioning. Returns reservation_id or None on 402."""
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
