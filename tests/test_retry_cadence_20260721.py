"""T-28 — the BEHAVIOURAL leg of the D-2 retry contract (ST-RESOLVE-1
Clause 7). The conformance AST binding proves the LITERALS agree with the
declared `retry_contract`; this leg proves the RUNTIME honours them (Go has
the equivalent; an AST pin alone proves constants, not behaviour).

Every expectation is read from scenarios.json's declared contract — the same
data file that drives Go and the AST binding — never re-typed here.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import redis.exceptions as rex
from fastapi import FastAPI

REPO = Path(__file__).resolve().parents[1]


def _contract() -> dict:
    doc = json.loads((REPO / "conformance" / "scenarios.json").read_text())
    item = next(i for i in doc["structural_conformance"] if i["id"] == "ST-RESOLVE-1")
    return item["retry_contract"]


class _FailRedis:
    def __init__(self, exc_factory):
        self.pings = 0
        self._exc = exc_factory

    async def ping(self):
        self.pings += 1
        raise self._exc()


def _plan():
    return {"redis_url": SimpleNamespace(
        value="redis://declared-host:6399/0", source="config:storage.redis_url")}


async def _run_gate(redis, storage: dict, sleeps: list):
    from ab0t_quota.redis_preflight import RedisUnreachableError
    from ab0t_quota.setup import _gate_redis_reachable

    real_sleep = asyncio.sleep

    async def rec_sleep(s):
        sleeps.append(s)
        await real_sleep(min(s, 0.01))  # record the REQUESTED cadence, sleep tiny

    app = FastAPI()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(asyncio, "sleep", rec_sleep)
        t0 = time.monotonic()
        with pytest.raises(RedisUnreachableError) as ei:
            await _gate_redis_reachable(app, redis, {"storage": storage}, _plan())
        return ei.value, time.monotonic() - t0


@pytest.mark.asyncio
async def test_auth_never_consumes_the_retry_budget():
    """Contract: auth_never_consumes_budget — a wrong password refuses on the
    FIRST attempt even under the default 30s budget (waiting cannot heal a
    credential), within seconds, not the budget."""
    rc = _contract()
    assert rc["auth_never_consumes_budget"] is True
    fake = _FailRedis(lambda: rex.AuthenticationError("WRONGPASS"))
    sleeps: list = []
    err, elapsed = await _run_gate(fake, {}, sleeps)  # storage omits the key => default
    assert fake.pings == 1, f"auth was retried {fake.pings} times — budget consumed"
    assert sleeps == [], f"auth failure slept {sleeps} — budget consumed"
    assert elapsed < 2.0
    assert err.kind == "auth"


@pytest.mark.asyncio
async def test_unreachable_retries_with_the_declared_cadence_then_refuses():
    """Contract: backoff_initial_seconds doubling within the budget, then the
    typed refusal — cadence asserted from the DECLARED numbers."""
    rc = _contract()
    initial = rc["backoff_initial_seconds"]
    budget = initial * 2.4  # room for exactly the first backoff step
    fake = _FailRedis(lambda: rex.ConnectionError("connection refused"))
    sleeps: list = []
    err, _ = await _run_gate(
        fake, {"connect_retry_seconds": budget}, sleeps)
    assert fake.pings >= 2, "an unreachable store must be retried within the budget"
    assert sleeps and sleeps[0] == initial, \
        f"first backoff {sleeps[:1]} != declared initial {initial}"
    for a, b in zip(sleeps, sleeps[1:]):
        assert b == min(a * 2, rc["backoff_cap_seconds"]), \
            f"backoff cadence {sleeps} is not doubling-capped as declared"
    assert err.kind == "unreachable"


@pytest.mark.asyncio
async def test_zero_budget_fails_immediately():
    rc = _contract()
    assert rc["zero_means"] == "fail immediately"
    fake = _FailRedis(lambda: rex.ConnectionError("connection refused"))
    sleeps: list = []
    err, elapsed = await _run_gate(fake, {"connect_retry_seconds": 0}, sleeps)
    assert fake.pings == 1 and sleeps == []
    assert elapsed < 2.0
