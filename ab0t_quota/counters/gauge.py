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

import logging
import math
import os
import re
from typing import Optional
from .base import Counter, finite_magnitude, dual_lua

logger = logging.getLogger(__name__)

# Idempotency-key TTL (seconds). Unchanged from the pre-Lua implementation.
# The migration flip gate must outwait this (keyspace spec §3.3).
_IDEM_TTL = 86400

# --- Idempotency-key contract guard (ticket 20260810, P1.1/P1.5) -----------
#
# THE BUG this defends against: a caller keys a gauge decrement/increment on
# a bare RECYCLABLE resource/container id — e.g. `counter:lifecycle:end:{id}`
# with no per-activation component. Warm-pool reuse means a SECOND, genuinely
# DISTINCT lifecycle event (a different resource activation that happens to
# reuse the same container id) computes the IDENTICAL key. The claim-then-
# mutate Lua is *correctly* doing its job when it treats that as a retry of
# the FIRST event and no-ops — the defect is the key, not the claim. Over
# many reused ids this silently drops decrements and the gauge sticks high
# forever (the live incident this ticket fixes: org cd790b95 "5/1" with zero
# running sandboxes).
#
# The fix is a caller discipline: every idempotency key must carry a
# component that CANNOT repeat across two distinct activations of the same
# recyclable id — an activation/claim GENERATION bumped on every reactivation
# (a trailing `:<int>`, the convention every current consumer already uses:
# `counter:lifecycle:end:{resource_id}:{claim_generation}`), or a fresh UUID
# per lifecycle event (a `:evt:<uuid4>` segment). This module cannot enforce
# CORRECTNESS of that discipline (it has no idea whether a caller's generation
# int is actually being bumped) — it can only catch the SHAPE of the mistake:
# a key with no such component at all, which is exactly the collision-bug
# shape. Defense-in-depth, not a proof.
#
# Default is WARN (many existing consumers must not break at import/runtime
# because of a heuristic); set AB0T_QUOTA_STRICT_IDEMPOTENCY_KEYS=1 to make a
# bad key a hard refusal instead. See ab0t-quota README.md "Idempotency keys"
# for the contract + a worked example.
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_EVT_MARKER_RE = re.compile(r"(?:^|:)evt:")
# A colon-delimited, purely-numeric segment ANYWHERE in the key — not just at
# the end. engine.increment_for_bundle/decrement_for_bundle suffix every
# consumer key with `:{resource_key}` before it reaches this counter
# (engine.py ~1085/1093/1117: `f"{idempotency_key}:{rk}"`), so the generation
# a caller embeds (e.g. `counter:lifecycle:end:{id}:{claim_generation}`)
# lands in the MIDDLE of the final key, not its tail.
_GENERATION_SEGMENT_RE = re.compile(r"(?:^|:)(?:gen:)?\d+(?::|$)")
# An opaque HIGH-ENTROPY token: a run of >= 24 hex chars. This is what
# `secrets.token_hex(16)` (32 hex) and a dash-less UUID (32 hex) produce —
# and, critically, exactly the shape of the library's OWN release key,
# `release:{activation_id}` where `activation_id = mint_activation_id()` =
# `"act_" + secrets.token_hex(16)` (activations.py). Such a token is minted
# FRESH per activation and never reused, so it is the strongest possible
# unique component — yet the three patterns above (dashed-UUID / `:evt:` /
# numeric-generation) all MISS it: it has no dashes, no `evt:`, and no purely
# numeric segment. Without this pattern the guard raised on the library's own
# `release:act_<hex>` key in strict mode (breaking `engine.release()` AFTER
# the ledger row is marked RELEASED but BEFORE the gauge decrement → the exact
# stuck-high drift this ticket exists to prevent) and warn-spammed on every
# release in the default mode. 24 hex = 96 bits of entropy: far too long to be
# a reused resource/pool id (e.g. `desktop-abc123` has only a 6-hex run), so
# accepting it does not weaken detection of the genuine bare-recyclable-id bug.
_OPAQUE_TOKEN_RE = re.compile(r"[0-9a-fA-F]{24,}")

_STRICT_IDEMPOTENCY_ENV = "AB0T_QUOTA_STRICT_IDEMPOTENCY_KEYS"


def idempotency_key_has_unique_component(key: str) -> bool:
    """True when ``key`` carries something that cannot repeat across two
    distinct activations of the same recyclable resource id: a UUID, an
    explicit ``:evt:`` marker, a numeric generation, or an opaque high-entropy
    token (a >= 24-hex run — a dash-less UUID or a ``secrets.token_hex`` id,
    e.g. the library's own ``release:act_<hex>`` key). False means the key
    looks like a bare recyclable id — the ticket-20260810 bug shape.
    Exposed for the Go port's parity test + for consumers that want to
    pre-validate a key before calling increment/decrement."""
    if not key:
        return True  # no idempotency requested at all is a separate, valid choice
    return bool(
        _UUID_RE.search(key) or _EVT_MARKER_RE.search(key)
        or _GENERATION_SEGMENT_RE.search(key) or _OPAQUE_TOKEN_RE.search(key)
    )


class RecyclableIdempotencyKeyError(ValueError):
    """Raised in strict mode when an idempotency key looks like a bare
    recyclable resource/container id with no unique-per-event component."""


def _strict_idempotency_enforcement() -> bool:
    return os.getenv(_STRICT_IDEMPOTENCY_ENV, "").strip().lower() in ("1", "true", "yes")


def guard_idempotency_key(key: Optional[str]) -> None:
    """Contract guard (P1.5): reject-or-warn an idempotency key shaped like a
    bare recyclable id. Called on every gauge increment/decrement/*_user path
    before the key ever reaches Lua."""
    if not key or idempotency_key_has_unique_component(key):
        return
    msg = (
        f"quota idempotency key {key!r} looks like a bare recyclable resource id "
        "(no activation-generation/:evt:/uuid component) -- this is exactly the "
        "shape that caused ticket 20260810's stuck-gauge collision: a reused "
        "container/pool id makes a genuinely NEW lifecycle event compute the SAME "
        "key as a past one, so it is silently treated as a duplicate and the "
        "mutation is dropped. Key on a unique-per-lifecycle-event id instead: an "
        "activation/claim generation bumped on every reactivation (trailing "
        "':<int>'), or a fresh UUID per event (':evt:<uuid4>'). See ab0t-quota "
        f"README.md 'Idempotency keys' for the contract + a worked example. (Set "
        f"{_STRICT_IDEMPOTENCY_ENV}=1 to make this a hard error instead of a warning.)"
    )
    if _strict_idempotency_enforcement():
        raise RecyclableIdempotencyKeyError(msg)
    logger.warning("quota_idempotency_key_looks_recyclable %s", msg)

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
        guard_idempotency_key(idempotency_key)  # P1.5 contract guard
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
        guard_idempotency_key(idempotency_key)  # P1.5 contract guard
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
        guard_idempotency_key(idempotency_key)  # P1.5 contract guard
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
        guard_idempotency_key(idempotency_key)  # P1.5 contract guard
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
        guard_idempotency_key(idempotency_key)  # P1.5 contract guard
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
        guard_idempotency_key(idempotency_key)  # P1.5 contract guard
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
