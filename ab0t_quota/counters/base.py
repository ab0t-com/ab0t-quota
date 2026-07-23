"""Abstract base for all counter types."""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import Optional

from redis.asyncio import Redis

from ..keyspace import Keyspace

# --- Dual-keyspace Lua helpers (K-1/K-3, keyspace spec §3.2) -----------------
# Every counter script is composed as dual_lua(nk, dual_argv, body):
#   KEYS[1..NK] = primary (authoritative) shape; KEYS[NK+1..2NK] = secondary
#   (only when DUAL). ARGV[dual_argv]=dual flag, ARGV[dual_argv+1]=primary-is-v2.
# ARGV[2] is the idem TTL in every script that claims (unchanged convention).
# Every Lua key argument below is a literal KEYS[...] reference — the QI-09
# static auditor (test_lua_keys_declared_qi09_20260710.py) enforces this;
# helper aliases would defeat it, so the dual side is always KEYS[NK+i] inline.
_HELPERS = """
local function seedv2(i)
  if not DUAL then return end
  if V2P then
    if redis.call('EXISTS', KEYS[i]) == 0 then
      local v = redis.call('GET', KEYS[NK+i])
      if v then redis.call('SET', KEYS[i], v) end
    end
  else
    if redis.call('EXISTS', KEYS[NK+i]) == 0 then
      local v = redis.call('GET', KEYS[i])
      if v then redis.call('SET', KEYS[NK+i], v) end
    end
  end
end
local function idem_dup(i)
  if redis.call('GET', KEYS[i]) then return true end
  if DUAL and redis.call('GET', KEYS[NK+i]) then return true end
  return false
end
local function idem_claim(i)
  redis.call('SET', KEYS[i], '1', 'NX', 'EX', ARGV[2])
  if DUAL then redis.call('SET', KEYS[NK+i], '1', 'NX', 'EX', ARGV[2]) end
end
local function incrboth(i, d)
  local v = redis.call('INCRBYFLOAT', KEYS[i], d)
  if DUAL then redis.call('INCRBYFLOAT', KEYS[NK+i], d) end
  return v
end
local function incrfloorboth(i, d)
  local v = redis.call('INCRBYFLOAT', KEYS[i], d)
  if tonumber(v) < 0 then redis.call('SET', KEYS[i], '0'); v = '0' end
  if DUAL then
    local w = redis.call('INCRBYFLOAT', KEYS[NK+i], d)
    if tonumber(w) < 0 then redis.call('SET', KEYS[NK+i], '0') end
  end
  return v
end
local function incrintboth(i)
  redis.call('INCR', KEYS[i])
  if DUAL then redis.call('INCR', KEYS[NK+i]) end
end
local function expboth(i, t)
  redis.call('EXPIRE', KEYS[i], t)
  if DUAL then redis.call('EXPIRE', KEYS[NK+i], t) end
end
"""


def dual_lua(nk: str, dual_argv: int, body: str) -> str:
    """Compose a dual-capable script: NK may be a Lua expression (``_ACQUIRE``
    computes it from ARGV). Single-mode (DUAL false) behaviour is identical to
    the pre-K-3 scripts — verified by the existing counter suites."""
    return (
        f"local NK = {nk}\n"
        f"local DUAL = ARGV[{dual_argv}] == '1'\n"
        f"local V2P = ARGV[{dual_argv + 1}] == '1'\n"
        + _HELPERS + body
    )


def finite_magnitude(delta: float) -> float:
    """Validate + normalise a counter delta at the LIBRARY BOUNDARY, before
    any Lua runs (ticket 20260709, W-T3 / defect ET-03).

    * Non-finite (NaN/±inf) raises ValueError. If it reached the Lua instead,
      the script would claim the idempotency key (SET NX / HSETNX) and THEN
      error on INCRBYFLOAT — Redis scripts do not roll back, so the claim
      persists and the caller's corrected retry is swallowed as a duplicate:
      the spend never lands (under-count / phantom headroom, the forbidden
      D-31 direction). Validating here keeps every claim paired with its
      mutation.
    * The magnitude (abs) is returned — counter deltas are magnitudes by
      library convention (increment adds |delta|, decrement subtracts
      |delta|), so a sign flip can never invert an operation and silently
      erase spend (D-31).
    """
    d = float(delta)
    if not math.isfinite(d):
        raise ValueError(
            f"counter delta must be finite, got {d!r} — refusing before Lua "
            "so the idempotency claim is not burned (D-31)"
        )
    return abs(d)


class Counter(ABC):
    """Base class for quota counters backed by Redis."""

    def __init__(self, redis: Redis, org_id: str, resource_key: str,
                 keyspace: Optional[Keyspace] = None):
        self._redis = redis
        self._org_id = org_id
        self._resource_key = resource_key
        # Default = v1 single-shape: bit-identical to pre-K-1 behaviour.
        self._ks = keyspace or Keyspace()

    @property
    def _key_prefix(self) -> str:
        """Primary (read-authoritative) shape prefix."""
        return self._ks._prefix(self._org_id, self._resource_key, self._ks.version)

    @property
    def _sv(self) -> Optional[int]:
        """Secondary shape version when dual-writing, else None."""
        return self._ks.secondary_version

    def _dual_argv(self) -> tuple[str, str]:
        return ("1" if self._sv else "0",
                "1" if self._ks.primary_is_v2 else "0")

    @abstractmethod
    async def get(self) -> float:
        """Read current counter value."""

    @abstractmethod
    async def increment(self, delta: float, idempotency_key: Optional[str] = None) -> float:
        """Add to counter. Returns new value."""

    @abstractmethod
    async def decrement(self, delta: float, idempotency_key: Optional[str] = None) -> float:
        """Subtract from counter. Returns new value. May raise for non-gauge types."""

    @abstractmethod
    async def reset(self, value: float = 0.0) -> None:
        """Force-set counter to a specific value (admin operation)."""
