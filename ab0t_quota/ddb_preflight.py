"""D-76 — the DynamoDB preflight. Redis is checked five ways; DDB was checked ZERO.

The activation ledger (authoritative for identity + cost, D-33) **and** the billing outbox
(money events nothing can reconstruct) both live in DynamoDB. It is **more** load-bearing
than Redis, and until now the library asserted nothing about it at all — it simply assumed
the table was there, the index was usable, the TTL was pointed at the right attribute, and
somebody had turned on backups.

Four checks, and their severities are NOT the same (a guard that cries wolf gets routed
around — D-49's false-503 lesson):

| Finding | Why | Consequence |
|---|---|---|
| table missing / not ACTIVE | nothing works | **FATAL** |
| a GSI not ACTIVE | D-32's own finding: real DynamoDB backfills a GSI **asynchronously**, and `list_pending` against a `CREATING` index silently misses rows — money events that exist and are never drained (DDB Local makes it immediate, so no DDB-Local test catches it) | **FATAL** |
| TTL enabled on an attribute we do NOT write | DynamoDB may **delete rows we never marked** — including OPEN activations, the ledger that is authoritative for identity and cost | **FATAL** |
| TTL disabled | released rows never reap: unbounded growth and cost, but **nothing is lost** | **WARN** (degrade) |
| PITR disabled | a money store with no point-in-time recovery | **FATAL** (waivable on the record) |
| PITR unanswerable | **DynamoDB Local cannot answer `DescribeContinuousBackups` at all** (`UnknownOperationException`) — PITR is the one thing ONLY real AWS can confirm | **FATAL unless the operator asserts** `storage.ddb_pitr_confirmed` — D-32's shape: an absent signal needs an assertion on the record, never a silent assumption |

D-75 applies here too: these are re-verified on the reconciler's interval, and a
safe→unsafe transition at runtime is **loud, not fatal** (degrade + alert), never a crash.
"""
from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

#: Env equivalent of `storage.ddb_pitr_confirmed` (config wins).
PITR_CONFIRM_ENV = "AB0T_QUOTA_DDB_PITR_CONFIRMED"


class DDBPreflightError(RuntimeError):
    """Startup refusal: a DynamoDB table the library depends on is unsafe (D-76)."""

    def __init__(self, store: str, detail: str):
        super().__init__(
            f"ab0t-quota cannot use its {store} DynamoDB table: {detail}. This table holds "
            f"state the library cannot reconstruct (the activation ledger is authoritative for "
            f"identity and cost; the outbox holds money events). Remedy: fix the table "
            f"configuration named above, or — for point-in-time recovery on a control plane "
            f"that cannot report it (DynamoDB Local, some emulators) — put the assertion on the "
            f"record with storage.ddb_pitr_confirmed: true (env: {PITR_CONFIRM_ENV}=true)."
        )
        self.store = store
        self.detail = detail


def pitr_confirmed_from(config: dict) -> bool:
    """The operator's on-the-record assertion. A positive act, never a default."""
    storage = (config or {}).get("storage", {}) or {}
    if "ddb_pitr_confirmed" in storage:
        return bool(storage["ddb_pitr_confirmed"])
    return os.getenv(PITR_CONFIRM_ENV, "").strip().lower() in ("1", "true", "yes")


async def verify_ddb_table(
    client,
    table: str,
    *,
    ttl_attribute: str,
    pitr_confirmed: bool = False,
) -> Tuple[str, Optional[str], Optional[str]]:
    """Verify one table. Never raises.

    Returns ``(capability_value, fatal_reason | None, warn_reason | None)``.
    The caller decides the consequence: refuse at boot; degrade + alert at runtime (D-75).
    """
    # --- table + GSIs -------------------------------------------------------
    try:
        desc = (await client.describe_table(TableName=table))["Table"]
    except Exception as e:
        detail = f"table {table!r} not found or not describable ({type(e).__name__})"
        return f"UNSAFE ({detail})", detail, None

    status = str(desc.get("TableStatus", "")).upper()
    if status != "ACTIVE":
        detail = f"table {table!r} is {status or 'UNKNOWN'}, not ACTIVE"
        return f"UNSAFE ({detail})", detail, None

    for gsi in desc.get("GlobalSecondaryIndexes", []) or []:
        gsi_status = str(gsi.get("IndexStatus", "")).upper()
        if gsi_status != "ACTIVE":
            detail = (f"GSI {gsi.get('IndexName')!r} is {gsi_status or 'UNKNOWN'}, not ACTIVE — "
                      f"queries against a backfilling index silently MISS rows (D-32)")
            return f"UNSAFE ({detail})", detail, None

    # --- TTL ----------------------------------------------------------------
    warn: Optional[str] = None
    ttl_note = "ttl=?"
    try:
        ttl = (await client.describe_time_to_live(TableName=table))["TimeToLiveDescription"]
        ttl_status = str(ttl.get("TimeToLiveStatus", "")).upper()
        ttl_attr = ttl.get("AttributeName")
        if ttl_status in ("ENABLED", "ENABLING"):
            if ttl_attr != ttl_attribute:
                detail = (f"TTL is enabled on attribute {ttl_attr!r}, but this library writes its "
                          f"expiry to {ttl_attribute!r} — DynamoDB may DELETE rows the library "
                          f"never marked for expiry (including OPEN activations)")
                return f"UNSAFE ({detail})", detail, None
            ttl_note = f"ttl={ttl_attr}"
        else:
            ttl_note = "ttl=DISABLED"
            warn = (f"TTL is DISABLED on {table!r}: rows the library marks with {ttl_attribute!r} "
                    f"will never reap (unbounded growth and cost — nothing is lost). Enable TTL on "
                    f"{ttl_attribute!r}.")
    except Exception as e:
        ttl_note = "ttl=unverified"
        warn = f"TTL could not be verified on {table!r} ({type(e).__name__})"

    # --- PITR (a money store) ----------------------------------------------
    try:
        cb = (await client.describe_continuous_backups(TableName=table))["ContinuousBackupsDescription"]
        pitr = str(cb.get("PointInTimeRecoveryDescription", {})
                     .get("PointInTimeRecoveryStatus", "")).upper()
    except Exception as e:
        # DynamoDB Local answers UnknownOperationException: PITR is the ONE thing only real
        # AWS can confirm. Absent signal ⇒ operator assertion, on the record (D-32's shape).
        if pitr_confirmed:
            return (f"ACTIVE ({ttl_note}, pitr=asserted by operator — control plane cannot report it)",
                    None, warn)
        detail = (f"point-in-time recovery could not be verified on {table!r} "
                  f"({type(e).__name__}) and storage.ddb_pitr_confirmed is not set — an "
                  f"unverified backup posture on a money store is not a safe one")
        return f"UNSAFE ({detail})", detail, warn

    if pitr != "ENABLED":
        if pitr_confirmed:
            return (f"ACTIVE ({ttl_note}, pitr={pitr or 'DISABLED'} — WAIVED by operator)", None, warn)
        detail = (f"PITR (point-in-time recovery) is {pitr or 'DISABLED'} on {table!r} — this table "
                  f"holds money/ledger state that cannot be reconstructed")
        return f"UNSAFE ({detail})", detail, warn

    return f"ACTIVE ({ttl_note}, pitr=ENABLED)", None, warn


async def verify_ddb_tables(client, tables: dict, *, pitr_confirmed: bool = False) -> Tuple[dict, list]:
    """D-75/D-76 — re-verify every DDB table the library depends on. Returns
    ``(capability_updates, unsafe)`` with unsafe = [(capability_key, detail), …].
    `tables` maps capability_key → (table_name, ttl_attribute)."""
    caps: dict = {}
    unsafe: list = []
    for cap_key, (table, ttl_attr) in tables.items():
        value, fatal, warn = await verify_ddb_table(
            client, table, ttl_attribute=ttl_attr, pitr_confirmed=pitr_confirmed)
        caps[cap_key] = value
        if fatal:
            unsafe.append((cap_key, fatal))
        elif warn:
            logger.warning("DDB preflight warning (%s): %s", cap_key, warn)
    return caps, unsafe
