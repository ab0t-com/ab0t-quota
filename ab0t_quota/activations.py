"""Activation ledger — the durable, GENERIC record of "one thing that was acquired".

Ticket 20260709_ab0t_quota_systemic_integrity_redesign, tasks P2.1 / P2.2.

WHY THIS EXISTS (DECISIONS D-10, the precedence law)
----------------------------------------------------
After this redesign the truth for a gauge could be asserted by four mechanisms
(live Redis counter, DDB snapshot, this ledger, the consumer's
`observed_usage_provider`). D-10 declares ONE of them authoritative:

    the Redis counter is a CACHE of ``Σ open activations``.

An activation is minted (``OPEN``) atomically with the counter spend, closed
(``RELEASED``) atomically with the counter release, and ``SETTLED`` when its cost
lands in billing. Because the counter is derived from the ledger, a snapshot
restore or a provider reconciliation can never resurrect a value the open
activations do not justify (this retires QI-07).

BOUNDARY (LEDGER_PLACEMENT_AND_BOUNDARY.md — binding)
-----------------------------------------------------
This entity is GENERIC: a sandbox, a mailbox and a card issuance are all the same
shape. It carries NO client-domain fields. It persists to the EMBEDDING CLIENT's
own datastore (the store is injected). The library never learns what a "sandbox"
is; consumer-domain semantics enter only via the `observed_usage_provider` seam.

STORAGE SHAPE (FUTURE §3 — requirements, not musings)
-----------------------------------------------------
* PK = a randomly-minted ``activation_id`` (NOT ``org_id`` — a whale org must not
  become a hot partition). Enumerate an org via a GSI on ``org_id``.
* Released+settled activations TTL away; only OPEN ones are load-bearing (they are
  the drift alarm — QB-03). The TTL is chosen from the longest legitimate
  create->terminate gap, NOT QI-05's old 24h idempotency horizon.

REAL-BACKEND CAVEAT
-------------------
Redis Lua here runs only under ``fakeredis[lua]`` (lupa) in tests; DDB only under
a stub. Neither has been exercised against a real Redis ``EVAL`` or real
DynamoDB. See ``not_verified`` in information_phase2_activation_20260710.md.
"""

from __future__ import annotations

import enum
import json
import logging
import secrets
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional, Protocol, runtime_checkable

logger = logging.getLogger("ab0t_quota.activations")

# Default TTL (seconds) for RELEASED / SETTLED activations. Chosen to outlive the
# longest legitimate create->terminate gap with margin, NOT anchored to QI-05's
# old 24h idempotency horizon. 14 days: a released record lingers long enough for
# audit/settlement reconciliation, then reaps itself. OPEN activations NEVER TTL
# (they are the drift alarm). Configurable per store.
DEFAULT_RELEASED_TTL_S = 14 * 24 * 3600


class ActivationState(str, enum.Enum):
    OPEN = "open"          # acquired, counter spent, not yet released — load-bearing
    RELEASED = "released"  # counter released; may still owe a settlement
    SETTLED = "settled"    # cost recorded in billing (terminal)


def mint_activation_id() -> str:
    """A random, unguessable activation id. Random PK => no org hot partition."""
    return f"act_{secrets.token_hex(16)}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Activation:
    """A generic activation record. GENERIC FIELDS ONLY — no client-domain data.

    ``spend`` is generic internal bookkeeping (resource_key -> delta) so
    ``release`` can return exactly what ``acquire`` spent; it carries no domain
    meaning. ``resource_key`` is the primary/bundle label the acquire was for.
    """
    activation_id: str
    org_id: str
    user_id: Optional[str]
    resource_key: str
    cost: Optional[str] = None            # decimal-as-string; None until settled
    opened_at: str = field(default_factory=_now_iso)
    state: str = ActivationState.OPEN.value
    # --- generic internal bookkeeping (not client-domain) ---
    spend: dict = field(default_factory=dict)   # resource_key -> float delta
    released_at: Optional[str] = None
    settled_at: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Activation":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@runtime_checkable
class ActivationStore(Protocol):
    """Persistence contract. Implemented against the client's own datastore."""

    async def put_open(self, activation: Activation) -> None: ...

    async def get(self, activation_id: str) -> Optional[Activation]: ...

    async def mark_released(self, activation_id: str) -> Optional[Activation]:
        """Idempotent: OPEN -> RELEASED. Returns the row if this call performed the
        transition; None if it was already released (so the caller can make the
        counter release a no-op on replay)."""
        ...

    async def mark_settled(self, activation_id: str, cost: str) -> Optional[Activation]:
        """Idempotent: record cost + mark SETTLED. Returns the row if this call
        performed the transition; None if already settled (replay-safe)."""
        ...

    async def list_open(self, org_id: str, *, limit: int = 100) -> list[Activation]: ...

    async def count_open(self, org_id: str) -> int: ...

    async def open_gauge_targets(self, *, limit: int = 5000) -> set:
        """(org_id, resource_key) pairs with >=1 OPEN activation — the LEDGER-side
        enumeration for seed recovery. D-10/E3: if the ledger is authoritative, seed
        enumeration must come from the ledger's open set, not from counter snapshots
        (a gauge with open activations but no snapshot row must still be restored)."""
        ...


class InMemoryActivationStore:
    """Process-local store. The DEFAULT when a client wires nothing — it keeps the
    library working out of the box, but is NOT durable across a restart. A client
    that wants durable activations wires a Redis/DDB store. (Mirrors the loud
    in-memory fallback pattern used for the handler ledger.)"""

    def __init__(self) -> None:
        self._rows: dict[str, Activation] = {}

    async def put_open(self, activation: Activation) -> None:
        # Idempotent on activation_id (minted unique) — first writer wins.
        self._rows.setdefault(activation.activation_id, activation)

    async def get(self, activation_id: str) -> Optional[Activation]:
        return self._rows.get(activation_id)

    async def mark_released(self, activation_id: str) -> Optional[Activation]:
        row = self._rows.get(activation_id)
        if row is None or row.state != ActivationState.OPEN.value:
            return None
        row.state = ActivationState.RELEASED.value
        row.released_at = _now_iso()
        return row

    async def mark_settled(self, activation_id: str, cost: str) -> Optional[Activation]:
        row = self._rows.get(activation_id)
        if row is None or row.state == ActivationState.SETTLED.value:
            return None
        row.cost = cost
        row.state = ActivationState.SETTLED.value
        row.settled_at = _now_iso()
        return row

    async def list_open(self, org_id: str, *, limit: int = 100) -> list[Activation]:
        out = [r for r in self._rows.values()
               if r.org_id == org_id and r.state == ActivationState.OPEN.value]
        out.sort(key=lambda r: r.opened_at)
        return out[:limit]

    async def count_open(self, org_id: str) -> int:
        return sum(1 for r in self._rows.values()
                   if r.org_id == org_id and r.state == ActivationState.OPEN.value)

    async def open_gauge_targets(self, *, limit: int = 5000) -> set:
        out: set = set()
        for r in self._rows.values():
            if r.state != ActivationState.OPEN.value:
                continue
            for rk in (r.spend or {}):
                out.add((r.org_id, rk))
                if len(out) >= limit:
                    return out
        return out


# Atomic idempotent state transition (single-node Redis / fakeredis).
#   KEYS[1]=row  ARGV[1]=from_state ARGV[2]=to_state ARGV[3]=released_ttl ARGV[4]=extra_json
# Returns the updated row json if THIS call transitioned it, else '' (already done).
_TRANSITION = """
local raw = redis.call('GET', KEYS[1])
if not raw then return '' end
local row = cjson.decode(raw)
if row['state'] ~= ARGV[1] then return '' end
row['state'] = ARGV[2]
local extra = cjson.decode(ARGV[4])
for k, v in pairs(extra) do row[k] = v end
local out = cjson.encode(row)
redis.call('SET', KEYS[1], out)
if tonumber(ARGV[3]) > 0 then redis.call('EXPIRE', KEYS[1], ARGV[3]) end
return out
"""


class RedisActivationStore:
    """Redis-backed store. PK = activation_id (random). Enumeration index: a per-org
    SET of open activation ids (removed on release/settle) — the Redis analogue of a
    GSI on ``org_id`` without an org hot partition on the primary key.

    Real-Redis Lua UNVERIFIED (see module docstring)."""

    def __init__(self, redis, *, key_prefix: str = "activation",
                 released_ttl_s: int = DEFAULT_RELEASED_TTL_S) -> None:
        self._redis = redis
        self._prefix = key_prefix
        self._ttl = released_ttl_s

    def _row_key(self, activation_id: str) -> str:
        return f"{self._prefix}:row:{activation_id}"

    def _open_index_key(self, org_id: str) -> str:
        return f"{self._prefix}:open:org:{org_id}"

    async def put_open(self, activation: Activation) -> None:
        key = self._row_key(activation.activation_id)
        # SET NX: first writer wins; a replayed acquire with the same (minted) id
        # is a no-op. No TTL on OPEN rows — they are load-bearing.
        ok = await self._redis.set(key, json.dumps(activation.to_dict()), nx=True)
        if ok:
            await self._redis.sadd(self._open_index_key(activation.org_id),
                                   activation.activation_id)

    async def get(self, activation_id: str) -> Optional[Activation]:
        raw = await self._redis.get(self._row_key(activation_id))
        if not raw:
            return None
        return Activation.from_dict(json.loads(raw))

    async def _transition(self, activation_id: str, frm: str, to: str, extra: dict) -> Optional[Activation]:
        out = await self._redis.eval(
            _TRANSITION, 1, self._row_key(activation_id),
            frm, to, str(self._ttl), json.dumps(extra),
        )
        if not out:
            return None
        if isinstance(out, bytes):
            out = out.decode()
        row = Activation.from_dict(json.loads(out))
        await self._redis.srem(self._open_index_key(row.org_id), activation_id)
        return row

    async def mark_released(self, activation_id: str) -> Optional[Activation]:
        return await self._transition(
            activation_id, ActivationState.OPEN.value, ActivationState.RELEASED.value,
            {"released_at": _now_iso()},
        )

    async def mark_settled(self, activation_id: str, cost: str) -> Optional[Activation]:
        # A settle may follow either OPEN or RELEASED. Try both transitions.
        for frm in (ActivationState.RELEASED.value, ActivationState.OPEN.value):
            row = await self._transition(
                activation_id, frm, ActivationState.SETTLED.value,
                {"settled_at": _now_iso(), "cost": cost},
            )
            if row is not None:
                return row
        return None

    async def list_open(self, org_id: str, *, limit: int = 100) -> list[Activation]:
        ids = await self._redis.smembers(self._open_index_key(org_id))
        out: list[Activation] = []
        for aid in list(ids)[:limit]:
            if isinstance(aid, bytes):
                aid = aid.decode()
            row = await self.get(aid)
            if row is not None and row.state == ActivationState.OPEN.value:
                out.append(row)
        out.sort(key=lambda r: r.opened_at)
        return out

    async def count_open(self, org_id: str) -> int:
        return int(await self._redis.scard(self._open_index_key(org_id)))

    async def open_gauge_targets(self, *, limit: int = 5000) -> set:
        out: set = set()
        pattern = f"{self._prefix}:open:org:*"
        cursor = 0
        while True:
            cursor, keys = await self._redis.scan(cursor, match=pattern, count=200)
            for k in keys:
                if isinstance(k, bytes):
                    k = k.decode()
                org = k.rsplit(":org:", 1)[-1]
                for a in await self.list_open(org, limit=limit):
                    for rk in (a.spend or {}):
                        out.add((org, rk))
            if cursor == 0 or len(out) >= limit:
                break
        return out


class DDBActivationStore:
    """DynamoDB-backed store, mirroring DDBLedgerStore's shape.

      PK   = ACT#{activation_id}          (random => no org hot partition)
      GSI1 = GSI1PK = ORGOPEN#{org_id}, GSI1SK = opened_at   (list an org's OPEN set)
    A released/settled row drops its GSI1PK (removed from the open index) and gets a
    TTL. Conditional writes make transitions idempotent.

    Exercised only against a DDB stub — real DynamoDB UNVERIFIED (module docstring)."""

    def __init__(self, ddb_client, *, table_name: str = "ab0t_quota_activations",
                 released_ttl_s: int = DEFAULT_RELEASED_TTL_S) -> None:
        self._ddb = ddb_client
        self.table = table_name
        self._ttl = released_ttl_s

    def _pk(self, activation_id: str) -> str:
        return f"ACT#{activation_id}"

    def _ttl_epoch(self) -> int:
        return int(time.time()) + self._ttl

    async def ensure_table(self, *, gsi_active_timeout_s: float = 60.0) -> None:
        """Create the table + GSI1 (the ORGOPEN open-index) if absent (idempotent),
        then WAIT for both to report ACTIVE — so a self-provisioned DDB activation
        ledger works out of the box (D-39). Mirrors DDBOutboxStore.ensure_table.
        The wait matters in PRODUCTION: real DynamoDB backfills a GSI asynchronously,
        so a list_open right after a fresh create can silently miss OPEN rows until
        the index is ready (DDB Local makes it immediate, so no DDB-Local test
        catches this)."""
        try:
            await self._ddb.describe_table(TableName=self.table)
            await self._wait_gsi_active(gsi_active_timeout_s)
            return
        except Exception as e:
            not_found = getattr(getattr(self._ddb, "exceptions", None),
                                "ResourceNotFoundException", ())
            if not (isinstance(e, not_found) or "ResourceNotFound" in type(e).__name__):
                raise  # a real error (perms, endpoint) — don't mask it
        await self._ddb.create_table(
            TableName=self.table,
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
            GlobalSecondaryIndexes=[{
                "IndexName": "GSI1",
                "KeySchema": [
                    {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        logger.info("created activation ledger table %s (+ GSI1)", self.table)
        await self._wait_gsi_active(gsi_active_timeout_s)

    async def _wait_gsi_active(self, timeout_s: float) -> None:
        """Poll describe_table until the table AND GSI1 are ACTIVE. Bounded; loud
        RuntimeError on timeout — never enumerate open activations against a
        still-backfilling index (it would silently miss OPEN rows → the reconciler
        would under-count → the forbidden direction, D-31/D-39)."""
        import asyncio as _asyncio
        import time as _t
        deadline = _t.monotonic() + timeout_s
        while True:
            desc = await self._ddb.describe_table(TableName=self.table)
            t = desc.get("Table", {})
            table_active = t.get("TableStatus") == "ACTIVE"
            gsi = next((g for g in t.get("GlobalSecondaryIndexes", [])
                        if g.get("IndexName") == "GSI1"), None)
            gsi_status = gsi.get("IndexStatus") if gsi else "MISSING"
            if table_active and gsi_status == "ACTIVE":
                return
            if _t.monotonic() > deadline:
                raise RuntimeError(
                    f"activation ledger table {self.table} not ready within {timeout_s}s "
                    f"(table={t.get('TableStatus')}, GSI1={gsi_status}). The reconciler must "
                    f"NOT enumerate open activations against a backfilling index (D-39).")
            await _asyncio.sleep(0.5)

    async def put_open(self, activation: Activation) -> None:
        item = {
            "PK": {"S": self._pk(activation.activation_id)},
            "SK": {"S": "META"},
            "GSI1PK": {"S": f"ORGOPEN#{activation.org_id}"},
            "GSI1SK": {"S": activation.opened_at},
            "activation_id": {"S": activation.activation_id},
            "org_id": {"S": activation.org_id},
            "resource_key": {"S": activation.resource_key},
            "state": {"S": activation.state},
            "opened_at": {"S": activation.opened_at},
            "spend": {"S": json.dumps(activation.spend)},
        }
        if activation.user_id:
            item["user_id"] = {"S": activation.user_id}
        try:
            await self._ddb.put_item(
                TableName=self.table, Item=item,
                ConditionExpression="attribute_not_exists(PK)",
            )
        except Exception as e:  # ConditionalCheckFailed => replay, no-op
            if not _is_conditional_check_failed(e):
                raise

    async def get(self, activation_id: str) -> Optional[Activation]:
        resp = await self._ddb.get_item(
            TableName=self.table,
            Key={"PK": {"S": self._pk(activation_id)}, "SK": {"S": "META"}},
        )
        item = resp.get("Item")
        return _activation_from_item(item) if item else None

    async def _transition(self, activation_id: str, frm: str, to: str, extra: dict) -> Optional[Activation]:
        names = {"#s": "state"}
        values = {":from": {"S": frm}, ":to": {"S": to}}
        set_parts = ["#s = :to", "GSI1PK = :gone"]
        values[":gone"] = {"S": "CLOSED"}   # drop out of the open index
        for k, v in extra.items():
            names[f"#{k}"] = k
            values[f":{k}"] = {"S": str(v)}
            set_parts.append(f"#{k} = :{k}")
        names["#ttl"] = "ttl"
        values[":ttl"] = {"N": str(self._ttl_epoch())}
        set_parts.append("#ttl = :ttl")
        try:
            resp = await self._ddb.update_item(
                TableName=self.table,
                Key={"PK": {"S": self._pk(activation_id)}, "SK": {"S": "META"}},
                UpdateExpression="SET " + ", ".join(set_parts),
                ConditionExpression="#s = :from",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
            return _activation_from_item(resp.get("Attributes"))
        except Exception as e:
            if _is_conditional_check_failed(e):
                return None
            raise

    async def mark_released(self, activation_id: str) -> Optional[Activation]:
        return await self._transition(
            activation_id, ActivationState.OPEN.value, ActivationState.RELEASED.value,
            {"released_at": _now_iso()},
        )

    async def mark_settled(self, activation_id: str, cost: str) -> Optional[Activation]:
        for frm in (ActivationState.RELEASED.value, ActivationState.OPEN.value):
            row = await self._transition(
                activation_id, frm, ActivationState.SETTLED.value,
                {"settled_at": _now_iso(), "cost": cost},
            )
            if row is not None:
                return row
        return None

    async def list_open(self, org_id: str, *, limit: int = 100) -> list[Activation]:
        resp = await self._ddb.query(
            TableName=self.table, IndexName="GSI1",
            KeyConditionExpression="GSI1PK = :pk",
            ExpressionAttributeValues={":pk": {"S": f"ORGOPEN#{org_id}"}},
            Limit=limit,
        )
        return [_activation_from_item(it) for it in resp.get("Items", [])]

    async def count_open(self, org_id: str) -> int:
        return len(await self.list_open(org_id, limit=1000))

    async def open_gauge_targets(self, *, limit: int = 5000) -> set:
        # Recovery-time scan for OPEN rows (infrequent — only at seed). Bounded by
        # `limit`. A whale-scale deployment would add a sparse global-open GSI; at
        # v0.x a scan is correct and simplest.
        out: set = set()
        kwargs = {
            "TableName": self.table,
            "FilterExpression": "#s = :open",
            "ExpressionAttributeNames": {"#s": "state"},
            "ExpressionAttributeValues": {":open": {"S": ActivationState.OPEN.value}},
        }
        while True:
            resp = await self._ddb.scan(**kwargs)
            for it in resp.get("Items", []):
                a = _activation_from_item(it)
                if a:
                    for rk in (a.spend or {}):
                        out.add((a.org_id, rk))
            lek = resp.get("LastEvaluatedKey")
            if not lek or len(out) >= limit:
                break
            kwargs["ExclusiveStartKey"] = lek
        return out


async def connect_ddb_activation_store(
    *, region: str, endpoint_url: Optional[str] = None,
    table_name: str = "ab0t_quota_activations", session=None,
):
    """Self-provision a DURABLE DDB activation ledger from standard configuration
    (D-39). The ledger is authoritative for IDENTITY and COST (D-33); it must not
    live in an evictable cache. Mirrors ``connect_ddb_outbox_store``: opens a
    long-lived aioboto3 client and returns ``(store, aclose)``. ``app.state.ddb_client``
    is an OPTIONAL override, never a precondition."""
    import aioboto3
    session = session or aioboto3.Session()
    kwargs = {"region_name": region}
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    cm = session.client("dynamodb", **kwargs)
    client = await cm.__aenter__()
    store = DDBActivationStore(client, table_name=table_name)

    async def _aclose():
        try:
            await cm.__aexit__(None, None, None)
        except Exception as e:  # pragma: no cover - shutdown best-effort
            logger.warning("activation ledger ddb client close failed: %s", e)

    return store, _aclose


def _activation_from_item(item: Optional[dict]) -> Optional[Activation]:
    if not item:
        return None
    def s(k):
        return item.get(k, {}).get("S")
    spend_raw = s("spend")
    return Activation(
        activation_id=s("activation_id"),
        org_id=s("org_id"),
        user_id=s("user_id"),
        resource_key=s("resource_key"),
        cost=s("cost"),
        opened_at=s("opened_at") or _now_iso(),
        state=s("state") or ActivationState.OPEN.value,
        spend=json.loads(spend_raw) if spend_raw else {},
        released_at=s("released_at"),
        settled_at=s("settled_at"),
    )


# ---------------------------------------------------------------------------
# The precedence law (P2.0, DECISIONS D-10) — SUPERSEDED by D-33/D-35.
# ---------------------------------------------------------------------------
# D-33 sharpened the law (provider is authoritative for EXISTENCE, so it can win
# over the ledger on a disagreement — not only when activations are absent). D-35
# then moved the law to ONE home: reconcile.LibraryReconciler._resolve_existence.
# The functions below are now MECHANISM ONLY (converge to Σ open activations);
# they make no source choice. The comment below is retained as history.
#
# Four mechanisms can each claim "the count" for a GAUGE:
#   (1) the live Redis counter, (2) the DDB snapshot restored by seed_redis,
#   (3) the open-activation ledger, (4) the consumer's observed_usage_provider.
# Without a declared order, adding (3)+(4) re-creates the drift class one layer up
# (QI-07). The law, per D-10:
#
#   * The ACTIVATION LEDGER is authoritative: the counter is a CACHE of
#     ``Σ open activations``.
#   * The SNAPSHOT is a snapshot OF the ledger, never of the raw counter — so a
#     restore can never resurrect a value the activations do not justify.
#   * The observed_usage_provider is a RECONCILIATION INPUT, not a competing
#     writer: it may only supply the observed level when activations are ABSENT,
#     and only for GAUGES (accumulators are NEVER reconciled — deltas, not levels).
#
# "deltas lie; levels heal" only works if exactly ONE level is authoritative.

def stale_open_activations(
    activations: list[Activation], *, older_than_s: float, now: Optional[datetime] = None,
) -> list[Activation]:
    """Filter OPEN activations opened more than ``older_than_s`` ago — the drift
    alarm (QB-03). An activation open far past any legitimate lifetime is either a
    missed release (the silent-drift class that shipped three times) or a genuinely
    long-lived resource; either way an operator should SEE it, not have it be
    invisible drift."""
    now = now or datetime.now(timezone.utc)
    out = []
    for a in activations:
        if a.state != ActivationState.OPEN.value:
            continue
        try:
            opened = datetime.fromisoformat(a.opened_at)
        except (ValueError, TypeError):
            continue
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        if (now - opened).total_seconds() > older_than_s:
            out.append(a)
    return out


def resolve_gauge_level(
    *,
    open_activation_sum: float,
    provider_observed: Optional[float] = None,
    activations_in_use: bool = True,
) -> tuple[float, str]:
    """DEPRECATED AS A LAW (D-35) — reduced to the MECHANISM.

    This once encoded a precedence LAW (provider wins when activations are
    absent). **D-35 removed that**: two implementations of "which source wins"
    (this + the reconciler) ARE the multi-source-of-truth hazard this ticket
    exists to kill (FUTURE §1) — they drift, and the drift is invisible because
    each is individually correct. The LAW now lives in exactly ONE place:
    ``reconcile.LibraryReconciler._resolve_existence``.

    What remains here is only the mechanism: the counter's value is the ledger's
    ``Σ open activations``. ``provider_observed`` and ``activations_in_use`` are
    retained for signature compatibility and IGNORED — the provider-vs-ledger
    decision is the reconciler's, not this function's.
    """
    return open_activation_sum, "activations"


async def converge_gauge(
    *,
    activation_store: "ActivationStore",
    org_id: str,
    resource_key: str,
    counter,
    opens: Optional[list] = None,
) -> tuple[float, str]:
    """MECHANISM (not law): set the Redis counter to ``Σ open activations`` for
    this (org, resource) GAUGE, DERIVE the per-user partitions from the ledger,
    and CLEAR stale per-user keys. It makes **no source choice** — the precedence
    LAW (provider vs ledger vs counter vs snapshot) lives in
    ``reconcile.LibraryReconciler._resolve_existence`` and nowhere else (D-35).
    The counter is a cache of ``Σ open activations`` (D-10).

    "Converge to Σ open activations" exists in exactly this one function; the
    reconciler's no-provider path and ``seed_redis`` both call it.

    GAUGES ONLY. Accumulators are never reconciled (correctness, not a knob) —
    call sites must not pass an accumulator counter here.

    ``opens``: a pre-fetched ``list_open(org_id)`` to avoid a second ledger read
    (the reconciler already has it); fetched here when ``None``.
    Returns ``(value, "activations")``.
    """
    if opens is None:
        opens = await activation_store.list_open(org_id)
    total = sum(float(a.spend.get(resource_key, 0.0)) for a in opens)
    before = await counter.get()
    await counter.reset(total)
    # P2.5: DERIVE per-user partitions from the ledger rather than storing them
    # separately. The open activations carry user_id + spend, so the per-user
    # level is Σ that user's open activations — reconstructed, never resurrected
    # from a possibly-drifted snapshot.
    if hasattr(counter, "reset_user") and hasattr(counter, "_user_key"):
        per_user: dict[str, float] = {}
        for a in opens:
            if a.user_id:
                per_user[a.user_id] = per_user.get(a.user_id, 0.0) + float(
                    a.spend.get(resource_key, 0.0))
        for uid, uval in per_user.items():
            await counter.reset_user(uid, uval)
        # CLEAR stale per-user keys — a user whose activations all closed must
        # have their partition zeroed, or per-user limits diverge from the org
        # total (QI-06). After a Redis wipe there is nothing to clear (no-op).
        redis = getattr(counter, "_redis", None)
        if redis is not None:
            try:
                prefix = counter._user_key("")   # "quota:{org}:{rk}:gauge:user:"
                async for k in redis.scan_iter(match=f"{prefix}*", count=100):
                    ks = k.decode() if isinstance(k, bytes) else str(k)
                    uid = ks[len(prefix):]
                    if uid not in per_user:
                        raw = await redis.get(ks)
                        if raw and float(raw) != 0.0:
                            await redis.delete(ks)
                            logger.warning(
                                "gauge_converge_stale_user_cleared org=%s resource=%s "
                                "user=%s previous=%s", org_id, resource_key, uid, float(raw))
            except Exception as e:
                logger.warning("gauge_converge_stale_user_scan_failed org=%s resource=%s "
                               "error=%s", org_id, resource_key, e)
    logger.info(
        "gauge_converged org=%s resource=%s previous=%s value=%s source=activations "
        "(open_activations=%d) — counter is a cache of Σ open activations (D-10); "
        "the source-precedence LAW is in reconcile._resolve_existence (D-35)",
        org_id, resource_key, before, total, len(opens),
    )
    return total, "activations"


def _is_conditional_check_failed(exc: Exception) -> bool:
    """Portable detector for a DDB conditional-write failure (botocore + stub)."""
    name = getattr(getattr(exc, "response", {}), "get", lambda *_: None)("Error") \
        if hasattr(exc, "response") else None
    if isinstance(name, dict) and name.get("Code") == "ConditionalCheckFailedException":
        return True
    return "ConditionalCheckFailed" in type(exc).__name__ or "ConditionalCheckFailed" in str(exc)
