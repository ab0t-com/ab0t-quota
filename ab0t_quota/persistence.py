"""
Persistence layer — DynamoDB backup for Redis counters and org tier/overrides.

Redis is the hot path (all reads/writes go through Redis counters).
DynamoDB is the durable store (periodic sync + recovery on Redis restart).

Data stored in DynamoDB:
  - Org tier assignments (PK=ORG#{org_id}, SK=TIER)
  - Per-org overrides (PK=ORG#{org_id}, SK=OVERRIDE#{resource_key})
  - Counter snapshots (PK=ORG#{org_id}, SK=COUNTER#{resource_key})
  - Increase requests (PK=ORG#{org_id}, SK=INCREASE#{request_id})

This is NOT in the critical path. Quota checks hit Redis only.
DynamoDB is read on startup (to seed Redis) and written to periodically.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from .models.core import QuotaOverride, TierLimits

logger = logging.getLogger("ab0t_quota.persistence")


class QuotaStore:
    """DynamoDB persistence for quota state.

    Call seed_redis() on startup to recover counters from DynamoDB.
    Call sync_to_dynamo() periodically to persist Redis state.
    """

    # Hosts allowed for endpoint_url (local dev / DynamoDB Local only)
    _ALLOWED_ENDPOINT_HOSTS = frozenset({
        "localhost", "127.0.0.1", "dynamodb-local", "dynamodb", "localstack",
    })

    def __init__(self, table_name: str = "ab0t_quota_state", region: str = "us-east-1", endpoint_url: Optional[str] = None):
        if endpoint_url:
            self._validate_endpoint_url(endpoint_url)
        self._table_name = table_name
        self._region = region
        self._endpoint_url = endpoint_url
        self._table = None

    @classmethod
    def _validate_endpoint_url(cls, url: str) -> None:
        """Restrict endpoint_url to localhost/known dev hosts only (SSRF protection)."""
        from urllib.parse import urlparse
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        if hostname not in cls._ALLOWED_ENDPOINT_HOSTS:
            raise ValueError(
                f"endpoint_url host '{hostname}' not in allowlist. "
                f"Allowed: {sorted(cls._ALLOWED_ENDPOINT_HOSTS)}. "
                f"Use None for production (uses default AWS endpoint)."
            )

    async def initialize(self, session=None):
        """Initialize DynamoDB table (create if not exists)."""
        import aioboto3

        self._session = session or aioboto3.Session()
        kwargs = {"region_name": self._region}
        if self._endpoint_url:
            kwargs["endpoint_url"] = self._endpoint_url

        async with self._session.client("dynamodb", **kwargs) as client:
            try:
                await client.describe_table(TableName=self._table_name)
                logger.info("Quota state table %s exists", self._table_name)
            except client.exceptions.ResourceNotFoundException:
                await client.create_table(
                    TableName=self._table_name,
                    KeySchema=[
                        {"AttributeName": "PK", "KeyType": "HASH"},
                        {"AttributeName": "SK", "KeyType": "RANGE"},
                    ],
                    AttributeDefinitions=[
                        {"AttributeName": "PK", "AttributeType": "S"},
                        {"AttributeName": "SK", "AttributeType": "S"},
                        {"AttributeName": "GSI1PK", "AttributeType": "S"},
                        {"AttributeName": "GSI1SK", "AttributeType": "S"},
                    ],
                    GlobalSecondaryIndexes=[
                        {
                            "IndexName": "GSI1",
                            "KeySchema": [
                                {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                                {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                            ],
                            "Projection": {"ProjectionType": "ALL"},
                        },
                    ],
                    BillingMode="PAY_PER_REQUEST",
                    Tags=[
                        {"Key": "Service", "Value": "ab0t-quota"},
                        {"Key": "ManagedBy", "Value": "ab0t-quota-library"},
                    ],
                )
                waiter = client.get_waiter("table_exists")
                await waiter.wait(TableName=self._table_name)
                logger.info("Created quota state table %s", self._table_name)

        # Open persistent resource for reads/writes
        self._resource_ctx = self._session.resource("dynamodb", **kwargs)
        dynamodb = await self._resource_ctx.__aenter__()
        self._table = await dynamodb.Table(self._table_name)

    # ------------------------------------------------------------------
    # Org Tier
    # ------------------------------------------------------------------

    async def get_org_tier(self, org_id: str) -> Optional[str]:
        """Read org tier from DynamoDB."""
        resp = await self._table.get_item(Key={"PK": f"ORG#{org_id}", "SK": "TIER"})
        item = resp.get("Item")
        return item["tier_id"] if item else None

    async def set_org_tier(self, org_id: str, tier_id: str, changed_by: Optional[str] = None) -> None:
        """Persist org tier to DynamoDB."""
        await self._table.put_item(Item={
            "PK": f"ORG#{org_id}",
            "SK": "TIER",
            "GSI1PK": "TIER",
            "GSI1SK": f"ORG#{org_id}",
            "tier_id": tier_id,
            "changed_by": changed_by or "system",
            "changed_at": datetime.now(timezone.utc).isoformat(),
        })

    # ------------------------------------------------------------------
    # Overrides
    # ------------------------------------------------------------------

    async def get_override(self, org_id: str, resource_key: str) -> Optional[QuotaOverride]:
        """Read per-org override from DynamoDB."""
        resp = await self._table.get_item(Key={
            "PK": f"ORG#{org_id}",
            "SK": f"OVERRIDE#{resource_key}",
        })
        item = resp.get("Item")
        if not item:
            return None
        return QuotaOverride(
            org_id=org_id,
            resource_key=resource_key,
            limit=float(item["limit"]) if item.get("limit") is not None else None,
            reason=item.get("reason"),
            expires_at=datetime.fromisoformat(item["expires_at"]) if item.get("expires_at") else None,
            created_by=item.get("created_by"),
            created_at=datetime.fromisoformat(item["created_at"]) if item.get("created_at") else datetime.now(timezone.utc),
        )

    async def set_override(self, override: QuotaOverride) -> None:
        """Persist per-org override to DynamoDB."""
        item = {
            "PK": f"ORG#{override.org_id}",
            "SK": f"OVERRIDE#{override.resource_key}",
            "GSI1PK": "OVERRIDE",
            "GSI1SK": f"ORG#{override.org_id}#{override.resource_key}",
            "limit": str(override.limit) if override.limit is not None else None,
            "reason": override.reason,
            "created_by": override.created_by,
            "created_at": override.created_at.isoformat(),
        }
        if override.expires_at:
            item["expires_at"] = override.expires_at.isoformat()
        await self._table.put_item(Item=item)

    async def delete_override(self, org_id: str, resource_key: str) -> None:
        """Remove per-org override."""
        await self._table.delete_item(Key={
            "PK": f"ORG#{org_id}",
            "SK": f"OVERRIDE#{resource_key}",
        })

    # ------------------------------------------------------------------
    # Counter Snapshots (for recovery)
    # ------------------------------------------------------------------

    async def snapshot_counter(self, org_id: str, resource_key: str, value: float) -> None:
        """Save a counter snapshot to DynamoDB (called periodically by sync worker)."""
        await self._table.put_item(Item={
            "PK": f"ORG#{org_id}",
            "SK": f"COUNTER#{resource_key}",
            "GSI1PK": "COUNTER",
            "GSI1SK": f"ORG#{org_id}#{resource_key}",
            "value": str(value),
            "snapshotted_at": datetime.now(timezone.utc).isoformat(),
        })

    async def get_counter_snapshot(self, org_id: str, resource_key: str) -> Optional[float]:
        """Read last counter snapshot."""
        resp = await self._table.get_item(Key={
            "PK": f"ORG#{org_id}",
            "SK": f"COUNTER#{resource_key}",
        })
        item = resp.get("Item")
        return float(item["value"]) if item else None

    # ------------------------------------------------------------------
    # Seed Redis from DynamoDB (startup recovery)
    # ------------------------------------------------------------------

    async def seed_redis(self, redis, registry) -> int:
        """On startup, restore Redis counters from DynamoDB snapshots.

        Uses GSI1 (GSI1PK=COUNTER) to query all counter snapshots without
        scanning the entire table.

        Returns number of counters restored.
        """
        from .counters.factory import create_counter

        restored = 0
        query_kwargs = {
            "IndexName": "GSI1",
            "KeyConditionExpression": "GSI1PK = :pk",
            "ExpressionAttributeValues": {":pk": "COUNTER"},
        }

        while True:
            response = await self._table.query(**query_kwargs)
            for item in response.get("Items", []):
                org_id = item["PK"].replace("ORG#", "")
                resource_key = item["SK"].replace("COUNTER#", "")
                value = float(item["value"])

                resource_def = registry.get(resource_key)
                if resource_def:
                    counter = create_counter(redis, org_id, resource_def)
                    current = await counter.get()
                    if current == 0 and value > 0:
                        await counter.reset(value)
                        restored += 1
                        logger.info("Restored counter %s for org %s: %s", resource_key, org_id, value)

            # Handle pagination
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            query_kwargs["ExclusiveStartKey"] = last_key

        logger.info("Seeded %d counters from DynamoDB", restored)
        return restored

    async def close(self):
        """Clean up DynamoDB resource."""
        if self._resource_ctx:
            await self._resource_ctx.__aexit__(None, None, None)
