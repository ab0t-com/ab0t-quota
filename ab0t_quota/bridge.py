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
import os
from typing import Any, Optional

import httpx

logger = logging.getLogger("ab0t_quota.bridge")

DEFAULT_TIMEOUT = 5.0  # seconds

#: D-12: the sentinel reported when billing cannot answer under fail-open.
#: Explicitly NOT a tier the operator assigned — never a real (cheapest) tier.
TIER_UNKNOWN = "unknown"


class BridgeUnavailableError(RuntimeError):
    """D-12: typed bridge-outage signal. Raised (default, fail-CLOSED) instead
    of inventing a tier when billing cannot answer a tier/usage read."""


def _bridge_fail_open() -> bool:
    """Bridge outage policy. DEFAULT = FAIL-CLOSED (deny), so a billing outage can
    NEVER admit unbilled usage — the money-safety invariant (never lose a tenant's
    usage or payment). A consumer that explicitly prefers availability-over-billing
    (let traffic through during a billing outage, accepting some unbilled usage) can
    opt in with AB0T_QUOTA_BRIDGE_FAIL_OPEN=true|1|yes|on. The choice is logged
    loudly at every fallback. Matches the middleware default (fail_open=False)."""
    return os.getenv("AB0T_QUOTA_BRIDGE_FAIL_OPEN", "").strip().lower() in ("1", "true", "yes", "on")


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
            # D-12: never invent a tier — same switch as every other bridge op.
            if self._tier_unavailable(org_id, f"network error: {e}", op="usage"):
                return {
                    "org_id": org_id, "tier_id": TIER_UNKNOWN,
                    "tier_display": "Unknown (billing unreachable)",
                    "resources": [], "error": str(e), "_bridge_error": True,
                }
            raise BridgeUnavailableError(
                f"billing unreachable while reading usage for org {org_id} "
                f"(network error: {e}) — failing CLOSED (D-12; "
                f"AB0T_QUOTA_BRIDGE_FAIL_OPEN=true opts into a tier-UNKNOWN result)")

    async def get_tier(self, org_id: str) -> str:
        url = f"{self._base}/billing/{org_id}/tier"
        try:
            resp = await self._client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                if "tier_id" in data:
                    return data["tier_id"]
                error = "HTTP 200 without a tier_id field"
            else:
                error = f"HTTP {resp.status_code}"
        except httpx.RequestError as e:
            error = f"network error: {e}"
        # D-12 (was ENV-17): no invented "free" — a billing outage or a bad mesh
        # key must never silently enforce a paid org at the cheapest tier.
        if self._tier_unavailable(org_id, error, op="get_tier"):
            return TIER_UNKNOWN
        raise BridgeUnavailableError(
            f"billing unreachable while resolving the tier for org {org_id} "
            f"({error}) — failing CLOSED (D-12; AB0T_QUOTA_BRIDGE_FAIL_OPEN=true "
            f"opts into allow with tier UNKNOWN, never an invented tier)")

    @staticmethod
    def _tier_unavailable(org_id: str, error: str, *, op: str) -> bool:
        """Log the outage + the chosen policy; True = fail OPEN (allow)."""
        fail_open = _bridge_fail_open()
        logger.error(
            "bridge_%s_unavailable org=%s — failing %s "
            "(AB0T_QUOTA_BRIDGE_FAIL_OPEN=%s): %s",
            op, org_id,
            "OPEN/allow with tier UNKNOWN" if fail_open else "CLOSED/deny",
            fail_open, error)
        return fail_open

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
        # Money-safety (D5, ticket 20260712_payment_credit_calls_404): FAIL-CLOSED by
        # default. A billing error must NOT admit unbilled usage — during a billing
        # outage the usage-record call fails too, so allowing = lost revenue. Opt into
        # fail-open (availability over billing) with AB0T_QUOTA_BRIDGE_FAIL_OPEN.
        _fail_open = _bridge_fail_open()
        logger.error(
            "bridge_%s_billing_error status=%d — failing %s (AB0T_QUOTA_BRIDGE_FAIL_OPEN=%s) detail=%s",
            op, resp.status_code, "OPEN/allow" if _fail_open else "CLOSED/deny", _fail_open, detail,
        )
        return {
            "decision": "allow" if _fail_open else "deny",
            "current": 0,
            "limit": None,
            "message": f"bridge error: {detail}",
            "_bridge_error": True,
            "_status": resp.status_code,
        }


def _network_error_result(resource_key: str, error: str) -> dict:
    """Shape of a check result when the network call itself failed."""
    # Money-safety (D5): FAIL-CLOSED by default — a bridge/network outage must not
    # admit unbilled usage. Opt into fail-open with AB0T_QUOTA_BRIDGE_FAIL_OPEN.
    _fail_open = _bridge_fail_open()
    logger.error(
        "bridge_network_error resource=%s — failing %s (AB0T_QUOTA_BRIDGE_FAIL_OPEN=%s): %s",
        resource_key, "OPEN/allow" if _fail_open else "CLOSED/deny", _fail_open, error,
    )
    return {
        "decision": "allow" if _fail_open else "deny",
        "resource_key": resource_key,
        "current": 0, "requested": 1, "limit": None,
        # D-12: report the outage, never an invented (cheapest) tier.
        "tier_id": TIER_UNKNOWN, "tier_display": "Unknown (billing unreachable)",
        "severity": "info" if _fail_open else "warning",
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
        # TODO(public-mesh-ga): Add batch increment/decrement endpoints to
        # the public mesh quota API before bridge mode is advertised as a
        # full drop-in replacement for engine-local mode. Backlink:
        # audit: 2026-05-16 public-mesh-ga readiness pass
        logger.warning("bridge increment_bundle is not yet supported — call increment per resource")
        return {}

    async def decrement_bundle(
        self, org_id: str, bundle: str,
        user_id: Optional[str] = None, idempotency_key: Optional[str] = None,
    ) -> dict:
        # TODO(public-mesh-ga): Keep this aligned with increment_bundle's
        # eventual batch endpoint so bundle checks and mutations have
        # equivalent semantics in bridge mode. Backlink:
        # audit: 2026-05-16 public-mesh-ga readiness pass
        logger.warning("bridge decrement_bundle is not yet supported — call decrement per resource")
        return {}

    async def usage(self, org_id: str):
        return await self._client.usage(org_id)

    async def reconcile_org(self, org_id: str, resource_key: Optional[str] = None) -> dict:
        # Ticket 20260810 (P2.1): reconcile_org recomputes counters from their type's
        # truth source. In bridge mode the counters live server-side (billing owns the
        # Redis + the reconciler), so the recalculate operation belongs to the server;
        # there is no public mesh endpoint for it yet. Fail LOUD-but-safe: report that
        # the operation is server-side rather than silently claiming a repair happened.
        # TODO(public-mesh-ga): add POST /billing/quota/{service}/{org}/reconcile and
        # call it here so bridge mode is a full drop-in for the recalculate button.
        logger.warning(
            "bridge reconcile_org is server-side in bridge mode — the billing service "
            "owns the counters + reconciler; no client-side recompute is performed.")
        return {"org_id": org_id, "resources": {},
                "status": "server_side_in_bridge_mode"}

    async def feature(self, org_id: str, feature_name: str) -> bool:
        # Bridge mode doesn't have a feature endpoint yet — derive from usage.
        u = await self._client.usage(org_id)
        # Features aren't included in usage response. Fall back to None.
        # TODO(public-mesh-ga): Add /quota/{service}/{org}/feature/{name}
        # to billing's public bridge API; returning False can hide paid
        # features during bridge adoption. Backlink:
        # audit: 2026-05-16 public-mesh-ga readiness pass
        return False
