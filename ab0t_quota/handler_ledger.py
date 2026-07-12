"""Handler ledger — observability + idempotency for auth-event handlers.

Three storage backends behind one Protocol:
  - InMemoryLedgerStore   (tests, degraded mode)
  - RedisLedgerStore      (default for bridge mode)
  - DDBLedgerStore        (default for mesh services with DDB available)

Wired in by setup_quota; consumers can pass `ledger_store=...` to override.

See tickets/20260428_idempotency_replay_framework/TICKET.md for design rationale.
"""
from __future__ import annotations

import asyncio
import dataclasses
import enum
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Optional, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class LedgerStatus(str, enum.Enum):
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"
    FAILED_PERMANENT = "failed_permanent"


@dataclasses.dataclass
class LedgerRow:
    handler_name: str
    event_id: str
    event_type: str
    status: LedgerStatus
    user_id: Optional[str] = None
    org_id: Optional[str] = None
    reason: Optional[str] = None
    side_effect_id: Optional[str] = None
    attempts: int = 1
    attempted_at: Optional[str] = None
    completed_at: Optional[str] = None
    lease_expires_at: Optional[str] = None
    error: Optional[str] = None
    event_payload: Optional[dict] = None

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "LedgerRow":
        d = dict(d)
        d["status"] = LedgerStatus(d["status"])
        return cls(**{k: v for k, v in d.items() if k in {f.name for f in dataclasses.fields(cls)}})


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

class LedgerStore(Protocol):
    """Storage contract for the handler ledger.

    Implementations must be idempotent on the conditional-write semantics:
    `record_attempt` must reject a second in-progress write for the same
    (handler, event_id) when the lease is still alive.
    """

    async def record_attempt(
        self,
        *,
        handler_name: str,
        event_id: str,
        event_type: str,
        event_payload: dict,
        user_id: Optional[str] = None,
        org_id: Optional[str] = None,
        lease_seconds: int = 60,
    ) -> "AttemptResult":
        ...

    async def record_outcome(
        self,
        *,
        handler_name: str,
        event_id: str,
        status: LedgerStatus,
        reason: Optional[str] = None,
        side_effect_id: Optional[str] = None,
        error: Optional[str] = None,
        attempts: Optional[int] = None,
    ) -> None:
        ...

    async def get_row(self, *, handler_name: str, event_id: str) -> Optional[LedgerRow]:
        ...

    async def already_done(self, *, dedup_key: str) -> bool:
        ...

    async def mark_done(
        self,
        *,
        dedup_key: str,
        source_handler: str,
        source_event_id: str,
        side_effect_id: Optional[str] = None,
    ) -> None:
        ...

    async def query_by_user(
        self,
        user_id: str,
        *,
        limit: int = 50,
        since_epoch: Optional[float] = None,
    ) -> list[LedgerRow]:
        ...

    async def query_by_status(
        self,
        status: LedgerStatus,
        *,
        limit: int = 50,
        since_epoch: Optional[float] = None,
    ) -> list[LedgerRow]:
        ...

    async def delete_user(self, user_id: str) -> int:
        """GDPR cascade. Returns number of rows deleted."""
        ...


@dataclasses.dataclass
class AttemptResult:
    """Returned by record_attempt. Tells the caller whether to run the
    handler body or short-circuit with a prior outcome."""
    proceed: bool                                # True if handler should run
    cached_row: Optional[LedgerRow] = None       # set when proceed=False (prior success/skip/fail/in-progress)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_key(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


async def _drain_stale(store, handler, now, limit: int = 100) -> int:
    """Shared stale-lease drain (QC-02/QC-03). Finds IN_PROGRESS rows whose lease
    has expired — a crashed worker that never wrote an outcome — atomically
    re-claims each (so two concurrent drainers can't double-process), and hands
    it to `handler(row)` for reprocessing. Returns the number reprocessed.

    Without this, a delivery whose first attempt crashed while auth had already
    been 200'd (redelivery skipped under the live lease) is stranded in_progress
    forever, invisible to `--status failed`. This is the drain that recovers it.
    """
    now = now or datetime.now(timezone.utc)
    reprocessed = 0
    rows = await store.query_by_status(LedgerStatus.IN_PROGRESS, limit=limit)
    for row in rows:
        if not row.lease_expires_at:
            continue
        try:
            expired = datetime.fromisoformat(row.lease_expires_at) <= now
        except (TypeError, ValueError):
            continue
        if not expired:
            continue
        # Atomic re-claim: only one drainer wins a fresh lease; others skip.
        res = await store.record_attempt(
            handler_name=row.handler_name,
            event_id=row.event_id,
            event_type=row.event_type,
            event_payload=row.event_payload or {},
            user_id=row.user_id,
            org_id=row.org_id,
        )
        if res.proceed:
            await handler(row)
            reprocessed += 1
    return reprocessed


class StaleLeaseSweeper:
    """D-50 / QC-02 — a library-owned periodic sweeper that RECLAIMS stranded
    in_progress ledger rows (a handler that crashed mid-delivery) and re-drives
    them via an injected ``redispatch`` callable.

    Why this exists: ``drain_stale_leases`` was implemented three times and its
    only caller was a test — a DISCONNECTED GUARANTEE. With no scheduler, a crashed
    handler stranded its row forever: auth already got its 200, the live lease
    blocked redelivery, and the status stayed ``in_progress`` (invisible to
    ``events --status failed``). For a credit-grant handler that means the grant is
    stranded permanently. This runs the drain on a real periodic loop under the
    D-50 contract: loud on sustained failure, liveness surfaced, money-critical
    (a dead sweeper fails ``/quota/health``).

    Generic: it takes a store + a ``redispatch(row)`` callable, so it carries no
    auth_events dependency (setup injects the re-dispatcher)."""

    _UNHEALTHY_STREAK = 3

    def __init__(self, store, redispatch, *, interval_seconds: float = 300.0, limit: int = 100):
        self._store = store
        self._redispatch = redispatch          # async fn(row) -> None
        self._interval = interval_seconds
        self._limit = limit
        self._task = None
        self._fail_streak = 0
        self._ever_started = False

    @staticmethod
    def _kill_switched() -> bool:
        import os
        return os.getenv("AB0T_QUOTA_STALE_LEASE_SWEEP_ENABLED", "true").strip().lower() in (
            "false", "0", "no", "off")

    async def sweep_once(self) -> int:
        """One sweep pass — reclaim + re-drive every expired-lease row. Exposed for
        a manual cron or a test; the background loop calls it."""
        return await self._store.drain_stale_leases(handler=self._redispatch, limit=self._limit)

    def start(self, interval_seconds: Optional[float] = None):
        import asyncio
        if self._task is not None and not self._task.done():
            return self._task
        self._ever_started = True
        interval = interval_seconds if interval_seconds is not None else self._interval
        self._task = asyncio.create_task(self._loop(interval), name="ab0t_quota_stale_lease_sweeper")
        logger.info("stale_lease_sweeper_started interval=%ss", interval)
        return self._task

    async def stop(self):
        import asyncio
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        logger.info("stale_lease_sweeper_stopped")

    async def _loop(self, interval: float):
        import asyncio
        self._fail_streak = 0
        while True:
            try:
                await asyncio.sleep(interval)
                if self._kill_switched():
                    continue
                n = await self.sweep_once()
                self._fail_streak = 0
                if n:
                    logger.info("stale_lease_sweep reprocessed=%d", n)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._fail_streak = min(self._fail_streak + 1, 5)
                # D-50 rule 2: a sweeper backing off forever is a dead worker —
                # stranded grants pile up silently. Be LOUD at the unhealthy streak.
                if self._fail_streak >= self._UNHEALTHY_STREAK:
                    logger.error("stale_lease_sweeper_UNHEALTHY: %s (fail_streak=%d) — stranded "
                                 "handler rows (e.g. credit grants) are NOT being recovered "
                                 "(QC-02). This must reach a human.", e, self._fail_streak)
                else:
                    logger.warning("stale_lease_sweep_error: %s (backoff x%d)", e, self._fail_streak)
                await asyncio.sleep(min(interval * self._fail_streak, 120))

    def loop_liveness(self) -> tuple:
        """D-50 liveness. Healthy when not managed here (manual sweep) or running
        below the unhealthy streak; unhealthy when a started sweeper died/stopped
        or is permanently backing off."""
        if not self._ever_started:
            return True, "stale-lease sweeper not managed here (manual sweep)"
        task = self._task
        if task is None or task.done():
            return False, "stale-lease sweeper was started but is no longer running"
        if self._fail_streak >= self._UNHEALTHY_STREAK:
            return False, (f"stale-lease sweeper backing off (fail_streak={self._fail_streak}) — "
                           f"stranded grants not recovered")
        return True, "on"


def _is_conditional_check_failed(exc: Exception) -> bool:
    """Portable detector for a DynamoDB conditional-put failure across the real
    boto client (botocore ClientError with code ConditionalCheckFailedException)
    and the in-test FakeDDB stub (an exception whose class name carries it)."""
    if type(exc).__name__ == "ConditionalCheckFailedException":
        return True
    if "ConditionalCheckFailed" in type(exc).__name__:
        return True
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        if code == "ConditionalCheckFailedException":
            return True
    return False


# ---------------------------------------------------------------------------
# In-memory implementation (tests + degraded mode)
# ---------------------------------------------------------------------------

class InMemoryLedgerStore:
    """Process-local dict. Loses everything on restart. Tests + fail-safe."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], LedgerRow] = {}      # (handler, event_id) -> row
        self._bizdedup: dict[str, dict] = {}                   # hash -> {raw_key, ...}
        self._lock = asyncio.Lock()

    async def record_attempt(self, **kwargs) -> AttemptResult:
        async with self._lock:
            key = (kwargs["handler_name"], kwargs["event_id"])
            existing = self._rows.get(key)
            if existing is not None:
                # Terminal states short-circuit
                if existing.status in (LedgerStatus.SUCCESS, LedgerStatus.SKIPPED, LedgerStatus.FAILED_PERMANENT):
                    return AttemptResult(proceed=False, cached_row=existing)
                # in_progress with live lease → conflict
                if existing.status == LedgerStatus.IN_PROGRESS and existing.lease_expires_at:
                    if datetime.fromisoformat(existing.lease_expires_at) > datetime.now(timezone.utc):
                        return AttemptResult(proceed=False, cached_row=existing)
                # Otherwise (failed-retryable, stale lease) → overwrite with new attempt
                attempts = (existing.attempts or 0) + 1
            else:
                attempts = 1

            from datetime import timedelta
            lease_exp = (datetime.now(timezone.utc) + timedelta(seconds=kwargs.get("lease_seconds", 60))).isoformat()
            row = LedgerRow(
                handler_name=kwargs["handler_name"],
                event_id=kwargs["event_id"],
                event_type=kwargs["event_type"],
                status=LedgerStatus.IN_PROGRESS,
                user_id=kwargs.get("user_id"),
                org_id=kwargs.get("org_id"),
                attempts=attempts,
                attempted_at=_now_iso(),
                lease_expires_at=lease_exp,
                event_payload=kwargs["event_payload"],
            )
            self._rows[key] = row
            return AttemptResult(proceed=True)

    async def record_outcome(self, **kwargs) -> None:
        async with self._lock:
            key = (kwargs["handler_name"], kwargs["event_id"])
            row = self._rows.get(key)
            if row is None:
                logger.warning("record_outcome: no in_progress row for %s", key)
                return
            row.status = kwargs["status"]
            row.reason = kwargs.get("reason")
            row.side_effect_id = kwargs.get("side_effect_id")
            row.error = kwargs.get("error")
            if kwargs.get("attempts") is not None:
                row.attempts = kwargs["attempts"]
            row.completed_at = _now_iso()
            row.lease_expires_at = None

    async def get_row(self, *, handler_name: str, event_id: str) -> Optional[LedgerRow]:
        return self._rows.get((handler_name, event_id))

    async def already_done(self, *, dedup_key: str) -> bool:
        return _hash_key(dedup_key) in self._bizdedup

    async def mark_done(self, **kwargs) -> None:
        h = _hash_key(kwargs["dedup_key"])
        self._bizdedup[h] = {
            "raw_key": kwargs["dedup_key"],
            "source_handler": kwargs["source_handler"],
            "source_event_id": kwargs["source_event_id"],
            "side_effect_id": kwargs.get("side_effect_id"),
            "marked_at": _now_iso(),
        }

    async def query_by_user(self, user_id, *, limit=50, since_epoch=None) -> list[LedgerRow]:
        rows = [r for r in self._rows.values() if r.user_id == user_id]
        if since_epoch is not None:
            rows = [r for r in rows if r.attempted_at and datetime.fromisoformat(r.attempted_at).timestamp() >= since_epoch]
        rows.sort(key=lambda r: r.attempted_at or "", reverse=True)
        return rows[:limit]

    async def query_by_status(self, status, *, limit=50, since_epoch=None) -> list[LedgerRow]:
        rows = [r for r in self._rows.values() if r.status == status]
        if since_epoch is not None:
            rows = [r for r in rows if r.attempted_at and datetime.fromisoformat(r.attempted_at).timestamp() >= since_epoch]
        rows.sort(key=lambda r: r.attempted_at or "", reverse=True)
        return rows[:limit]

    async def delete_user(self, user_id: str) -> int:
        keys = [k for k, r in self._rows.items() if r.user_id == user_id]
        for k in keys:
            del self._rows[k]
        return len(keys)

    async def drain_stale_leases(self, *, handler, now=None, limit: int = 100) -> int:
        return await _drain_stale(self, handler, now, limit)


# ---------------------------------------------------------------------------
# Redis implementation (default for bridge mode, default when DDB absent)
# ---------------------------------------------------------------------------

class RedisLedgerStore:
    """Redis-backed ledger. 72h TTL on rows — replay window, not audit log.

    Keys:
      ledger:row:{handler}:{event_id}         -> JSON LedgerRow
      ledger:by_user:{user_id}                -> sorted set (score=epoch, member=row_key)
      ledger:by_status:{status}               -> sorted set (score=epoch, member=row_key)
      ledger:bizdedup:{sha256(key)}           -> JSON business dedup row

    Concurrent attempts arbitrated via SET NX on the in_progress row.
    """

    ROW_TTL_SECONDS = 72 * 3600

    # Compare-and-swap for the stale-lease RECLAIM path (QC-01 / R3): overwrite
    # the row ONLY if it still holds exactly the stale value we read. Two drains
    # racing to reclaim the same stale lease: the first CAS matches and wins; the
    # second sees the winner's new row and returns 0 (→ caller re-reads, sees a
    # live lease, skips). Returns 1 on success, 0 on loss.
    _CAS_RECLAIM = (
        "if redis.call('GET', KEYS[1]) == ARGV[1] then "
        "redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3]); return 1 "
        "else return 0 end"
    )

    def __init__(self, redis, *, key_prefix: str = "ledger") -> None:
        self.redis = redis
        self.prefix = key_prefix

    def _row_key(self, handler: str, event_id: str) -> str:
        return f"{self.prefix}:row:{handler}:{event_id}"

    def _user_key(self, user_id: str) -> str:
        return f"{self.prefix}:by_user:{user_id}"

    def _status_key(self, status: LedgerStatus) -> str:
        return f"{self.prefix}:by_status:{status.value}"

    def _dedup_key(self, raw: str) -> str:
        return f"{self.prefix}:bizdedup:{_hash_key(raw)}"

    def _build_row(self, kwargs, attempts: int) -> "LedgerRow":
        from datetime import timedelta
        lease_exp = (datetime.now(timezone.utc) + timedelta(seconds=kwargs.get("lease_seconds", 60))).isoformat()
        return LedgerRow(
            handler_name=kwargs["handler_name"],
            event_id=kwargs["event_id"],
            event_type=kwargs["event_type"],
            status=LedgerStatus.IN_PROGRESS,
            user_id=kwargs.get("user_id"),
            org_id=kwargs.get("org_id"),
            attempts=attempts,
            attempted_at=_now_iso(),
            lease_expires_at=lease_exp,
            event_payload=kwargs["event_payload"],
        )

    async def _index_in_progress(self, row: "LedgerRow", row_key: str) -> None:
        score = time.time()
        if row.user_id:
            await self.redis.zadd(self._user_key(row.user_id), {row_key: score})
            await self.redis.expire(self._user_key(row.user_id), self.ROW_TTL_SECONDS)
        await self.redis.zadd(self._status_key(LedgerStatus.IN_PROGRESS), {row_key: score})
        await self.redis.expire(self._status_key(LedgerStatus.IN_PROGRESS), self.ROW_TTL_SECONDS)

    @staticmethod
    def _decide_existing(existing: "LedgerRow") -> Optional[AttemptResult]:
        """Return a short-circuit AttemptResult for an existing row, or None if
        the caller may (re)claim it (stale in_progress / failed-retryable)."""
        if existing.status in (LedgerStatus.SUCCESS, LedgerStatus.SKIPPED, LedgerStatus.FAILED_PERMANENT):
            return AttemptResult(proceed=False, cached_row=existing)
        if existing.status == LedgerStatus.IN_PROGRESS and existing.lease_expires_at:
            if datetime.fromisoformat(existing.lease_expires_at) > datetime.now(timezone.utc):
                return AttemptResult(proceed=False, cached_row=existing)
        return None

    async def record_attempt(self, **kwargs) -> AttemptResult:
        """Atomic claim (QC-01). The create case — the race where two concurrent
        deliveries both read no row and both proceed — is closed with an atomic
        `SET NX`: exactly one creator wins the key; the loser re-reads and sees
        the winner's live-lease row (proceed=False). Existing rows keep the
        state-machine semantics (terminal → cached; live lease → conflict; stale
        / failed-retryable → re-claim)."""
        row_key = self._row_key(kwargs["handler_name"], kwargs["event_id"])
        existing_raw = await self.redis.get(row_key)

        if existing_raw is None:
            # --- create race: atomic claim. Only one of N concurrent creators wins.
            row = self._build_row(kwargs, attempts=1)
            claimed = await self.redis.set(
                row_key, json.dumps(row.to_dict()), ex=self.ROW_TTL_SECONDS, nx=True,
            )
            if claimed:
                await self._index_in_progress(row, row_key)
                return AttemptResult(proceed=True)
            # Lost the create race — fall through to read the winner's row.
            existing_raw = await self.redis.get(row_key)
            if existing_raw is None:
                # Evicted in the tiny window; the safe answer is "someone else has it".
                return AttemptResult(proceed=False, cached_row=None)

        existing = LedgerRow.from_dict(json.loads(existing_raw))
        short = self._decide_existing(existing)
        if short is not None:
            return short

        # Stale in_progress or failed-retryable → re-claim via COMPARE-AND-SWAP
        # (R3): overwrite only if the row still holds the stale value we read, so
        # two concurrent drains can't both reclaim the same lease.
        row = self._build_row(kwargs, attempts=(existing.attempts or 0) + 1)
        won = await self.redis.eval(
            self._CAS_RECLAIM, 1,
            row_key, existing_raw, json.dumps(row.to_dict()), self.ROW_TTL_SECONDS,
        )
        if won in (1, b"1", "1", True):
            await self._index_in_progress(row, row_key)
            return AttemptResult(proceed=True)
        # Lost the reclaim race — re-read and defer to the winner's row.
        existing_raw = await self.redis.get(row_key)
        if existing_raw is None:
            return AttemptResult(proceed=False, cached_row=None)
        existing = LedgerRow.from_dict(json.loads(existing_raw))
        return self._decide_existing(existing) or AttemptResult(proceed=False, cached_row=existing)

    async def record_outcome(self, **kwargs) -> None:
        row_key = self._row_key(kwargs["handler_name"], kwargs["event_id"])
        existing_raw = await self.redis.get(row_key)
        if existing_raw is None:
            logger.warning("record_outcome: no row at %s", row_key)
            return
        row = LedgerRow.from_dict(json.loads(existing_raw))
        old_status = row.status
        row.status = kwargs["status"]
        row.reason = kwargs.get("reason")
        row.side_effect_id = kwargs.get("side_effect_id")
        row.error = kwargs.get("error")
        if kwargs.get("attempts") is not None:
            row.attempts = kwargs["attempts"]
        row.completed_at = _now_iso()
        row.lease_expires_at = None
        await self.redis.set(row_key, json.dumps(row.to_dict()), ex=self.ROW_TTL_SECONDS)
        # Move between status sorted sets
        await self.redis.zrem(self._status_key(old_status), row_key)
        await self.redis.zadd(self._status_key(row.status), {row_key: time.time()})
        await self.redis.expire(self._status_key(row.status), self.ROW_TTL_SECONDS)

    async def get_row(self, *, handler_name, event_id) -> Optional[LedgerRow]:
        raw = await self.redis.get(self._row_key(handler_name, event_id))
        return LedgerRow.from_dict(json.loads(raw)) if raw else None

    async def already_done(self, *, dedup_key: str) -> bool:
        return await self.redis.exists(self._dedup_key(dedup_key)) > 0

    async def mark_done(self, **kwargs) -> None:
        payload = {
            "raw_key": kwargs["dedup_key"],
            "source_handler": kwargs["source_handler"],
            "source_event_id": kwargs["source_event_id"],
            "side_effect_id": kwargs.get("side_effect_id"),
            "marked_at": _now_iso(),
        }
        # No TTL — promotional credits don't expire. Operator clears via CLI if needed.
        await self.redis.set(self._dedup_key(kwargs["dedup_key"]), json.dumps(payload))

    async def query_by_user(self, user_id, *, limit=50, since_epoch=None) -> list[LedgerRow]:
        min_score = since_epoch if since_epoch is not None else "-inf"
        keys = await self.redis.zrevrangebyscore(self._user_key(user_id), "+inf", min_score, start=0, num=limit)
        return await self._fetch_rows(keys)

    async def query_by_status(self, status, *, limit=50, since_epoch=None) -> list[LedgerRow]:
        min_score = since_epoch if since_epoch is not None else "-inf"
        keys = await self.redis.zrevrangebyscore(self._status_key(status), "+inf", min_score, start=0, num=limit)
        return await self._fetch_rows(keys)

    async def _fetch_rows(self, keys: Iterable) -> list[LedgerRow]:
        rows: list[LedgerRow] = []
        for k in keys:
            key_str = k.decode("utf-8") if isinstance(k, (bytes, bytearray)) else k
            raw = await self.redis.get(key_str)
            if raw is not None:
                rows.append(LedgerRow.from_dict(json.loads(raw)))
        return rows

    async def delete_user(self, user_id: str) -> int:
        user_idx = self._user_key(user_id)
        keys = await self.redis.zrange(user_idx, 0, -1)
        count = 0
        for k in keys:
            key_str = k.decode("utf-8") if isinstance(k, (bytes, bytearray)) else k
            raw = await self.redis.get(key_str)
            if raw:
                row = LedgerRow.from_dict(json.loads(raw))
                await self.redis.zrem(self._status_key(row.status), key_str)
            await self.redis.delete(key_str)
            count += 1
        await self.redis.delete(user_idx)
        return count

    async def drain_stale_leases(self, *, handler, now=None, limit: int = 100) -> int:
        return await _drain_stale(self, handler, now, limit)


# ---------------------------------------------------------------------------
# DDB implementation (default for mesh services)
# ---------------------------------------------------------------------------

class DDBLedgerStore:
    """DynamoDB-backed ledger. 90-day TTL. Persistent + GSI-queryable.

    Table: ab0t_quota_handler_ledger
      PK = HANDLER#{handler}#{event_id}
      SK = META
      GSI1: PK = USER#{user_id}, SK = attempted_at
      GSI2: PK = STATUS#{status}, SK = attempted_at
    """

    ROW_TTL_DAYS = 90

    def __init__(self, ddb_client, *, table_name: str = "ab0t_quota_handler_ledger") -> None:
        self.ddb = ddb_client
        self.table = table_name

    async def ensure_table(self, *, active_timeout_s: float = 60.0) -> None:
        """D-82 — CREATE the table if absent (idempotent), then WAIT for it to be ACTIVE.

        This store previously ASSUMED its table existed. The outbox and the activation store
        both provision theirs; the handler ledger did neither, so a client who wired it hit a
        `ResourceNotFoundException` at their FIRST auth webhook — in production. It was
        invisible for the most instructive reason (D-78): *a fake never notices, because a
        fake creates nothing.*

        Also enables TTL on `ttl` — the attribute this store actually writes — so the D-76
        preflight (which FAILS a table whose TTL points anywhere else) passes on a table we
        provisioned ourselves.
        """
        import asyncio
        import time as _t

        try:
            await self.ddb.describe_table(TableName=self.table)
        except Exception as e:
            not_found = getattr(getattr(self.ddb, "exceptions", None),
                                "ResourceNotFoundException", ())
            if not (isinstance(e, not_found) or "ResourceNotFound" in type(e).__name__):
                raise  # a real error (perms, endpoint) — don't mask it
            await self.ddb.create_table(
                TableName=self.table,
                KeySchema=[
                    {"AttributeName": "PK", "KeyType": "HASH"},
                    {"AttributeName": "SK", "KeyType": "RANGE"},
                ],
                AttributeDefinitions=[
                    {"AttributeName": "PK", "AttributeType": "S"},
                    {"AttributeName": "SK", "AttributeType": "S"},
                ],
                BillingMode="PAY_PER_REQUEST",
            )
            logger.info("created handler-ledger table %s", self.table)

        deadline = _t.monotonic() + active_timeout_s
        while True:
            desc = await self.ddb.describe_table(TableName=self.table)
            if desc.get("Table", {}).get("TableStatus") == "ACTIVE":
                break
            if _t.monotonic() >= deadline:
                raise RuntimeError(
                    f"handler-ledger table {self.table} did not become ACTIVE within "
                    f"{active_timeout_s}s — refusing to run the idempotency ledger against a "
                    f"table that is not ready (D-82)")
            await asyncio.sleep(0.5)

        # TTL on the attribute this store writes (`ttl`). Best-effort: a client whose IAM
        # forbids UpdateTimeToLive still gets a working ledger — the D-76 preflight will WARN
        # that rows never reap (growth/cost), which is not a correctness leak.
        try:
            cur = await self.ddb.describe_time_to_live(TableName=self.table)
            if cur.get("TimeToLiveDescription", {}).get("TimeToLiveStatus") not in (
                    "ENABLED", "ENABLING"):
                await self.ddb.update_time_to_live(
                    TableName=self.table,
                    TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"})
        except Exception as e:
            logger.warning("handler-ledger TTL not enabled on %s (%s) — released rows will not "
                           "reap; nothing is lost (D-76 will WARN)", self.table, e)

    def _pk(self, handler: str, event_id: str) -> str:
        return f"HANDLER#{handler}#{event_id}"

    def _ttl_epoch(self) -> int:
        return int(time.time()) + (self.ROW_TTL_DAYS * 86400)

    @staticmethod
    def _str(v: Optional[str]) -> dict:
        return {"S": v} if v else {"NULL": True}

    @staticmethod
    def _num(v: Optional[float]) -> dict:
        return {"N": str(v)} if v is not None else {"NULL": True}

    def _build_item(self, kwargs, attempts: int) -> dict:
        from datetime import timedelta
        lease_exp = (datetime.now(timezone.utc) + timedelta(seconds=kwargs.get("lease_seconds", 60))).isoformat()
        item = {
            "PK": {"S": self._pk(kwargs["handler_name"], kwargs["event_id"])},
            "SK": {"S": "META"},
            "handler_name": {"S": kwargs["handler_name"]},
            "event_id": {"S": kwargs["event_id"]},
            "event_type": {"S": kwargs["event_type"]},
            "status": {"S": LedgerStatus.IN_PROGRESS.value},
            "attempts": {"N": str(attempts)},
            "attempted_at": {"S": _now_iso()},
            "lease_expires_at": {"S": lease_exp},
            "event_payload": {"S": json.dumps(kwargs["event_payload"])},
            "ttl": {"N": str(self._ttl_epoch())},
        }
        if kwargs.get("user_id"):
            item["user_id"] = {"S": kwargs["user_id"]}
            item["gsi1_pk"] = {"S": f"USER#{kwargs['user_id']}"}
            item["gsi1_sk"] = {"S": item["attempted_at"]["S"]}
        if kwargs.get("org_id"):
            item["org_id"] = {"S": kwargs["org_id"]}
        item["gsi2_pk"] = {"S": f"STATUS#{LedgerStatus.IN_PROGRESS.value}"}
        item["gsi2_sk"] = {"S": item["attempted_at"]["S"]}
        return item

    @staticmethod
    def _decide_existing(existing: "LedgerRow") -> Optional[AttemptResult]:
        if existing.status in (LedgerStatus.SUCCESS, LedgerStatus.SKIPPED, LedgerStatus.FAILED_PERMANENT):
            return AttemptResult(proceed=False, cached_row=existing)
        if existing.status == LedgerStatus.IN_PROGRESS and existing.lease_expires_at:
            if datetime.fromisoformat(existing.lease_expires_at) > datetime.now(timezone.utc):
                return AttemptResult(proceed=False, cached_row=existing)
        return None

    async def record_attempt(self, **kwargs) -> AttemptResult:
        """Atomic claim via a conditional put (QC-01). The create race is closed
        with `ConditionExpression=attribute_not_exists(PK)`: exactly one concurrent
        creator's put succeeds; the loser gets ConditionalCheckFailed, re-reads,
        and sees the winner's live-lease row (proceed=False). Existing rows keep
        the state-machine semantics."""
        existing = await self.get_row(handler_name=kwargs["handler_name"], event_id=kwargs["event_id"])

        if existing is None:
            item = self._build_item(kwargs, attempts=1)
            try:
                await self.ddb.put_item(
                    TableName=self.table, Item=item,
                    ConditionExpression="attribute_not_exists(PK)",
                )
                return AttemptResult(proceed=True)
            except Exception as e:
                if not _is_conditional_check_failed(e):
                    raise
                # Lost the create race — read the winner's row and decide.
                existing = await self.get_row(
                    handler_name=kwargs["handler_name"], event_id=kwargs["event_id"],
                )
                if existing is None:
                    return AttemptResult(proceed=False, cached_row=None)

        short = self._decide_existing(existing)
        if short is not None:
            return short

        # Stale in_progress / failed-retryable → re-claim (unconditional overwrite).
        item = self._build_item(kwargs, attempts=(existing.attempts or 0) + 1)
        await self.ddb.put_item(TableName=self.table, Item=item)
        return AttemptResult(proceed=True)

    async def record_outcome(self, **kwargs) -> None:
        new_status = kwargs["status"]
        expr_attrs = {
            ":st": {"S": new_status.value},
            ":ca": {"S": _now_iso()},
            ":gs2pk": {"S": f"STATUS#{new_status.value}"},
        }
        sets = ["#st = :st", "completed_at = :ca", "gsi2_pk = :gs2pk", "gsi2_sk = :ca"]
        remove = ["lease_expires_at"]
        names = {"#st": "status"}
        if kwargs.get("reason") is not None:
            sets.append("reason = :rs"); expr_attrs[":rs"] = {"S": kwargs["reason"]}
        if kwargs.get("side_effect_id") is not None:
            sets.append("side_effect_id = :sei"); expr_attrs[":sei"] = {"S": kwargs["side_effect_id"]}
        if kwargs.get("error") is not None:
            sets.append("#err = :er"); expr_attrs[":er"] = {"S": kwargs["error"]}; names["#err"] = "error"
        if kwargs.get("attempts") is not None:
            sets.append("attempts = :att"); expr_attrs[":att"] = {"N": str(kwargs["attempts"])}

        update_expr = "SET " + ", ".join(sets) + " REMOVE " + ", ".join(remove)
        await self.ddb.update_item(
            TableName=self.table,
            Key={"PK": {"S": self._pk(kwargs["handler_name"], kwargs["event_id"])}, "SK": {"S": "META"}},
            UpdateExpression=update_expr,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=expr_attrs,
        )

    async def get_row(self, *, handler_name, event_id) -> Optional[LedgerRow]:
        res = await self.ddb.get_item(
            TableName=self.table,
            Key={"PK": {"S": self._pk(handler_name, event_id)}, "SK": {"S": "META"}},
        )
        item = res.get("Item")
        return self._row_from_item(item) if item else None

    async def already_done(self, *, dedup_key: str) -> bool:
        res = await self.ddb.get_item(
            TableName=self.table,
            Key={"PK": {"S": f"BIZDEDUP#{_hash_key(dedup_key)}"}, "SK": {"S": "META"}},
        )
        return "Item" in res

    async def mark_done(self, **kwargs) -> None:
        item = {
            "PK": {"S": f"BIZDEDUP#{_hash_key(kwargs['dedup_key'])}"},
            "SK": {"S": "META"},
            "raw_key": {"S": kwargs["dedup_key"]},
            "source_handler": {"S": kwargs["source_handler"]},
            "source_event_id": {"S": kwargs["source_event_id"]},
            "marked_at": {"S": _now_iso()},
        }
        if kwargs.get("side_effect_id"):
            item["side_effect_id"] = {"S": kwargs["side_effect_id"]}
        await self.ddb.put_item(TableName=self.table, Item=item)

    async def query_by_user(self, user_id, *, limit=50, since_epoch=None) -> list[LedgerRow]:
        kwargs = {
            "TableName": self.table,
            "IndexName": "gsi1",
            "KeyConditionExpression": "gsi1_pk = :pk",
            "ExpressionAttributeValues": {":pk": {"S": f"USER#{user_id}"}},
            "Limit": limit,
            "ScanIndexForward": False,
        }
        if since_epoch is not None:
            kwargs["KeyConditionExpression"] += " AND gsi1_sk >= :sk"
            kwargs["ExpressionAttributeValues"][":sk"] = {"S": datetime.fromtimestamp(since_epoch, tz=timezone.utc).isoformat()}
        res = await self.ddb.query(**kwargs)
        return [self._row_from_item(i) for i in res.get("Items", [])]

    async def query_by_status(self, status, *, limit=50, since_epoch=None) -> list[LedgerRow]:
        kwargs = {
            "TableName": self.table,
            "IndexName": "gsi2",
            "KeyConditionExpression": "gsi2_pk = :pk",
            "ExpressionAttributeValues": {":pk": {"S": f"STATUS#{status.value}"}},
            "Limit": limit,
            "ScanIndexForward": False,
        }
        if since_epoch is not None:
            kwargs["KeyConditionExpression"] += " AND gsi2_sk >= :sk"
            kwargs["ExpressionAttributeValues"][":sk"] = {"S": datetime.fromtimestamp(since_epoch, tz=timezone.utc).isoformat()}
        res = await self.ddb.query(**kwargs)
        return [self._row_from_item(i) for i in res.get("Items", [])]

    async def delete_user(self, user_id: str) -> int:
        # Page through GSI1 and delete each row's PK/SK
        count = 0
        last_key = None
        while True:
            kwargs = {
                "TableName": self.table,
                "IndexName": "gsi1",
                "KeyConditionExpression": "gsi1_pk = :pk",
                "ExpressionAttributeValues": {":pk": {"S": f"USER#{user_id}"}},
                "ProjectionExpression": "PK, SK",
            }
            if last_key:
                kwargs["ExclusiveStartKey"] = last_key
            res = await self.ddb.query(**kwargs)
            for item in res.get("Items", []):
                await self.ddb.delete_item(
                    TableName=self.table,
                    Key={"PK": item["PK"], "SK": item["SK"]},
                )
                count += 1
            last_key = res.get("LastEvaluatedKey")
            if not last_key:
                break
        return count

    async def drain_stale_leases(self, *, handler, now=None, limit: int = 100) -> int:
        return await _drain_stale(self, handler, now, limit)

    @staticmethod
    def _row_from_item(item: dict) -> LedgerRow:
        def _s(field):
            v = item.get(field)
            return v["S"] if v and "S" in v else None
        def _n(field):
            v = item.get(field)
            return int(v["N"]) if v and "N" in v else None
        payload_raw = _s("event_payload")
        return LedgerRow(
            handler_name=_s("handler_name") or "",
            event_id=_s("event_id") or "",
            event_type=_s("event_type") or "",
            status=LedgerStatus(_s("status") or "in_progress"),
            user_id=_s("user_id"),
            org_id=_s("org_id"),
            reason=_s("reason"),
            side_effect_id=_s("side_effect_id"),
            attempts=_n("attempts") or 1,
            attempted_at=_s("attempted_at"),
            completed_at=_s("completed_at"),
            lease_expires_at=_s("lease_expires_at"),
            error=_s("error"),
            event_payload=json.loads(payload_raw) if payload_raw else None,
        )


# ---------------------------------------------------------------------------
# HandlerContext + @idempotent decorator
# ---------------------------------------------------------------------------

class SkipOutcome:
    """Sentinel return: lib records status=skipped with reason."""
    __slots__ = ("reason",)
    def __init__(self, reason: str): self.reason = reason


class SuccessOutcome:
    """Sentinel return: lib records status=success with side_effect_id."""
    __slots__ = ("side_effect_id",)
    def __init__(self, side_effect_id: Optional[str] = None): self.side_effect_id = side_effect_id


@dataclasses.dataclass
class HandlerContext:
    handler_name: str
    event_id: str
    event_type: str
    event_payload: dict
    ledger: Any                        # LedgerStore
    _dedup_key: Optional[str] = None   # filled by @idempotent from the `key` callable

    async def already_done(self) -> bool:
        if self._dedup_key is None:
            return False
        return await self.ledger.already_done(dedup_key=self._dedup_key)

    async def mark_done(self, *, side_effect_id: Optional[str] = None) -> None:
        if self._dedup_key is None:
            return
        await self.ledger.mark_done(
            dedup_key=self._dedup_key,
            source_handler=self.handler_name,
            source_event_id=self.event_id,
            side_effect_id=side_effect_id,
        )

    def skip(self, reason: str) -> SkipOutcome:
        return SkipOutcome(reason)

    def success(self, *, side_effect_id: Optional[str] = None) -> SuccessOutcome:
        return SuccessOutcome(side_effect_id)


DEFAULT_RETRY = {"attempts": 3, "backoff": "exponential", "initial_seconds": 1.0, "max_seconds": 30.0}


def idempotent(
    *,
    handler: str,
    key: Optional[Callable[[dict], str]] = None,
    retry: Any = None,
    lease_seconds: int = 60,
):
    """Decorator: wrap an auth-event handler with delivery dedup, business
    dedup (if `key` provided), ledger persistence, and auto-retry.

    Marks the handler with metadata that auth_events.make_router reads at
    dispatch time. The wrapped function still accepts (event, ctx).

    Args:
      handler: stable name for the handler (used as part of ledger PK).
      key: optional fn(event_dict) -> str. Used for business dedup. If
           omitted, only delivery dedup applies.
      retry: dict of {attempts, backoff, initial_seconds, max_seconds}.
             False to disable. None = default 3/exponential/1s/30s.
      lease_seconds: in-progress lease for concurrent-worker arbitration.
    """
    if retry is False:
        retry_cfg = None
    elif retry is None:
        retry_cfg = DEFAULT_RETRY
    else:
        retry_cfg = {**DEFAULT_RETRY, **retry}

    def _decorator(fn: Callable[[dict, HandlerContext], Awaitable[Any]]):
        fn._ab0t_idempotent = True              # type: ignore[attr-defined]
        fn._ab0t_idempotent_config = {           # type: ignore[attr-defined]
            "handler_name": handler,
            "key_fn": key,
            "retry": retry_cfg,
            "lease_seconds": lease_seconds,
        }
        return fn

    return _decorator


def is_idempotent_handler(fn) -> bool:
    return getattr(fn, "_ab0t_idempotent", False) is True


def idempotent_config(fn) -> dict:
    return getattr(fn, "_ab0t_idempotent_config", {})


# ---------------------------------------------------------------------------
# Auto-select store
# ---------------------------------------------------------------------------

def auto_select_store(*, redis=None, ddb_client=None) -> LedgerStore:
    """Return the best LedgerStore for the available infra. Mesh services
    get DDB; bridge clients get Redis; fallback is InMemory (logged loudly)."""
    if ddb_client is not None:
        logger.info("handler ledger: using DDB backend")
        return DDBLedgerStore(ddb_client)
    if redis is not None:
        logger.info("handler ledger: using Redis backend (72h TTL)")
        return RedisLedgerStore(redis)
    logger.warning("handler ledger: NO PERSISTENT STORE — falling back to InMemoryLedgerStore. "
                   "Ledger rows will be lost on restart. Provide redis or ddb_client to fix.")
    return InMemoryLedgerStore()
