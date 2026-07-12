"""Real-Redis conformance for the money-critical Lua (W-RR, 2026-07-11).

The rest of the suite runs on `fakeredis[lua]` (lupa). Per D-57 the emulators
disagree with each other AND with real Redis, and no Lua had ever met a real
`redis-server`. This module points the ACTUAL library scripts at a real server.

SKIPPED unless `REAL_REDIS_ADDR` is set (e.g. "127.0.0.1:6500"), so it never
breaks the emulator-only CI. Reproduce:

    docker run -d --name t-redis -p 127.0.0.1:6500:6379 redis:7-alpine \
        redis-server --save "" --appendonly no --maxmemory-policy noeviction
    REAL_REDIS_ADDR=127.0.0.1:6500 .venv/bin/python -m pytest \
        tests/test_real_redis_conformance_20260711.py -v

Findings recorded by the leg (all FOUND against redis:7.4.9):
  * Atomicity HOLDS under real concurrency: 50 racers, exactly `limit` admitted.
  * SET NX EX, HSETNX, floor, dedup, _ACQUIRE marshaling, _TRANSITION, _CAS_RECLAIM
    all match fakeredis behaviour and are correct.
  * INCRBYFLOAT DIVERGES: real Redis uses long double (0.1+0.2 -> "0.3";
    1.0-10x0.1 -> "0"), fakeredis uses IEEE754 double ("0.30000000000000004";
    residual 1.4e-16). No library logic depends on the exact bits — admission
    is computed in Lua doubles, so the fractional-limit DENY holds identically.
  * D-59 claim-burn is REAL on real Redis: a non-finite delta reaching _INCR with
    an idem claim burns the SET NX claim (INCRBYFLOAT aborts, no rollback). The
    client-side `finite_magnitude` guard (base.py) fires BEFORE eval, so the
    library API never reaches this — verified below.
"""
import asyncio
import math
import os

import pytest

import redis.asyncio as aioredis

from ab0t_quota.counters.gauge import GaugeCounter
from ab0t_quota.counters.base import finite_magnitude


ADDR = os.getenv("REAL_REDIS_ADDR")
pytestmark = pytest.mark.skipif(
    not ADDR, reason="REAL_REDIS_ADDR not set — real-Redis conformance is operator-gated"
)


def _client():
    host, _, port = ADDR.partition(":")
    return aioredis.Redis(host=host, port=int(port or 6379), decode_responses=True)


@pytest.mark.asyncio
async def test_incrbyfloat_never_widens_and_floors_clean():
    r = _client()
    try:
        await r.flushall()
        g = GaugeCounter(r, "org-c", "sandboxes")
        await g.reset(1.0)
        v = 1.0
        for _ in range(10):
            v = await g.decrement(0.1)
        assert v >= 0.0, "gauge went negative through float residue (forbidden D-31)"
        assert v < 1e-9, f"residual {v} unexpectedly large on real Redis"
        # ten more stay floored at zero
        for _ in range(10):
            v = await g.decrement(0.1)
        assert v == 0.0
    finally:
        await r.aclose()


@pytest.mark.asyncio
async def test_fractional_boundary_deny_holds_on_real_redis():
    """The admission comparison runs in Lua doubles, not long-double storage, so
    the fractional-limit deny is stable across emulator and real Redis
    (pre-deploy gate A1, closed here)."""
    r = _client()
    try:
        await r.flushall()
        g = GaugeCounter(r, "org-c2", "sandboxes")
        _, a1 = await g.try_increment(0.1, 0.3)
        assert a1
        _, a2 = await g.try_increment(0.2, 0.3)
        assert not a2, "0.1 then 0.2 at limit 0.3 was ADMITTED on real Redis (flipped from fakeredis DENY)"
    finally:
        await r.aclose()


@pytest.mark.asyncio
async def test_client_side_finite_guard_fires_before_eval():
    """D-57/D-59: non-finite deltas must be rejected client-side, before the Lua,
    so a NaN can never burn an idempotency claim on real Redis."""
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            finite_magnitude(bad)
    r = _client()
    try:
        await r.flushall()
        g = GaugeCounter(r, "org-c3", "sandboxes")
        with pytest.raises(ValueError):
            await g.increment(float("nan"), idempotency_key="k")
        # the claim key must NOT exist — the guard fired before any Redis call
        assert await r.get("quota:org-c3:sandboxes:idem:k") is None
    finally:
        await r.aclose()


@pytest.mark.asyncio
async def test_atomicity_under_real_concurrency():
    """The property fakeredis (single-threaded, in-process) CANNOT prove:
    N real concurrent connections race an atomic acquire; exactly `limit` win."""
    limit, racers = 10, 50

    async def one():
        r = _client()
        try:
            g = GaugeCounter(r, "org-race", "sandboxes")
            _, admitted = await g.try_increment(1, limit)
            return 1 if admitted else 0
        finally:
            await r.aclose()

    admin = _client()
    try:
        for _ in range(3):
            await admin.flushall()
            res = await asyncio.gather(*[one() for _ in range(racers)])
            assert sum(res) == limit, f"admitted {sum(res)} of a limit of {limit} — NOT atomic"
            final = await admin.get("quota:org-race:sandboxes:gauge")
            assert float(final) == limit
    finally:
        await admin.aclose()
