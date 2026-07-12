"""Generic heartbeat monitor for resource health tracking.

.. deprecated::
    **DEPRECATED / SUPERSEDED (D-67, ticket 20260709 — QB-04).** Use the
    ``observed_usage_provider`` seam + the library reconciler (D-33) instead.

    Heartbeats are a consumer-PUSHED liveness model: the consumer must call
    ``record()`` on every tick, and a *crashed* process cannot push its own "I'm
    dead" heartbeat. The ``observed_usage_provider`` is a provider-PULLED model —
    the library asks the consumer's product store "what is actually live?" — which
    reads the world regardless of any process crashing, and is the authoritative
    existence signal under D-33. ``engine.stale_open_activations`` (surfaced from
    the reconciler pass) is the "open too long" alarm. Together they subsume this
    monitor's job with a stronger guarantee.

    This monitor is now **disabled by default** (``heartbeat.enabled`` in the
    config; default ``false``) so no deployment carries an unfed, dormant loop that
    creates false confidence. If a consumer explicitly enables it, it becomes a
    REQUIRED, visible loop (D-66) and a *configured-but-unfed* monitor degrades
    ``/quota/health`` — "you turned it on and never fed it" is loud, not silent.
    It is NOT removed (D-65: public API on a shipped library; removal needs owner
    sign-off).

Usage (deprecated — prefer observed_usage_provider):
    monitor = HeartbeatMonitor(redis=redis_client, emitter=lifecycle_emitter)
    asyncio.create_task(monitor.start())
    await monitor.record("resource_123", {...})
"""

from __future__ import annotations

import asyncio
import logging
import warnings
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

logger = logging.getLogger("ab0t_quota.billing.heartbeat")


class HeartbeatMonitor:
    """Monitors resource heartbeats and triggers stop events for stale resources.

    Args:
        redis: async Redis client
        emitter: LifecycleEmitter instance for emitting synthetic stop events
        stale_threshold_seconds: How long without heartbeat = stale (default 900 = 15 min)
        check_interval_seconds: How often to scan for stale (default 60)
        key_prefix: Redis key prefix (default "heartbeat:")
        key_ttl_seconds: Redis key TTL for auto-cleanup (default 1800 = 30 min)
    """

    def __init__(
        self,
        redis,
        emitter,
        stale_threshold_seconds: int = 900,
        check_interval_seconds: int = 60,
        key_prefix: str = "heartbeat:",
        key_ttl_seconds: int = 1800,
    ):
        warnings.warn(
            "HeartbeatMonitor is DEPRECATED and superseded by the "
            "observed_usage_provider seam + the library reconciler (D-33): "
            "provider-PULLED existence truth is more reliable than consumer-PUSHED "
            "heartbeats (a crashed process cannot heartbeat). Use "
            "setup_quota(observed_usage_provider=...) plus stale_open_activations "
            "for the 'open too long' alarm. Disabled by default (heartbeat.enabled); "
            "removal pending owner sign-off (D-65).",
            DeprecationWarning, stacklevel=2,
        )
        self.redis = redis
        self.emitter = emitter
        self.stale_threshold = stale_threshold_seconds
        self.check_interval = check_interval_seconds
        self.prefix = key_prefix
        self.ttl = key_ttl_seconds
        self._running = False
        # D-67 refuse-gate: track whether a heartbeat was EVER recorded. A monitor
        # that a consumer opted into but never feeds is a misconfiguration (crashes
        # go undetected → usage unbilled), surfaced by monitor_liveness().
        self._ever_fed = False

    async def start(self):
        """Start the monitor loop. Run as asyncio.create_task()."""
        self._running = True
        logger.info("heartbeat_monitor_started")
        while self._running:
            try:
                await asyncio.sleep(self.check_interval)
                await self._scan()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("heartbeat_monitor_error: %s", e)
                await asyncio.sleep(5)

    def stop(self):
        self._running = False

    def monitor_liveness(self) -> tuple:
        """D-67 refuse-gate. A monitor a consumer OPTED INTO but never fed is a
        misconfiguration: crashes go undetected → runtime never settled. Returns
        (healthy, detail). Healthy once a heartbeat has been recorded; unhealthy
        while it has never been fed (the loud 'configured but unfed' state). The
        default path never constructs the monitor, so this never fires there."""
        if self._ever_fed:
            return True, "on (receiving heartbeats)"
        return False, ("configured but UNFED — no heartbeats ever recorded "
                       "(deprecated; prefer observed_usage_provider, D-67)")

    async def record(self, resource_id: str, data: dict):
        """Record a heartbeat. Called by cost tracker or health check."""
        self._ever_fed = True
        key = f"{self.prefix}{resource_id}"
        mapping = {
            "resource_id": resource_id,
            "reservation_id": data.get("reservation_id", ""),
            "org_id": data.get("org_id", ""),
            "user_id": data.get("user_id", ""),
            "hourly_rate": str(data.get("hourly_rate", "0")),
            "allocation_fee": str(data.get("allocation_fee", "0")),
            "started_at": data.get("started_at", ""),
            "resource_type": data.get("resource_type", ""),
            "last_seen": datetime.now(timezone.utc).isoformat(),
        }
        await self.redis.hset(key, mapping=mapping)
        await self.redis.expire(key, self.ttl)

    async def _scan(self):
        now = datetime.now(timezone.utc)
        cursor = 0
        while True:
            cursor, keys = await self.redis.scan(cursor, match=f"{self.prefix}*", count=100)
            for key in keys:
                await self._check(key, now)
            if cursor == 0:
                break

    async def _check(self, key, now: datetime):
        try:
            data = await self.redis.hgetall(key)
            if not data:
                return

            last_seen_str = self._decode(data, "last_seen")
            if not last_seen_str:
                return

            last_seen = self._parse_dt(last_seen_str)
            if not last_seen:
                return

            age = (now - last_seen).total_seconds()
            if age <= self.stale_threshold:
                return

            resource_id = self._decode(data, "resource_id")
            logger.warning("stale_resource: resource_id=%s age=%ds", resource_id, int(age))

            # Emit synthetic stop event
            started_at_str = self._decode(data, "started_at")
            started_at = self._parse_dt(started_at_str) if started_at_str else None

            hr = self._decode(data, "hourly_rate")
            af = self._decode(data, "allocation_fee")

            await self.emitter.resource_stopped(
                org_id=self._decode(data, "org_id"),
                user_id=self._decode(data, "user_id"),
                resource_id=resource_id,
                resource_type=self._decode(data, "resource_type") or "unknown",
                reservation_id=self._decode(data, "reservation_id") or None,
                hourly_rate=Decimal(hr) if hr else Decimal("0"),
                allocation_fee=Decimal(af) if af else Decimal("0"),
                started_at=started_at,
                reason="heartbeat_timeout",
            )

            await self.redis.delete(key)

        except Exception as e:
            logger.debug("stale_check_error: key=%s error=%s", key, e)

    @staticmethod
    def _decode(data: dict, field: str) -> str:
        val = data.get(field.encode(), data.get(field, b""))
        return val.decode() if isinstance(val, bytes) else str(val) if val else ""

    @staticmethod
    def _parse_dt(value: str) -> Optional[datetime]:
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            return None
