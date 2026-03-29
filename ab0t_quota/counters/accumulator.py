"""Accumulator counter — monotonic within a reset period (e.g. monthly spend)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from ..models.core import ResetPeriod
from .base import Counter


class AccumulatorCounter(Counter):
    """Calendar-aligned accumulator that resets on period boundaries.

    Redis key: quota:{org_id}:{resource_key}:acc:{period_key}
    Type: string (INCRBYFLOAT)
    TTL: set to expire at end of period + buffer

    Example period_key for MONTHLY: "2026-03"
    """

    def __init__(self, redis, org_id: str, resource_key: str, reset_period: ResetPeriod):
        super().__init__(redis, org_id, resource_key)
        self._reset_period = reset_period

    def _period_key(self, now: Optional[datetime] = None) -> str:
        now = now or datetime.utcnow()
        if self._reset_period == ResetPeriod.HOURLY:
            return now.strftime("%Y-%m-%dT%H")
        if self._reset_period == ResetPeriod.DAILY:
            return now.strftime("%Y-%m-%d")
        if self._reset_period == ResetPeriod.WEEKLY:
            # ISO week
            return f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"
        if self._reset_period == ResetPeriod.MONTHLY:
            return now.strftime("%Y-%m")
        return "all"  # NEVER

    def _period_ttl_seconds(self) -> int:
        """TTL for the Redis key — period length + 1 day buffer for dashboards."""
        buffer = 86400
        if self._reset_period == ResetPeriod.HOURLY:
            return 3600 + buffer
        if self._reset_period == ResetPeriod.DAILY:
            return 86400 + buffer
        if self._reset_period == ResetPeriod.WEEKLY:
            return 604800 + buffer
        if self._reset_period == ResetPeriod.MONTHLY:
            return 2678400 + buffer  # 31 days
        return 0  # NEVER — no expiry

    @property
    def _redis_key(self) -> str:
        return f"{self._key_prefix}:acc:{self._period_key()}"

    async def get(self) -> float:
        val = await self._redis.get(self._redis_key)
        return float(val) if val else 0.0

    async def increment(self, delta: float, idempotency_key: Optional[str] = None) -> float:
        if idempotency_key and await self._check_idempotency(idempotency_key):
            return await self.get()
        new_val = await self._redis.incrbyfloat(self._redis_key, delta)
        ttl = self._period_ttl_seconds()
        if ttl > 0:
            await self._redis.expire(self._redis_key, ttl)
        if idempotency_key:
            await self._set_idempotency(idempotency_key)
        return float(new_val)

    async def decrement(self, delta: float, idempotency_key: Optional[str] = None) -> float:
        raise TypeError("Accumulator counters cannot be decremented — they reset on period boundary")

    async def reset(self, value: float = 0.0) -> None:
        await self._redis.set(self._redis_key, value)
        ttl = self._period_ttl_seconds()
        if ttl > 0:
            await self._redis.expire(self._redis_key, ttl)

    async def _check_idempotency(self, key: str) -> bool:
        return bool(await self._redis.exists(f"{self._key_prefix}:idem:{key}"))

    async def _set_idempotency(self, key: str) -> None:
        await self._redis.set(f"{self._key_prefix}:idem:{key}", "1", ex=86400)
