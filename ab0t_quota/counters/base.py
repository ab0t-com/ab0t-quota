"""Abstract base for all counter types."""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import Optional

from redis.asyncio import Redis


def finite_magnitude(delta: float) -> float:
    """Validate + normalise a counter delta at the LIBRARY BOUNDARY, before
    any Lua runs (ticket 20260709, W-T3 / defect ET-03).

    * Non-finite (NaN/±inf) raises ValueError. If it reached the Lua instead,
      the script would claim the idempotency key (SET NX / HSETNX) and THEN
      error on INCRBYFLOAT — Redis scripts do not roll back, so the claim
      persists and the caller's corrected retry is swallowed as a duplicate:
      the spend never lands (under-count / phantom headroom, the forbidden
      D-31 direction). Validating here keeps every claim paired with its
      mutation.
    * The magnitude (abs) is returned — counter deltas are magnitudes by
      library convention (increment adds |delta|, decrement subtracts
      |delta|), so a sign flip can never invert an operation and silently
      erase spend (D-31).
    """
    d = float(delta)
    if not math.isfinite(d):
        raise ValueError(
            f"counter delta must be finite, got {d!r} — refusing before Lua "
            "so the idempotency claim is not burned (D-31)"
        )
    return abs(d)


class Counter(ABC):
    """Base class for quota counters backed by Redis."""

    def __init__(self, redis: Redis, org_id: str, resource_key: str):
        self._redis = redis
        self._org_id = org_id
        self._resource_key = resource_key

    @property
    def _key_prefix(self) -> str:
        return f"quota:{self._org_id}:{self._resource_key}"

    @abstractmethod
    async def get(self) -> float:
        """Read current counter value."""

    @abstractmethod
    async def increment(self, delta: float, idempotency_key: Optional[str] = None) -> float:
        """Add to counter. Returns new value."""

    @abstractmethod
    async def decrement(self, delta: float, idempotency_key: Optional[str] = None) -> float:
        """Subtract from counter. Returns new value. May raise for non-gauge types."""

    @abstractmethod
    async def reset(self, value: float = 0.0) -> None:
        """Force-set counter to a specific value (admin operation)."""
