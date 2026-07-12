"""Gauge counter — tracks current level (e.g. concurrent sandboxes).

Integrity note (ticket 20260709, task P1.1): the idempotency claim, the counter
mutation, and the floor-at-zero are performed in ONE atomic Lua script per op
(QI-01 claim-then-mutate crash window; QI-02 non-atomic floor read-modify-write).
Keys, values, and the 24h idem TTL are UNCHANGED — wire-compatible with the live
fleet and with the Go port's counter semantics.

Phase-2 integrity (task P2.2 / P2.3, QI-03 + QI-05):

* ``try_increment`` / ``try_increment_user`` fold the LIMIT check and the spend
  into ONE atomic Lua op — an admission decision under concurrency (QI-03: the
  old check()->increment() TOCTOU let two racers both pass a read-only check and
  both spend). At the limit, the loser is refused *inside the same script* that
  would have spent, so the gauge can never exceed the limit.

* ``decrement_user`` now scopes a caller-supplied idempotency key by a
  library-managed per-(org,user,resource) CREATE generation (``seq``). This
  retires the reused-resource-id collision (QI-05.1): two DISTINCT activations of
  the same reused id release under distinct generations, so the second teardown's
  decrement is no longer suppressed by the first's already-claimed key — WITHOUT
  widening the TTL or special-casing any key (both recorded REJECTED approaches).
  The generation is minted by the library, not the caller: this is the
  "implicit activation" of P2.3. The fully retry-safe path is
  ``engine.acquire``/``engine.release`` keyed on a minted ``activation_id``; this
  counter-level shim is best-effort under the adversarial timing noted in
  ``activations.py`` (a teardown retry that lands AFTER a new create of a reused
  id). See DECISIONS D-10 and information_phase2_activation_20260710.md.
"""

from __future__ import annotations

import math
from typing import Optional
from .base import Counter, finite_magnitude

# Idempotency-key TTL (seconds). Unchanged from the pre-Lua implementation.
_IDEM_TTL = 86400

# --- Atomic Lua scripts -----------------------------------------------------
# Convention for every script:
#   ARGV[1] = magnitude (always a non-negative number; decrements negate it)
#   ARGV[2] = idem TTL seconds
#   ARGV[3] = '1' if an idempotency key is supplied (claim it), else '0'
# When the idem claim fails (duplicate), the script mutates NOTHING and returns
# the current value — identical observable behaviour to the old two-call path,
# but now atomic with the mutation so a crash cannot claim-without-mutating.

# org-only increment: KEYS[1]=idem, KEYS[2]=org
_INCR = """
if ARGV[3] == '1' then
  if not redis.call('SET', KEYS[1], '1', 'NX', 'EX', ARGV[2]) then
    local c = redis.call('GET', KEYS[2]); if c then return c else return '0' end
  end
end
return redis.call('INCRBYFLOAT', KEYS[2], ARGV[1])
"""

# org-only decrement + floor: KEYS[1]=idem, KEYS[2]=org
_DECR = """
if ARGV[3] == '1' then
  if not redis.call('SET', KEYS[1], '1', 'NX', 'EX', ARGV[2]) then
    local c = redis.call('GET', KEYS[2]); if c then return c else return '0' end
  end
end
local v = redis.call('INCRBYFLOAT', KEYS[2], '-'..ARGV[1])
if tonumber(v) < 0 then redis.call('SET', KEYS[2], '0'); v = '0' end
return v
"""

# per-user increment (org total + user partition):
# KEYS[1]=idem, KEYS[2]=org, KEYS[3]=user, KEYS[4]=create-generation seq
# A non-duplicate create bumps the per-(org,user,resource) generation so a later
# teardown can scope its idempotency key by it (QI-05.1). Returns the user value.
_INCR_USER = """
if ARGV[3] == '1' then
  if not redis.call('SET', KEYS[1], '1', 'NX', 'EX', ARGV[2]) then
    local u = redis.call('GET', KEYS[3]); if u then return u else return '0' end
  end
end
redis.call('INCR', KEYS[4])
redis.call('INCRBYFLOAT', KEYS[2], ARGV[1])
return redis.call('INCRBYFLOAT', KEYS[3], ARGV[1])
"""

# per-user decrement + floor both (org total + user partition):
# KEYS[1]=idem-generation HASH, KEYS[2]=org, KEYS[3]=user, KEYS[4]=create-gen seq
# The idempotency CLAIM is scoped by the current create generation (KEYS[4]) so a
# caller key reused across two distinct activations (QI-05.1) claims two DISTINCT
# generations and neither teardown is suppressed. A genuine retry of the SAME
# teardown (before the next create bumps the generation) still collides -> no-op.
# QI-09: the generation is a HASH FIELD of the DECLARED KEYS[1], NOT a key suffix.
# The old code did `SET KEYS[1]..':gen:'..gen` — a key COMPUTED inside Lua and never
# declared in KEYS. fakeredis tolerates it; real Redis Cluster REJECTS an undeclared
# key access outright. HSETNX(KEYS[1], gen, 1) is the same claim semantics with every
# accessed key declared (fields need no declaration). Returns the floored user value.
_DECR_USER = """
if ARGV[3] == '1' then
  local gen = redis.call('GET', KEYS[4]); if not gen then gen = '0' end
  if redis.call('HSETNX', KEYS[1], gen, '1') == 0 then
    local u = redis.call('GET', KEYS[3]); if u then return u else return '0' end
  end
  redis.call('EXPIRE', KEYS[1], ARGV[2])
end
local o = redis.call('INCRBYFLOAT', KEYS[2], '-'..ARGV[1])
if tonumber(o) < 0 then redis.call('SET', KEYS[2], '0') end
local u = redis.call('INCRBYFLOAT', KEYS[3], '-'..ARGV[1])
if tonumber(u) < 0 then redis.call('SET', KEYS[3], '0'); u = '0' end
return u
"""

# --- Atomic check-and-spend (QI-03: admission decision, not read-only check) ---
# The limit check and the increment are ONE op, so two racers at limit-1 cannot
# both be admitted. Returns {value, admitted} where admitted is '1'/'0'.
# An empty ARGV[4]/ARGV[5] means "unlimited / no per-user limit" (skip that check).

# org-only: KEYS[1]=idem, KEYS[2]=org ; ARGV[1]=delta [2]=ttl [3]=has_idem [4]=limit
_TRY_INCR = """
local cur = redis.call('GET', KEYS[2]); if not cur then cur = '0' end
if ARGV[3] == '1' and redis.call('GET', KEYS[1]) then
  return {cur, '1'}
end
if ARGV[4] ~= '' and (tonumber(cur) + tonumber(ARGV[1])) > tonumber(ARGV[4]) then
  return {cur, '0'}
end
if ARGV[3] == '1' then
  if not redis.call('SET', KEYS[1], '1', 'NX', 'EX', ARGV[2]) then
    local c = redis.call('GET', KEYS[2]); if not c then c = '0' end
    return {c, '1'}
  end
end
local v = redis.call('INCRBYFLOAT', KEYS[2], ARGV[1])
return {v, '1'}
"""

# per-user: KEYS[1]=idem, KEYS[2]=org, KEYS[3]=user, KEYS[4]=seq
# ARGV[1]=delta [2]=ttl [3]=has_idem [4]=org_limit [5]=user_limit
_TRY_INCR_USER = """
local o = redis.call('GET', KEYS[2]); if not o then o = '0' end
local u = redis.call('GET', KEYS[3]); if not u then u = '0' end
if ARGV[3] == '1' and redis.call('GET', KEYS[1]) then
  return {u, '1', 'dup'}
end
if ARGV[4] ~= '' and (tonumber(o) + tonumber(ARGV[1])) > tonumber(ARGV[4]) then
  return {u, '0', 'org'}
end
if ARGV[5] ~= '' and (tonumber(u) + tonumber(ARGV[1])) > tonumber(ARGV[5]) then
  return {u, '0', 'user'}
end
if ARGV[3] == '1' then
  if not redis.call('SET', KEYS[1], '1', 'NX', 'EX', ARGV[2]) then
    local cu = redis.call('GET', KEYS[3]); if not cu then cu = '0' end
    return {cu, '1', 'dup'}
  end
end
redis.call('INCR', KEYS[4])
redis.call('INCRBYFLOAT', KEYS[2], ARGV[1])
local nu = redis.call('INCRBYFLOAT', KEYS[3], ARGV[1])
return {nu, '1', 'ok'}
"""


class GaugeCounter(Counter):
    """Bidirectional counter: increment on create, decrement on destroy.

    Redis key: quota:{org_id}:{resource_key}:gauge
    Per-user:  quota:{org_id}:{resource_key}:gauge:user:{user_id}
    Type: string (INCRBYFLOAT)
    TTL: none (persists until explicitly reset)
    """

    @property
    def _redis_key(self) -> str:
        return f"{self._key_prefix}:gauge"

    def _user_key(self, user_id: str) -> str:
        return f"{self._key_prefix}:gauge:user:{user_id}"

    def _seq_user_key(self, user_id: str) -> str:
        """Per-(org, user, resource) CREATE generation. Bumped on each create so
        a teardown can scope its idempotency key by it (QI-05.1)."""
        return f"{self._key_prefix}:gauge:seq:user:{user_id}"

    def _idem_key(self, key: Optional[str]) -> str:
        # Real key when claiming; an unused placeholder when not (never touched
        # because the script's has_idem flag is '0').
        return f"{self._key_prefix}:idem:{key}" if key else f"{self._key_prefix}:idem:__unused__"

    def _idem_gen_key(self, key: Optional[str]) -> str:
        # Generation-scoped teardown claim: a HASH whose fields are create
        # generations (QI-05.1). Distinct `:idemgen:` namespace so it never collides
        # (type-wise) with the string `:idem:` create-claim keys. DECLARED in KEYS
        # (QI-09) — the generation is a field, not a computed key suffix.
        return f"{self._key_prefix}:idemgen:{key}" if key else f"{self._key_prefix}:idemgen:__unused__"

    async def get_user(self, user_id: str) -> float:
        """Get a specific user's usage within this org gauge."""
        val = await self._redis.get(self._user_key(user_id))
        return float(val) if val else 0.0

    async def _claim_idempotency(self, key: str) -> bool:
        """Atomically claim an idempotency key (SET NX). Retained for backward
        compatibility; the primary ops now claim inside their Lua script."""
        result = await self._redis.set(
            f"{self._key_prefix}:idem:{key}", "1", ex=_IDEM_TTL, nx=True,
        )
        return result is not None

    async def increment_user(self, user_id: str, delta: float, idempotency_key: Optional[str] = None) -> float:
        """Increment both the org-level gauge AND the user partition (atomic).
        Bumps the per-user CREATE generation so a later teardown can scope its
        idempotency key by it (QI-05.1)."""
        delta = finite_magnitude(delta)  # W-T3/ET-03: validate BEFORE Lua
        idem_key = f"{user_id}:{idempotency_key}" if idempotency_key else None
        has_idem = "1" if idem_key else "0"
        result = await self._redis.eval(
            _INCR_USER, 4,
            self._idem_key(idem_key), self._redis_key, self._user_key(user_id),
            self._seq_user_key(user_id),
            delta, _IDEM_TTL, has_idem,
        )
        return float(result)

    async def decrement_user(self, user_id: str, delta: float, idempotency_key: Optional[str] = None) -> float:
        """Decrement both the org-level gauge AND the user partition, flooring
        each at zero — all atomic (QI-02). A caller-supplied idempotency key is
        scoped by the current CREATE generation so a reused resource-id does not
        collide across two distinct activations (QI-05.1)."""
        delta = finite_magnitude(delta)  # W-T3/ET-03: validate BEFORE Lua
        idem_key = f"{user_id}:{idempotency_key}" if idempotency_key else None
        has_idem = "1" if idem_key else "0"
        result = await self._redis.eval(
            _DECR_USER, 4,
            self._idem_gen_key(idem_key), self._redis_key, self._user_key(user_id),
            self._seq_user_key(user_id),
            delta, _IDEM_TTL, has_idem,
        )
        return float(result)

    @staticmethod
    def _fmt_limit(limit: Optional[float]) -> str:
        """'' means unlimited / no-limit (the Lua skips that check).

        W-T3/ET-01 (D-31): a NaN limit makes every Lua comparison false and
        ADMITS everything — a corrupted limit silently widening to infinity.
        Refuse it loudly here. (+inf compares like 'unlimited' and -inf denies
        everything — both mathematically consistent, both left alone.)"""
        if limit is None:
            return ""
        lim = float(limit)
        if math.isnan(lim):
            raise ValueError(
                "quota limit is NaN — refusing (a NaN limit admits everything; "
                "D-31 forbids silently widening a limit)"
            )
        return repr(lim)

    async def try_increment(
        self, delta: float, limit: Optional[float], idempotency_key: Optional[str] = None,
    ) -> tuple[float, bool]:
        """Atomic check-and-spend at the org level (QI-03). Returns
        (new_or_current_value, admitted). If admitting would exceed `limit`, the
        gauge is NOT mutated and admitted=False."""
        delta = finite_magnitude(delta)  # W-T3/ET-03: validate BEFORE Lua
        has_idem = "1" if idempotency_key else "0"
        res = await self._redis.eval(
            _TRY_INCR, 2,
            self._idem_key(idempotency_key), self._redis_key,
            delta, _IDEM_TTL, has_idem, self._fmt_limit(limit),
        )
        value, admitted = res[0], res[1]
        return float(value), (admitted in (b"1", "1", 1))

    async def try_increment_user(
        self, user_id: str, delta: float,
        org_limit: Optional[float], user_limit: Optional[float],
        idempotency_key: Optional[str] = None,
    ) -> tuple[float, bool]:
        """Atomic check-and-spend at BOTH the org and per-user level (QI-03),
        bumping the CREATE generation. Returns (user_value, admitted)."""
        delta = finite_magnitude(delta)  # W-T3/ET-03: validate BEFORE Lua
        idem_key = f"{user_id}:{idempotency_key}" if idempotency_key else None
        has_idem = "1" if idem_key else "0"
        res = await self._redis.eval(
            _TRY_INCR_USER, 4,
            self._idem_key(idem_key), self._redis_key, self._user_key(user_id),
            self._seq_user_key(user_id),
            delta, _IDEM_TTL, has_idem,
            self._fmt_limit(org_limit), self._fmt_limit(user_limit),
        )
        value, admitted = res[0], res[1]
        return float(value), (admitted in (b"1", "1", 1))

    async def get(self) -> float:
        val = await self._redis.get(self._redis_key)
        return float(val) if val else 0.0

    async def increment(self, delta: float, idempotency_key: Optional[str] = None) -> float:
        delta = finite_magnitude(delta)  # W-T3/ET-03: validate BEFORE Lua
        has_idem = "1" if idempotency_key else "0"
        result = await self._redis.eval(
            _INCR, 2,
            self._idem_key(idempotency_key), self._redis_key,
            delta, _IDEM_TTL, has_idem,
        )
        return float(result)

    async def decrement(self, delta: float, idempotency_key: Optional[str] = None) -> float:
        delta = finite_magnitude(delta)  # W-T3/ET-03: validate BEFORE Lua
        has_idem = "1" if idempotency_key else "0"
        result = await self._redis.eval(
            _DECR, 2,
            self._idem_key(idempotency_key), self._redis_key,
            delta, _IDEM_TTL, has_idem,
        )
        return float(result)

    async def reset(self, value: float = 0.0) -> None:
        await self._redis.set(self._redis_key, value)

    async def reset_user(self, user_id: str, value: float = 0.0) -> None:
        """Force-set a user partition (admin/reconciliation). Used by
        converge_gauge to DERIVE per-user partitions from the ledger (P2.5)."""
        await self._redis.set(self._user_key(user_id), value)
