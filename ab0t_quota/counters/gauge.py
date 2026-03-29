"""Gauge counter — tracks current level (e.g. concurrent sandboxes)."""

from __future__ import annotations

from typing import Optional
from .base import Counter


class GaugeCounter(Counter):
    """Bidirectional counter: increment on create, decrement on destroy.

    Redis key: quota:{org_id}:{resource_key}:gauge
    Per-user:  quota:{org_id}:{resource_key}:gauge:user:{user_id}
    Type: string (INCRBYFLOAT)
    TTL: none (persists until explicitly reset)
    """

    @property
    def _redis_key(self) -> str:
        return f"{self._key_prefix}:gauge"

    def _user_key(self, user_id: str) -> str:
        return f"{self._key_prefix}:gauge:user:{user_id}"

    async def get_user(self, user_id: str) -> float:
        """Get a specific user's usage within this org gauge."""
        val = await self._redis.get(self._user_key(user_id))
        return float(val) if val else 0.0

    async def increment_user(self, user_id: str, delta: float, idempotency_key: Optional[str] = None) -> float:
        """Increment both the org-level gauge AND the user partition."""
        idem_key = f"{user_id}:{idempotency_key}" if idempotency_key else None
        if idem_key and await self._check_idempotency(idem_key):
            return await self.get_user(user_id)
        pipe = self._redis.pipeline()
        pipe.incrbyfloat(self._redis_key, delta)            # org total
        pipe.incrbyfloat(self._user_key(user_id), delta)    # user partition
        results = await pipe.execute()
        if idem_key:
            await self._set_idempotency(idem_key)
        return float(results[1])  # return user value

    async def decrement_user(self, user_id: str, delta: float, idempotency_key: Optional[str] = None) -> float:
        """Decrement both the org-level gauge AND the user partition."""
        idem_key = f"{user_id}:{idempotency_key}" if idempotency_key else None
        if idem_key and await self._check_idempotency(idem_key):
            return await self.get_user(user_id)
        pipe = self._redis.pipeline()
        pipe.incrbyfloat(self._redis_key, -abs(delta))
        pipe.incrbyfloat(self._user_key(user_id), -abs(delta))
        results = await pipe.execute()
        # Floor at zero
        if float(results[0]) < 0:
            await self._redis.set(self._redis_key, 0)
        if float(results[1]) < 0:
            await self._redis.set(self._user_key(user_id), 0)
        if idem_key:
            await self._set_idempotency(idem_key)
        return max(0, float(results[1]))

    async def get(self) -> float:
        val = await self._redis.get(self._redis_key)
        return float(val) if val else 0.0

    async def increment(self, delta: float, idempotency_key: Optional[str] = None) -> float:
        if idempotency_key and await self._check_idempotency(idempotency_key):
            return await self.get()
        new_val = await self._redis.incrbyfloat(self._redis_key, delta)
        if idempotency_key:
            await self._set_idempotency(idempotency_key)
        return float(new_val)

    async def decrement(self, delta: float, idempotency_key: Optional[str] = None) -> float:
        if idempotency_key and await self._check_idempotency(idempotency_key):
            return await self.get()
        new_val = await self._redis.incrbyfloat(self._redis_key, -abs(delta))
        # Floor at zero — don't go negative from drift
        if float(new_val) < 0:
            await self._redis.set(self._redis_key, 0)
            new_val = 0
        if idempotency_key:
            await self._set_idempotency(idempotency_key)
        return float(new_val)

    async def reset(self, value: float = 0.0) -> None:
        await self._redis.set(self._redis_key, value)

    async def _check_idempotency(self, key: str) -> bool:
        return bool(await self._redis.exists(f"{self._key_prefix}:idem:{key}"))

    async def _set_idempotency(self, key: str) -> None:
        await self._redis.set(f"{self._key_prefix}:idem:{key}", "1", ex=86400)
