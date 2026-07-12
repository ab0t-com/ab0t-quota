"""D-48 — the enforcement-contract matrix (the structural fix).

Ticket 20260709_ab0t_quota_systemic_integrity_redesign. Companion:
information_tests_activation_algebra_20260710.md.

WHY THIS EXISTS
---------------
D-14 (unknown bundle / unknown tier) and D-15 (kill switch / enabled / shadow) were
implemented against `check()` / `check_for_bundle()`. `acquire()` was written later,
by a different worker, and inherited NEITHER — so the primitive the redesign PROMOTES
as the retry-safe replacement silently ADMITTED under the kill switch and on a config
typo (W-T1's two fail-open findings). *We fixed the deprecated path and shipped the
recommended one without the fix.* That is the class D-48 names.

The cure is not another one-off test — it is an INVARIANT over the whole surface:

    every enforcement knob × every admission gate → IDENTICAL outcome.

This matrix is data-driven: a new gate is one entry in ``ADMISSION_GATES``; a new knob
or scenario is one row in ``SCENARIOS``. A gate that ignores a knob DISAGREES with its
siblings and the matrix goes red — so "add an entry point that forgets a knob" cannot
pass review. (This is the first scenario of the cross-runtime conformance suite, D-43:
Go's ``Acquire`` must pass the identical matrix.)

SCOPE NOTE (honest, not a loophole)
-----------------------------------
The three ADMISSION GATES (`check`, `check_for_bundle`, `acquire`) must give an
IDENTICAL allow/deny. `increment` / `increment_for_bundle` are a DIFFERENT class by
design — post-provisioning counters that "count at the fact, never refuse" (D-24):
refusing to count an already-provisioned resource is the phantom-headroom defect this
ticket exists to kill. They are asserted separately (``TestCounterContractD24``): they
must COUNT under every knob and be LOUD (never silent) on an unknown bundle — but they
do NOT deny, and forcing them to would reintroduce QG-06.
"""
from __future__ import annotations

import fakeredis.aioredis
import pytest

from ab0t_quota.activations import InMemoryActivationStore
from ab0t_quota.counters.gauge import GaugeCounter
from ab0t_quota.engine import QuotaEngine
from ab0t_quota.models.core import (
    CounterType, EnforcementConfig, ResourceDef, TierConfig, TierLimits,
)
from ab0t_quota.models.requests import QuotaCheckRequest, QuotaIncrementRequest
from ab0t_quota.providers import StaticTierProvider
from ab0t_quota.registry import ResourceRegistry

RK = "rk.c"
CONC = ResourceDef(service="t", resource_key=RK, display_name="C",
                   counter_type=CounterType.GAUGE, unit="u")
LIMIT = 1


def _build_engine(redis, *, enf: dict, tier: str):
    reg = ResourceRegistry()
    reg.register(CONC)
    tiers = {"free": TierConfig(tier_id="free", display_name="F",
                                limits={RK: TierLimits(limit=LIMIT)})}
    return QuotaEngine(
        redis=redis, tier_provider=StaticTierProvider({"o": tier}),
        registry=reg, tiers=tiers, resource_bundles={"b": [RK]},
        activation_store=InMemoryActivationStore(),
        enforcement=EnforcementConfig(**enf),
    )


# --- the admission gates, each normalized to a single allow/deny bool -------
async def _run_check(engine, sc) -> bool:
    r = await engine.check(QuotaCheckRequest(org_id="o", resource_key=RK, increment=1.0))
    return r.allowed


async def _run_check_for_bundle(engine, sc) -> bool:
    r = await engine.check_for_bundle("o", sc["bundle"])
    return r.allowed


async def _run_acquire(engine, sc) -> bool:
    r = await engine.acquire("o", sc["bundle"])
    return r.admitted


# A new admission entry point is ONE row here — and is then held to every scenario.
ADMISSION_GATES = {
    "check": _run_check,
    "check_for_bundle": _run_check_for_bundle,
    "acquire": _run_acquire,
}


# --- the scenarios: every knob-state × a well-defined expected outcome -------
# `gates` names which gates the scenario is comparable across (check has no bundle
# concept, so unknown-bundle scenarios exclude it; it raises on an unknown resource).
SCENARIOS = [
    dict(name="under_limit_baseline", enf={}, tier="free", bundle="b", fill=False,
         gates=("check", "check_for_bundle", "acquire"), expected=True),
    dict(name="over_limit_baseline", enf={}, tier="free", bundle="b", fill=True,
         gates=("check", "check_for_bundle", "acquire"), expected=False),
    # the kill switch must DENY every gate, under OR over the limit.
    dict(name="under_limit_kill_switch", enf=dict(global_kill_switch=True), tier="free",
         bundle="b", fill=False, gates=("check", "check_for_bundle", "acquire"),
         expected=False),
    dict(name="over_limit_kill_switch", enf=dict(global_kill_switch=True), tier="free",
         bundle="b", fill=True, gates=("check", "check_for_bundle", "acquire"),
         expected=False),
    # enforcement disabled: allow everything, even past the limit (N-1 — the knob
    # must actually do something; acquire used to still deny here).
    dict(name="over_limit_disabled", enf=dict(enabled=False), tier="free", bundle="b",
         fill=True, gates=("check", "check_for_bundle", "acquire"), expected=True),
    # shadow: a would-be DENY becomes an ALLOW on every gate.
    dict(name="over_limit_shadow", enf=dict(shadow_mode=True), tier="free", bundle="b",
         fill=True, gates=("check", "check_for_bundle", "acquire"), expected=True),
    # unknown bundle: deny in enforce mode; allow under shadow. (No `check` — it has no
    # bundle concept and raises on an unknown resource.)
    dict(name="unknown_bundle_baseline", enf={}, tier="free", bundle="typo", fill=False,
         gates=("check_for_bundle", "acquire"), expected=False),
    dict(name="unknown_bundle_shadow", enf=dict(shadow_mode=True), tier="free",
         bundle="typo", fill=False, gates=("check_for_bundle", "acquire"), expected=True),
    # unknown tier: explicit deny, NOT shadowed — a config error must surface (D-14).
    dict(name="unknown_tier_baseline", enf={}, tier="ghost", bundle="b", fill=False,
         gates=("check", "check_for_bundle", "acquire"), expected=False),
    dict(name="unknown_tier_shadow", enf=dict(shadow_mode=True), tier="ghost",
         bundle="b", fill=False, gates=("check", "check_for_bundle", "acquire"),
         expected=False),
]


async def _outcome(gate_name: str, sc: dict, *, engine_cls=QuotaEngine) -> bool:
    """Build a FRESH engine (acquire spends, so gates must not share state), apply the
    scenario's setup, run one gate, return its normalized allow/deny."""
    redis = fakeredis.aioredis.FakeRedis()
    try:
        engine = _build_engine(redis, enf=sc["enf"], tier=sc["tier"])
        if engine_cls is not QuotaEngine:
            engine.__class__ = engine_cls  # swap in a deliberately-broken gate
        if sc["fill"]:
            await GaugeCounter(redis, "o", RK).increment(float(LIMIT))  # to the limit
        return await ADMISSION_GATES[gate_name](engine, sc)
    finally:
        await redis.flushall()
        await redis.aclose()


class TestEnforcementContractMatrix:
    @pytest.mark.parametrize("sc", SCENARIOS, ids=[s["name"] for s in SCENARIOS])
    @pytest.mark.asyncio
    async def test_all_gates_agree_and_match_expected(self, sc):
        """THE invariant: for this knob-state, every applicable admission gate produces
        the SAME allow/deny, and it equals the declared expectation."""
        outcomes = {g: await _outcome(g, sc) for g in sc["gates"]}
        assert len(set(outcomes.values())) == 1, (
            f"admission gates DISAGREE for {sc['name']}: {outcomes} — a gate ignores a "
            f"knob its siblings honor (the D-48 defect class)")
        assert all(v is sc["expected"] for v in outcomes.values()), (
            f"{sc['name']}: gates agreed on {outcomes} but expected {sc['expected']}")


# --- NEGATIVE CONTROL: prove the matrix CATCHES an ignored knob -------------
class _KillBlindAcquireEngine(QuotaEngine):
    """A deliberately-broken engine whose `acquire` FORGETS the global kill switch —
    exactly the D-48 defect (a new/edited gate that skips a knob). The matrix must
    catch it."""
    async def acquire(self, *a, **k):
        real = self._enforcement
        # blind ONLY the kill switch for this call, then restore — simulates a gate
        # written without the kill-switch guard.
        self._enforcement = real.model_copy(update={"global_kill_switch": False})
        try:
            return await super().acquire(*a, **k)
        finally:
            self._enforcement = real


class TestMatrixNegativeControl:
    @pytest.mark.asyncio
    async def test_matrix_catches_a_gate_that_ignores_the_kill_switch(self):
        """Delete one guard from one gate and prove the agreement assertion fails.
        A matrix that has never caught anything has told you nothing."""
        sc = next(s for s in SCENARIOS if s["name"] == "under_limit_kill_switch")
        # the CORRECT gates deny under the kill switch:
        assert await _outcome("check", sc) is False
        assert await _outcome("acquire", sc) is False
        # the BLIND acquire IGNORES the switch and admits:
        blind = await _outcome("acquire", sc, engine_cls=_KillBlindAcquireEngine)
        assert blind is True, "the broken gate must actually ignore the knob"
        # therefore it DISAGREES with its siblings — which is exactly what the matrix's
        # `len(set(outcomes)) == 1` assertion flags as a failure:
        outcomes = {"check": await _outcome("check", sc),
                    "check_for_bundle": await _outcome("check_for_bundle", sc),
                    "acquire_blind": blind}
        assert len(set(outcomes.values())) != 1, (
            "the matrix FAILED to distinguish a kill-switch-blind gate — it has no teeth")


# --- the post-fact counter class (D-24) — a DIFFERENT contract, asserted apart ---
def _counter_engine(redis, *, enf: dict, tier="free"):
    reg = ResourceRegistry()
    reg.register(CONC)
    tiers = {"free": TierConfig(tier_id="free", display_name="F",
                                limits={RK: TierLimits(limit=LIMIT)})}
    return QuotaEngine(
        redis=redis, tier_provider=StaticTierProvider({"o": tier}), registry=reg,
        tiers=tiers, resource_bundles={"b": [RK]},
        activation_store=InMemoryActivationStore(), enforcement=EnforcementConfig(**enf))


class TestCounterContractD24:
    """increment / increment_for_bundle are post-provisioning counters: they COUNT at
    the fact and never DENY (D-24). A knob may change what is authorized (the gates),
    never whether an existing resource is acknowledged — that inversion is QG-06."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("enf", [dict(global_kill_switch=True), dict(enabled=False)],
                             ids=["kill_switch", "disabled"])
    async def test_increment_counts_regardless_of_knob(self, enf):
        redis = fakeredis.aioredis.FakeRedis()
        try:
            engine = _counter_engine(redis, enf=enf)
            # count past the limit — must record reality, never refuse
            await engine.increment(QuotaIncrementRequest(org_id="o", resource_key=RK))
            v = await engine.increment(QuotaIncrementRequest(org_id="o", resource_key=RK))
            assert v == 2.0, "increment must COUNT at the fact even under an enforcement knob (D-24)"
        finally:
            await redis.flushall()
            await redis.aclose()

    @pytest.mark.asyncio
    async def test_increment_for_bundle_unknown_is_loud_not_silent(self, caplog):
        """A typo'd bundle name must not silently count NOTHING (that leaves the
        provisioned resources uncounted → phantom headroom). It warns, loudly."""
        import logging
        redis = fakeredis.aioredis.FakeRedis()
        try:
            engine = _counter_engine(redis, enf={})
            with caplog.at_level(logging.WARNING, logger="ab0t_quota"):
                out = await engine.increment_for_bundle("o", "typo_bundle")
            assert out == {}, "an unknown bundle counts nothing (it can't know what to count)"
            assert any("unknown_bundle" in r.message for r in caplog.records), (
                "an unknown bundle must be LOUD, never a silent no-op")
        finally:
            await redis.flushall()
            await redis.aclose()
