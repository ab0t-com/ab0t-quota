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
        "host.docker.internal",
    })

    def __init__(self, table_name: str = "ab0t_quota_state", region: str = "us-east-1", endpoint_url: Optional[str] = None):
        if endpoint_url:
            self._validate_endpoint_url(endpoint_url)
        self._table_name = table_name
        self._region = region
        self._endpoint_url = endpoint_url
        self._table = None
        # Background sync worker state — set by start_sync_worker(), cleared by stop_sync_worker()
        self._sync_task = None
        # Per-(org,resource) cache of last-snapshot value to skip no-op writes
        self._last_snapshot: dict[tuple[str, str], float] = {}

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

    async def initialize(self, session=None, *, create: bool = True):
        """Initialize DynamoDB table (create if not exists). `create=False` is
        the T-6/D-3 call-site policy: an existing table is used as-is; a
        missing one is a refusal naming `storage.auto_create_tables`."""
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
                if not create:
                    raise RuntimeError(
                        f"quota state table {self._table_name} does not exist and "
                        f"storage.auto_create_tables is false (the default) — "
                        f"pre-create the table or set storage.auto_create_tables: "
                        f"true (ENV-04)")
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

    async def snapshot_counter(self, org_id: str, resource_key: str, value: float,
                               service: Optional[str] = None) -> None:
        """Save a counter snapshot to DynamoDB (called periodically by sync worker).
        K-4 (spec §7 row 8): a v2 counter row is service-scoped
        (SK=COUNTER#v2#{svc}#{rk}) so two bridge services' same resource_key
        cannot collide in one state table. ``service=None`` = v1 row, unchanged."""
        sk = (f"COUNTER#v2#{service}#{resource_key}" if service
              else f"COUNTER#{resource_key}")
        gsi1sk = (f"ORG#{org_id}#v2#{service}#{resource_key}" if service
                  else f"ORG#{org_id}#{resource_key}")
        await self._table.put_item(Item={
            "PK": f"ORG#{org_id}",
            "SK": sk,
            "GSI1PK": "COUNTER",
            "GSI1SK": gsi1sk,
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

    async def seed_redis(self, redis, registry, activation_store=None,
                         keyspace=None) -> int:
        """On startup, restore Redis counters from DynamoDB.

        Uses GSI1 (GSI1PK=COUNTER) to enumerate the (org, resource) pairs to
        restore without scanning the whole table.

        SNAPSHOT v2 (P2.5, DECISIONS D-10) — when `activation_store` is provided,
        a GAUGE is restored from the ACTIVATION LEDGER (`Σ open activations`), NOT
        from the raw counter snapshot: the counter is a *cache of the ledger*, so a
        wipe + seed can never resurrect a value the open activations don't justify
        (retires QI-07 in the seed path). Per-user partitions are DERIVED from the
        ledger, not stored.

        E3: enumeration is LEDGER-authoritative for gauges. We converge EVERY
        (org, resource) with open activations (from the ledger's open index) AND
        every gauge that has a snapshot row (to clear drift down to Σ open, which
        may be 0). A gauge with open activations but NO snapshot row is therefore
        restored (previously it seeded to 0 = undercount). ACCUMULATORS
        (money/usage) are not ledger-derived, so they restore from their snapshot.
        Without an `activation_store` the legacy raw-snapshot restore is used.

        Returns number of counters restored.
        """
        from .counters.factory import create_counter
        from .models.core import CounterType

        restored = 0
        seen_gauges: set = set()

        async def _converge(org_id: str, resource_key: str, snapshot_value=None) -> bool:
            if (org_id, resource_key) in seen_gauges:
                return False
            resource_def = registry.get(resource_key)
            if not resource_def or resource_def.counter_type != CounterType.GAUGE:
                return False
            from .activations import converge_gauge
            counter = create_counter(redis, org_id, resource_def, keyspace=keyspace)
            v, src = await converge_gauge(
                activation_store=activation_store, org_id=org_id,
                resource_key=resource_key, counter=counter,
            )
            seen_gauges.add((org_id, resource_key))
            logger.info(
                "Restored gauge %s for org %s from ledger: %s (snapshot said %s; "
                "source=%s — snapshot cannot resurrect unjustified drift, D-10/QI-07)",
                resource_key, org_id, v, snapshot_value, src,
            )
            return True

        # (1) LEDGER-authoritative enumeration: every gauge with open activations,
        # even ones with no snapshot row (E3 — the previous undercount).
        if activation_store is not None:
            for (org_id, resource_key) in await activation_store.open_gauge_targets():
                if await _converge(org_id, resource_key):
                    restored += 1

        # (2) Snapshot rows: accumulators restore from snapshot; gauges converge
        # (clearing drift to Σ open, possibly 0) unless already done in (1).
        query_kwargs = {
            "IndexName": "GSI1",
            "KeyConditionExpression": "GSI1PK = :pk",
            "ExpressionAttributeValues": {":pk": "COUNTER"},
        }
        while True:
            response = await self._table.query(**query_kwargs)
            for item in response.get("Items", []):
                org_id = item["PK"].replace("ORG#", "")
                sk = item["SK"]
                if sk.startswith("COUNTER#v2#"):
                    # K-4: v2 rows are service-scoped (SK=COUNTER#v2#{svc}#{rk});
                    # seed only our own scope — another bridge service's rows
                    # belong to its engine (spec §7 row 8).
                    _c, _v, svc, resource_key = sk.split("#", 3)
                    if keyspace is None or getattr(keyspace, "service", None) != svc:
                        continue
                else:
                    resource_key = sk[len("COUNTER#"):]
                value = float(item["value"])

                resource_def = registry.get(resource_key)
                if not resource_def:
                    continue

                if (resource_def.counter_type == CounterType.GAUGE
                        and activation_store is not None):
                    if await _converge(org_id, resource_key, snapshot_value=value):
                        restored += 1
                    continue

                counter = create_counter(redis, org_id, resource_def, keyspace=keyspace)
                current = await counter.get()
                if current == 0 and value > 0:
                    await counter.reset(value)
                    restored += 1
                    logger.info("Restored counter %s for org %s: %s", resource_key, org_id, value)

            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            query_kwargs["ExclusiveStartKey"] = last_key

        logger.info("Seeded %d counters from DynamoDB", restored)
        return restored

    # ------------------------------------------------------------------
    # Periodic Redis → DynamoDB snapshot worker
    # ------------------------------------------------------------------

    def start_sync_worker(self, redis, registry, interval_seconds: int = 300):
        """Start a background task that snapshots all live Redis counters
        to DynamoDB every `interval_seconds`. Returns the asyncio.Task.

        The worker uses Redis SCAN (not KEYS) so it never blocks the
        server; each pass walks `quota:*:gauge` and `quota:*:acc:*` keys
        across all orgs, calling `snapshot_counter()` for any value that
        has changed since the previous snapshot. No-op writes are skipped
        in-memory to keep DynamoDB write cost bounded.

        This is the recovery path's mirror image: `seed_redis()` reads
        snapshots back on startup if Redis was wiped.
        """
        import asyncio
        if self._sync_task is not None and not self._sync_task.done():
            return self._sync_task  # already running
        self._sync_task = asyncio.create_task(
            self._sync_loop(redis, registry, interval_seconds),
            name="ab0t_quota_sync_worker",
        )
        logger.info("snapshot_worker_started interval=%ds", interval_seconds)
        return self._sync_task

    async def stop_sync_worker(self):
        """Cancel the background sync task. Safe to call if not running."""
        import asyncio
        task = self._sync_task
        self._sync_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        logger.info("snapshot_worker_stopped")

    async def _sync_loop(self, redis, registry, interval_seconds: int):
        """Run snapshot passes forever, sleeping `interval_seconds` between passes."""
        import asyncio
        fail_streak = 0
        while True:
            try:
                await asyncio.sleep(interval_seconds)
                snapshotted = await self.snapshot_all(redis, registry)
                fail_streak = 0
                if snapshotted:
                    logger.debug("snapshot_pass_complete count=%d", snapshotted)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                fail_streak += 1
                # D-50 rule 2: a loop that keeps failing must become LOUD — a
                # repeating WARNING nobody reads is a dead worker inside a healthy
                # process (recovery-cache staleness accrues silently otherwise).
                if fail_streak >= 3:
                    logger.error("snapshot_worker_UNHEALTHY: %s (fail_streak=%d) — Redis "
                                 "counters are NOT being snapshotted; recovery on restart "
                                 "will be stale.", e, fail_streak)
                else:
                    logger.warning("snapshot_pass_error: %s", e)
                # Don't tight-loop on persistent failure
                await asyncio.sleep(min(interval_seconds, 30))

    async def snapshot_all(self, redis, registry) -> int:
        """One snapshot pass: SCAN all quota counter keys, write any
        whose value has changed since the last pass to DynamoDB.

        Returns the number of counters actually written this pass.
        Used internally by the sync worker; also exposed for manual
        flushing on shutdown or for tests.
        """
        from .models.core import CounterType
        # Build a quick lookup of registered resource keys → resource def
        keys_by_name = {r.resource_key: r for r in registry.all()}
        if not keys_by_name:
            return 0

        # SCAN pattern catches gauge keys (`:gauge`) and accumulator
        # period keys (`:acc:<period>`). Rate counters are excluded —
        # they're sliding windows that self-expire and don't need snapshots.
        written = 0
        cursor = 0
        while True:
            cursor, batch = await redis.scan(
                cursor=cursor, match="quota:*", count=500,
            )
            for key in batch:
                key_str = key.decode() if isinstance(key, bytes) else key
                from .keyspace import parse_counter_key
                full = parse_counter_key(key_str)
                if full is None:
                    continue
                _version, service, org_id, resource_key, kind = full

                resource_def = keys_by_name.get(resource_key)
                if resource_def is None:
                    continue
                # Skip per-user partition keys — org-level totals already cover them
                if kind == "user":
                    continue
                # Snapshot gauges and accumulators only
                if resource_def.counter_type not in (CounterType.GAUGE, CounterType.ACCUMULATOR):
                    continue

                raw = await redis.get(key)
                if raw is None:
                    continue
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    continue

                # Skip no-op writes (cache keyed per shape — during dual both
                # shapes' rows are maintained, K-4)
                cache_key = (org_id, resource_key, service)
                if self._last_snapshot.get(cache_key) == value:
                    continue
                try:
                    await self.snapshot_counter(org_id, resource_key, value,
                                                service=service)
                    self._last_snapshot[cache_key] = value
                    written += 1
                except Exception as e:
                    # Not silent: a dropped snapshot write leaves the recovery cache
                    # STALE — a later seed restores an older value (gauges reconverge
                    # from the ledger anyway; accumulators would be stale). Log loudly.
                    logger.error(
                        "snapshot_counter_failed org=%s resource=%s error=%s — recovery "
                        "cache is now STALE for this counter until the next successful pass.",
                        org_id, resource_key, e,
                    )

            if cursor == 0:
                break
        return written

    @staticmethod
    def _parse_quota_key(key: str):
        """Parse a Redis quota key of EITHER shape into (org_id, resource_key, kind).

        Recognized layouts (K-4: v1 AND v2 — the one home for shape is
        ab0t_quota.keyspace.parse_counter_key; spec §7 row 7):
          quota:{org}:{rk}:gauge[...]            v1
          quota:v2:{svc/org}:{rk}:gauge[...]     v2
          (+ :gauge:user:{uid} → "user", :acc:{period} → "acc")

        Returns None for keys that don't match (idem, alert, tier cache, etc).
        """
        from .keyspace import parse_counter_key
        parsed = parse_counter_key(key)
        if parsed is None:
            return None
        _version, _service, org_id, resource_key, kind = parsed
        return (org_id, resource_key, kind)

    async def close(self):
        """Clean up DynamoDB resource. Stops the snapshot worker if running."""
        await self.stop_sync_worker()
        if getattr(self, "_resource_ctx", None):
            await self._resource_ctx.__aexit__(None, None, None)
