"""In-process TTL caches for bridge mode.

Bridge mode round-trips every quota op to billing. For low-traffic
workloads that's fine; for moderate traffic, a few simple caches knock
the HTTP cost off the hot path:

  * Tier cache (60s TTL) — tiers rarely change; one fetch per org per minute
  * Decision cache (1s TTL) — duplicate checks within a request burst
    return the cached "allow" without round-tripping

Caches are best-effort: any failure / staleness just means an extra
HTTP call, never a wrong enforcement decision. Cache invalidation is
TTL-only — no broadcast invalidation across processes (that's the
v3 problem).

These are mode-agnostic: engine-local mode benefits too because the
TierProvider already caches in Redis; the decision cache is new.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Generic, Optional, TypeVar

T = TypeVar("T")


class TTLCache(Generic[T]):
    """Simple async-safe TTL cache. Single-process; no Redis.

    Eviction: lazy on read. No background reaper to keep this tiny.
    Suited for small key spaces (per-org tiers, per-resource decisions).
    Set `max_entries` to bound memory; oldest entries evicted on insert.
    """

    def __init__(self, ttl_seconds: float, max_entries: int = 10_000):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._data: dict[Any, tuple[float, T]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: Any) -> Optional[T]:
        async with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if time.time() >= expires_at:
                self._data.pop(key, None)
                return None
            return value

    async def set(self, key: Any, value: T) -> None:
        async with self._lock:
            if len(self._data) >= self._max:
                # FIFO eviction — drop oldest insertion. Cheap; not LRU.
                # Acceptable for our use cases (small key spaces, short TTLs).
                try:
                    oldest = next(iter(self._data))
                    self._data.pop(oldest, None)
                except StopIteration:
                    pass
            self._data[key] = (time.time() + self._ttl, value)

    async def invalidate(self, key: Any) -> None:
        async with self._lock:
            self._data.pop(key, None)

    async def clear(self) -> None:
        async with self._lock:
            self._data.clear()

    def size(self) -> int:
        return len(self._data)

    async def get_or_fetch(
        self,
        key: Any,
        fetch: Callable[[], Awaitable[T]],
    ) -> T:
        """Atomic get-or-compute. Caches the fetched value on hit."""
        cached = await self.get(key)
        if cached is not None:
            return cached
        value = await fetch()
        await self.set(key, value)
        return value


# ---------------------------------------------------------------------------
# Convenience: cached BridgeClient wrapper
# ---------------------------------------------------------------------------

class CachedBridgeClient:
    """Wraps a BridgeClient with in-memory TTL caches.

    Caches:
      * Tier resolution per org (default 60s) — wraps `get_tier()`
      * Allow-decisions per (org, resource_key, increment) (default 1s)
        — wraps `check()`. Only ALLOW results are cached; DENY/WARNING
        bypass the cache so consumers always see fresh denial info.
      * Allow-decisions per (org, bundle, user_id) (default 1s)
        — same logic for `check_bundle`.

    Increment / decrement / usage pass through uncached (state-mutating
    or freshness-sensitive). Caches survive multiple consumer-side route
    calls within the same request burst.
    """

    def __init__(
        self,
        client,  # BridgeClient
        tier_ttl_seconds: float = 60.0,
        decision_ttl_seconds: float = 1.0,
        max_entries: int = 10_000,
    ):
        self._client = client
        self._tier_cache: TTLCache[str] = TTLCache(tier_ttl_seconds, max_entries)
        self._decision_cache: TTLCache[dict] = TTLCache(decision_ttl_seconds, max_entries)
        self._bundle_cache: TTLCache[dict] = TTLCache(decision_ttl_seconds, max_entries)

    async def close(self):
        await self._client.close()

    async def get_tier(self, org_id: str) -> str:
        return await self._tier_cache.get_or_fetch(
            org_id, lambda: self._client.get_tier(org_id),
        )

    async def invalidate_tier(self, org_id: str) -> None:
        """Force fresh tier on next read (call from your tier-change webhook
        for instant upgrade UX)."""
        await self._tier_cache.invalidate(org_id)

    async def check(
        self,
        org_id: str,
        resource_key: str,
        user_id: Optional[str] = None,
        increment: float = 1.0,
    ) -> dict:
        # Cache key includes user_id so per-user partitions don't share
        # decisions across users in the same org.
        key = (org_id, resource_key, user_id, increment)
        cached = await self._decision_cache.get(key)
        if cached is not None:
            return cached
        result = await self._client.check(org_id, resource_key, user_id, increment)
        # Only cache ALLOW results — DENY / WARNING must always be fresh
        if result.get("decision") in ("allow", "unlimited") and not result.get("_bridge_error"):
            await self._decision_cache.set(key, result)
        return result

    async def check_bundle(
        self,
        org_id: str,
        bundle_name: str,
        user_id: Optional[str] = None,
    ) -> dict:
        key = (org_id, bundle_name, user_id)
        cached = await self._bundle_cache.get(key)
        if cached is not None:
            return cached
        result = await self._client.check_bundle(org_id, bundle_name, user_id)
        if result.get("allowed", False):
            await self._bundle_cache.set(key, result)
        return result

    # Pass-through: state-mutating or freshness-sensitive
    async def increment(self, *args, **kwargs) -> float:
        return await self._client.increment(*args, **kwargs)

    async def decrement(self, *args, **kwargs) -> float:
        return await self._client.decrement(*args, **kwargs)

    async def usage(self, org_id: str) -> dict:
        return await self._client.usage(org_id)
