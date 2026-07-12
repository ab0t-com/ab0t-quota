"""D-43 — the shared cross-runtime conformance suite: the PYTHON runner.

Ticket 20260709_ab0t_quota_systemic_integrity_redesign (W-CONF, 2026-07-11).
Companion: information_conformance_suite_20260711.md (ticket dir).

One data file, two thin runners. This runner loads conformance/scenarios.json
(the canonical copy lives in THIS repo; ab0t-quota-go/conformance/ holds the
synced copy its runner reads) and replays every scenario against the Python
engine, asserting the recorded expectations. The Go runner
(ab0t-quota-go/conformance/conformance_suite_20260711_test.go) replays the
SAME file. A change to either runtime that breaks parity fails CI on the same
JSON row in both languages.

Adding a scenario = one JSON entry (via conformance/generate_scenarios.py,
which derives every expectation by EXECUTING this reference implementation).
Adding a runtime = one runner.

The fixture folds in the D-48 enforcement matrix and the D-58 golden-semantics
set; the per-runtime originals (test_enforcement_contract_matrix_20260710.py,
golden_semantics_python_20260710.json) REMAIN — this suite is additive, per the
D-18/D-20 rule (never rewrite a peer's test).

Known divergences (D-54 pattern) are carried IN the fixture as
`expect_go`/`expect_events_go` overrides + a `known_divergences` register —
this runner always asserts the Python-side `expect`, so a divergence is
documented, never blessed into the Python contract.
"""
from __future__ import annotations

import copy
import json
import os
import sys

import fakeredis.aioredis
import pytest

CONF_DIR = os.path.join(os.path.dirname(__file__), "..", "conformance")
sys.path.insert(0, CONF_DIR)

import generate_scenarios as ref  # the executor IS the reference mapping

with open(os.path.join(CONF_DIR, "scenarios.json")) as _f:
    _DOC = json.load(_f)
SCENARIOS = _DOC["scenarios"]

PYTHON_GATES = {"check_resource", "check_bundle", "acquire_resource", "acquire_bundle"}


async def _fresh_env(sc, engine_cls=None):
    redis = fakeredis.aioredis.FakeRedis()
    fault = any(s.get("do") == "fault" for s in sc.get("setup", []))
    store = (ref._PutFailsStore() if fault
             else ref.InMemoryActivationStore())
    engine = ref._build_engine(redis, sc["config"], store)
    if engine_cls is not None:
        engine.__class__ = engine_cls  # negative-control hook
    cap = ref._CaptureAlerts()
    engine.set_alert_manager(cap)
    ctx = {"handles": {}, "provider": None}
    await ref._seed(redis, store, sc.get("setup", []), ctx)
    return redis, engine, cap, ctx


async def _replay(sc, engine_cls=None) -> list[str]:
    """Replay one scenario against the Python engine; return the list of
    divergence descriptions (empty == conformant)."""
    diverged: list[str] = []

    if sc["kind"] == "gate_matrix":
        outcomes = {}
        for gate in sc["gates"]:
            if gate not in PYTHON_GATES:
                continue
            redis, engine, _cap, _ctx = await _fresh_env(sc, engine_cls)
            try:
                outcomes[gate] = await ref._run_gate(engine, gate, sc)
            finally:
                await redis.flushall()
                await redis.aclose()
        assert outcomes, f"{sc['id']}: runner executed NO gates — vacuous row"
        if len(set(outcomes.values())) != 1:
            diverged.append(f"gates disagree (D-48 class): {outcomes}")
        for gate, got in outcomes.items():
            if got is not sc["expect"]["admitted"]:
                diverged.append(
                    f"gate {gate}: admitted={got}, fixture={sc['expect']['admitted']}")
        return diverged

    redis, engine, cap, ctx = await _fresh_env(sc, engine_cls)
    try:
        for i, op in enumerate(sc["ops"]):
            observed = await ref._run_op(op, engine, redis, ctx, sc)
            expect = op["expect"]  # Python side ALWAYS asserts `expect`
            if expect.get("error"):
                if not observed.get("error"):
                    diverged.append(
                        f"op[{i}]({op['op']}): fixture expects an ERROR, got {observed}")
                continue
            if observed.get("error"):
                diverged.append(f"op[{i}]({op['op']}): unexpected ERROR")
                continue
            for k, v in expect.items():
                if observed.get(k) != v:
                    diverged.append(
                        f"op[{i}]({op['op']}).{k}: observed={observed.get(k)!r}, "
                        f"fixture={v!r}")
        if "expect_events" in sc and cap.events != sc["expect_events"]:
            diverged.append(
                f"events: observed={cap.events}, fixture={sc['expect_events']}")
        return diverged
    finally:
        await redis.flushall()
        await redis.aclose()


class TestConformanceSuite:
    pytestmark = pytest.mark.asyncio

    @pytest.mark.parametrize("sc", copy.deepcopy(SCENARIOS),
                             ids=[s["id"] for s in SCENARIOS])
    async def test_scenario(self, sc):
        diverged = await _replay(sc)
        assert not diverged, (
            f"RUNTIME DIVERGENCE (python) on {sc['id']} "
            f"[{', '.join(sc.get('decision_refs', []))}]:\n  " + "\n  ".join(diverged))


class TestFixtureIntegrity:
    def test_fixture_declares_contract_metadata(self):
        assert _DOC["conformance_version"] == "1"
        assert "python" in _DOC["reference_runtime"]
        # every known divergence carries a decision ref — an undocumented
        # divergence is a silent blessing (D-43/D-54).
        for kd in _DOC["known_divergences"]:
            assert kd.get("decision_refs"), f"known_divergence {kd} lacks a decision ref"
            assert kd.get("note"), f"known_divergence {kd} lacks a note"

    def test_every_ticket_scenario_class_is_present(self):
        """The folded-in inventory the ticket demands (D-43 item 3)."""
        ids = {s["id"] for s in SCENARIOS}
        for required in [
            "gm_under_limit_kill_switch",          # D-48 matrix
            "gm_over_limit_shadow",                # D-55
            "gm_unknown_bundle_enforce",           # D-14
            "gm_unknown_tier_enforce",             # D-14
            "seq_double_release",                  # D-58
            "seq_settle_after_release",            # D-46/D-58
            "seq_release_unknown_id",              # D-58
            "seq_increment_negative_delta_magnitude",  # GT-02
            "seq_rate_cost_event_count",           # GT-04
            "seq_cost_zero_gauge",                 # F-2
            "seq_acquire_persist_failure_fail_closed",  # D-27/D-31
            "rec_provider_wins_existence_divergence",   # D-33
            "rec_provider_unreachable_do_nothing",      # D-31
            "rec_recent_touch_guard",              # D-62
        ]:
            assert required in ids, f"required conformance scenario missing: {required}"


# --- NEGATIVE CONTROL (in-runner): a suite that cannot catch a broken guard
# certifies nothing. Blind ONE gate to ONE knob and the kill-switch row must
# report a divergence. (The cross-runtime scratch-copy control is recorded in
# information_conformance_suite_20260711.md.)
class _KillBlindEngine(ref.QuotaEngine):
    async def acquire(self, *a, **k):
        real = self._enforcement
        self._enforcement = real.model_copy(update={"global_kill_switch": False})
        try:
            return await super().acquire(*a, **k)
        finally:
            self._enforcement = real


class TestNegativeControl:
    pytestmark = pytest.mark.asyncio

    async def test_suite_flags_a_kill_switch_blind_acquire(self):
        sc = copy.deepcopy(next(
            s for s in SCENARIOS if s["id"] == "gm_under_limit_kill_switch"))
        diverged = await _replay(sc, engine_cls=_KillBlindEngine)
        assert diverged, (
            "the conformance suite FAILED to flag an acquire() that ignores the "
            "global kill switch — it has no teeth")
        assert any("acquire" in d for d in diverged), diverged
