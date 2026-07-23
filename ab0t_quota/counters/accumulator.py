"""Accumulator counter — monotonic within a reset period (e.g. monthly spend)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from ..models.core import ResetPeriod
from .base import Counter, finite_magnitude, dual_lua

_IDEM_TTL = 86400

# Atomic claim + INCRBYFLOAT + EXPIRE (QI-01 for the accumulator).
#   KEYS[1]=idem, KEYS[2]=acc
#   ARGV[1]=delta [2]=idem_ttl [3]=has_idem [4]=period_ttl (0=none) [5]=dual [6]=v2p
# K-3: dual seeds the v2 period bucket from v1, dual-claims the latch, and
# maintains both shapes (period suffix makes seeding period-scoped, spec §6.1).
_ACC_INCR = dual_lua("2", 5, """
seedv2(2)
if ARGV[3] == '1' then
  if idem_dup(1) then
    local c = redis.call('GET', KEYS[2]); if c then return c else return '0' end
  end
  idem_claim(1)
end
local v = incrboth(2, ARGV[1])
if tonumber(ARGV[4]) > 0 then expboth(2, ARGV[4]) end
return v
""")


class AccumulatorCounter(Counter):
    """Calendar-aligned accumulator that resets on period boundaries.

    Redis key: quota:{org_id}:{resource_key}:acc:{period_key}
    Type: string (INCRBYFLOAT)
    TTL: set to expire at end of period + buffer

    Example period_key for MONTHLY: "2026-03"
    """

    def __init__(self, redis, org_id: str, resource_key: str, reset_period: ResetPeriod,
                 keyspace=None):
        super().__init__(redis, org_id, resource_key, keyspace=keyspace)
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
        return self._ks.acc_key(self._org_id, self._resource_key, self._period_key())

    def _redis_key2(self):
        """Secondary-shape period key during dual-write, else None."""
        if not self._sv:
            return None
        return self._ks.acc_key(self._org_id, self._resource_key,
                                self._period_key(), version=self._sv)

    async def get(self) -> float:
        val = await self._redis.get(self._redis_key)
        if val is None and self._sv:
            val = await self._redis.get(self._redis_key2())
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
        keys = [self._ks.idem_key(self._org_id, self._resource_key, idempotency_key),
                self._redis_key]
        if self._sv:
            keys += [self._ks.idem_key(self._org_id, self._resource_key,
                                       idempotency_key, version=self._sv),
                     self._redis_key2()]
        result = await self._redis.eval(
            _ACC_INCR, len(keys), *keys,
            delta, _IDEM_TTL, has_idem, self._period_ttl_seconds(),
            *self._dual_argv(),
        )
        return float(result)

    async def decrement(self, delta: float, idempotency_key: Optional[str] = None) -> float:
        raise TypeError("Accumulator counters cannot be decremented — they reset on period boundary")

    async def reset(self, value: float = 0.0) -> None:
        ttl = self._period_ttl_seconds()
        for key in filter(None, (self._redis_key, self._redis_key2() if self._sv else None)):
            await self._redis.set(key, value)
            if ttl > 0:
                await self._redis.expire(key, ttl)

    async def _claim_idempotency(self, key: str) -> bool:
        """Atomically claim an idempotency key. Returns True if this is the first attempt."""
        result = await self._redis.set(
            self._ks.idem_key(self._org_id, self._resource_key, key),
            "1", ex=86400, nx=True,
        )
        return result is not None
