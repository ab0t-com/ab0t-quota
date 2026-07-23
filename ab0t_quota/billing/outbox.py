"""Durable outbox for lifecycle settlement events (QB-01 / D-29).

The LifecycleEmitter writes an intent row to a DURABLE store BEFORE it attempts
the SNS publish, marks it delivered on success, and voids+alerts it past the
retry horizon. The drain reads PENDING intents FROM THE STORE — so a process
crash / pod restart between "intent written" and "delivered" RESUMES delivery
instead of silently losing the money event.

This is the whole point of D-29: an in-process list is not durable. A queue that
evaporates on the crash it exists to survive is a retry loop wearing the word
"durable". The store here lives in an EXTERNAL service (Redis or DynamoDB), so it
survives the process.

Backends (mirrors handler_ledger.auto_select_store):
  - DDBOutboxStore      durable; mesh services with a DDB client
  - RedisOutboxStore    durable across restarts (Redis is an external service) —
                        this is what the ledger store already resolves to when no
                        DDB client is wired, so it is "the durable store already
                        wired" in the common deployment
  - InMemoryOutboxStore  tests + explicit degraded mode — NOT crash-durable

Timestamps are epoch seconds (`time.time()`), never `monotonic()`, so the retry
horizon (D-9) is meaningful across a restart.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import time
from typing import Any, List, Optional, Protocol

logger = logging.getLogger("ab0t_quota.billing.outbox")

PENDING = "pending"
DELIVERED = "delivered"
VOIDED = "voided"


@dataclasses.dataclass
class OutboxRecord:
    key: str                       # reservation_id:event_type (activation_id:event_type after Phase 3)
    event: dict                    # the SNS message payload
    event_type: str
    resource_type: str
    reservation_id: str
    status: str = PENDING
    first_ts: float = 0.0          # EPOCH seconds — survives restart (not monotonic)
    attempts: int = 0
    reason: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), default=str)

    @classmethod
    def from_json(cls, raw) -> "OutboxRecord":
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        d = json.loads(raw)
        fields = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in fields})


class OutboxStore(Protocol):
    async def put_intent(self, rec: OutboxRecord) -> OutboxRecord: ...
    async def mark_delivered(self, key: str) -> None: ...
    async def mark_voided(self, key: str, reason: str) -> None: ...
    async def bump_attempt(self, key: str) -> None: ...
    async def list_pending(self, limit: int = 100) -> List[OutboxRecord]: ...
    def durable(self) -> bool: ...


# ---------------------------------------------------------------------------
# In-memory (tests + explicit degraded mode) — NOT crash-durable
# ---------------------------------------------------------------------------

class InMemoryOutboxStore:
    def __init__(self) -> None:
        self._rows: dict[str, OutboxRecord] = {}

    async def put_intent(self, rec: OutboxRecord) -> OutboxRecord:
        existing = self._rows.get(rec.key)
        if existing is not None and existing.status == PENDING:
            return existing  # preserve first_ts / attempts across a re-emit
        self._rows[rec.key] = rec
        return rec

    async def mark_delivered(self, key: str) -> None:
        self._rows.pop(key, None)

    async def mark_voided(self, key: str, reason: str) -> None:
        r = self._rows.get(key)
        if r is not None:
            r.status = VOIDED
            r.reason = reason

    async def bump_attempt(self, key: str) -> None:
        r = self._rows.get(key)
        if r is not None:
            r.attempts += 1

    async def list_pending(self, limit: int = 100) -> List[OutboxRecord]:
        pend = [r for r in self._rows.values() if r.status == PENDING]
        pend.sort(key=lambda r: r.first_ts)
        return pend[:limit]

    def durable(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# Redis-backed — durable across restarts (Redis is external)
# ---------------------------------------------------------------------------

class RedisOutboxStore:
    def __init__(self, redis, *, prefix: str = "outbox") -> None:
        self.redis = redis
        self.prefix = prefix

    def _intent_key(self, key: str) -> str:
        return f"{self.prefix}:intent:{key}"

    @property
    def _pending_idx(self) -> str:
        return f"{self.prefix}:pending"

    async def put_intent(self, rec: OutboxRecord) -> OutboxRecord:
        ik = self._intent_key(rec.key)
        # Only create if absent — preserves first_ts (the horizon anchor) across
        # a re-emit of the same key.
        created = await self.redis.set(ik, rec.to_json(), nx=True)
        if created:
            await self.redis.zadd(self._pending_idx, {rec.key: rec.first_ts})
            return rec
        raw = await self.redis.get(ik)
        return OutboxRecord.from_json(raw) if raw else rec

    async def mark_delivered(self, key: str) -> None:
        await self.redis.delete(self._intent_key(key))
        await self.redis.zrem(self._pending_idx, key)

    async def mark_voided(self, key: str, reason: str) -> None:
        ik = self._intent_key(key)
        raw = await self.redis.get(ik)
        if raw:
            rec = OutboxRecord.from_json(raw)
            rec.status = VOIDED
            rec.reason = reason
            await self.redis.set(ik, rec.to_json())
        await self.redis.zrem(self._pending_idx, key)

    async def bump_attempt(self, key: str) -> None:
        ik = self._intent_key(key)
        raw = await self.redis.get(ik)
        if raw:
            rec = OutboxRecord.from_json(raw)
            rec.attempts += 1
            await self.redis.set(ik, rec.to_json())

    async def list_pending(self, limit: int = 100) -> List[OutboxRecord]:
        keys = await self.redis.zrange(self._pending_idx, 0, limit - 1)
        out: List[OutboxRecord] = []
        for kb in keys:
            k = kb.decode("utf-8") if isinstance(kb, (bytes, bytearray)) else kb
            raw = await self.redis.get(self._intent_key(k))
            if raw:
                rec = OutboxRecord.from_json(raw)
                if rec.status == PENDING:
                    out.append(rec)
        return out

    def durable(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# DynamoDB-backed — durable; mesh services with a DDB client
# ---------------------------------------------------------------------------

class DDBOutboxStore:
    """Durable DDB outbox. Rows: PK=OUTBOX#{key}, SK=META, with a status GSI
    (gsi_status_pk=OUTBOXSTATUS#{status}) for list_pending, mirroring the handler
    ledger's status-index pattern. NOTE: exercised only against the in-test
    FakeDDB in this ticket — real DynamoDB is UNVERIFIED (see the leg's artifact)."""

    def __init__(self, ddb_client, *, table_name: str = "ab0t_quota_outbox") -> None:
        self.ddb = ddb_client
        self.table = table_name

    async def ensure_table(self, *, gsi_active_timeout_s: float = 60.0,
                           create: bool = True) -> None:
        """Create the table + `gsi_status` GSI if absent (idempotent), then WAIT
        for both to report ACTIVE. Mirrors QuotaStore.initialize so
        `outbox.store=ddb` works out of the box. The wait matters in PRODUCTION:
        real DynamoDB backfills a GSI asynchronously, so `list_pending` right
        after a fresh create can silently miss rows until the index is ready
        (DDB Local makes it immediate, so no DDB-Local test catches this).
        `create=False` is the T-6/D-3 call-site policy."""
        try:
            await self.ddb.describe_table(TableName=self.table)
            await self._wait_gsi_active(gsi_active_timeout_s)
            return
        except Exception as e:
            not_found = getattr(getattr(self.ddb, "exceptions", None), "ResourceNotFoundException", ())
            if not (isinstance(e, not_found) or "ResourceNotFound" in type(e).__name__):
                raise  # a real error (perms, endpoint) — don't mask it
        if not create:
            raise RuntimeError(
                f"outbox table {self.table} does not exist and "
                f"storage.auto_create_tables is false (the default) — pre-create "
                f"the table or set storage.auto_create_tables: true (ENV-04)")
        await self.ddb.create_table(
            TableName=self.table,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "gsi_status_pk", "AttributeType": "S"},
                {"AttributeName": "gsi_status_sk", "AttributeType": "N"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "gsi_status",
                "KeySchema": [
                    {"AttributeName": "gsi_status_pk", "KeyType": "HASH"},
                    {"AttributeName": "gsi_status_sk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
            Tags=[
                {"Key": "Service", "Value": "ab0t-quota"},
                {"Key": "ManagedBy", "Value": "ab0t-quota-library"},
            ],
        )
        logger.info("created lifecycle outbox table %s (+ gsi_status)", self.table)
        await self._wait_gsi_active(gsi_active_timeout_s)

    async def _wait_gsi_active(self, timeout_s: float) -> None:
        """Poll describe_table until the table AND the gsi_status GSI are ACTIVE.
        Bounded; loud RuntimeError on timeout (never proceed to list_pending
        against a still-backfilling index — that silently misses pending money)."""
        import asyncio
        import time as _t
        deadline = _t.monotonic() + timeout_s
        while True:
            desc = await self.ddb.describe_table(TableName=self.table)
            t = desc.get("Table", {})
            table_active = t.get("TableStatus") == "ACTIVE"
            gsi = next((g for g in t.get("GlobalSecondaryIndexes", [])
                        if g.get("IndexName") == "gsi_status"), None)
            gsi_status = gsi.get("IndexStatus") if gsi else "MISSING"
            if table_active and gsi_status == "ACTIVE":
                return
            if _t.monotonic() > deadline:
                raise RuntimeError(
                    f"outbox table {self.table} not ready within {timeout_s}s "
                    f"(table={t.get('TableStatus')}, gsi_status={gsi_status}). Money "
                    f"events must not be drained against a backfilling index."
                )
            await asyncio.sleep(0.5)

    def _pk(self, key: str) -> str:
        return f"OUTBOX#{key}"

    def _item(self, rec: OutboxRecord) -> dict:
        return {
            "PK": {"S": self._pk(rec.key)},
            "SK": {"S": "META"},
            "okey": {"S": rec.key},
            "event": {"S": json.dumps(rec.event, default=str)},
            "event_type": {"S": rec.event_type},
            "resource_type": {"S": rec.resource_type},
            "reservation_id": {"S": rec.reservation_id},
            "status": {"S": rec.status},
            "first_ts": {"N": str(rec.first_ts)},
            "attempts": {"N": str(rec.attempts)},
            "gsi_status_pk": {"S": f"OUTBOXSTATUS#{rec.status}"},
            "gsi_status_sk": {"N": str(rec.first_ts)},
        }

    @staticmethod
    def _rec(item: dict) -> OutboxRecord:
        def s(f):
            v = item.get(f); return v["S"] if v and "S" in v else None
        def n(f):
            v = item.get(f); return float(v["N"]) if v and "N" in v else 0.0
        return OutboxRecord(
            key=s("okey") or "",
            event=json.loads(s("event") or "{}"),
            event_type=s("event_type") or "",
            resource_type=s("resource_type") or "",
            reservation_id=s("reservation_id") or "",
            status=s("status") or PENDING,
            first_ts=n("first_ts"),
            attempts=int(n("attempts")),
            reason=s("reason"),
        )

    async def _get(self, key: str) -> Optional[OutboxRecord]:
        res = await self.ddb.get_item(
            TableName=self.table, Key={"PK": {"S": self._pk(key)}, "SK": {"S": "META"}},
        )
        item = res.get("Item")
        return self._rec(item) if item else None

    async def put_intent(self, rec: OutboxRecord) -> OutboxRecord:
        try:
            await self.ddb.put_item(
                TableName=self.table, Item=self._item(rec),
                ConditionExpression="attribute_not_exists(PK)",
            )
            return rec
        except Exception as e:
            from ..handler_ledger import _is_conditional_check_failed
            if not _is_conditional_check_failed(e):
                raise
            existing = await self._get(rec.key)
            return existing or rec

    async def mark_delivered(self, key: str) -> None:
        await self.ddb.delete_item(
            TableName=self.table, Key={"PK": {"S": self._pk(key)}, "SK": {"S": "META"}},
        )

    async def mark_voided(self, key: str, reason: str) -> None:
        rec = await self._get(key)
        if rec is None:
            return
        rec.status = VOIDED
        rec.reason = reason
        item = self._item(rec)
        item["reason"] = {"S": reason}
        await self.ddb.put_item(TableName=self.table, Item=item)

    async def bump_attempt(self, key: str) -> None:
        rec = await self._get(key)
        if rec is None:
            return
        rec.attempts += 1
        await self.ddb.put_item(TableName=self.table, Item=self._item(rec))

    async def list_pending(self, limit: int = 100) -> List[OutboxRecord]:
        res = await self.ddb.query(
            TableName=self.table, IndexName="gsi_status",
            KeyConditionExpression="gsi_status_pk = :pk",
            ExpressionAttributeValues={":pk": {"S": f"OUTBOXSTATUS#{PENDING}"}},
            Limit=limit, ScanIndexForward=True,
        )
        return [self._rec(i) for i in res.get("Items", [])]

    def durable(self) -> bool:
        return True


def auto_select_outbox_store(*, redis=None, ddb_client=None) -> Optional[OutboxStore]:
    """DDB > Redis > None. Mirrors handler_ledger.auto_select_store. Returns None
    when NO durable backend is available — the caller must fail loudly (D-29),
    never silently fall back to RAM."""
    if ddb_client is not None:
        logger.info("lifecycle outbox: using DDB backend")
        return DDBOutboxStore(ddb_client)
    if redis is not None:
        logger.info("lifecycle outbox: using Redis backend")
        return RedisOutboxStore(redis)
    return None


# ---------------------------------------------------------------------------
# D-32 — self-provisioning + durability self-checks (correctness must not depend
# on a human remembering to wire a client or set a Redis policy).
# ---------------------------------------------------------------------------

async def connect_ddb_outbox_store(
    *, region: str, endpoint_url: Optional[str] = None,
    table_name: str = "ab0t_quota_outbox", session=None,
):
    """Self-provision a DURABLE DDB outbox from standard configuration — so
    `app.state.ddb_client` is an OPTIONAL override, never a precondition (D-32
    Claim 1 / Pillar 2: a client who does nothing gets correctness, not a cache).

    Opens a long-lived aioboto3 client (kept open for the app lifetime) and
    returns (store, aclose) where `aclose` closes the client on shutdown."""
    import aioboto3
    session = session or aioboto3.Session()
    kwargs = {"region_name": region}
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    cm = session.client("dynamodb", **kwargs)
    client = await cm.__aenter__()
    store = DDBOutboxStore(client, table_name=table_name)

    async def _aclose():
        try:
            await cm.__aexit__(None, None, None)
        except Exception as e:  # pragma: no cover - shutdown best-effort
            logger.warning("outbox ddb client close failed: %s", e)

    return store, _aclose


from ..redis_preflight import EVICTING_POLICIES as _EVICTING_POLICIES  # noqa: F401  (kept: importers)
from ..redis_preflight import check_redis_durability


async def check_redis_outbox_durability(redis, *, confirmed: bool = False) -> tuple[bool, str]:
    """OPERATOR CHECK 2 as a machine check (D-32 Claim 2): the OUTBOX needs persistence
    AND a non-evicting policy — money events cannot be reconstructed.

      - maxmemory-policy is allkeys-* → NOT durable (pending money events are
        eligible for eviction). Hard fail, NOT overridable by `confirmed`.
      - no persistence (appendonly=no AND no save points) → NOT durable (a
        restart/failover loses pending events). Hard fail unless `confirmed`.
      - CONFIG unavailable (e.g. ElastiCache disables it) → cannot verify;
        require the explicit operator assertion `confirmed`
        (outbox.redis_durability_confirmed=true) — on the record, not silent.

    D-72 generalised this judgement: the ONE implementation now lives in
    `ab0t_quota.redis_preflight` (which also checks the COUNTER's eviction policy — the
    same law, different severity: the counter tolerates a restart, never an eviction).
    This function is a NAME, not a second copy. Returns (durable, human_reason).
    """
    return await check_redis_durability(redis, confirmed=confirmed)
