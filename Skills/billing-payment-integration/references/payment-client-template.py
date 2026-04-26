"""
Payment Service Client — Drop-in template for mesh consumers.

Copy this file to your service's app/ directory and rename.
Set PAYMENT_SERVICE_URL and PAYMENT_SERVICE_API_KEY in your .env.

Usage:
    from .payment_client import PaymentServiceClient, PaymentServiceError
    payment_client = PaymentServiceClient()

    # In a route:
    plans = await payment_client.get_plans(consumer_org)
    session = await payment_client.create_checkout_session(org_id, plan_id, ...)
    portal = await payment_client.create_portal_session(org_id, return_url)
"""

import httpx
import os
from typing import Dict, Any, Optional
import structlog

logger = structlog.get_logger()


class PaymentServiceError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Payment service error ({status_code}): {detail}")


class PaymentServiceClient:
    def __init__(self):
        self.base_url = os.getenv("PAYMENT_SERVICE_URL", "").rstrip("/")
        self.api_key = os.getenv("PAYMENT_SERVICE_API_KEY", "")
        self.client = httpx.AsyncClient(timeout=15.0)

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        url = f"{self.base_url}{path}"
        try:
            response = await self.client.request(method, url, headers=self._headers(), **kwargs)
        except httpx.ConnectError:
            raise PaymentServiceError(503, "Payment service unreachable")
        except httpx.TimeoutException:
            raise PaymentServiceError(504, "Payment service timeout")
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text[:200])
            except Exception:
                detail = response.text[:200]
            raise PaymentServiceError(response.status_code, str(detail))
        return response.json()

    async def close(self):
        await self.client.aclose()

    # --- Plans (public) ---

    async def get_plans(self, org_id: str, provider_org: Optional[str] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {"include_prices": "true"}
        if provider_org:
            params["provider_org"] = provider_org
        return await self._request("GET", f"/checkout/{org_id}/plans", params=params)

    # --- Checkout ---

    async def create_checkout_session(
        self, org_id: str, plan_id: str,
        success_url: str, cancel_url: str,
        customer_email: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"success_url": success_url, "cancel_url": cancel_url}
        if customer_email:
            body["customer_email"] = customer_email
        return await self._request("POST", f"/checkout/{org_id}/plan/{plan_id}", json=body)

    async def create_topup_session(
        self, org_id: str, amount: float,
        success_url: str, cancel_url: str,
    ) -> Dict[str, Any]:
        return await self._request("POST", f"/checkout/{org_id}/session", json={
            "amount": amount, "currency": "usd",
            "description": f"Balance top-up ${amount:.2f}",
            "mode": "payment",
            "success_url": success_url, "cancel_url": cancel_url,
            "metadata": {"type": "account_funding", "org_id": org_id},
        })

    async def init_checkout(self) -> Dict[str, Any]:
        return await self._request("POST", "/checkout/init")

    async def verify_checkout_session(self, session_id: str, process_if_complete: bool = True) -> Dict[str, Any]:
        return await self._request("GET", f"/checkout/sessions/{session_id}/verify",
                                   params={"process_if_complete": str(process_if_complete).lower()})

    # --- Portal ---

    async def create_portal_session(self, org_id: str, return_url: str) -> Dict[str, Any]:
        return await self._request("POST", f"/portal/{org_id}/session", json={"return_url": return_url})

    # --- Subscriptions ---

    async def get_subscriptions(self, org_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/subscriptions/{org_id}")

    # --- Invoices ---

    async def get_invoices(self, org_id: str, limit: int = 10) -> Dict[str, Any]:
        return await self._request("GET", f"/invoices/{org_id}/", params={"limit": limit})

    # --- Payment Methods ---

    async def get_payment_methods(self, org_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/payment-methods/{org_id}")

    # --- Webhook forwarding ---

    async def forward_webhook(self, body: bytes, stripe_signature: str) -> Dict[str, Any]:
        url = f"{self.base_url}/webhooks/stripe"
        headers = {"Content-Type": "application/json", "Stripe-Signature": stripe_signature}
        try:
            response = await self.client.request("POST", url, content=body, headers=headers)
        except httpx.ConnectError:
            raise PaymentServiceError(503, "Payment service unreachable")
        if response.status_code >= 400:
            raise PaymentServiceError(response.status_code, "Webhook processing error")
        return response.json()
