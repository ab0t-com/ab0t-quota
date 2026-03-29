"""Rate counter — sliding window (e.g. API requests per hour)."""

from __future__ import annotations

import time
from typing import Optional
from .base import Counter


class RateCounter(Counter):
    """Sliding window rate limiter using Redis sorted sets.

    Redis key: quota:{org_id}:{resource_key}:rate
    Type: sorted set (ZADD with timestamp scores)
    TTL: auto-pruned on each read/write (entries older than window)
    """

    def __init__(self, redis, org_id: str, resource_key: str, window_seconds: int):
        super().__init__(redis, org_id, resource_key)
        self._window = window_seconds

    @property
    def _redis_key(self) -> str:
        return f"{self._key_prefix}:rate"

    async def get(self) -> float:
        now = time.time()
        cutoff = now - self._window
        await self._redis.zremrangebyscore(self._redis_key, "-inf", cutoff)
        count = await self._redis.zcard(self._redis_key)
        return float(count)

    async def increment(self, delta: float, idempotency_key: Optional[str] = None) -> float:
        now = time.time()
        cutoff = now - self._window

        pipe = self._redis.pipeline()
        pipe.zremrangebyscore(self._redis_key, "-inf", cutoff)
        # Add `delta` entries (usually 1) with current timestamp
        member = idempotency_key or f"{now}:{id(self)}"
        for i in range(int(delta)):
            pipe.zadd(self._redis_key, {f"{member}:{i}": now})
        pipe.expire(self._redis_key, self._window + 60)  # TTL safety margin
        pipe.zcard(self._redis_key)
        results = await pipe.execute()
        return float(results[-1])

    async def decrement(self, delta: float, idempotency_key: Optional[str] = None) -> float:
        raise TypeError("Rate counters cannot be decremented — events expire automatically")

    async def reset(self, value: float = 0.0) -> None:
        await self._redis.delete(self._redis_key)

    async def seconds_until_slot(self) -> Optional[int]:
        """Seconds until the oldest entry expires (for retry_after header)."""
        oldest = await self._redis.zrange(self._redis_key, 0, 0, withscores=True)
        if not oldest:
            return None
        oldest_time = oldest[0][1]
        expires_at = oldest_time + self._window
        remaining = expires_at - time.time()
        return max(1, int(remaining))
