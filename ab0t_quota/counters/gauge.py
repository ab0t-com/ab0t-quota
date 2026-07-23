"""Gauge counter — tracks current level (e.g. concurrent sandboxes).

Integrity note (ticket 20260709, task P1.1): the idempotency claim, the counter
mutation, and the floor-at-zero are performed in ONE atomic Lua script per op
(QI-01 claim-then-mutate crash window; QI-02 non-atomic floor read-modify-write).
Keys, values, and the 24h idem TTL are UNCHANGED in v1 single mode — wire-
compatible with the live fleet and with the Go port's counter semantics.

Phase-2 integrity (task P2.2 / P2.3, QI-03 + QI-05):

* ``try_increment`` / ``try_increment_user`` fold the LIMIT check and the spend
  into ONE atomic Lua op (QI-03 TOCTOU kill). At the limit, the loser is
  refused *inside the same script* that would have spent.

* ``decrement_user`` scopes a caller-supplied idempotency key by a
  library-managed per-(org,user,resource) CREATE generation (``seq``) —
  QI-05.1. See DECISIONS D-10 and information_phase2_activation_20260710.md.

K-3 (keyspace spec §3.2/§6.2): every script is dual-capable. During a keyspace
migration the same atomic op seeds the v2 twin from v1, checks the idempotency
LATCH on BOTH shapes, claims BOTH, and mutates BOTH — a retry first claimed
pre-dual is still recognised across the flip (the double-charge trap, K-3).
"""

from __future__ import annotations

import math
from typing import Optional
from .base import Counter, finite_magnitude, dual_lua

# Idempotency-key TTL (seconds). Unchanged from the pre-Lua implementation.
# The migration flip gate must outwait this (keyspace spec §3.3).
_IDEM_TTL = 86400

# --- Atomic Lua scripts -----------------------------------------------------
# Convention for every script:
#   ARGV[1] = magnitude (always a non-negative number; decrements negate it)
#   ARGV[2] = idem TTL seconds
#   ARGV[3] = '1' if an idempotency key is supplied (claim it), else '0'
# On duplicate the script mutates NOTHING and returns the current value.
# Dual convention (base.dual_lua): KEYS doubled, dual/v2p flags appended.

# org-only increment: KEYS[1]=idem, KEYS[2]=org ; ARGV[4]=dual [5]=v2p
_INCR = dual_lua("2", 4, """
seedv2(2)
if ARGV[3] == '1' then
  if idem_dup(1) then
    local c = redis.call('GET', KEYS[2]); if c then return c else return '0' end
  end
  idem_claim(1)
end
return incrboth(2, ARGV[1])
""")

# org-only decrement + floor: KEYS[1]=idem, KEYS[2]=org ; ARGV[4]=dual [5]=v2p
_DECR = dual_lua("2", 4, """
seedv2(2)
if ARGV[3] == '1' then
  if idem_dup(1) then
    local c = redis.call('GET', KEYS[2]); if c then return c else return '0' end
  end
  idem_claim(1)
end
return incrfloorboth(2, '-'..ARGV[1])
""")

# per-user increment (org total + user partition):
# KEYS[1]=idem, KEYS[2]=org, KEYS[3]=user, KEYS[4]=create-generation seq
# A non-duplicate create bumps the per-(org,user,resource) generation (QI-05.1).
_INCR_USER = dual_lua("4", 4, """
seedv2(2); seedv2(3); seedv2(4)
if ARGV[3] == '1' then
  if idem_dup(1) then
    local u = redis.call('GET', KEYS[3]); if u then return u else return '0' end
  end
  idem_claim(1)
end
incrintboth(4)
incrboth(2, ARGV[1])
return incrboth(3, ARGV[1])
""")

# per-user decrement + floor both:
# KEYS[1]=idem-generation HASH, KEYS[2]=org, KEYS[3]=user, KEYS[4]=create-gen seq
# The claim is scoped by the current create generation (QI-05.1) as a HASH FIELD
# of the DECLARED KEYS[1] (QI-09). Dual: gen checked/claimed on BOTH hashes; the
# generation VALUE migrates with the seeded seq key (spec §6.2).
_DECR_USER = dual_lua("4", 4, """
seedv2(2); seedv2(3); seedv2(4)
if ARGV[3] == '1' then
  local gen = redis.call('GET', KEYS[4]); if not gen then gen = '0' end
  local dup = redis.call('HEXISTS', KEYS[1], gen) == 1
  if DUAL and redis.call('HEXISTS', KEYS[NK+1], gen) == 1 then dup = true end
  if dup then
    local u = redis.call('GET', KEYS[3]); if u then return u else return '0' end
  end
  redis.call('HSETNX', KEYS[1], gen, '1')
  redis.call('EXPIRE', KEYS[1], ARGV[2])
  if DUAL then
    redis.call('HSETNX', KEYS[NK+1], gen, '1')
    redis.call('EXPIRE', KEYS[NK+1], ARGV[2])
  end
end
incrfloorboth(2, '-'..ARGV[1])
return incrfloorboth(3, '-'..ARGV[1])
""")

# --- Atomic check-and-spend (QI-03: admission decision, not read-only check) ---
# Limit checks read the PRIMARY (authoritative) side, post-seed.

# org-only: KEYS[1]=idem, KEYS[2]=org
# ARGV[1]=delta [2]=ttl [3]=has_idem [4]=limit [5]=dual [6]=v2p
_TRY_INCR = dual_lua("2", 5, """
seedv2(2)
local cur = redis.call('GET', KEYS[2]); if not cur then cur = '0' end
if ARGV[3] == '1' and idem_dup(1) then
  return {cur, '1'}
end
if ARGV[4] ~= '' and (tonumber(cur) + tonumber(ARGV[1])) > tonumber(ARGV[4]) then
  return {cur, '0'}
end
if ARGV[3] == '1' then idem_claim(1) end
local v = incrboth(2, ARGV[1])
return {v, '1'}
""")

# per-user: KEYS[1]=idem, KEYS[2]=org, KEYS[3]=user, KEYS[4]=seq
# ARGV[1]=delta [2]=ttl [3]=has_idem [4]=org_limit [5]=user_limit [6]=dual [7]=v2p
_TRY_INCR_USER = dual_lua("4", 6, """
seedv2(2); seedv2(3); seedv2(4)
local o = redis.call('GET', KEYS[2]); if not o then o = '0' end
local u = redis.call('GET', KEYS[3]); if not u then u = '0' end
if ARGV[3] == '1' and idem_dup(1) then
  return {u, '1', 'dup'}
end
if ARGV[4] ~= '' and (tonumber(o) + tonumber(ARGV[1])) > tonumber(ARGV[4]) then
  return {u, '0', 'org'}
end
if ARGV[5] ~= '' and (tonumber(u) + tonumber(ARGV[1])) > tonumber(ARGV[5]) then
  return {u, '0', 'user'}
end
if ARGV[3] == '1' then idem_claim(1) end
incrintboth(4)
incrboth(2, ARGV[1])
local nu = incrboth(3, ARGV[1])
return {nu, '1', 'ok'}
""")


class GaugeCounter(Counter):
    """Bidirectional counter: increment on create, decrement on destroy.

    v1 key: quota:{org_id}:{resource_key}:gauge (+ :gauge:user:{uid} etc.)
    v2 key: quota:v2:{svc/org}:{resource_key}:gauge (keyspace spec §2.1)
    Type: string (INCRBYFLOAT); TTL: none.
    """

    @property
    def _redis_key(self) -> str:
        return self._ks.gauge_key(self._org_id, self._resource_key)

    def _user_key(self, user_id: str) -> str:
        return self._ks.user_key(self._org_id, self._resource_key, user_id)

    def _seq_user_key(self, user_id: str) -> str:
        """Per-(org, user, resource) CREATE generation (QI-05.1)."""
        return self._ks.seq_user_key(self._org_id, self._resource_key, user_id)

    def _idem_key(self, key: Optional[str]) -> str:
        return self._ks.idem_key(self._org_id, self._resource_key, key)

    def _idem_gen_key(self, key: Optional[str]) -> str:
        """Generation-scoped teardown claim HASH (QI-05.1/QI-09)."""
        return self._ks.idem_gen_key(self._org_id, self._resource_key, key)

    # Secondary-shape twins (only meaningful during dual-write).
    def _keys2(self, *kinds) -> list:
        """Secondary-shape keys for (kind, *args) tuples, [] when not dual."""
        sv = self._sv
        if not sv:
            return []
        out = []
        for kind, *args in kinds:
            fn = getattr(self._ks, kind)
            out.append(fn(self._org_id, self._resource_key, *args, version=sv))
        return out

    async def get_user(self, user_id: str) -> float:
        """Get a specific user's usage within this org gauge (dual-read)."""
        val = await self._redis.get(self._user_key(user_id))
        if val is None and self._sv:
            val = await self._redis.get(self._ks.user_key(
                self._org_id, self._resource_key, user_id, version=self._sv))
        return float(val) if val else 0.0

    async def _claim_idempotency(self, key: str) -> bool:
        """Atomically claim an idempotency key (SET NX). Retained for backward
        compatibility; the primary ops now claim inside their Lua script."""
        result = await self._redis.set(
            self._idem_key(key), "1", ex=_IDEM_TTL, nx=True,
        )
        return result is not None

    async def increment_user(self, user_id: str, delta: float, idempotency_key: Optional[str] = None) -> float:
        """Increment both the org-level gauge AND the user partition (atomic);
        bumps the CREATE generation (QI-05.1)."""
        delta = finite_magnitude(delta)  # W-T3/ET-03: validate BEFORE Lua
        idem_key = f"{user_id}:{idempotency_key}" if idempotency_key else None
        has_idem = "1" if idem_key else "0"
        keys = [self._idem_key(idem_key), self._redis_key,
                self._user_key(user_id), self._seq_user_key(user_id)]
        keys += self._keys2(("idem_key", idem_key), ("gauge_key",),
                            ("user_key", user_id), ("seq_user_key", user_id))
        result = await self._redis.eval(
            _INCR_USER, len(keys), *keys,
            delta, _IDEM_TTL, has_idem, *self._dual_argv(),
        )
        return float(result)

    async def decrement_user(self, user_id: str, delta: float, idempotency_key: Optional[str] = None) -> float:
        """Decrement org gauge AND user partition, flooring both at zero —
        atomic (QI-02); claim scoped by the CREATE generation (QI-05.1)."""
        delta = finite_magnitude(delta)  # W-T3/ET-03: validate BEFORE Lua
        idem_key = f"{user_id}:{idempotency_key}" if idempotency_key else None
        has_idem = "1" if idem_key else "0"
        keys = [self._idem_gen_key(idem_key), self._redis_key,
                self._user_key(user_id), self._seq_user_key(user_id)]
        keys += self._keys2(("idem_gen_key", idem_key), ("gauge_key",),
                            ("user_key", user_id), ("seq_user_key", user_id))
        result = await self._redis.eval(
            _DECR_USER, len(keys), *keys,
            delta, _IDEM_TTL, has_idem, *self._dual_argv(),
        )
        return float(result)

    @staticmethod
    def _fmt_limit(limit: Optional[float]) -> str:
        """'' means unlimited / no-limit (the Lua skips that check).

        W-T3/ET-01 (D-31): a NaN limit makes every Lua comparison false and
        ADMITS everything. Refuse it loudly here."""
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
        (new_or_current_value, admitted)."""
        delta = finite_magnitude(delta)  # W-T3/ET-03: validate BEFORE Lua
        has_idem = "1" if idempotency_key else "0"
        keys = [self._idem_key(idempotency_key), self._redis_key]
        keys += self._keys2(("idem_key", idempotency_key), ("gauge_key",))
        res = await self._redis.eval(
            _TRY_INCR, len(keys), *keys,
            delta, _IDEM_TTL, has_idem, self._fmt_limit(limit), *self._dual_argv(),
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
        keys = [self._idem_key(idem_key), self._redis_key,
                self._user_key(user_id), self._seq_user_key(user_id)]
        keys += self._keys2(("idem_key", idem_key), ("gauge_key",),
                            ("user_key", user_id), ("seq_user_key", user_id))
        res = await self._redis.eval(
            _TRY_INCR_USER, len(keys), *keys,
            delta, _IDEM_TTL, has_idem,
            self._fmt_limit(org_limit), self._fmt_limit(user_limit),
            *self._dual_argv(),
        )
        value, admitted = res[0], res[1]
        return float(value), (admitted in (b"1", "1", 1))

    async def get(self) -> float:
        val = await self._redis.get(self._redis_key)
        if val is None and self._sv:
            val = await self._redis.get(self._ks.gauge_key(
                self._org_id, self._resource_key, version=self._sv))
        return float(val) if val else 0.0

    async def increment(self, delta: float, idempotency_key: Optional[str] = None) -> float:
        delta = finite_magnitude(delta)  # W-T3/ET-03: validate BEFORE Lua
        has_idem = "1" if idempotency_key else "0"
        keys = [self._idem_key(idempotency_key), self._redis_key]
        keys += self._keys2(("idem_key", idempotency_key), ("gauge_key",))
        result = await self._redis.eval(
            _INCR, len(keys), *keys,
            delta, _IDEM_TTL, has_idem, *self._dual_argv(),
        )
        return float(result)

    async def decrement(self, delta: float, idempotency_key: Optional[str] = None) -> float:
        delta = finite_magnitude(delta)  # W-T3/ET-03: validate BEFORE Lua
        has_idem = "1" if idempotency_key else "0"
        keys = [self._idem_key(idempotency_key), self._redis_key]
        keys += self._keys2(("idem_key", idempotency_key), ("gauge_key",))
        result = await self._redis.eval(
            _DECR, len(keys), *keys,
            delta, _IDEM_TTL, has_idem, *self._dual_argv(),
        )
        return float(result)

    async def reset(self, value: float = 0.0) -> None:
        await self._redis.set(self._redis_key, value)
        if self._sv:
            await self._redis.set(self._ks.gauge_key(
                self._org_id, self._resource_key, version=self._sv), value)

    async def reset_user(self, user_id: str, value: float = 0.0) -> None:
        """Force-set a user partition (admin/reconciliation, P2.5)."""
        await self._redis.set(self._user_key(user_id), value)
        if self._sv:
            await self._redis.set(self._ks.user_key(
                self._org_id, self._resource_key, user_id, version=self._sv), value)
