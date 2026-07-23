"""Factory to create the right counter type from a ResourceDef."""

from redis.asyncio import Redis

from ..models.core import ResourceDef, CounterType
from .base import Counter
from .gauge import GaugeCounter
from .rate import RateCounter
from .accumulator import AccumulatorCounter


def create_counter(redis: Redis, org_id: str, resource_def: ResourceDef,
                   keyspace=None) -> Counter:
    """Create the appropriate counter implementation for a resource definition.
    ``keyspace`` (K-1) selects the key shape/dual state; None = v1 single."""
    key = resource_def.resource_key

    if resource_def.counter_type == CounterType.GAUGE:
        return GaugeCounter(redis, org_id, key, keyspace=keyspace)

    if resource_def.counter_type == CounterType.RATE:
        return RateCounter(redis, org_id, key, resource_def.window_seconds,
                           keyspace=keyspace)

    if resource_def.counter_type == CounterType.ACCUMULATOR:
        return AccumulatorCounter(redis, org_id, key, resource_def.reset_period,
                                  keyspace=keyspace)

    raise ValueError(f"Unknown counter type: {resource_def.counter_type}")
