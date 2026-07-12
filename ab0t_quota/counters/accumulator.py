"""Accumulator counter — monotonic within a reset period (e.g. monthly spend)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from ..models.core import ResetPeriod
from .base import Counter, finite_magnitude

_IDEM_TTL = 86400

# Atomic claim + INCRBYFLOAT + EXPIRE (QI-01 for the accumulator).
#   KEYS[1]=idem, KEYS[2]=acc
#   ARGV[1]=delta, ARGV[2]=idem_ttl, ARGV[3]=has_idem, ARGV[4]=period_ttl (0=none)
_ACC_INCR = """
if ARGV[3] == '1' then
  if not redis.call('SET', KEYS[1], '1', 'NX', 'EX', ARGV[2]) then
    local c = redis.call('GET', KEYS[2]); if c then return c else return '0' end
  end
end
local v = redis.call('INCRBYFLOAT', KEYS[2], ARGV[1])
if tonumber(ARGV[4]) > 0 then redis.call('EXPIRE', KEYS[2], ARGV[4]) end
return v
"""


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
        now = now or datetime.now(timezone.utc)
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
        """Claim the idempotency key, add to the period total, and (re)set the
        period expiry — all in one atomic Lua script (QI-01). A crash can no
        longer claim the key without applying the increment.

        W-T3/ET-02 (D-31): the delta is a MAGNITUDE (|delta|), validated
        finite BEFORE the Lua. Pre-fix, increment(-4) silently ERASED 4 units
        of period spend — on the counter class whose own decrement() raises
        "cannot be decremented". A sign flip must never invert the op."""
        delta = finite_magnitude(delta)
        has_idem = "1" if idempotency_key else "0"
        idem_key = (
            f"{self._key_prefix}:idem:{idempotency_key}" if idempotency_key
            else f"{self._key_prefix}:idem:__unused__"
        )
        result = await self._redis.eval(
            _ACC_INCR, 2,
            idem_key, self._redis_key,
            delta, _IDEM_TTL, has_idem, self._period_ttl_seconds(),
        )
        return float(result)

    async def decrement(self, delta: float, idempotency_key: Optional[str] = None) -> float:
        raise TypeError("Accumulator counters cannot be decremented — they reset on period boundary")

    async def reset(self, value: float = 0.0) -> None:
        await self._redis.set(self._redis_key, value)
        ttl = self._period_ttl_seconds()
        if ttl > 0:
            await self._redis.expire(self._redis_key, ttl)

    async def _claim_idempotency(self, key: str) -> bool:
        """Atomically claim an idempotency key. Returns True if this is the first attempt."""
        result = await self._redis.set(
            f"{self._key_prefix}:idem:{key}", "1", ex=86400, nx=True,
        )
        return result is not None
