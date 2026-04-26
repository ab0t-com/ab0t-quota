"""Bridge-mode backends — thin HTTP clients that target billing's
public mesh quota API instead of running an in-process engine.

These swap into setup_quota when `mode="bridge"` is selected. The
client's QuotaContext API is identical to engine-local mode; only the
implementation changes.

See docs/mesh-quota-api.md for the wire protocol this targets.

When to use bridge mode:
  * Third-party consumer in a different cloud / region with no access
    to shared mesh infrastructure
  * Low-volume per-org checks where 50ms latency is acceptable
  * Prototypes that don't want to provision Redis

When NOT to use bridge mode:
  * High-frequency rate-limit enforcement (use library engine-local
    or BYO-Redis instead)
  * Anything in the request hot path of a high-throughput service

The library API the consumer sees is identical across modes —
`setup_quota(app, mode=...)` picks the implementation transparently.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger("ab0t_quota.bridge")

DEFAULT_TIMEOUT = 5.0  # seconds


class BridgeClient:
    """Async HTTPS client for the mesh quota service. Single instance
    per consumer process; pooled connections."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        service_name: str,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._service = service_name
        self._timeout = timeout
        # Long-lived client with connection pooling. Closed via close().
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"X-API-Key": api_key},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def check(
        self,
        org_id: str,
        resource_key: str,
        user_id: Optional[str] = None,
        increment: float = 1.0,
    ) -> dict:
        url = f"{self._base}/billing/quota/{self._service}/{org_id}/check/{resource_key}"
        params: dict[str, Any] = {"increment": increment}
        if user_id is not None:
            params["user_id"] = user_id
        try:
            resp = await self._client.post(url, params=params)
            return self._parse(resp, op="check")
        except httpx.RequestError as e:
            return _network_error_result(resource_key, str(e))

    async def check_bundle(
        self,
        org_id: str,
        bundle_name: str,
        user_id: Optional[str] = None,
    ) -> dict:
        url = f"{self._base}/billing/quota/{self._service}/{org_id}/check-bundle/{bundle_name}"
        params: dict[str, Any] = {}
        if user_id is not None:
            params["user_id"] = user_id
        try:
            resp = await self._client.post(url, params=params)
            return self._parse(resp, op="check_bundle")
        except httpx.RequestError as e:
            return {"allowed": False, "results": [], "denied_resources": [], "error": str(e)}

    async def increment(
        self,
        org_id: str,
        resource_key: str,
        user_id: Optional[str] = None,
        delta: float = 1.0,
        idempotency_key: Optional[str] = None,
    ) -> float:
        url = f"{self._base}/billing/quota/{self._service}/{org_id}/increment/{resource_key}"
        params: dict[str, Any] = {"delta": delta}
        if user_id is not None:
            params["user_id"] = user_id
        if idempotency_key is not None:
            params["idempotency_key"] = idempotency_key
        try:
            resp = await self._client.post(url, params=params)
            data = self._parse(resp, op="increment")
            return float(data.get("new_value", 0.0))
        except httpx.RequestError as e:
            logger.warning("bridge_increment_network_error: %s", e)
            return 0.0

    async def decrement(
        self,
        org_id: str,
        resource_key: str,
        user_id: Optional[str] = None,
        delta: float = 1.0,
        idempotency_key: Optional[str] = None,
    ) -> float:
        url = f"{self._base}/billing/quota/{self._service}/{org_id}/decrement/{resource_key}"
        params: dict[str, Any] = {"delta": delta}
        if user_id is not None:
            params["user_id"] = user_id
        if idempotency_key is not None:
            params["idempotency_key"] = idempotency_key
        try:
            resp = await self._client.post(url, params=params)
            data = self._parse(resp, op="decrement")
            return float(data.get("new_value", 0.0))
        except httpx.RequestError as e:
            logger.warning("bridge_decrement_network_error: %s", e)
            return 0.0

    async def usage(self, org_id: str) -> dict:
        url = f"{self._base}/billing/quota/{self._service}/{org_id}/usage"
        try:
            resp = await self._client.get(url)
            return self._parse(resp, op="usage")
        except httpx.RequestError as e:
            logger.warning("bridge_usage_network_error: %s", e)
            return {
                "org_id": org_id, "tier_id": "free", "tier_display": "Free",
                "resources": [], "error": str(e),
            }

    async def get_tier(self, org_id: str) -> str:
        url = f"{self._base}/billing/{org_id}/tier"
        try:
            resp = await self._client.get(url)
            if resp.status_code == 200:
                return resp.json().get("tier_id", "free")
        except httpx.RequestError as e:
            logger.warning("bridge_tier_fetch_network_error: %s", e)
        return "free"

    @staticmethod
    def _parse(resp: httpx.Response, op: str) -> dict:
        if 200 <= resp.status_code < 300:
            return resp.json()
        # Build a structured error response that mirrors the engine-local
        # shape so the QuotaContext caller doesn't need to branch on mode.
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text or f"HTTP {resp.status_code}"
        logger.warning("bridge_%s_error status=%d detail=%s", op, resp.status_code, detail)
        return {
            "decision": "allow",  # fail-open on bridge errors (configurable later)
            "current": 0,
            "limit": None,
            "message": f"bridge error: {detail}",
            "_bridge_error": True,
            "_status": resp.status_code,
        }


def _network_error_result(resource_key: str, error: str) -> dict:
    """Shape of a check result when the network call itself failed."""
    return {
        "decision": "allow",  # fail-open by default
        "resource_key": resource_key,
        "current": 0, "requested": 1, "limit": None,
        "tier_id": "free", "tier_display": "Free",
        "severity": "info",
        "message": f"bridge unreachable: {error}",
        "_bridge_error": True,
    }


# ---------------------------------------------------------------------------
# Counter / TierProvider / Store implementations that delegate to BridgeClient
# ---------------------------------------------------------------------------
# These plug into the existing QuotaEngine surface so bridge-mode and
# engine-local mode look the same to the consumer code.

class RemoteTierProvider:
    """TierProvider that fetches tier from the mesh quota service."""

    def __init__(self, client: BridgeClient):
        self._client = client

    async def get_tier(self, org_id: str, **kwargs) -> str:
        return await self._client.get_tier(org_id)


class BridgeContext:
    """Bridge-mode equivalent of QuotaContext — identical surface, but
    every operation is an HTTPS call. Returned by setup_quota when
    mode='bridge'.

    Stashed on app.state.quota; the consumer's route handlers call
    .check(), .increment(), .decrement(), etc — same API as engine-local.
    """

    def __init__(self, client: BridgeClient):
        self._client = client

    async def check(self, org_id: str, resource_key: str, **kwargs):
        from fastapi import HTTPException
        result = await self._client.check(
            org_id=org_id,
            resource_key=resource_key,
            user_id=kwargs.get("user_id"),
            increment=kwargs.get("increment", 1.0),
        )
        if result.get("decision") == "deny":
            raise HTTPException(status_code=429, detail=result)
        return result

    async def check_bundle(self, org_id: str, bundle: str, user_id: Optional[str] = None):
        from fastapi import HTTPException
        result = await self._client.check_bundle(org_id, bundle, user_id=user_id)
        if not result.get("allowed", True):
            raise HTTPException(status_code=429, detail=result)
        return result

    async def increment_bundle(
        self, org_id: str, bundle: str,
        user_id: Optional[str] = None, idempotency_key: Optional[str] = None,
    ) -> dict:
        # Bridge mode: no batch endpoint for bundle increment yet — fan out
        # via single increments. Each carries a per-resource idempotency key.
        # Note: this is N HTTP calls; in engine-local mode it's one Redis pipeline.
        # Acceptable for low-volume; revisit if a batch endpoint is added.
        # The list of resources is unknown without an extra round-trip — for
        # now this is a no-op; consumers should call increment() per resource.
        # TODO: add batch_increment endpoint to the public API.
        logger.warning("bridge increment_bundle is not yet supported — call increment per resource")
        return {}

    async def decrement_bundle(
        self, org_id: str, bundle: str,
        user_id: Optional[str] = None, idempotency_key: Optional[str] = None,
    ) -> dict:
        logger.warning("bridge decrement_bundle is not yet supported — call decrement per resource")
        return {}

    async def usage(self, org_id: str):
        return await self._client.usage(org_id)

    async def feature(self, org_id: str, feature_name: str) -> bool:
        # Bridge mode doesn't have a feature endpoint yet — derive from usage.
        u = await self._client.usage(org_id)
        # Features aren't included in usage response. Fall back to None.
        # TODO: add /quota/{service}/{org}/feature/{name} endpoint.
        return False
