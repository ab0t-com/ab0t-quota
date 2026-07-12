"""The SETTLEMENT REQUEST CONTRACT — pinned against billing's REAL model and REAL law, and
frozen for Go.

Ticket: billing/output/tickets/20260712_revenue_chain_integrity (**B-D13**, W-CHAIN).
Supersedes: `tests/.archive/test_d12_proration_conformance_20260712_superseded_by_BD13.py`.

WHAT CHANGED, AND WHY THIS FILE REPLACED THE OTHER ONE
-------------------------------------------------------
The archived test existed to make a **forced duplicate of billing's money law** detectable:
`/settle` took a pre-computed `actual_cost`, so this library carried a port of billing's
proration, and a frozen vector table was the tripwire that would fire if the two drifted.

**Billing now takes the INPUTS and prices them itself.** The library's proration is **archived,
not synchronised** — *a copy kept in sync is still a copy*. So there is no longer a second
implementation of the money law to guard, and nothing for that test to do.

**But the contract did not disappear — it MOVED.** What the library can still get wrong is the
**request payload**: the field names, the timestamp format, the money-as-string convention, and —
most sharply — **what it does with a value it does not have.** That is what this file pins:

  1. every payload the library builds is **accepted by billing's REAL pydantic model**
     (`SettleActivationRequest`) — not by my model of it;
  2. billing's **REAL `price_usage`** prices it, and that cost is **frozen into the vector table**
     the **Go** runtime replays (Go cannot import billing's Python; this is its only link to the
     other house);
  3. the library sends **no cost, ever**;
  4. a value it does not have is **OMITTED, not sent as `null`** (B-D14 — see below).

⚠️ THE TABLE IS A TRIPWIRE, NOT A CERTIFIER (B-D1)
--------------------------------------------------
A frozen table derived from a function that has **since changed** is exactly the stale-double
failure B-D1 describes — it would keep agreeing with a law nobody runs any more. So it is
**re-derived by EXECUTING billing** on every run of this file, never transcribed. And it still
cannot certify Go against the real billing service: **a Go↔real-billing test remains OWED.**

RUN (needs both houses' deps in one interpreter — billing's venv has them):
    cd /home/ubuntu/infra/infra/code/billing/output
    PYTHONPATH=/home/ubuntu/infra/infra/code/shared/ab0t-quota ./venv/bin/python -m pytest \\
      /home/ubuntu/infra/infra/code/shared/ab0t-quota/tests/test_d12_settlement_contract_20260712.py -v
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from ab0t_quota.billing.observation import UnsettleableEvent, observe

BILLING_ROOT = Path("/home/ubuntu/infra/infra/code/billing/output")
VECTORS = Path(__file__).parent / "data" / "settlement_contract_vectors_20260712.json"

_IMPORT_ERROR = None
try:
    if str(BILLING_ROOT) not in sys.path:
        sys.path.insert(0, str(BILLING_ROOT))
    from app.core.proration import price_usage as BILLING_PRICE
    from app.models.billing import SettleActivationRequest
except Exception as e:  # pragma: no cover - environment guard
    _IMPORT_ERROR = e


T0 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

#: (id, elapsed_seconds, hourly_rate, allocation_fee) — `None` means THE EVENT HAS NO VALUE.
#:
#: ⚠️ B-D16 IS BINDING ON THIS TABLE. Whole-hour lifetimes are where a wrong law HIDES: the right
#: law and a naive one AGREE there. Every row that separates them is deliberate and must stay —
#: the 60s floor, the rounding boundary, the fractional hour. A table of whole hours would be
#: structurally incapable of seeing the bug it exists to catch.
CASES = [
    ("sub_minute_floor",       30,            "1.00",   "0"),      # < 60s → the floor bites
    ("one_second",             1,             "3.60",   "0"),      # the extreme of the floor
    ("rounds_UP_not_down",     3601,          "0.0208", "0"),      # the 1e-6 ROUND_UP boundary
    ("exactly_one_minute",     60,            "1.00",   "0"),
    ("whole_hour",             3600,          "1.00",   "0"),
    ("fractional_hour",        4237,          "0.137",  "0"),
    ("with_allocation_fee",    7200,          "0.50",   "0.25"),   # fee added AFTER quantize
    ("NO_RATE_fee_only",       3600,          None,     "1.00"),   # rate-less: runtime → ZERO
    ("NO_RATE_NO_FEE",         3600,          None,     None),     # a $0 settlement is a RECORD
    ("zero_rate_explicit",     3600,          "0",      "0"),
    ("long_running_desktop",   9 * 24 * 3600, "0.0416", "0"),      # the >24h flagship product
    ("tiny_rate_long_run",     86400,         "0.0001", "0"),
]


def _skip_if_no_billing():
    if _IMPORT_ERROR is not None:
        pytest.skip(
            "CANNOT IMPORT BILLING'S REAL SETTLEMENT MODEL/LAW "
            f"({type(_IMPORT_ERROR).__name__}: {_IMPORT_ERROR}). The settlement contract is "
            "therefore **NOT VERIFIED** against the other house — this suite refuses to certify "
            "it against a model I wrote myself (B-D1)."
        )


def _event(seconds, rate, fee, org="org-1", rid="res-1"):
    ev = {
        "event_type": "resource.stopped",
        "org_id": org,
        "resource_id": "sbx-1",
        "reservation_id": rid,
        "started_at": T0.isoformat(),
        "stopped_at": (T0 + timedelta(seconds=seconds)).isoformat(),
        # The library ALWAYS SETS these keys — to None when there is no value. That is exactly the
        # shape that produced B-D14 on the other side, which is why the PAYLOAD must omit them.
        "hourly_rate": rate,
        "allocation_fee": fee,
    }
    return ev


# --- 1. The payload billing REALLY accepts ------------------------------------------------


@pytest.mark.parametrize("case_id,seconds,rate,fee", CASES)
def test_the_payload_is_accepted_by_BILLINGS_REAL_request_model(case_id, seconds, rate, fee):
    """⭐ Validated by billing's OWN pydantic model — not by my model of it.

    This is the assertion that would have caught the contract MOVING under us: when `/settle`
    stopped taking `actual_cost` and started taking the inputs, a suite asserting against a
    hand-written double would still have been green.
    """
    _skip_if_no_billing()
    obs = observe(_event(seconds, rate, fee))
    body = {"settlement_key": "res-1", "reservation_id": "res-1", **obs.to_settlement_payload()}

    req = SettleActivationRequest(**body)   # billing's REAL model. It raises if we are wrong.

    assert req.settlement_key == "res-1"
    assert req.started_at == T0
    assert req.stopped_at == T0 + timedelta(seconds=seconds)
    assert (req.hourly_rate is None) == (rate is None), (
        "a rate we do not have must arrive at billing as None, and a rate we DO have must arrive "
        "intact — anything else silently changes what the customer is charged"
    )
    assert not hasattr(req, "actual_cost"), (
        "billing's model still has an actual_cost field?! The caller must not be pricing usage."
    )


@pytest.mark.parametrize("case_id,seconds,rate,fee", CASES)
def test_the_payload_NEVER_carries_a_cost(case_id, seconds, rate, fee):
    """B-D13's whole point, asserted on every vector. A caller that cannot compute a cost cannot
    compute it wrong — so the wire must never carry one."""
    _skip_if_no_billing()
    payload = observe(_event(seconds, rate, fee)).to_settlement_payload()
    for forbidden in ("actual_cost", "cost", "amount", "total"):
        assert forbidden not in payload, (
            f"the settlement payload carries {forbidden!r}. B-D13 is regressed: the library is "
            f"pricing usage again, which means a second implementation of the money law is back."
        )


def test_a_value_we_do_not_have_is_OMITTED_never_sent_as_null():
    """⚠️ **B-D14, and not recreating it.**

    The bug billing just fixed was an **always-present key whose value was sometimes `None`**:
    this library always set `hourly_rate` (to `None` when unpriced), and billing's
    `event.get("hourly_rate", "0.10")` only defaults on an **ABSENT** key — so it computed
    `Decimal(str(None))` → `InvalidOperation` → the money event **DLQ'd**, and the `"0.10"`
    fallback was **unreachable**.

    Pydantic accepts either an omitted key or an explicit `null`. But **any future consumer doing
    `.get(k, default)` — the exact bug we just killed — is safe against an omitted key and broken
    by a null one.** Send absence as absence.
    """
    payload = observe(_event(3600, None, None)).to_settlement_payload()

    assert "hourly_rate" not in payload, "a missing rate must be OMITTED, not null"
    assert "allocation_fee" not in payload, "a missing fee must be OMITTED, not null"
    assert set(payload) == {"started_at", "stopped_at"}

    # And a value we DO have is sent — including an explicit zero, which is a value, not an absence.
    priced = observe(_event(3600, "0", "0")).to_settlement_payload()
    assert priced["hourly_rate"] == "0" and priced["allocation_fee"] == "0", (
        "an explicit ZERO rate is a real quoted price and must be forwarded — conflating it with "
        "'no rate' would hide a pricing-config gap that billing's alert exists to surface"
    )


def test_the_library_NEVER_invents_a_rate():
    """A fabricated price is worse than no price (D-36).

    Billing's dead `"0.10"` fallback would have charged a rate **nobody configured**. Billing's
    new law prices a rate-less runtime at **ZERO and ALERTS**. The library's job is to report the
    absence faithfully — **not** to ship a competing default, and **not** to raise a competing
    alert. That policy lives in exactly one place.
    """
    obs = observe(_event(3600, None, None))
    assert obs.hourly_rate is None, "the library invented a rate"
    assert obs.allocation_fee is None
    assert not hasattr(obs, "actual_cost") and not hasattr(obs, "cost")


@pytest.mark.parametrize("bad,why", [
    ({"org_id": None}, "unattributable usage cannot be settled"),
    ({"started_at": None}, "a lifetime with no start cannot be priced"),
    ({"started_at": "not-a-date"}, "an unparseable start is not a start"),
])
def test_an_event_that_cannot_be_OBSERVED_is_unsettleable_not_guessed_at(bad, why):
    """The permanent-failure direction: void + alert. We never invent an input either."""
    ev = _event(3600, "1.00", "0")
    ev.update(bad)
    with pytest.raises(UnsettleableEvent):
        observe(ev)


def test_a_lifetime_that_runs_BACKWARDS_is_refused_before_it_reaches_billing():
    """Billing 400s this ("Settlement lifetime is invalid"). We catch it locally so a broken event
    is voided + alerted rather than burning a round-trip to be refused."""
    ev = _event(3600, "1.00", "0")
    ev["stopped_at"] = (T0 - timedelta(hours=1)).isoformat()
    with pytest.raises(UnsettleableEvent):
        observe(ev)


# --- 2. Freeze the contract for the GO runtime --------------------------------------------


def test_the_contract_table_is_FROZEN_for_the_go_runtime():
    """One contract, two runtimes, ONE table.

    Go cannot import billing's Python. So Python — which can — builds each payload with the REAL
    library, validates it through **billing's REAL request model**, prices it with **billing's
    REAL `price_usage`**, and freezes the result. The Go suite replays this exact file.

    **Re-derived by EXECUTING billing on every run**, never transcribed: a frozen table derived
    from a law that has since changed is the stale-double failure B-D1 describes, and it would keep
    agreeing with a law nobody runs.

    ⚠️ It is a **tripwire, not a certifier**. It cannot prove Go talks to the real billing service.
    **A Go↔real-billing test remains OWED** (DECISIONS B-D1). Do not let this table quietly become
    the certifier it is not.
    """
    _skip_if_no_billing()
    VECTORS.parent.mkdir(parents=True, exist_ok=True)

    rows = []
    for case_id, seconds, rate, fee in CASES:
        obs = observe(_event(seconds, rate, fee))
        payload = obs.to_settlement_payload()

        # Billing's REAL model must accept it...
        req = SettleActivationRequest(
            settlement_key="res-1", reservation_id="res-1", **payload
        )
        # ...and billing's REAL law prices it.
        cost = BILLING_PRICE(
            started_at=req.started_at,
            stopped_at=req.stopped_at,
            hourly_rate=req.hourly_rate,
            allocation_fee=req.allocation_fee,
        )
        assert "actual_cost" not in payload
        rows.append({
            "id": case_id,
            "event": {
                "started_at": T0.isoformat(),
                "stopped_at": (T0 + timedelta(seconds=seconds)).isoformat(),
                "hourly_rate": rate,          # null == the event carries no rate
                "allocation_fee": fee,
                "org_id": "org-1",
                "resource_id": "sbx-1",
                "reservation_id": "res-1",
            },
            "expected_request_body": payload,  # ← what Go MUST build. Absent keys are ABSENT.
            "billing_priced_cost": str(cost),  # ← what billing charges. Go does NOT compute this.
        })

    doc = {
        "contract": "POST /billing/{org_id}/settle — the caller sends the INPUTS; billing prices them",
        "law_owner": "billing/output/app/core/proration.py::price_usage",
        "request_model": "billing/output/app/models/billing.py::SettleActivationRequest",
        "derived_by": "EXECUTING billing's real model + real law (not transcribed from them)",
        "conformance_id": "ST-SETTLE-1",
        "ticket": "20260712_revenue_chain_integrity (B-D13)",
        "runtimes": ["python", "go"],
        "caveat": (
            "A TRIPWIRE, NOT A CERTIFIER. It proves Go builds the body billing accepts and records "
            "what billing charges for it. It does NOT prove Go can talk to the real billing "
            "service — a Go<->real-billing test remains OWED (B-D1)."
        ),
        "notes": [
            "expected_request_body OMITS a money key the event has no value for — it is never "
            "null (B-D14: an always-present key whose value is sometimes null is a landmine for "
            "any `.get(k, default)` on the other side).",
            "A rate-less event prices its RUNTIME at ZERO and billing ALERTS. Neither runtime "
            "invents a rate — a fabricated price is worse than no price (D-36).",
            "B-D16: the vectors deliberately include the sub-minute floor and the rounding "
            "boundary. Whole-hour lifetimes are where a wrong law HIDES.",
        ],
        "vectors": rows,
    }
    VECTORS.write_text(json.dumps(doc, indent=2) + "\n")

    reloaded = json.loads(VECTORS.read_text())
    assert len(reloaded["vectors"]) == len(CASES)
    assert any(v["id"] == "sub_minute_floor" for v in reloaded["vectors"]), (
        "the floor vector MUST be in the frozen table — B-D16"
    )
    no_rate = next(v for v in reloaded["vectors"] if v["id"] == "NO_RATE_fee_only")
    assert "hourly_rate" not in no_rate["expected_request_body"], "absence must be frozen as ABSENCE"
    # Compare as MONEY, never as bytes: billing renders Decimal("1.000000"), and asserting on the
    # string would make this test about formatting rather than about the amount charged (D-68).
    assert Decimal(no_rate["billing_priced_cost"]) == Decimal("1.00"), (
        "a rate-less hour costs the FEE only — its runtime is ZERO, not an invented $0.10/h"
    )
    # And the guard that makes that meaningful: an invented $0.10/h rate would have charged MORE.
    assert Decimal(no_rate["billing_priced_cost"]) < Decimal("1.10")
