"""Resource lifecycle event emitter for billing proration.

Any mesh service that uses ab0t-quota[billing] can emit lifecycle events
so the billing service handles proration automatically.

Usage:
    from ab0t_quota.billing.lifecycle import LifecycleEmitter

    emitter = LifecycleEmitter(
        sns_topic_arn="arn:aws:sns:us-east-1:...:resource-lifecycle",
        aws_endpoint_url="http://localhost:4566",  # LocalStack for dev
    )

    # On resource stop/delete:
    await emitter.resource_stopped(
        org_id="org_123",
        user_id="user_456",
        resource_id="browser_abc",
        resource_type="browser",
        reservation_id="reservation_xyz",
        hourly_rate=Decimal("0.10"),
        allocation_fee=Decimal("0.01"),
        started_at=container.created_at,
        reason="user_stopped",
    )

When constructed with `engine=...`, the emitter ALSO increments the
service's monthly-cost accumulator on resource.stopped/resource.deleted
events. This closes the silent monthly-cost-cap bypass: tier limits like
"$10/month on free" actually enforce, with no extra wiring from the
consumer service. See setup_quota() for the wired-up default.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..engine import QuotaEngine

logger = logging.getLogger("ab0t_quota.billing.lifecycle")


class LifecycleEmitter:
    """Emits resource lifecycle events to SNS for billing proration."""

    # Lifecycle events that should drive cost recording (terminal events
    # carry the final duration; heartbeats are intentionally excluded to
    # avoid double-counting against the stopped event's total cost).
    _COST_RECORDING_EVENTS = frozenset({"resource.stopped", "resource.deleted"})

    def __init__(
        self,
        sns_topic_arn: Optional[str] = None,
        aws_endpoint_url: Optional[str] = None,
        aws_region: Optional[str] = None,
        *,
        engine: Optional["QuotaEngine"] = None,
        cost_resource_key: Optional[str] = None,
    ):
        # Topic ARN: prefer the mesh-namespaced name (consumer-facing
        # convention from setup_quota); fall back to the legacy name for
        # backward compat with services on older configs.
        self._topic_arn = (
            sns_topic_arn
            or os.getenv("AB0T_MESH_SNS_LIFECYCLE_TOPIC_ARN")
            or os.getenv("SNS_LIFECYCLE_TOPIC_ARN")
        )
        self._endpoint = aws_endpoint_url or os.getenv("AWS_ENDPOINT_URL")
        self._client = None

        # Optional quota integration. When set, terminal lifecycle events
        # auto-increment the configured monthly-cost accumulator before
        # publishing SNS. Increment failures are logged + best-effort —
        # billing remains the authoritative source of truth.
        self._engine = engine
        self._cost_resource_key = cost_resource_key

        # Extract region from ARN or env
        if self._topic_arn and len(self._topic_arn.split(":")) > 3:
            self._region = self._topic_arn.split(":")[3]
        else:
            self._region = aws_region or os.getenv("AWS_REGION", "us-east-1")

    def _get_client(self):
        if self._client is None:
            if not self._topic_arn:
                return None
            import boto3
            kwargs = {"region_name": self._region}
            if self._endpoint:
                kwargs["endpoint_url"] = self._endpoint
            self._client = boto3.client("sns", **kwargs)
        return self._client

    async def emit(
        self,
        event_type: str,
        org_id: str,
        user_id: str,
        resource_id: str,
        resource_type: str,
        reservation_id: Optional[str] = None,
        instance_type: Optional[str] = None,
        hourly_rate: Optional[Decimal] = None,
        allocation_fee: Optional[Decimal] = None,
        started_at: Optional[datetime] = None,
        stopped_at: Optional[datetime] = None,
        reason: str = "user_action",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Emit a lifecycle event. Returns True if published, False if not configured.

        When the emitter was constructed with `engine=...`, terminal events
        (resource.stopped / resource.deleted) ALSO increment the configured
        monthly-cost accumulator before publishing SNS. Increment failure
        is logged but never blocks the SNS publish — billing is authoritative.
        """
        # Auto-record cost for terminal events when wired to a quota engine.
        # Runs first; failures are best-effort (billing service is the
        # source of truth for charges, this just keeps the quota cap honest).
        if (
            self._engine is not None
            and self._cost_resource_key
            and event_type in self._COST_RECORDING_EVENTS
        ):
            await self._record_cost(
                org_id=org_id,
                resource_id=resource_id,
                hourly_rate=hourly_rate,
                allocation_fee=allocation_fee,
                started_at=started_at,
                stopped_at=stopped_at,
            )

        client = self._get_client()
        if not client:
            return False

        event = {
            "event_type": event_type,
            "org_id": org_id,
            "user_id": user_id,
            "resource_id": resource_id,
            "resource_type": resource_type,
            "reservation_id": reservation_id,
            "instance_type": instance_type,
            "hourly_rate": str(hourly_rate) if hourly_rate else None,
            "allocation_fee": str(allocation_fee) if allocation_fee else "0",
            "started_at": started_at.isoformat() if started_at else None,
            "stopped_at": (stopped_at or datetime.now(timezone.utc)).isoformat(),
            "reason": reason,
            "metadata": metadata or {},
            "emitted_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            import asyncio
            await asyncio.to_thread(
                client.publish,
                TopicArn=self._topic_arn,
                Message=json.dumps(event, default=str),
                MessageAttributes={
                    "event_type": {"DataType": "String", "StringValue": event_type},
                    "resource_type": {"DataType": "String", "StringValue": resource_type},
                },
            )
            logger.debug("lifecycle_event_emitted: %s %s", event_type, resource_id)
            return True
        except Exception as e:
            logger.warning("lifecycle_event_failed: %s %s %s", event_type, resource_id, e)
            return False

    async def _record_cost(
        self,
        *,
        org_id: str,
        resource_id: str,
        hourly_rate: Optional[Decimal],
        allocation_fee: Optional[Decimal],
        started_at: Optional[datetime],
        stopped_at: Optional[datetime],
    ) -> None:
        """Increment the monthly-cost accumulator for this resource's lifetime.

        Idempotent on (resource_id) — replayed terminal events are no-ops.
        Heartbeat events are intentionally NOT recorded here; recording them
        would risk double-counting against this terminal total.
        """
        if hourly_rate is None and allocation_fee is None:
            return  # nothing to charge; pricing not configured for this resource
        if started_at is None:
            logger.debug("cost_record_skipped: no started_at for %s", resource_id)
            return

        end = stopped_at or datetime.now(timezone.utc)
        # Defend against tz-naive datetimes from older callers
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)

        seconds = max(0.0, (end - started_at).total_seconds())
        hours = Decimal(str(seconds / 3600.0))
        rate = hourly_rate or Decimal("0")
        alloc = allocation_fee or Decimal("0")
        cost = float(hours * rate + alloc)
        if cost <= 0:
            return  # zero-cost resources don't move the accumulator

        # Lazy imports to avoid circular dep with engine module
        from ..models.requests import QuotaIncrementRequest

        try:
            await self._engine.increment(QuotaIncrementRequest(
                org_id=org_id,
                resource_key=self._cost_resource_key,
                delta=cost,
                # Idempotency on the resource lifecycle: replay-safe even if
                # the same stop event is delivered twice (SNS at-least-once).
                idempotency_key=f"cost:lifecycle:{resource_id}",
            ))
            logger.debug(
                "cost_recorded org=%s resource=%s delta=%.4f key=%s",
                org_id, resource_id, cost, self._cost_resource_key,
            )
        except Exception as e:
            # Quota-side failure must never block the SNS publish that
            # reaches the authoritative billing pipeline.
            logger.warning(
                "cost_record_failed org=%s resource=%s error=%s",
                org_id, resource_id, str(e),
            )

    # Convenience methods
    async def resource_started(self, **kwargs) -> bool:
        return await self.emit(event_type="resource.started", **kwargs)

    async def resource_stopped(self, **kwargs) -> bool:
        return await self.emit(event_type="resource.stopped", **kwargs)

    async def resource_deleted(self, **kwargs) -> bool:
        return await self.emit(event_type="resource.deleted", **kwargs)

    async def resource_heartbeat(self, **kwargs) -> bool:
        return await self.emit(event_type="resource.heartbeat", **kwargs)
