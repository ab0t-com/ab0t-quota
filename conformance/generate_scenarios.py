"""D-43 — the shared, language-neutral conformance suite: scenario GENERATOR.

Ticket 20260709_ab0t_quota_systemic_integrity_redesign (W-CONF, 2026-07-11).
Companion: information_conformance_suite_20260711.md (ticket dir).

WHAT THIS IS
------------
`scenarios.json` is D-43's single machine-checked contract: ONE data file, a
thin runner in each runtime. It folds the two existing per-runtime conformance
artifacts into one suite:

  * the D-48 enforcement matrix   (tests/test_enforcement_contract_matrix_*.py,
                                   ab0t-quota-go/engine/enforcement_matrix_d48_test.go)
  * the D-58 golden-semantics fixture
                                  (ab0t-quota-go/engine/testdata/golden_semantics_*.json)

plus every hard-won boundary scenario from the ticket: double release,
settle-after-release, unknown-id release, negative-delta magnitude (GT-02/03),
rate-cost semantics (GT-04), cost==0, settle cost validation (D-47), the
crash-fail-closed DIRECTION (D-27/D-28/D-31), the reconciler precedence law
(D-33), and the recent-activity guard (D-62).

HOW EXPECTATIONS ARE DERIVED (the golden-key standard, D-58)
------------------------------------------------------------
Every `expect` in the emitted JSON is produced by EXECUTING the Python
reference implementation (fakeredis), never by reading code or writing a
number by hand. Where a scenario is NORMATIVE (a kill switch must deny; a
crash may only over-count — D-31), the scenario declares the required outcome
here and generation ABORTS if the executed Python reference disagrees — so a
Python regression fails generation instead of being silently blessed into the
fixture.

`expect_go` overrides are the KNOWN DIVERGENCES (D-54/D-58 pattern): values
derived by EXECUTING the Go runtime (the runner's observed output), recorded
with `known_divergence` + decision refs so the suite DOCUMENTS the delta
instead of silently blessing it or failing forever. Removing the override the
moment Go aligns turns the row back into a hard parity gate.

RUN
---
    /home/ubuntu/infra/infra/code/shared/ab0t-quota/.venv/bin/python \
        conformance/generate_scenarios.py

writes conformance/scenarios.json (canonical). Sync the copy the Go runner
reads:

    cp conformance/scenarios.json \
       /home/ubuntu/infra/infra/code/shared/ab0t-quota-go/conformance/scenarios.json

(The Go runner byte-compares the two when the canonical path is visible, so a
stale copy is a test failure, not a silent fork.)

Emulator caveat (D-57): executed on fakeredis (lupa); Go replays on miniredis
(gopher-lua). The suite pins EMULATOR-level agreement; real-Redis behaviour
for both lanes is the standing pre-deploy gate (V-BATCH blocker A1).
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import fakeredis.aioredis

from ab0t_quota.activations import (
    Activation, ActivationState, InMemoryActivationStore,
)
from ab0t_quota.counters.gauge import GaugeCounter
from ab0t_quota.engine import QuotaEngine
from ab0t_quota.models.core import (
    CounterType, EnforcementConfig, ResourceDef, TierConfig, TierLimits,
)
from ab0t_quota.models.requests import (
    QuotaCheckRequest, QuotaDecrementRequest, QuotaIncrementRequest,
)
from ab0t_quota.providers import StaticTierProvider
from ab0t_quota.reconcile import LibraryReconciler, ReconcileConfig
from ab0t_quota.registry import ResourceRegistry

ORG = "conf-org"
USER = "user-1"
G = "conf.g"           # the gauge resource every scenario shares
R = "conf.r"           # the rate resource (window 60s)
BUNDLE = "conf_bundle"
TIER = "conf_tier"
UNKNOWN_ID = "act_" + "0" * 32

OUT = os.path.join(os.path.dirname(__file__), "scenarios.json")

# ---------------------------------------------------------------------------
# scenario DATA. `expect` values present here are NORMATIVE (generation aborts
# if the executed reference disagrees); absent ones are filled by execution.
# `expect_go` / `known_divergence` document runtime deltas (see module doc).
# ---------------------------------------------------------------------------


def _cfg(limit=10.0, enforcement=None, org_tier=TIER, guard=None):
    c = {
        "resources": [
            {"key": G, "counter_type": "gauge"},
            {"key": R, "counter_type": "rate", "window_seconds": 60},
        ],
        "limits": {G: limit},
        "org_tier": org_tier,
        "bundles": {BUNDLE: [G]},
        "enforcement": enforcement or {},
    }
    if guard is not None:
        c["reconcile"] = {"activity_guard_seconds": guard}
    return c


ALL_GATES = ["check_resource", "check_bundle", "acquire_resource", "acquire_bundle"]
BUNDLE_GATES = ["check_bundle", "acquire_bundle"]
RESOURCE_GATES = ["check_resource", "acquire_resource"]


def _gm(id, *, enforcement=None, org_tier=TIER, fill=False, gates=ALL_GATES,
        admitted, refs, notes="", target=None, expect_go=None, known_divergence=None):
    sc = {
        "id": id, "kind": "gate_matrix", "group": "enforcement_matrix",
        "decision_refs": refs, "notes": notes,
        "config": _cfg(limit=1.0, enforcement=enforcement, org_tier=org_tier),
        "setup": ([{"do": "set_gauge", "resource": G, "value": 1.0}] if fill else []),
        "gates": list(gates),
        "expect": {"admitted": admitted},
    }
    if target:
        sc["target"] = target  # {"resource": ..} / {"bundle": ..} override (typos)
    if expect_go:
        sc["expect_go"] = expect_go
    if known_divergence:
        sc["known_divergence"] = known_divergence
    return sc


# (The QG-08 unknown-tier divergence record was retired when Go's fix landed — see
# DEFECTS.md for the history. A register may only describe divergences that still exist.)


SCENARIOS = [
    # ---- D-48: the enforcement matrix. Every knob × every admission gate. ----
    _gm("gm_under_limit_baseline", admitted=True, refs=["D-48"],
        notes="sanity: the matrix is not vacuously denying everything"),
    _gm("gm_over_limit_baseline", fill=True, admitted=False, refs=["D-48"]),
    _gm("gm_under_limit_kill_switch", enforcement={"global_kill_switch": True},
        admitted=False, refs=["D-48", "D-31"],
        notes="the emergency stop must stop EVERY gate, under or over the limit"),
    _gm("gm_over_limit_kill_switch", enforcement={"global_kill_switch": True},
        fill=True, admitted=False, refs=["D-48", "D-31"]),
    _gm("gm_over_limit_disabled", enforcement={"enabled": False}, fill=True,
        admitted=True, refs=["D-48", "D-55"],
        notes="enabled=false bypasses on every gate (N-1: the knob must DO something)"),
    _gm("gm_over_limit_shadow", enforcement={"shadow_mode": True}, fill=True,
        admitted=True, refs=["D-48", "D-55"],
        notes="shadow observes, never refuses — a hard-deny here blocks the rollout shadow exists to make safe"),
    _gm("gm_unknown_bundle_enforce", gates=BUNDLE_GATES, admitted=False,
        refs=["D-14", "D-48"], target={"bundle": "typo_bundle"},
        notes="a config typo must not silently disable enforcement"),
    _gm("gm_unknown_bundle_shadow", enforcement={"shadow_mode": True},
        gates=BUNDLE_GATES, admitted=True, refs=["D-14", "D-48"],
        target={"bundle": "typo_bundle"}),
    _gm("gm_unknown_resource_enforce", gates=RESOURCE_GATES, admitted=False,
        refs=["D-14", "D-48"], target={"resource": "typo.resource"},
        notes="resource-key analogue of the unknown-bundle hole; an error at a gate is a non-admission (fail-closed)"),
    _gm("gm_unknown_tier_enforce", org_tier="ghost_tier", admitted=False,
        refs=["D-14", "D-48"],
        notes="a mis-mapped paying org must NOT get a silent unlimited admit",
        ),
    _gm("gm_unknown_tier_shadow", enforcement={"shadow_mode": True},
        org_tier="ghost_tier", admitted=False, refs=["D-14", "D-48"],
        notes="unknown tier is a config ERROR, not a shadowable deny — it must surface"),

    # ---- D-58 golden semantics: the activation-lifecycle boundary algebra ----
    {"id": "seq_acquire_at_limit", "kind": "sequence", "group": "semantics",
     "decision_refs": ["D-58"], "config": _cfg(limit=2.0), "setup": [],
     "notes": "admit to the limit, deny past it; denied acquire mints NO activation id",
     "ops": [
         {"op": "acquire", "resource": G, "expect": {"admitted": True, "minted": True}},
         {"op": "acquire", "resource": G, "expect": {"admitted": True, "minted": True}},
         {"op": "acquire", "resource": G, "expect": {"admitted": False, "minted": False}},
         {"op": "get_gauge", "resource": G},
     ]},
    {"id": "seq_double_release", "kind": "sequence", "group": "semantics",
     "decision_refs": ["D-58", "D-20"], "config": _cfg(), "setup": [],
     "notes": "release is idempotent on activation_id; the second release performs NOTHING (no double-decrement)",
     "ops": [
         {"op": "acquire", "resource": G, "save": "a1"},
         {"op": "release_activation", "ref": "a1", "expect": {"performed": True}},
         {"op": "get_gauge", "resource": G},
         {"op": "release_activation", "ref": "a1", "expect": {"performed": False}},
         {"op": "get_gauge", "resource": G},
     ]},
    {"id": "seq_release_unknown_id", "kind": "sequence", "group": "semantics",
     "decision_refs": ["D-58"], "config": _cfg(), "setup": [],
     "notes": "an unknown activation_id releases NOTHING — it may not touch the gauge",
     "ops": [
         {"op": "acquire", "resource": G},
         {"op": "release_activation", "id": UNKNOWN_ID, "expect": {"performed": False}},
         {"op": "get_gauge", "resource": G, "expect": {"value": 1.0}},
     ]},
    {"id": "seq_settle_without_release", "kind": "sequence", "group": "semantics",
     "decision_refs": ["D-58", "D-60"], "config": _cfg(), "setup": [],
     "notes": "F-1: settle without release strands the gauge slot (over-count, fail-CLOSED); the reconciler heals it",
     "ops": [
         {"op": "acquire", "resource": G, "save": "a1"},
         {"op": "settle", "ref": "a1", "cost": "0.42", "expect": {"settled": True}},
         {"op": "get_gauge", "resource": G},
         {"op": "release_activation", "ref": "a1"},
         {"op": "get_gauge", "resource": G},
     ]},
    {"id": "seq_settle_after_release", "kind": "sequence", "group": "semantics",
     "decision_refs": ["D-58", "D-46"], "config": _cfg(), "setup": [],
     "notes": "settle after release still lands (the money is real); the re-settle with a "
              "DIFFERENT cost replays idempotently (first wins) AND fires the loud "
              "settle_conflict alert (D-46) — never a silent choice between two costs.",
     "expect_events": [{"kind": "settle_conflict", "resource": G}],
     "ops": [
         {"op": "acquire", "resource": G, "save": "a1"},
         {"op": "release_activation", "ref": "a1", "expect": {"performed": True}},
         {"op": "settle", "ref": "a1", "cost": "1.00", "expect": {"settled": True}},
         {"op": "settle", "ref": "a1", "cost": "9.99", "expect": {"settled": False}},
         {"op": "get_gauge", "resource": G},
     ]},
    {"id": "seq_duplicate_acquire_same_idem", "kind": "sequence", "group": "semantics",
     "decision_refs": ["D-58"], "config": _cfg(), "setup": [],
     "notes": "an idempotent replay admits WITHOUT re-spending and mints NO second id",
     "ops": [
         {"op": "acquire", "resource": G, "idem": "create-1"},
         {"op": "acquire", "resource": G, "idem": "create-1"},
         {"op": "get_gauge", "resource": G, "expect": {"value": 1.0}},
     ]},
    {"id": "seq_concurrent_release_one_id", "kind": "sequence", "group": "semantics",
     "decision_refs": ["D-58", "D-20"], "config": _cfg(), "setup": [],
     "notes": "10 racing releases of ONE id perform exactly one decrement (mark-then-decrement)",
     "ops": [
         {"op": "acquire", "resource": G, "save": "a1"},
         {"op": "acquire", "resource": G},
         {"op": "concurrent_release", "ref": "a1", "n": 10,
          "expect": {"performed_count": 1}},
         {"op": "get_gauge", "resource": G, "expect": {"value": 1.0}},
     ]},
    {"id": "seq_per_user_acquire_release", "kind": "sequence", "group": "semantics",
     "decision_refs": ["D-58"], "config": _cfg(), "setup": [],
     "notes": "per-user partition + seq key maintained alongside the org gauge, and released with it",
     "ops": [
         {"op": "acquire", "resource": G, "user": USER, "save": "a1"},
         {"op": "get_gauge", "resource": G, "scope": "user", "user": USER},
         {"op": "get_gauge", "resource": G, "scope": "user_seq", "user": USER},
         {"op": "release_activation", "ref": "a1", "expect": {"performed": True}},
         {"op": "get_gauge", "resource": G},
         {"op": "get_gauge", "resource": G, "scope": "user", "user": USER},
     ]},
    {"id": "seq_legacy_floor", "kind": "sequence", "group": "semantics",
     "decision_refs": ["D-58", "D-20"], "config": _cfg(), "setup": [],
     "notes": "QG-06: a gauge floors at zero — a decrement may never manufacture headroom",
     "ops": [
         {"op": "increment", "resource": G, "delta": 1},
         {"op": "decrement", "resource": G, "delta": 5},
         {"op": "get_gauge", "resource": G, "expect": {"value": 0.0}},
         {"op": "decrement", "resource": G, "delta": 1},
         {"op": "get_gauge", "resource": G, "expect": {"value": 0.0}},
     ]},
    {"id": "seq_increment_negative_delta_magnitude", "kind": "sequence",
     "group": "semantics", "decision_refs": ["D-58", "D-31"],
     "config": _cfg(), "setup": [{"do": "set_gauge", "resource": G, "value": 5.0}],
     "notes": "GT-02: a negative increment delta is a MAGNITUDE — it may never erase a spend "
              "(pre-fix Go applied the raw negative). Deprecated: next major REJECTS it (D-58).",
     "ops": [
         {"op": "increment", "resource": G, "delta": -3, "expect": {"value": 8.0}},
         {"op": "get_gauge", "resource": G, "expect": {"value": 8.0}},
     ]},
    {"id": "seq_decrement_negative_delta_boundary", "kind": "sequence",
     "group": "semantics", "decision_refs": ["D-58", "D-31"],
     "config": _cfg(), "setup": [{"do": "set_gauge", "resource": G, "value": 5.0}],
     "notes": "GT-03 at the ENGINE boundary: Python REJECTS decrement(delta<0) (model gt=0); "
              "Go Release() coerces to magnitude. Both are fail-safe directions but they "
              "DIVERGE; D-58's deprecation (reject at the boundary) is the convergence target.",
     "known_divergence": {
         "runtime": "go", "class": "accepted_pending_fix",
         "decision_refs": ["D-58"],
         "note": "Go Release(-2) == Release(2) (magnitude); Python raises a validation "
                 "error and the gauge does not move. expect_go values derived by "
                 "executing the Go runner."},
     "ops": [
         {"op": "decrement", "resource": G, "delta": -2,
          "expect": {"error": True}, "expect_go": {"error": False}},
         {"op": "get_gauge", "resource": G,
          "expect": {"value": 5.0}, "expect_go": {"value": 3.0}},
     ]},
    {"id": "seq_rate_cost_event_count", "kind": "sequence", "group": "semantics",
     "decision_refs": ["D-58", "D-31"], "config": _cfg(), "setup": [],
     "notes": "GT-04: a rate delta is an EVENT COUNT — increment(3) records 3 events. "
              "Pre-fix Go recorded ONE: its rate limit was silently 3x wider.",
     "ops": [
         {"op": "increment", "resource": R, "delta": 3, "expect": {"value": 3.0}},
     ]},
    {"id": "seq_rate_fractional_cost", "kind": "sequence", "group": "semantics",
     "decision_refs": ["D-60"], "config": _cfg(), "setup": [],
     "notes": "F-4: a fractional rate delta truncates to ZERO events in BOTH runtimes "
              "(agreed, and agreed-WRONG: fail-open; D-60 says reject non-integer rate "
              "costs at the boundary). This row pins the AGREEMENT until that lands.",
     "ops": [
         {"op": "increment", "resource": R, "delta": 0.5, "expect": {"value": 0.0}},
     ]},
    {"id": "seq_cost_zero_gauge", "kind": "sequence", "group": "semantics",
     "decision_refs": ["D-58"], "config": _cfg(), "setup": [],
     "notes": "F-2: cost==0 is a NO-OP in Python; Go coerces 0 to 1 (its CheckInput cannot "
              "distinguish unset from explicit zero) — a silent over-count. D-58: Go must "
              "adopt the no-op; until then this is a recorded divergence, not a blessing.",
     "known_divergence": {
         "runtime": "go", "class": "accepted_pending_fix",
         "decision_refs": ["D-58"],
         "note": "expect_go derived by executing the Go runner (Spend treats Cost==0 as 1)."},
     "ops": [
         {"op": "increment", "resource": G, "delta": 0,
          "expect": {"value": 0.0}, "expect_go": {"value": 1.0}},
         {"op": "get_gauge", "resource": G,
          "expect": {"value": 0.0}, "expect_go": {"value": 1.0}},
     ]},
    {"id": "seq_settle_rejects_nonfinite_cost", "kind": "sequence", "group": "money",
     "decision_refs": ["D-47"], "config": _cfg(), "setup": [],
     "notes": "D-47: NaN is money poison — one accepted NaN makes every subsequent read NaN. "
              "Python rejects BEFORE the ledger; Go has NO settle-cost validation (defect).",
     "ops": [
         {"op": "acquire", "resource": G, "save": "a1"},
         {"op": "settle", "ref": "a1", "cost": "NaN",
          "expect": {"error": True}},
     ]},
    {"id": "seq_settle_rejects_negative_cost", "kind": "sequence", "group": "money",
     "decision_refs": ["D-47"], "config": _cfg(), "setup": [],
     "notes": "D-47: a negative cost is a refund wearing usage's clothes — refunds go "
              "through billing, never settle.",
     "ops": [
         {"op": "acquire", "resource": G, "save": "a1"},
         {"op": "settle", "ref": "a1", "cost": "-1",
          "expect": {"error": True}},
     ]},
    {"id": "seq_acquire_persist_failure_fail_closed", "kind": "sequence",
     "group": "crash", "decision_refs": ["D-27", "D-28", "D-31"],
     "config": _cfg(),
     "setup": [{"do": "fault", "type": "activation_put_fails"}],
     "notes": "THE fail-direction row. When the activation store dies between the spend "
              "and the persist, acquire must ERROR (never a silent admitted=true) and the "
              "spend must STAY (over-count/deny is the only acceptable silent direction). "
              "A runtime that rolls the gauge back — or admits — under-counts: forbidden.",
     "ops": [
         {"op": "acquire", "resource": G, "expect": {"error": True}},
         {"op": "get_gauge", "resource": G, "expect": {"value": 1.0}},
     ]},
    {"id": "seq_over_limit_admitted_event", "kind": "sequence", "group": "observability",
     "decision_refs": ["D-24", "D-26"], "config": _cfg(limit=1.0), "setup": [],
     "notes": "D-24 B's premise: the legacy counter counts at the fact and never refuses, "
              "so crossing the limit MUST become an observable event (a sink, not a comment).",
     "ops": [
         {"op": "increment", "resource": G, "delta": 1},
         {"op": "increment", "resource": G, "delta": 1, "expect": {"value": 2.0}},
     ],
     "expect_events": [{"kind": "over_limit_admitted", "resource": G}]},

    # ---- D-33: the reconciler precedence law -------------------------------
    {"id": "rec_ledger_only_heals_drift", "kind": "sequence", "group": "reconcile",
     "decision_refs": ["D-33"], "config": _cfg(guard=0),
     "setup": [
         {"do": "seed_open", "resource": G, "n": 1, "age_seconds": 3600},
         {"do": "set_gauge", "resource": G, "value": 5.0},
     ],
     "notes": "D-33 §1: no provider -> the ledger is the best proxy for existence; a "
              "crash-orphaned over-count heals DOWN to Σ open activations. No divergence "
              "alarm (there is no second source to disagree with).",
     "ops": [
         {"op": "reconcile_org",
          "expect": {"divergence_alert": False, "unreachable_alert": False}},
         {"op": "get_gauge", "resource": G, "expect": {"value": 1.0}},
     ]},
    {"id": "rec_provider_wins_existence_divergence", "kind": "sequence",
     "group": "reconcile", "decision_refs": ["D-33", "D-36"],
     "config": _cfg(guard=0),
     "setup": [
         {"do": "seed_open", "resource": G, "n": 1, "age_seconds": 3600},
         {"do": "set_gauge", "resource": G, "value": 1.0},
         {"do": "provider", "observed": {G: 3}},
     ],
     "notes": "D-33 §2: provider and ledger DISAGREE about existence — that is a BUG, not "
              "drift. Converge to the PROVIDER (reality), ALERT, never fabricate a row.",
     "ops": [
         {"op": "reconcile_org",
          "expect": {"divergence_alert": True, "unreachable_alert": False}},
         {"op": "get_gauge", "resource": G, "expect": {"value": 3.0}},
     ]},
    {"id": "rec_provider_unreachable_do_nothing", "kind": "sequence",
     "group": "reconcile", "decision_refs": ["D-33", "D-31"],
     "config": _cfg(guard=0),
     "setup": [
         {"do": "seed_open", "resource": G, "n": 1, "age_seconds": 3600},
         {"do": "set_gauge", "resource": G, "value": 99.0},
         {"do": "provider_unreachable"},
     ],
     "notes": "D-31 binds the reconciler: provider unreachable -> do NOTHING and alert. "
              "Never converge to the ledger as a fallback — that erases reality exactly "
              "when the record is the thing that's broken.",
     "ops": [
         {"op": "reconcile_org", "expect": {"unreachable_alert": True}},
         {"op": "get_gauge", "resource": G, "expect": {"value": 99.0}},
     ]},
    {"id": "rec_provider_absent_key_means_unknown", "kind": "sequence",
     "group": "reconcile", "decision_refs": ["D-51", "D-31", "D-33"],
     "config": _cfg(guard=0),
     "setup": [
         {"do": "seed_open", "resource": G, "n": 1, "age_seconds": 3600},
         {"do": "set_gauge", "resource": G, "value": 1.0},
         {"do": "provider", "observed": {}},
     ],
     "notes": "D-51: a resource_key ABSENT from the provider result is 'no observation', "
              "never an affirmative zero. Converging absence to 0 erases a live spend — "
              "the forbidden direction.",
     # QG-09 was FIXED in Go (W-GO leg 11, 2026-07-11): a missing provider key is now
     # skip + alert, matching Python. The divergence RECORD is retired with the override —
     # a register that describes a divergence which no longer exists is a lie of the same
     # class as a doc describing a stub as shipped (D-38). The row is now a HARD parity
     # gate: if either runtime regresses, it fails loudly here. History lives in DEFECTS.md.
     "ops": [
         {"op": "reconcile_org"},
         {"op": "get_gauge", "resource": G, "expect": {"value": 1.0}},
     ]},
    {"id": "rec_recent_touch_guard", "kind": "sequence", "group": "reconcile",
     "decision_refs": ["D-62", "D-33"], "config": _cfg(guard=90),
     "setup": [],
     "notes": "D-62: the recent-activity guard is a correctness PRECONDITION. A provider "
              "lags creation; force-setting a just-touched (org,resource) down is "
              "under-count/phantom headroom on every fast create (the 20260626 incident).",
     "ops": [
         {"op": "acquire", "resource": G, "expect": {"admitted": True}},
         {"op": "set_gauge", "resource": G, "value": 5.0},
         {"op": "reconcile_org",
          "expect": {"divergence_alert": False, "unreachable_alert": False}},
         {"op": "get_gauge", "resource": G, "expect": {"value": 5.0}},
     ]},
]

# Divergences that exist but are NOT executable as an engine scenario — they
# are recorded so the suite documents them rather than silently blessing them.
STATIC_KNOWN_DIVERGENCES = [
    {"id": "SD-1", "runtime": "go", "class": "structural",
     "decision_refs": ["D-43"],
     "note": "Go has no check-bundle gate (Python check_for_bundle). The gate_matrix "
             "rows list abstract gates; each runner executes the intersection it "
             "implements, so bundle admission in Go is still matrix-checked via "
             "Acquire(BundleName)."},
    {"id": "SD-2", "runtime": "go", "class": "accepted_pending_fix",
     "decision_refs": ["D-54", "D-56"],
     "note": "Go derives enable_paid from billing config / a wired mesh client; Python "
             "defaults it true. Setup-level, not engine-level — no executable scenario. "
             "Align to Python's default once Go's emit path lands (D-56)."},
    {"id": "SD-3", "runtime": "go", "class": "dev_only",
     "decision_refs": ["D-58"],
     "note": "F-3: the IN-MEMORY idempotency claim never expires in Go; Python's carries "
             "a 24h TTL. Dev-only stores; the Redis-backed claim agrees."},
]


# Contracts both runtimes must satisfy IDENTICALLY that are NOT expressible as an
# engine scenario, because they are decided at SETUP (before an engine exists) —
# the engine replayer has no "boot the library against a clustered Redis" verb.
# Forcing them into a scenario row would be a lie about what the row executes; so
# they are declared here as STRUCTURAL conformance items with a machine-checkable
# payload, and each runtime's own test asserts ITS behaviour against THIS file
# (Python: tests/test_cluster_topology_guard_d71_20260711.py; Go:
# quota/topology_guard_d71_test.go). Same data file, two runners — D-43 holds.
STRUCTURAL_CONFORMANCE = [
    {
        "id": "ST-TOPOLOGY-1",
        "title": "Redis topology is machine-checked at startup; a cluster is REFUSED",
        "decision_refs": ["D-71", "D-23", "D-32", "D-49", "D-51"],
        "runtimes": ["python", "go"],
        "level": "setup",
        "why_not_a_scenario": (
            "the check runs in setup_quota / quota.Setup, before an engine exists; the "
            "engine replayer has no boot verb. Declared structurally rather than forced "
            "into a scenario row that would not execute what it claims."),
        "contract": [
            "CLUSTER INFO reporting cluster_enabled:1 => REFUSE TO START (typed error).",
            "CLUSTER INFO unavailable/unparseable => UNKNOWN => REFUSE TO START, unless "
            "storage.redis_cluster_confirmed_disabled is explicitly true (an operator "
            "assertion on the record).",
            "An operator assertion NEVER overrides a positive cluster_enabled:1 — a "
            "definitive negative is not overridable (the allkeys-* eviction analogue, D-32).",
            "cluster_enabled:0 => single-node => start (the guard is not a blanket reject).",
            "The verdict is published as capability `redis_topology` and a non-single-node "
            "verdict FAILS the money-aware health probe (D-40/D-49/D-51).",
        ],
        "capability_key": "redis_topology",
        "capability_values": ["single-node", "CLUSTER (unsupported)", "unknown"],
        "config_key": "storage.redis_cluster_confirmed_disabled",
        "env_key": "AB0T_QUOTA_REDIS_CLUSTER_CONFIRMED_DISABLED",
        # Both runtimes' refusals must name the CAUSE and the REMEDY. These
        # substrings are the machine-checked part of "identical behaviour".
        "cluster_error_must_contain": [
            "CROSSSLOT", "multi-key", "cluster_enabled:1", "single-node", "roadmap",
        ],
        "unknown_error_must_contain": [
            "CROSSSLOT", "redis_cluster_confirmed_disabled",
        ],
        "roadmap": (
            "cluster SUPPORT (hash-tagged quota:{org} keyspace, storage.keyspace_version + "
            "dual-read/write) is gated future work (D-23). v1 ships an honest refusal."),
    },
    {
        "id": "ST-PREFLIGHT-1",
        "title": "The COUNTER's Redis is machine-checked at startup: eviction, scripting, version",
        "decision_refs": ["D-72", "D-73", "D-74", "D-32", "D-31", "D-49", "D-51"],
        "runtimes": ["python", "go"],
        "level": "setup",
        "why_not_a_scenario": (
            "the checks run in setup_quota / quota.Setup, before an engine exists; the engine "
            "replayer has no boot verb. Declared structurally rather than forced into a scenario "
            "row that would not execute what it claims."),
        "contract": [
            "D-72: maxmemory-policy=allkeys-* on the COUNTER's Redis => REFUSE TO START. Redis "
            "evicts a LIVE gauge under memory pressure; the counter then reads zero for a "
            "resource that is still running => under-count => phantom headroom => OVER-ADMISSION "
            "(D-31's forbidden direction). Unlike the topology guard this fails SILENTLY at "
            "runtime, as free quota, behind a green health check — which is why it outranks D-71.",
            "D-72: CONFIG unavailable => REFUSE, unless storage.redis_durability_confirmed is "
            "explicitly true (an operator assertion on the record). An assertion NEVER overrides "
            "an allkeys-* policy the server actually reported (D-32's law).",
            "D-72: the counter's fatal property is EVICTION, not persistence — appendonly=no "
            "alone must NOT block startup (a restart-lost counter heals via the reconciler, "
            "D-28; an evicted one silently under-counts). The OUTBOX needs both; the counter "
            "needs only the first.",
            "D-73: SCRIPT LOAD of the REAL acquire source at boot; a Redis that cannot run the "
            "counter's Lua => REFUSE TO START (never a first-acquire outage).",
            "D-74: a Redis below the supported version floor => REFUSE. An unreadable version is "
            "`unknown` (recorded, not a refusal — a stated deviation, see the artifact).",
            "The verdicts are published as capabilities `counter_eviction_policy` / "
            "`redis_scripting` / `redis_version`; the first two FAIL the money-aware health "
            "probe, and their ABSENCE degrades it (D-40/D-49/D-51).",
        ],
        "capability_keys": ["counter_eviction_policy", "redis_scripting", "redis_version"],
        "config_key": "storage.redis_durability_confirmed",
        "env_key": "AB0T_QUOTA_REDIS_DURABILITY_CONFIRMED",
        "evicting_policies": ["allkeys-lru", "allkeys-lfu", "allkeys-random"],
        "version_floor": "6.0.0",
        # Both runtimes' refusals must name the CAUSE and the REMEDY. These substrings are
        # the machine-checked part of "identical behaviour" (matched case-insensitively).
        "eviction_error_must_contain": [
            "evict", "under-count", "phantom headroom", "over-admission", "noeviction",
            "redis_durability_confirmed",
        ],
        "scripting_error_must_contain": ["eval", "script", "acquire"],
    },
    {
        "id": "ST-RUNTIME-1",
        "title": "The infrastructure invariants are RE-verified on an interval, and a runtime "
                 "violation is LOUD but NOT FATAL",
        "decision_refs": ["D-75", "D-76", "D-77", "D-50", "D-40", "D-49", "D-51", "D-31"],
        "runtimes": ["python", "go"],
        "level": "setup+runtime",
        "why_not_a_scenario": (
            "this is about TIME, not about an engine operation: it asserts that a boot-time "
            "verdict is re-checked while the process runs. The engine replayer has no clock and "
            "no boot verb. Declared structurally rather than forced into a scenario row."),
        "law": "An assumption machine-checked once is an assumption trusted thereafter.",
        "contract": [
            "D-75: the Redis invariants (topology, eviction policy, scripting, version, memory "
            "headroom) and the DDB tables are RE-VERIFIED on an interval — riding the RECONCILER "
            "loop, never a new worker (every loop we add is another thing that can be dead, D-50).",
            "D-75: a safe->unsafe transition at RUNTIME is LOUD, NOT FATAL. It updates "
            "Capabilities, degrades the health probe IMMEDIATELY, and fires a money-incident "
            "alert. It MUST NOT crash or refuse: a running service that suddenly refuses is its "
            "own outage. The operator decides whether to drain.",
            "D-75: the transition is not a one-way latch — a repaired Redis HEALS the probe with "
            "no restart, and a paired `restored` alert fires (D-26's resolve trail).",
            "D-76: the DynamoDB tables holding the activation ledger and the outbox are verified "
            "at boot and re-verified on the interval: table ACTIVE, every GSI ACTIVE (a "
            "backfilling index silently MISSES rows), TTL enabled on the attribute the library "
            "actually writes (a TTL on any other attribute may DELETE rows we never marked), and "
            "PITR enabled. PITR-unverifiable (DynamoDB Local cannot answer it) requires the "
            "on-the-record storage.ddb_pitr_confirmed assertion.",
            "D-76: a DISABLED TTL WARNS but does not refuse (rows never reap: growth and cost; "
            "nothing is lost). Refusing there would be the D-49 false-503 mistake.",
            "D-77: memory headroom is surfaced and DEGRADES as it approaches the cliff. "
            "`noeviction` + a tight maxmemory fails CLOSED (the safe direction) but takes the "
            "service DOWN with no warning.",
        ],
        "capability_keys": ["counter_eviction_policy", "redis_topology", "redis_scripting",
                            "memory_headroom", "ddb_outbox", "ddb_activations"],
        "config_key": "storage.ddb_pitr_confirmed",
        "env_key": "AB0T_QUOTA_DDB_PITR_CONFIRMED",
        "runtime_violation_is_fatal": False,
        "runtime_violation_degrades_health": True,
        "runtime_violation_alerts": True,
        "reverification_rides": "the reconciler loop",
        "memory_warn_ratio": 0.90,
        "ddb_fatal_findings": ["table_missing", "table_not_active", "gsi_not_active",
                               "ttl_on_unexpected_attribute", "pitr_disabled",
                               "pitr_unverifiable_without_assertion"],
        "ddb_warn_findings": ["ttl_disabled"],
    },
    {
        "id": "ST-EFFECT-1",
        "title": "Check the EFFECT, not just the policy; and a guarantee the client can switch "
                 "off may not disappear quietly",
        "decision_refs": ["D-80", "D-79", "D-78", "D-66", "D-50", "D-31", "D-49"],
        "runtimes": ["python", "go"],
        "level": "setup+runtime",
        "why_not_a_scenario": (
            "both contracts are about the world OUTSIDE the engine (what a Redis already DID, and "
            "which loops a config REQUIRES). The engine replayer has neither a server nor a clock."),
        "laws": [
            "D-80: for every assumption we verify, ask whether an observable FACT proves it was "
            "ALREADY violated — and check that too. The policy is the forecast; INFO "
            "stats.evicted_keys is the fact.",
            "D-79: wiring SATISFIES a contract; it does not DEFINE one (D-66). A required loop "
            "that is absent degrades the probe.",
            "D-78: a double can prove what YOUR code does. It can never prove the OTHER side "
            "behaves as you modelled it.",
        ],
        "contract": [
            "D-80: `INFO stats.evicted_keys` > 0 on the Redis holding the COUNTER is a MONEY "
            "INCIDENT — degrade the health probe, alert a human, and mark the counter untrusted so "
            "the reconcile pass converges it. It fires even when maxmemory-policy now reads "
            "`noeviction`: a server corrected AFTER it evicted a live gauge passes every policy "
            "check, while the counter under-counts a resource that still exists (D-31).",
            "D-80: zero evictions is healthy (the guard is not a blanket alarm); an unreadable "
            "INFO stats is `unknown` and does not degrade (the policy check already fails closed "
            "on an unverifiable server).",
            "D-79: if the counter lives on Redis, `preflight_reverification` is a REQUIRED loop, "
            "DERIVED from config. Absent (no reconciler loop) or empty (a live reconciler carrying "
            "no preflight) ⇒ the probe DEGRADES. Liveness of the carrier is not delivery of the "
            "cargo.",
            "D-79: an in-memory counter derives no such requirement (n/a) — a false 503 trains "
            "operators to ignore the probe (D-49).",
        ],
        "capability_keys": ["counter_evictions_observed", "preflight_reverification"],
        "eviction_fact_field": "INFO stats.evicted_keys",
        "eviction_fact_degrades_health": True,
        "eviction_fact_alerts": True,
        "eviction_fact_marks_counter_untrusted": True,
        "reverification_required_when": "the counter store is redis",
    },
    {
        "id": "ST-WORKING-1",
        "title": "A CONFIGURED guarantee is not a WORKING one; and a store must provision the "
                 "table it depends on",
        "decision_refs": ["D-81", "D-82", "D-80", "D-78", "D-76", "D-35", "D-49", "D-31"],
        "runtimes": ["python", "go"],
        "level": "setup+runtime",
        "why_not_a_scenario": (
            "both are about the world outside the engine — whether a Redis is actually persisting, "
            "and whether a DynamoDB table exists. The engine replayer has neither a disk nor a "
            "control plane."),
        "law": "The config is the intent; the status field is the fact. Verify both.",
        "contract": [
            "D-81: a Redis reporting aof_last_write_status / rdb_last_bgsave_status / "
            "aof_last_bgrewrite_status != ok is NOT durable, however green `appendonly` reads. "
            "The ONE durability check (D-35) asks both, so the existing boot gate refuses it.",
            "D-81: at RUNTIME a persist failure on the Redis holding the OUTBOX is a MONEY "
            "INCIDENT (degrade + CRITICAL alert): a lost outbox row is money nobody can "
            "reconstruct. The SAME failure on a Redis that only holds the COUNTER is NOT: the "
            "counter heals (reconciler → Σ open activations). Severity by CONSEQUENCE, not "
            "uniformity — reporting both as money loss would be the D-49 false-503 mistake.",
            "D-81: an unreadable INFO persistence is `unknown` and does not degrade — the CONFIG "
            "check already fails closed on a server it cannot interrogate.",
            "D-82: the handler-ledger DDB store PROVISIONS its table (create + wait ACTIVE + TTL "
            "on the `ttl` attribute it writes) and is PREFLIGHTED (D-76), exactly like the outbox "
            "and the activation store. It previously ASSUMED the table existed — invisible "
            "because the only thing exercising it was a fake, and a fake creates nothing (D-78).",
        ],
        "capability_keys": ["redis_persist_status", "ddb_handler_ledger"],
        "persist_fact_fields": ["aof_last_write_status", "rdb_last_bgsave_status",
                                "aof_last_bgrewrite_status"],
        "persist_failure_is_money_incident_when": "the outbox is on that redis",
        "ledger_provisions_its_table": True,
        "ledger_ttl_attribute": "ttl",
    },
    {
        "id": "ST-SETTLE-1",
        "title": "A money event past its reservation window is SETTLED, not voided — and the "
                 "void survives as the fallback for what genuinely cannot settle",
        "decision_refs": ["D-12", "D-31", "D-35", "D-36", "D-64", "B-D1", "B-D9", "B-D11"],
        "runtimes": ["python", "go"],
        "level": "outbox+runtime",
        "ticket": "billing/output/tickets/20260712_revenue_chain_integrity (the CALLER leg)",
        "why_not_a_scenario": (
            "this is about the world OUTSIDE the engine — an HTTP call to another service, its "
            "status codes, and a durable dedup marker in ITS database. The engine replayer has "
            "no billing service and no network. It is declared structurally, and each runtime "
            "proves it against real infrastructure: Python against billing's REAL FastAPI route "
            "on real DynamoDB Local + real Redis; Go against a real HTTP round-trip plus the "
            "FROZEN proration vectors that Python derived by EXECUTING billing's real function."),
        "law": (
            "Commit-cannot-take-it does not mean nothing-can-take-it. An undeliverable money "
            "event is SETTLED against billing's durable, activation-scoped path; it is voided "
            "ONLY if it genuinely cannot settle."),
        "contract": [
            "D-12/D-64: the drain used to VOID a past-horizon money event on the premise that "
            "'a late commit would 404 at billing anyway'. Billing now has POST /billing/{org_id}"
            "/settle, so that premise is FALSE — and until this contract landed, NOTHING CALLED "
            "IT. The endpoint existed and the revenue was still lost: a mechanism is not a "
            "guarantee.",

            "THE TERMINAL-EVENT GATE (load-bearing): ONLY resource.stopped / resource.deleted "
            "may settle. resource.started and resource.heartbeat ride the SAME outbox, reach the "
            "SAME void path and carry a reservation_id — so they LOOK settleable. Settling one "
            "would BURN the settlement key (which is the reservation_id, and whose dedup at "
            "billing is DURABLE AND ETERNAL) on a partial, wrong amount, and the REAL terminal "
            "settlement that follows would then be REFUSED as a duplicate: the customer charged "
            "the WRONG amount AND the true settlement LOST. Strictly worse than the defect.",

            "THE KEY IS THE RESERVATION_ID — the same key billing's OWN SQS lifecycle consumer "
            "settles under. That is what makes the two settlement paths dedup AGAINST EACH "
            "OTHER when an SNS copy of an already-settled event is later delivered. A different "
            "key (e.g. activation_id) is TWO keys for ONE usage — a double charge.",

            "D-31 FAIL DIRECTION: it fails toward RETRYING, never toward DISCARDING; and toward "
            "NOT DEBITING, never toward DEBITING TWICE. A 5xx / timeout / unreachable billing "
            "leaves the intent PENDING in the durable store (retried forever), and is NEVER "
            "voided — a network blip must not consume real revenue. This is safe on a TIMEOUT, "
            "where the caller cannot know whether the settlement landed, because the no-double-"
            "debit guarantee is billing's DynamoDB conditional write (NO TTL), not a branch of "
            "client code: a retry of a settlement that DID land returns the original result and "
            "moves no money.",

            "⚠️ A 409 IS **NOT** A SUCCESS, AND MUST NEVER BE ACKED (this REPLACED the earlier "
            "clause, which said the opposite and was a REVENUE-LOSS BUG). Billing returns ONE "
            "OPAQUE 409 by design — distinct codes would build a CROSS-TENANT ENUMERATION ORACLE, "
            "because its precheck reads Redis BEFORE it checks tenancy. That single code covers "
            "'reservation_still_live:use_commit' (THE MONEY IS NOT TAKEN), 'org_mismatch' "
            "(nothing settled) AND 'already_committed:ledger_row_exists' (the money IS booked). "
            "TWO OF THE THREE mean the settlement did not land, so acking a 409 retires the "
            "durable outbox row and DISCARDS the revenue — D-12's loss, re-entering through the "
            "ERROR CONTRACT. It must RETRY. Ambiguity is not success (D-49: 'not obviously a "
            "failure' is not 'definitely a success'). Retrying is FREE: billing's dedup is a "
            "durable conditional write, so an already-settled event moves no money and returns "
            "200/replayed.",

            "THE ONLY THING THAT MAY RETIRE A MONEY EVENT IS AN AFFIRMATIVE ANSWER — a 200 that "
            "positively says the usage is accounted for (settled, or replayed=true). No outcome "
            "may be INFERRED from an ambiguous refusal. OWED (B-D24): billing cannot currently "
            "give that affirmative signal for an already-committed reservation without re-opening "
            "the enumeration oracle, so such an event retries until a human retires it. That is "
            "loud and safe, and it is the correct direction to be wrong in.",

            "A 4xx that is not 409 (400 negative cost, 403, 404 unknown org) is PERMANENT: void "
            "+ ALERT, do not churn. The void/alert path is KEPT, and a human still hears it.",

            "B-D11: dispatch on the STATUS CODE, never on the error's text. Billing's own "
            "consumer dispatched on str(e) — the EMPTY STRING for an HTTPException on its pinned "
            "starlette — and its entire revenue-loss alarm was dead code for months.",

            "⭐ B-D13 (D-35, RESOLVED — this REPLACED the earlier clause): the caller sends the "
            "INPUTS (started_at, stopped_at, hourly_rate?, allocation_fee?) and BILLING PRICES "
            "THEM. /settle used to take a COMPUTED actual_cost, which forced every caller to "
            "reimplement billing's proration — THREE implementations of one money law (billing's, "
            "ab0t-quota's, ab0t-quota-go's), guarded by a frozen vector table. A copy kept in sync "
            "is still a copy. Both libraries' proration is now ARCHIVED, not synchronised: "
            "A CALLER THAT CANNOT COMPUTE A COST CANNOT COMPUTE IT WRONG. Neither runtime may "
            "reintroduce cost arithmetic.",

            "NEVER INVENT A PRICE (D-36). A missing hourly_rate is reported as MISSING — it is "
            "NOT a reason to refuse (the allocation fee may still be owed, and refusing re-creates "
            "a revenue-loss path). Billing prices a rate-less RUNTIME at ZERO and ALERTS "
            "(settle_missing_hourly_rate). That policy lives in exactly ONE place: the libraries "
            "neither ship a competing default (billing's dead fallback would have invented "
            "$0.10/hr — a price nobody configured) nor raise a competing alert. A fabricated price "
            "is worse than no price: nothing is honest, recoverable and alertable; fabricated is "
            "an overcharge we cannot defend.",

            "B-D14, AND NOT RECREATING IT: a money value the caller does not have is OMITTED from "
            "the payload, NEVER sent as an explicit null. The bug just fixed was an "
            "ALWAYS-PRESENT KEY WHOSE VALUE WAS SOMETIMES NULL — the library always set "
            "hourly_rate (to null when unpriced) and billing's .get(k, \"0.10\") only defaults on "
            "an ABSENT key, so it computed Decimal(\"None\") -> InvalidOperation -> the money event "
            "DLQ'd, and the fallback was unreachable. Send absence as ABSENCE.",

            "THE CONTRACT IS PINNED, NOT THE ARITHMETIC: settlement_contract_vectors_20260712.json "
            "freezes, for each observed event, the EXACT request body billing's REAL pydantic model "
            "accepts and the cost billing's REAL price_usage computes — derived by EXECUTING "
            "billing. Go replays it. B-D16 is binding ON THAT TABLE: it MUST contain the "
            "sub-minute floor and the rounding boundary, because whole-hour lifetimes are where a "
            "wrong law HIDES (NC-5 swapped in a flatly wrong law and a whole-hour suite on real "
            "infrastructure stayed GREEN).",

            "B-D1: the Go suite's billing server is a STAND-IN and is honest about it — a double "
            "proves what YOUR code does, never that the OTHER house behaves as you modelled it. "
            "The cross-house certification is Python's, against billing's real route.",
        ],
        "capability_keys": ["outbox_settlement_fallback"],
        "terminal_cost_events": ["resource.stopped", "resource.deleted"],
        "caller_sends_inputs_not_cost": True,
        "caller_computes_no_cost": True,
        "settlement_request_fields": ["settlement_key", "started_at", "stopped_at",
                                      "hourly_rate?", "allocation_fee?", "reservation_id?",
                                      "usage_record_id?"],
        "missing_rate_prices_runtime_at_zero_and_billing_alerts": True,
        "caller_never_invents_a_rate": True,
        "absent_money_keys_are_OMITTED_never_null": True,
        "settlement_key": "reservation_id",
        "settlement_endpoint": "POST /billing/{org_id}/settle",
        "transient_statuses_retry_never_void": [500, 502, 503, 504, 408, 429],
        "permanent_statuses_void_and_alert": [400, 401, 403, 404, 422],
        "ambiguous_409_must_RETRY_never_ack": True,
        "409_is_not_a_success": True,
        "only_an_affirmative_200_may_retire_a_money_event": True,
        "dedup_is_durable_no_ttl": True,
        "contract_vector_table": "settlement_contract_vectors_20260712.json",
        "unsettleable_still_voids_and_alerts": True,
    },
]


# ---------------------------------------------------------------------------
# the Python reference EXECUTOR (this is what derives every expectation)
# ---------------------------------------------------------------------------

class _PutFailsStore(InMemoryActivationStore):
    """Fault injection for the crash-fail-closed row: the persist dies AFTER
    the atomic spend landed."""
    async def put_open(self, activation):
        raise RuntimeError("injected: activation store unavailable")


class _CaptureAlerts:
    """Normalizes the over-limit alert sink to {kind, resource} events."""
    def __init__(self):
        self.events = []

    async def maybe_alert(self, alert):
        self.events.append({"kind": alert.message, "resource": alert.resource_key})


def _build_engine(redis, cfg, store):
    reg = ResourceRegistry()
    for r in cfg["resources"]:
        kw = dict(service="conf", resource_key=r["key"], display_name=r["key"],
                  counter_type=CounterType(r["counter_type"]), unit="u")
        if r.get("window_seconds"):
            kw["window_seconds"] = r["window_seconds"]
        reg.register(ResourceDef(**kw))
    tiers = {TIER: TierConfig(
        tier_id=TIER, display_name="T",
        limits={k: TierLimits(limit=v) for k, v in cfg.get("limits", {}).items()})}
    return QuotaEngine(
        redis=redis,
        tier_provider=StaticTierProvider({ORG: cfg.get("org_tier", TIER)}),
        registry=reg, tiers=tiers,
        resource_bundles=cfg.get("bundles", {}),
        activation_store=store,
        enforcement=EnforcementConfig(**cfg.get("enforcement", {})),
    )


async def _seed(redis, store, setup, ctx):
    for s in setup:
        if s["do"] == "set_gauge":
            await GaugeCounter(redis, ORG, s["resource"]).reset(float(s["value"]))
        elif s["do"] == "seed_open":
            opened = (datetime.now(timezone.utc)
                      - timedelta(seconds=s.get("age_seconds", 3600))).isoformat()
            for i in range(s.get("n", 1)):
                await store.put_open(Activation(
                    activation_id=f"act_seed_{s['resource']}_{i}",
                    org_id=ORG, user_id=None, resource_key=s["resource"],
                    spend={s["resource"]: 1.0}, state=ActivationState.OPEN.value,
                    opened_at=opened))
        elif s["do"] == "provider":
            observed = {k: {"total": float(v), "per_user": {}}
                        for k, v in s["observed"].items()}
            ctx["provider"] = lambda org_id, _o=observed: _o
        elif s["do"] == "provider_unreachable":
            def _raise(org_id):
                raise ConnectionError("injected: provider unreachable")
            ctx["provider"] = _raise
        elif s["do"] == "fault":
            pass  # handled at store construction
        else:
            raise ValueError(f"unknown setup do={s['do']}")


async def _run_gate(engine, gate, sc):
    """Normalized gate outcome. An ERROR at an admission gate is a
    NON-ADMISSION (fail-closed) — the documented mapping rule."""
    target = sc.get("target", {})
    rk = target.get("resource", G)
    bundle = target.get("bundle", BUNDLE)
    try:
        if gate == "check_resource":
            r = await engine.check(QuotaCheckRequest(
                org_id=ORG, resource_key=rk, increment=1.0))
            return bool(r.allowed)
        if gate == "check_bundle":
            r = await engine.check_for_bundle(ORG, bundle)
            return bool(r.allowed)
        if gate == "acquire_resource":
            r = await engine.acquire(ORG, resource_key=rk)
            return bool(r.admitted)
        if gate == "acquire_bundle":
            r = await engine.acquire(ORG, bundle)
            return bool(r.admitted)
    except Exception:
        return False
    raise ValueError(f"unknown gate {gate}")


async def _run_op(op, engine, redis, ctx, sc):
    """Execute ONE op against the Python reference; return the normalized
    observation dict. Any raise → {'error': True}."""
    handles = ctx["handles"]
    try:
        if op["op"] == "acquire":
            kw = {}
            if op.get("user"):
                kw["user_id"] = op["user"]
            if op.get("idem"):
                kw["idempotency_key"] = op["idem"]
            if op.get("bundle"):
                r = await engine.acquire(ORG, op["bundle"], **kw)
            else:
                r = await engine.acquire(ORG, resource_key=op["resource"], **kw)
            if op.get("save"):
                handles[op["save"]] = r.activation_id
            return {"admitted": bool(r.admitted), "minted": bool(r.activation_id),
                    "reason": r.reason}
        if op["op"] == "release_activation":
            aid = handles[op["ref"]] if "ref" in op else op["id"]
            performed = await engine.release(aid)
            return {"performed": bool(performed)}
        if op["op"] == "settle":
            settled = await engine.settle(handles[op["ref"]], op["cost"])
            return {"settled": bool(settled)}
        if op["op"] == "increment":
            v = await engine.increment(QuotaIncrementRequest(
                org_id=ORG, resource_key=op["resource"],
                delta=float(op["delta"]),
                **({"user_id": op["user"]} if op.get("user") else {})))
            return {"value": float(v)}
        if op["op"] == "decrement":
            v = await engine.decrement(QuotaDecrementRequest(
                org_id=ORG, resource_key=op["resource"], delta=float(op["delta"])))
            return {"value": float(v)}
        if op["op"] == "check":
            r = await engine.check(QuotaCheckRequest(
                org_id=ORG, resource_key=op["resource"],
                increment=float(op.get("increment", 1.0))))
            return {"allowed": bool(r.allowed)}
        if op["op"] == "get_gauge":
            g = GaugeCounter(redis, ORG, op["resource"])
            scope = op.get("scope", "org")
            if scope == "org":
                return {"value": float(await g.get())}
            if scope == "user":
                return {"value": float(await g.get_user(op["user"]))}
            if scope == "user_seq":
                raw = await redis.get(
                    f"quota:{ORG}:{op['resource']}:gauge:seq:user:{op['user']}")
                return {"value": float(raw) if raw else None}
        if op["op"] == "set_gauge":
            await GaugeCounter(redis, ORG, op["resource"]).reset(float(op["value"]))
            return {}
        if op["op"] == "concurrent_release":
            aid = handles[op["ref"]]
            results = await asyncio.gather(
                *[engine.release(aid) for _ in range(op["n"])])
            return {"performed_count": sum(1 for x in results if x)}
        if op["op"] == "reconcile_org":
            guard = sc["config"].get("reconcile", {}).get("activity_guard_seconds", 0)
            rec = LibraryReconciler(
                engine, observed_usage_provider=ctx.get("provider"),
                config=ReconcileConfig(activity_guard_seconds=guard))
            res = await rec.reconcile_org(ORG)
            return {"divergence_alert": len(res.divergences) > 0,
                    "unreachable_alert": res.skipped == "provider_unreachable"}
    except Exception:
        return {"error": True}
    raise ValueError(f"unknown op {op['op']}")


def _check_declared(sid, where, declared, observed):
    """A declared (normative) expect must match the executed reference —
    otherwise the reference regressed and generation must ABORT, never bless."""
    for k, v in declared.items():
        got = observed.get(k) if observed else None
        if got != v:
            raise SystemExit(
                f"NORMATIVE MISMATCH {sid} {where}: declared {k}={v!r} but the "
                f"executed Python reference produced {got!r} (observed={observed}). "
                f"Either the reference regressed or the scenario is wrong. NOT emitting.")


async def _execute(sc):
    redis = fakeredis.aioredis.FakeRedis()
    try:
        fault = any(s.get("do") == "fault" for s in sc.get("setup", []))
        store = _PutFailsStore() if fault else InMemoryActivationStore()
        engine = _build_engine(redis, sc["config"], store)
        cap = _CaptureAlerts()
        engine.set_alert_manager(cap)
        ctx = {"handles": {}, "provider": None}
        await _seed(redis, store, sc.get("setup", []), ctx)

        if sc["kind"] == "gate_matrix":
            outcomes = {}
            for gate in sc["gates"]:
                # each gate gets a FRESH engine+state (acquire spends)
                r2 = fakeredis.aioredis.FakeRedis()
                st2 = InMemoryActivationStore()
                e2 = _build_engine(r2, sc["config"], st2)
                c2 = {"handles": {}, "provider": None}
                await _seed(r2, st2, sc.get("setup", []), c2)
                outcomes[gate] = await _run_gate(e2, gate, sc)
                await r2.flushall()
                await r2.aclose()
            if len(set(outcomes.values())) != 1:
                raise SystemExit(
                    f"MATRIX DISAGREEMENT {sc['id']}: {outcomes} — a Python gate "
                    f"ignores a knob its siblings honor (D-48). NOT emitting.")
            _check_declared(sc["id"], "gates", sc["expect"],
                            {"admitted": next(iter(outcomes.values()))})
            sc["expect"] = {"admitted": next(iter(outcomes.values()))}
            return sc

        # sequence
        for i, op in enumerate(sc["ops"]):
            observed = await _run_op(op, engine, redis, ctx, sc)
            declared = op.get("expect", {})
            _check_declared(sc["id"], f"op[{i}]({op['op']})", declared, observed)
            if observed.get("error"):
                op["expect"] = {"error": True}
            else:
                merged = dict(observed)
                merged.pop("error", None)
                # keep reason only when the scenario declared it matters —
                # otherwise it is an implementation string, not the contract
                if "reason" in merged and "reason" not in declared and \
                        op.get("idem") is None:
                    merged.pop("reason")
                op["expect"] = merged
        if "expect_events" in sc:
            _check_declared(sc["id"], "events", {"events": sc["expect_events"]},
                            {"events": cap.events})
            sc["expect_events"] = cap.events
        elif cap.events:
            sc["expect_events"] = cap.events
        return sc
    finally:
        await redis.flushall()
        await redis.aclose()


async def main():
    scenarios = []
    for sc in copy.deepcopy(SCENARIOS):
        scenarios.append(await _execute(sc))
    known = list(STATIC_KNOWN_DIVERGENCES)
    for sc in scenarios:
        if "known_divergence" in sc:
            known.append({"id": sc["id"], **sc["known_divergence"]})
    doc = {
        "conformance_version": "1",
        "reference_runtime": "python (shared/ab0t-quota, executed on fakeredis)",
        "generated_by": "conformance/generate_scenarios.py",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ticket": "20260709_ab0t_quota_systemic_integrity_redesign (D-43)",
        "normalization_rules": {
            "gate_error": "an error/exception at an admission gate is a NON-admission (fail-closed)",
            "op_error": "an op that raises normalizes to {'error': true}; no other field is asserted",
            "gates": "each runner executes the INTERSECTION of a row's gates it implements, and must run at least one",
            "expect_go": "per-op override for a recorded known_divergence — derived by EXECUTING Go, removed when Go aligns",
        },
        "known_divergences": known,
        "structural_conformance": STRUCTURAL_CONFORMANCE,
        "scenarios": scenarios,
    }
    with open(OUT, "w") as f:
        json.dump(doc, f, indent=2, sort_keys=False)
        f.write("\n")
    print(f"wrote {OUT}: {len(scenarios)} scenarios "
          f"({sum(1 for s in scenarios if s['kind'] == 'gate_matrix')} gate_matrix, "
          f"{sum(1 for s in scenarios if s['kind'] == 'sequence')} sequence), "
          f"{len(known)} known divergences")


if __name__ == "__main__":
    asyncio.run(main())
