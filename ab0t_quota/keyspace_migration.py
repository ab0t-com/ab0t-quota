"""Keyspace migration engine (K-5) — dual-on → backfill → verify → flip → reap.

Spec: tickets/20260721_keyspace_versioning/SPEC_keyspace_versioning_20260721.md §5.
Runbook: tickets/20260721_keyspace_versioning/RUNBOOK_keyspace_migration.md.
All verbs idempotent and resumable — interruption is the normal case.
CLI wiring (``python -m ab0t_quota keyspace …``) belongs to the tooling lane;
this module is the mechanism.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Callable, Optional

from .errors import QuotaConfigError
from .keyspace import IDEM_TTL_SECONDS, Keyspace, marker_key

logger = logging.getLogger("ab0t_quota.keyspace_migration")

_EPS = 1e-9
_VERSIONISH = re.compile(r"^v[0-9]+$")
# v1 counter suffix families (spec §2.1). rate/idem/idemgen are enumerated for
# reap; only VALUE keys (gauge/user/seq/acc) are backfilled — rate is
# dual-written from dual-on (never copied), latches are TTL-bounded dual-claims.
_BACKFILL_TAILS = ("gauge", "acc")
_REAP_TAILS = ("gauge", "acc", "rate", "idem", "idemgen")

# Atomic seed-if-absent, the SAME semantics as the hot path's seedv2 (§6.1):
# copy v1 → v2 only when v2 is absent; TTL carried so a backfilled acc bucket
# still expires. KEYS[1]=v1, KEYS[2]=v2.
_SEED = """
if redis.call('EXISTS', KEYS[2]) == 0 then
  local v = redis.call('GET', KEYS[1])
  if v then
    redis.call('SET', KEYS[2], v)
    local t = redis.call('PTTL', KEYS[1])
    if tonumber(t) > 0 then redis.call('PEXPIRE', KEYS[2], t) end
    return 1
  end
end
return 0
"""


def classify_v1_counter_key(key: str):
    """(org, rk, tail) for a v1 COUNTER key, else None. tail = everything after
    the rk segment (e.g. 'gauge', 'gauge:user:u9', 'acc:2026-07', 'idem:k')."""
    if not key.startswith("quota:") or key.startswith("quota:v2:"):
        return None
    parts = key.split(":")
    if len(parts) < 4 or "." not in parts[2] or _VERSIONISH.match(parts[1]):
        return None
    return parts[1], parts[2], ":".join(parts[3:])


class KeyspaceMigrationError(RuntimeError):
    """A migration verb refused to run — the message names why."""


class KeyspaceMigrator:
    """Drives one service scope's v1→v2 migration against its Redis.

    ``now_fn`` is injectable for tests; every verb re-reads the marker so an
    interrupted run resumes from storage, not memory.
    """

    def __init__(self, redis, service: str, *,
                 max_rate_window_seconds: int = 0,
                 now_fn: Callable[[], float] = time.time):
        self._redis = redis
        self._ks = Keyspace(service=service, version=2)  # builder for v2 twins
        self._service = service
        self._max_rate_window = int(max_rate_window_seconds)
        self._now = now_fn
        self._verified_ok_at: Optional[float] = None  # this-instance verify gate

    # ------------------------------------------------------------- marker

    async def read_marker(self) -> Optional[dict]:
        raw = await self._redis.get(marker_key(self._service))
        if raw is None:
            return None
        return json.loads(raw if isinstance(raw, str) else raw.decode())

    async def _write_marker(self, **fields) -> dict:
        cur = await self.read_marker() or {}
        cur.update(fields)
        cur["updated_at"] = self._now()
        cur.setdefault("by", "ab0t-quota keyspace migrator")
        await self._redis.set(marker_key(self._service), json.dumps(cur))
        return cur

    # -------------------------------------------------------------- verbs

    async def dual_on(self) -> dict:
        """Record the dual window start. Idempotent: dual_since is kept on
        re-run (the flip gate measures from the FIRST dual-on)."""
        cur = await self.read_marker() or {}
        if cur.get("high_water") == "v2-final":
            raise KeyspaceMigrationError(
                "keyspace regression: storage records a completed v2 migration "
                "(marker high_water=v2-final) — dual-on for v1 would resurrect "
                "orphaned keys (QUOTA-CFG-011 posture)")
        if cur.get("phase") in ("dual", "flipped"):
            return cur
        return await self._write_marker(phase="dual", dual_since=self._now(),
                                        high_water="dual")

    async def _scan_v1(self, tails):
        async for key in self._redis.scan_iter(match="quota:*", count=500):
            ks = key.decode() if isinstance(key, bytes) else str(key)
            c = classify_v1_counter_key(ks)
            if c is None:
                continue
            org, rk, tail = c
            if tail.split(":", 1)[0] in tails:
                yield ks, org, rk, tail

    def _v2_twin(self, org: str, rk: str, tail: str) -> str:
        return f"{self._ks._prefix(org, rk, 2)}:{tail}"

    async def backfill(self, *, budget: int = 0) -> dict:
        """Seed-if-absent every v1 VALUE key's v2 twin. Fully resumable: a key
        already seeded (by a prior run or the hot path) is a no-op. ``budget``
        bounds seeds per pass (reconciler pacing idiom); 0 = unbounded."""
        marker = await self.read_marker()
        if not marker or marker.get("phase") not in ("dual", "flipped"):
            raise KeyspaceMigrationError(
                "backfill requires dual-on first (marker phase=dual) — seeding "
                "without dual-write live would immediately drift")
        seeded = scanned = 0
        async for key, org, rk, tail in self._scan_v1(_BACKFILL_TAILS):
            scanned += 1
            seeded += int(await self._redis.eval(_SEED, 2, key, self._v2_twin(org, rk, tail)))
            if budget and seeded >= budget:
                break
        return {"scanned": scanned, "seeded": seeded}

    async def verify(self, *, tolerance: float = _EPS) -> dict:
        """Value-compare every v1 VALUE key against its v2 twin. Divergence ⇒
        report + the flip/reap gates stay closed. Catches a broken dual-write
        (V3 plant (a)) — equality at the instant is not the gate, maintenance is."""
        divergent: list = []
        compared = 0
        async for key, org, rk, tail in self._scan_v1(_BACKFILL_TAILS):
            compared += 1
            v1 = await self._redis.get(key)
            v2 = await self._redis.get(self._v2_twin(org, rk, tail))
            f1 = float(v1) if v1 else 0.0
            f2 = float(v2) if v2 else 0.0
            if v2 is None or abs(f1 - f2) > tolerance:
                divergent.append({"key": key, "v1": f1, "v2": None if v2 is None else f2})
        ok = not divergent
        self._verified_ok_at = self._now() if ok else None
        return {"ok": ok, "compared": compared, "divergent": divergent}

    def flip_gate(self, marker: dict) -> Optional[str]:
        """None when the flip may proceed, else the refusal reason. The gate is
        machine-enforced (§3.3): dual_since must outwait the idem TTL AND the
        longest rate window, or a pre-dual latch/window is not yet twinned."""
        if not marker or marker.get("phase") not in ("dual", "flipped"):
            return "not in dual phase — run dual-on and backfill first"
        wait = max(IDEM_TTL_SECONDS, self._max_rate_window)
        age = self._now() - float(marker.get("dual_since", self._now()))
        if age < wait:
            return (f"dual window too young: {age:.0f}s < {wait}s — a retry of a "
                    "pre-dual op (or a rate window) is not yet represented in v2; "
                    "flipping now double-charges on retry (spec §6.2)")
        if self._verified_ok_at is None:
            return "verify has not passed in this run — run verify first"
        return None

    async def flip(self) -> dict:
        """Record readiness for the (2,true) config flip. The config change +
        rolling restart is the operator's; this refuses until the gate opens."""
        marker = await self.read_marker()
        reason = self.flip_gate(marker or {})
        if reason:
            raise KeyspaceMigrationError(f"flip refused: {reason}")
        return await self._write_marker(phase="flipped", high_water="flipped")

    async def reap(self, *, i_confirm_no_other_scope_reads_v1: bool = False) -> dict:
        """THE one irreversible step. Guards (§3.3): verify green in THIS run;
        marker set to v2-final BEFORE the first delete; explicit operator
        confirmation that no other service scope still reads the (unscoped) v1
        keys — in bridge Redis the v1 keyspace is shared (F-1), so one scope's
        reap is every scope's reap."""
        marker = await self.read_marker()
        if not marker or marker.get("phase") not in ("flipped", "reaped"):
            raise KeyspaceMigrationError("reap refused: flip has not been recorded")
        if self._verified_ok_at is None:
            raise KeyspaceMigrationError(
                "reap refused: verify has not passed in this run (reap deletes "
                "state; it never runs on a stale verdict)")
        if not i_confirm_no_other_scope_reads_v1:
            raise KeyspaceMigrationError(
                "reap refused: v1 counter keys carry no service scope — confirm "
                "no other service scope still reads them "
                "(i_confirm_no_other_scope_reads_v1=True)")
        await self._write_marker(high_water="v2-final", phase="reaped")
        deleted = 0
        async for key, _org, _rk, _tail in self._scan_v1(_REAP_TAILS):
            deleted += await self._redis.delete(key)
        return {"deleted": deleted, "marker": await self.read_marker()}

    async def status(self) -> dict:
        """Marker + keyspace census; the straggler guard's data source (K-6):
        v1 counter keys still present (and, post-flip, still being written)
        are named, not assumed absent."""
        v1 = v2 = 0
        async for key in self._redis.scan_iter(match="quota:*", count=500):
            ks = key.decode() if isinstance(key, bytes) else str(key)
            if ks.startswith("quota:v2:"):
                v2 += 1
            elif classify_v1_counter_key(ks):
                v1 += 1
        return {"service": self._service, "marker": await self.read_marker(),
                "v1_counter_keys": v1, "v2_keys": v2}


# ------------------------------------------------------------ straggler guard

async def check_v1_stragglers(redis, service: str, alert_fn=None) -> dict:
    """K-6 post-flip straggler guard (spec §11.1, top risk): after the flip,
    any v1 counter key still present means a pre-mechanism replica (or an
    out-of-repo script) is writing keys nobody reads — silent spend loss.
    LOUD: error log + optional alert_fn(payload). Run periodically / from
    `status`. Before the flip it reports quietly (v1 is still authoritative)."""
    raw = await redis.get(marker_key(service))
    marker = json.loads(raw if isinstance(raw, str) else raw.decode()) if raw else None
    post_flip = bool(marker and marker.get("phase") in ("flipped", "reaped"))
    stragglers: list = []
    async for key in redis.scan_iter(match="quota:*", count=500):
        ks = key.decode() if isinstance(key, bytes) else str(key)
        if classify_v1_counter_key(ks):
            stragglers.append(ks)
            if len(stragglers) >= 50:
                break
    payload = {"service": service, "post_flip": post_flip,
               "v1_stragglers": len(stragglers), "sample": stragglers[:10]}
    if post_flip and stragglers:
        logger.error(
            "keyspace_v1_straggler_writes service=%s count=%d sample=%s — a "
            "writer is still producing v1 counter keys AFTER the flip; its "
            "spend lands where nobody reads (spec §11.1). Find the straggler "
            "before reap.", service, len(stragglers), stragglers[:5],
        )
        if alert_fn is not None:
            res = alert_fn(payload)
            if hasattr(res, "__await__"):
                await res
    return payload


# ---------------------------------------------------------------- boot guards

async def check_boot_keyspace(redis, keyspace: Keyspace) -> Optional[dict]:
    """Boot refusals (spec §3.3): QUOTA-CFG-011 (version regression against a
    completed migration) and QUOTA-CFG-012 (brownfield v2 with live v1 keys and
    no completed migration). Call at setup after Redis is reachable.
    Returns the migration marker (None when absent/unscoped) — the one read
    setup/preflight/doctor report the phase from (K-9)."""
    if keyspace.service is None:
        return None  # v1-only consumer with no scope — nothing recorded to regress
    raw = await redis.get(marker_key(keyspace.service))
    marker = json.loads(raw if isinstance(raw, str) else raw.decode()) if raw else None
    final = bool(marker and marker.get("high_water") == "v2-final")
    v1_authoritative = keyspace.version == 1
    if final and (v1_authoritative or keyspace.dual_write):
        raise QuotaConfigError(
            name="keyspace version", config_key="storage.keyspace_version",
            code="QUOTA-CFG-011",
            state=f"config declares v{keyspace.version} (dual={keyspace.dual_write}) "
                  "but storage records a COMPLETED v2 migration (marker v2-final)",
            env_names=(),
            remedy="keyspace version regression: a v1 engine would read orphaned "
                   "keys and every counter would read zero. Declare "
                   "keyspace_version: 2, keyspace_dual_write: false. This refusal "
                   "is not operator-overridable (definitive negative).",
            docs_anchor="keyspace",
        )
    if keyspace.version == 2 and not keyspace.dual_write and not final:
        found = None
        async for key in redis.scan_iter(match="quota:*", count=200):
            ks = key.decode() if isinstance(key, bytes) else str(key)
            if classify_v1_counter_key(ks):
                found = ks
                break
        if found is not None:
            raise QuotaConfigError(
                name="keyspace version", config_key="storage.keyspace_version",
                code="QUOTA-CFG-012",
                state=f"v1 counter keys exist (e.g. {found}) but no completed "
                      "migration is recorded",
                env_names=(),
                remedy="run the keyspace migration (dual-on → backfill → verify "
                       "→ flip → reap) or declare keyspace_version: 1 — booting "
                       "v2-only now would silently orphan live counters",
                docs_anchor="keyspace",
            )
    return marker
