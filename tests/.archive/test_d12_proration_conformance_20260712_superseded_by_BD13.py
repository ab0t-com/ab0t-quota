"""The PRORATION LAW — pinned against billing's REAL function, and frozen for Go.

Ticket: billing/output/tickets/20260712_revenue_chain_integrity (worker W-CHAIN, D-12 caller leg).
Companion: `ab0t_quota/proration.py` (read its module docstring first — it explains why a money
law is duplicated here at all, and why the duplication is FORCED by billing's endpoint contract
rather than chosen).

WHY THIS TEST EXISTS
--------------------
`POST /billing/{org_id}/settle` takes a **computed `actual_cost`**, not the proration inputs. So
the library must do billing's arithmetic. That is a **second implementation of a money law** —
the D-35 hazard the parent ticket exists to kill. It cannot be avoided (a shared library cannot
import a service's app code), so it must instead be made **detectable**:

  ⚠️ **If billing changes its proration and `ab0t_quota/proration.py` does not follow, THIS TEST
     GOES RED.** That is the entire point. Without it, a settled amount would silently drift from
     a committed amount for the same usage, and nothing would notice.

THIS TEST FOUND A REAL HOLE IN ITS OWN SUITE
--------------------------------------------
The cross-house settlement suite originally used only whole-hour lifetimes (2h/3h/4h). Negative
control **NC-5** — swapping the proration for the library's *quota-cap* float arithmetic — left
all 11 tests **GREEN**, because both arithmetics agree on whole hours. The law was not pinned at
all; it only looked pinned. The vectors below deliberately target the three places the two
arithmetics DISAGREE:

  * **the 60-second floor** — a 30s sandbox bills a full minute;
  * **ROUND_UP at 1e-6** — a rate that does not divide evenly rounds UP, never down;
  * **the allocation fee** — added AFTER the runtime is quantized, so the fee is never rounded.

Each of those is a case where the wrong arithmetic charges the customer a *different amount*.

RUN (needs billing's source; billing's venv has both houses' deps):
    cd /home/ubuntu/infra/infra/code/billing/output
    PYTHONPATH=/home/ubuntu/infra/infra/code/shared/ab0t-quota ./venv/bin/python -m pytest \\
      /home/ubuntu/infra/infra/code/shared/ab0t-quota/tests/test_d12_proration_conformance_20260712.py -v
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from ab0t_quota.proration import (
    UnsettleableEvent,
    calculate_prorated_cost,
    settlement_cost,
)

BILLING_ROOT = Path("/home/ubuntu/infra/infra/code/billing/output")
VECTORS = Path(__file__).parent / "data" / "proration_vectors_20260712.json"

_IMPORT_ERROR = None
try:
    if str(BILLING_ROOT) not in sys.path:
        sys.path.insert(0, str(BILLING_ROOT))
    from app.core.proration import calculate_prorated_cost as BILLING_PRORATION
except Exception as e:  # pragma: no cover - environment guard
    _IMPORT_ERROR = e


T0 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

#: (id, seconds, hourly_rate, allocation_fee)
#: The first three rows are the ones that separate billing's arithmetic from a naive one.
#: If you add a row, add it to the Go table too — `conformance/scenarios.json` (ST-SETTLE-1)
#: declares that both runtimes are held to this same table.
CASES = [
    ("sub_minute_floor",        30,     "1.00",   "0"),       # < 60s → the floor bites
    ("one_second",              1,      "3.60",   "0"),       # the extreme of the floor
    ("rounds_UP_not_down",      3601,   "0.0208", "0"),       # 1e-6 ROUND_UP
    ("exactly_one_minute",      60,     "1.00",   "0"),
    ("whole_hour",              3600,   "1.00",   "0"),
    ("three_hours",             10800,  "1.00",   "0"),
    ("with_allocation_fee",     7200,   "0.50",   "0.25"),    # fee added AFTER quantize
    ("fee_only_zero_rate",      3600,   "0",      "1.00"),
    ("zero_rate_zero_fee",      3600,   "0",      "0"),       # a $0 settlement is still a record
    ("long_running_desktop",    9 * 24 * 3600, "0.0416", "0"),  # the >24h flagship product
    ("odd_seconds",             4237,   "0.137",  "0"),
    ("tiny_rate_long_run",      86400,  "0.0001", "0"),
]


def _skip_if_no_billing():
    if _IMPORT_ERROR is not None:
        pytest.skip(
            "CANNOT IMPORT BILLING'S REAL PRORATION "
            f"({type(_IMPORT_ERROR).__name__}: {_IMPORT_ERROR}). The proration law is therefore "
            "**NOT VERIFIED** against the other house — this suite refuses to certify a money "
            "law against a table I wrote myself (B-D1)."
        )


@pytest.mark.parametrize("case_id,seconds,rate,fee", CASES)
def test_the_library_proration_EQUALS_billings_REAL_proration(case_id, seconds, rate, fee):
    """⭐ THE CROSS-HOUSE ASSERTION. Not a table I invented — billing's own function, executed.

    A settled amount must be identical, to the last decimal place, to what billing would have
    charged had the same usage been COMMITTED. Money must not depend on whether the SNS event
    happened to arrive in time.
    """
    _skip_if_no_billing()
    started, stopped = T0, T0 + timedelta(seconds=seconds)

    mine = calculate_prorated_cost(started, stopped, Decimal(rate), Decimal(fee))
    theirs = BILLING_PRORATION(started, stopped, Decimal(rate), Decimal(fee))

    assert mine == theirs, (
        f"PRORATION DRIFT on '{case_id}': the library would settle {mine} where billing would "
        f"commit {theirs}. The same usage charges two different amounts depending only on which "
        f"path it took. Re-port `ab0t_quota/proration.py` from billing's app/core/proration.py."
    )


def test_the_sub_minute_FLOOR_is_real_and_is_what_separates_the_two_arithmetics():
    """The single most important vector: the library's *quota-cap* cost function
    (`lifecycle._record_cost`) has NO 60s floor. If anyone ever reuses it to settle, a 30-second
    sandbox is billed for 30 seconds while billing would bill a minute — and NC-5 proved that a
    whole-hours-only test suite CANNOT SEE THAT."""
    _skip_if_no_billing()
    started, stopped = T0, T0 + timedelta(seconds=30)

    billed = BILLING_PRORATION(started, stopped, Decimal("1.00"), Decimal("0"))
    naive = Decimal(str(float(Decimal("30") / Decimal("3600") * Decimal("1.00"))))

    assert billed == calculate_prorated_cost(started, stopped, Decimal("1.00"), Decimal("0"))
    assert billed != naive, "the floor must actually bite, or this vector proves nothing"
    assert billed == Decimal("0.016667"), "60s at $1.00/h, rounded UP to 1e-6"


def test_rounding_is_UP_never_down_so_we_never_undercharge_by_a_fraction():
    _skip_if_no_billing()
    started, stopped = T0, T0 + timedelta(seconds=3601)
    mine = calculate_prorated_cost(started, stopped, Decimal("0.0208"), Decimal("0"))
    assert mine == BILLING_PRORATION(started, stopped, Decimal("0.0208"), Decimal("0"))
    exact = Decimal("0.0208") * (Decimal("3601") / Decimal("3600"))
    assert mine >= exact, "ROUND_UP: the platform never rounds a charge down"


# --- The event → cost seam ---------------------------------------------------------------


def test_settlement_cost_reads_an_event_the_way_BILLING_reads_it():
    """`settlement_cost()` must derive from a lifecycle event exactly what billing's lifecycle
    consumer derives from the same event — same fields, same defaults, same result."""
    _skip_if_no_billing()
    started = T0
    stopped = T0 + timedelta(seconds=7200)
    event = {
        "org_id": "org-1",
        "resource_id": "sbx-1",
        "reservation_id": "res-1",
        "hourly_rate": "0.50",
        "allocation_fee": "0.25",
        "started_at": started.isoformat(),
        "stopped_at": stopped.isoformat(),
    }
    assert settlement_cost(event) == BILLING_PRORATION(
        started, stopped, Decimal("0.50"), Decimal("0.25")
    )


@pytest.mark.parametrize("bad,why", [
    ({"org_id": None}, "unattributable usage cannot be settled"),
    ({"started_at": None}, "a lifetime with no start cannot be prorated"),
    ({"started_at": "not-a-date"}, "an unparseable start is not a start"),
])
def test_an_event_that_cannot_yield_a_COST_is_UNSETTLEABLE_not_guessed_at(bad, why):
    """The permanent-failure direction. We never invent a number for money."""
    event = {
        "org_id": "org-1",
        "resource_id": "sbx-1",
        "hourly_rate": "1.00",
        "allocation_fee": "0",
        "started_at": T0.isoformat(),
        "stopped_at": (T0 + timedelta(hours=1)).isoformat(),
    }
    event.update(bad)
    with pytest.raises(UnsettleableEvent):
        settlement_cost(event)


def test_a_RATE_LESS_event_settles_ZERO_rather_than_crashing_or_inventing_a_rate():
    """FOUND, and reported to billing's owner (see the ticket artifact):

        billing: Decimal(str(event.get("hourly_rate", "0.10")))

    `dict.get(k, default)` returns the default only when the key is **ABSENT**. This library
    ALWAYS SETS `hourly_rate` (to `None` when there is no price), so billing computes
    `Decimal(str(None))` → `Decimal('None')` → **`decimal.InvalidOperation`**. Billing's `"0.10"`
    fallback is unreachable from this library's events, and a rate-less money event CRASHES its
    consumer.

    There is therefore no billing *amount* to agree with here — only a crash. We settle **$0**,
    which under QM-02 is a positive, auditable record ("metered, charged nothing"), rather than
    crashing or inventing a $0.10/h rate nobody agreed to.
    """
    from decimal import InvalidOperation
    with pytest.raises(InvalidOperation):
        Decimal(str(None))       # pins the billing-side crash this test's premise rests on

    event = {
        "org_id": "org-1",
        "resource_id": "sbx-1",
        "hourly_rate": None,
        "allocation_fee": None,
        "started_at": T0.isoformat(),
        "stopped_at": (T0 + timedelta(hours=1)).isoformat(),
    }
    assert settlement_cost(event) == Decimal("0")


# --- Freeze the vectors so the GO runtime is held to the SAME law -------------------------


def test_the_vector_table_is_FROZEN_for_the_go_runtime():
    """One law, two runtimes, ONE vector table.

    Go cannot import billing's Python. So Python — which CAN — computes the expectations with
    billing's REAL function and freezes them here; the Go suite replays this exact file. If the
    two runtimes' proration ever diverges, Go goes red against Python's cross-house-verified
    numbers rather than against a table a Go author wrote from the same misunderstanding.

    Declared as **ST-SETTLE-1** in `conformance/scenarios.json`.
    """
    _skip_if_no_billing()
    VECTORS.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for case_id, seconds, rate, fee in CASES:
        started, stopped = T0, T0 + timedelta(seconds=seconds)
        expected = BILLING_PRORATION(started, stopped, Decimal(rate), Decimal(fee))
        assert expected == calculate_prorated_cost(started, stopped, Decimal(rate), Decimal(fee))
        rows.append({
            "id": case_id,
            "started_at": started.isoformat(),
            "stopped_at": stopped.isoformat(),
            "elapsed_seconds": seconds,
            "hourly_rate": rate,
            "allocation_fee": fee,
            "expected_cost": str(expected),
        })

    doc = {
        "law": "settlement cost = allocation_fee + ROUND_UP(hourly_rate * max(elapsed, 60s)/3600, 1e-6)",
        "source_of_truth": "billing/output/app/core/proration.py::calculate_prorated_cost",
        "derived_by": "EXECUTING billing's real function (not transcribed from it)",
        "conformance_id": "ST-SETTLE-1",
        "ticket": "20260712_revenue_chain_integrity (D-12 caller leg)",
        "runtimes": ["python", "go"],
        "vectors": rows,
    }
    VECTORS.write_text(json.dumps(doc, indent=2) + "\n")

    reloaded = json.loads(VECTORS.read_text())
    assert len(reloaded["vectors"]) == len(CASES)
    assert any(v["id"] == "sub_minute_floor" for v in reloaded["vectors"]), (
        "the floor vector MUST be in the frozen table — it is the one NC-5 proved a "
        "whole-hours suite cannot see"
    )
