"""Abstract base for all counter types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from redis.asyncio import Redis


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
