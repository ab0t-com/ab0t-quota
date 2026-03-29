"""Tier providers — resolve an org_id to its tier.

Services pick the provider that fits their architecture:
- JWTTierProvider: reads tier from JWT claims (zero network calls)
- AuthServiceTierProvider: calls auth service API (cached in Redis)
- StaticTierProvider: hardcoded mapping (for tests)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional, Callable, Awaitable

from redis.asyncio import Redis


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
    """Call auth service to get org tier, cached in Redis.

    Usage:
        provider = AuthServiceTierProvider(
            fetch_fn=my_auth_client.get_org_tier,
            redis=redis,
            cache_ttl=300,
        )
    """

    def __init__(
        self,
        fetch_fn: Callable[[str], Awaitable[str]],
        redis: Optional[Redis] = None,
        cache_ttl: int = 300,
        default_tier: str = "free",
    ):
        self._fetch_fn = fetch_fn
        self._redis = redis
        self._cache_ttl = cache_ttl
        self._default_tier = default_tier

    async def get_tier(self, org_id: str, **kwargs) -> str:
        cache_key = f"quota:tier:{org_id}"

        if self._redis:
            cached = await self._redis.get(cache_key)
            if cached:
                return cached.decode() if isinstance(cached, bytes) else cached

        try:
            tier = await self._fetch_fn(org_id)
        except Exception:
            tier = self._default_tier

        if self._redis:
            await self._redis.set(cache_key, tier, ex=self._cache_ttl)

        return tier

    async def invalidate(self, org_id: str) -> None:
        """Clear cached tier for an org. Call after tier change for instant effect."""
        if self._redis:
            await self._redis.delete(f"quota:tier:{org_id}")


class StaticTierProvider(TierProvider):
    """Hardcoded tier mapping — for tests and local dev."""

    def __init__(self, mapping: Optional[dict[str, str]] = None, default_tier: str = "free"):
        self._mapping = mapping or {}
        self._default_tier = default_tier

    async def get_tier(self, org_id: str, **kwargs) -> str:
        return self._mapping.get(org_id, self._default_tier)
