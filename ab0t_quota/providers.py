"""Tier providers — resolve an org_id to its tier.

Services pick the provider that fits their architecture:
- JWTTierProvider: reads tier from JWT claims (zero network calls)
- AuthServiceTierProvider: calls auth service API (cached in Redis)
- StaticTierProvider: hardcoded mapping (for tests)
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Optional, Callable, Awaitable

from redis.asyncio import Redis

logger = logging.getLogger("ab0t_quota.providers")


class TierFetchError(RuntimeError):
    """Raised by a fetch_fn when the tier source (billing/auth) cannot be
    reached OR answers without a usable tier.

    Ticket 20260810_tier_failclosed_lastknowngood (CLASS-1 DSD-01/02): a
    fetch_fn MUST NOT invent a tier (e.g. return `free`) on failure — an
    invented tier is indistinguishable from a real assignment and, once
    cached, enforces a paying org at the wrong (cheapest) tier for the whole
    TTL window. Raise this instead; AuthServiceTierProvider then serves the
    last-known-good tier (never a downgrade). Mirrors the fail-CLOSED
    discipline of bridge.py's BridgeUnavailableError, but for the
    engine-local path the safe direction is last-known-good, not deny."""


class TierProvider(ABC):
    """Abstract base — resolves org_id to tier_id string."""

    @abstractmethod
    async def get_tier(self, org_id: str, **kwargs) -> str:
        """Return the tier_id for an org (e.g. 'free', 'starter', 'pro')."""


class JWTTierProvider(TierProvider):
    """Read tier from JWT token claims. Zero network calls.

    Requires auth-service to embed `org_tier` in the JWT.
    Falls back to default_tier if claim is missing.

    Usage:
        provider = JWTTierProvider(claim_key="org_tier", default_tier="free")
        tier = await provider.get_tier(org_id, token_claims=user.token_claims)
    """

    def __init__(self, claim_key: str = "org_tier", default_tier: str = "free"):
        self._claim_key = claim_key
        self._default_tier = default_tier

    async def get_tier(self, org_id: str, **kwargs) -> str:
        claims = kwargs.get("token_claims", {})
        return claims.get(self._claim_key, self._default_tier)


class AuthServiceTierProvider(TierProvider):
    """Call auth service / billing to get org tier, cached in Redis, with a
    fail-safe last-known-good fallback.

    Usage:
        provider = AuthServiceTierProvider(
            fetch_fn=my_auth_client.get_org_tier,
            redis=redis,
            cache_ttl=300,
        )

    Failure semantics (ticket 20260810_tier_failclosed_lastknowngood,
    CLASS-1 Distributed State Drift, DSD-01/02)
    ----------------------------------------------------------------------
    A tier is written to the cache ONLY from a real, successful fetch. On a
    transient fetch failure (fetch_fn raises — e.g. TierFetchError, a network
    error, or a non-200 from billing) the provider:

      * NEVER overwrites the good cached tier and NEVER persists an invented
        `default_tier` — cache-poisoning a paying org to the cheapest tier is
        the exact bug this fixes;
      * serves the LAST-KNOWN-GOOD tier (the value from the most recent
        successful fetch) if one exists, surfacing its age in the logs;
      * only when NO last-known-good exists (a genuinely new / never-fetched
        org) falls back to `default_tier`, and does NOT cache that fallback so
        the very next request retries the source.

    Only an EXPLICIT, confirmed change moves a paid org down — that arrives as
    a *successful* fetch returning the new (lower) tier, which updates both the
    fast cache and the last-known-good record. A billing blip can never
    downgrade a paying org's limits (fail in the safe direction for the
    customer / money-path).

    `fetch_fn` MUST raise on failure rather than return an invented tier — see
    TierFetchError. A legacy fetch_fn that swallows errors and returns a
    default still works (non-breaking) but forfeits the last-known-good
    protection, since a swallowed failure is indistinguishable from a real
    assignment.
    """

    #: Fast read cache — short TTL, hot path. Bare tier string.
    _CACHE_PREFIX = "quota:tier:"
    #: Last-known-good record — written only on a successful fetch. JSON
    #: {"tier": str, "ts": float}. Long-lived (see lkg_ttl) so it survives
    #: many fast-cache expiries and is available to serve during an outage.
    _LKG_PREFIX = "quota:tier:lkg:"

    def __init__(
        self,
        fetch_fn: Callable[[str], Awaitable[str]],
        redis: Optional[Redis] = None,
        cache_ttl: int = 300,
        default_tier: str = "free",
        lkg_ttl: Optional[int] = None,
    ):
        self._fetch_fn = fetch_fn
        self._redis = redis
        self._cache_ttl = cache_ttl
        self._default_tier = default_tier
        # None ⇒ persist the last-known-good with no expiry (strongest
        # never-downgrade guarantee — a real downgrade always arrives as a
        # successful fetch that overwrites it). Operators may bound staleness
        # with an explicit TTL via config `tier_provider.lkg_ttl_seconds`.
        self._lkg_ttl = lkg_ttl

    @staticmethod
    def _decode(value) -> str:
        return value.decode() if isinstance(value, (bytes, bytearray)) else value

    async def get_tier(self, org_id: str, **kwargs) -> str:
        cache_key = f"{self._CACHE_PREFIX}{org_id}"
        lkg_key = f"{self._LKG_PREFIX}{org_id}"

        if self._redis:
            cached = await self._redis.get(cache_key)
            if cached:
                return self._decode(cached)

        try:
            tier = await self._fetch_fn(org_id)
        except Exception as e:
            # Transient failure. Do NOT cache a fallback and do NOT overwrite
            # the good cached/last-known-good tier (DSD-01/02).
            lkg = await self._read_last_known_good(lkg_key)
            if lkg is not None:
                tier_val, age = lkg
                logger.warning(
                    "tier_fetch_failed_serving_last_known_good org=%s tier=%s "
                    "age_seconds=%.0f error=%s",
                    org_id, tier_val, age, e,
                )
                return tier_val
            # Genuinely unknown / never-fetched org — default is correct, but
            # do NOT cache it so the next request re-attempts the source.
            logger.warning(
                "tier_fetch_failed_no_last_known_good org=%s using default_tier=%s "
                "(uncached; will retry) error=%s",
                org_id, self._default_tier, e,
            )
            return self._default_tier

        # Success — this is the ONLY path that writes the cache and advances
        # the last-known-good record (an explicit downgrade lands here too).
        if self._redis:
            await self._redis.set(cache_key, tier, ex=self._cache_ttl)
            await self._write_last_known_good(lkg_key, tier)

        return tier

    async def _read_last_known_good(self, lkg_key: str) -> Optional[tuple[str, float]]:
        """Return (tier, age_seconds) from the last-known-good record, or None.

        Never raises — a failure to read the fallback must not turn a transient
        fetch blip into a crash on the request hot path."""
        if not self._redis:
            return None
        try:
            raw = await self._redis.get(lkg_key)
            if not raw:
                return None
            data = json.loads(self._decode(raw))
            tier = data.get("tier")
            if not tier:
                return None
            age = max(0.0, time.time() - float(data.get("ts", 0.0)))
            return tier, age
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("last_known_good_read_failed key=%s error=%s", lkg_key, e)
            return None

    async def _write_last_known_good(self, lkg_key: str, tier: str) -> None:
        """Persist the successfully-fetched tier as the last-known-good."""
        payload = json.dumps({"tier": tier, "ts": time.time()})
        try:
            if self._lkg_ttl and self._lkg_ttl > 0:
                await self._redis.set(lkg_key, payload, ex=self._lkg_ttl)
            else:
                await self._redis.set(lkg_key, payload)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("last_known_good_write_failed key=%s error=%s", lkg_key, e)

    async def invalidate(self, org_id: str) -> None:
        """Clear the fast tier cache for an org so the next read re-fetches.

        Intentionally leaves the last-known-good record intact: if that
        re-fetch fails we still serve the last good tier rather than
        downgrading. An explicit tier change is picked up by the successful
        re-fetch, which overwrites both records."""
        if self._redis:
            await self._redis.delete(f"{self._CACHE_PREFIX}{org_id}")


class StaticTierProvider(TierProvider):
    """Hardcoded tier mapping — for tests and local dev."""

    def __init__(self, mapping: Optional[dict[str, str]] = None, default_tier: str = "free"):
        self._mapping = mapping or {}
        self._default_tier = default_tier

    async def get_tier(self, org_id: str, **kwargs) -> str:
        return self._mapping.get(org_id, self._default_tier)
