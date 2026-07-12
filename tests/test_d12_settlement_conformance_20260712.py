"""ST-SETTLE-1 — the settlement contract, asserted against the PYTHON implementation.

Ticket: billing/output/tickets/20260712_revenue_chain_integrity (the CALLER leg).

A structural-conformance entry that nothing checks is prose. This binds the declaration in
`conformance/scenarios.json` to the actual Python constants, so the two cannot drift.
`ab0t-quota-go/conformance/settle_st_settle_1_test.go` asserts the SAME entry against Go's
constants — **one spec, two runtimes**, which is the whole point of the conformance file.

This suite needs no infrastructure: it is a spec-vs-code assertion, not a behaviour test. The
behaviour is proven in `test_d12_cross_house_settlement_20260712.py` (billing's REAL route, real
DynamoDB Local, real Redis).
"""
import json
from pathlib import Path

import pytest

from ab0t_quota.billing.lifecycle import LifecycleEmitter

SCENARIOS = Path(__file__).parent.parent / "conformance" / "scenarios.json"


@pytest.fixture(scope="module")
def spec():
    doc = json.loads(SCENARIOS.read_text())
    for entry in doc.get("structural_conformance", []):
        if entry.get("id") == "ST-SETTLE-1":
            return entry
    pytest.fail(
        "ST-SETTLE-1 is MISSING from conformance/scenarios.json — the settlement contract is "
        "undeclared, so the two runtimes are no longer held to one spec."
    )


def test_the_terminal_event_gate_matches_the_spec(spec):
    """⭐ The gate that stops a `resource.started` from burning the settlement key.

    The dangerous direction is settling an event the spec does NOT declare terminal: it would
    burn `settlement_key=reservation_id` on a partial amount, and the REAL terminal settlement
    would then be refused as a duplicate — wrong amount charged AND true settlement lost.
    """
    declared = set(spec["terminal_cost_events"])
    implemented = set(LifecycleEmitter._COST_RECORDING_EVENTS)

    assert declared, "the spec declares no terminal events — a vacuous gate"
    assert implemented == declared, (
        f"the settleable event set has DRIFTED from ST-SETTLE-1.\n"
        f"  implemented: {sorted(implemented)}\n"
        f"  spec:        {sorted(declared)}"
    )
    for forbidden in ("resource.started", "resource.heartbeat"):
        assert forbidden not in implemented, (
            f"{forbidden} must NEVER be settleable — it has no final cost, and settling it "
            f"burns the settlement key for the terminal event that follows"
        )


def test_the_contract_is_declared_for_both_runtimes(spec):
    assert spec["settlement_key"] == "reservation_id", (
        "the settlement key MUST be reservation_id — the key billing's OWN SQS lifecycle "
        "consumer settles under, which is what makes the two settlement paths dedup against "
        "each other. A different key is two keys for one usage: a double charge."
    )
    assert spec["settlement_endpoint"] == "POST /billing/{org_id}/settle"
    assert spec["dedup_is_durable_no_ttl"] is True, (
        "the spec must record that billing's dedup is DURABLE with NO TTL — that is why a "
        "client-side retry is safe, and why we do not invent client-side dedup"
    )
    assert spec["unsettleable_still_voids_and_alerts"] is True, (
        "the void/alert path must survive as the FALLBACK — an event that genuinely cannot "
        "settle must still reach a human"
    )
    assert set(spec["runtimes"]) == {"python", "go"}
    assert len(spec["contract"]) >= 5, "too thin to be a spec"


def test_the_fail_direction_table_is_unambiguous(spec):
    """If a status were both transient and permanent, the same failure could both retry and
    void — undefined money behaviour."""
    transient = set(spec["transient_statuses_retry_never_void"])
    permanent = set(spec["permanent_statuses_void_and_alert"])

    assert not (transient & permanent), (
        f"status(es) {sorted(transient & permanent)} are declared BOTH transient and permanent"
    )
    # ⚠️ INVERTED 2026-07-12. This used to assert `already_accounted_status_is_success == 409`,
    # i.e. that a 409 was a SUCCESS. That was a REVENUE-LOSS BUG: billing's 409 is opaque by
    # design (distinct codes would build a cross-tenant enumeration oracle) and also covers
    # "reservation still live" (money NOT taken) and "org mismatch". Acking it DISCARDED the
    # settlement. Ambiguity is not success (D-49).
    assert spec["409_is_not_a_success"] is True
    assert spec["ambiguous_409_must_RETRY_never_ack"] is True, (
        "a 409 must RETRY. Acking it retires the durable outbox row and discards revenue — "
        "D-12's loss re-entering through the error contract."
    )
    assert spec["only_an_affirmative_200_may_retire_a_money_event"] is True
    assert 409 not in permanent, (
        "409 must not be a permanent VOID either — the money may yet be owed; we simply cannot "
        "confirm it either way, so we retry"
    )
    assert "already_accounted_status_is_success" not in spec, (
        "the old (wrong) key is back: a 409 is NOT a success"
    )
    assert {400, 404} <= permanent, (
        "400 (negative cost) and 404 (unknown org) are PERMANENT — retrying them forever "
        "helps nobody"
    )
    assert all(s >= 500 or s in (408, 429) for s in transient)


def test_the_emitter_actually_HAS_the_settlement_seam():
    """D-64, guarded: the whole defect was a mechanism with no caller. If this seam is ever
    removed, the library silently returns to voiding-and-alerting the money away."""
    em = LifecycleEmitter(sns_topic_arn=None)
    assert hasattr(em, "_settlement_client"), "the settlement seam is GONE"
    assert hasattr(em, "_settle_or_void"), "the settle-or-void path is GONE"
    assert hasattr(em, "settled_ledger"), "the settled mirror is GONE"
    assert em._settlement_client is None, (
        "an emitter constructed with no billing must default to the pre-existing "
        "void-and-alert behaviour — never to a half-wired settlement"
    )
