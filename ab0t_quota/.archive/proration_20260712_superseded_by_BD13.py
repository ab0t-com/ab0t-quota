"""The proration law — the arithmetic that turns a resource lifetime into money.

Ticket: billing/output/tickets/20260712_revenue_chain_integrity (D-12, the caller leg).

WHY THIS FILE EXISTS, AND WHY IT IS A DUPLICATE (read before editing)
---------------------------------------------------------------------
This is a **faithful port of billing's `app/core/proration.py::calculate_prorated_cost`**.
Duplicating a money law is exactly the multi-source-of-truth hazard the parent ticket exists
to kill (**D-35: one law, one implementation**). It is duplicated here anyway, because the
contract forces it:

  * `POST /billing/{org_id}/settle` accepts a **computed `actual_cost`** — a number. It does
    NOT accept the proration *inputs* (started_at / hourly_rate / allocation_fee). So the
    caller MUST do the arithmetic.
  * A shared library cannot import a service's application code (wrong dependency direction),
    and billing's endpoint is outside this ticket-leg's write allowlist.

So the duplication is **forced by the contract**, not chosen. What is chosen is that it is
made **SAFE**, in the only way a forced duplicate can be:

  ⚠️ `tests/test_d12_proration_conformance_20260712.py` imports billing's **REAL**
     `calculate_prorated_cost` from the billing source tree and asserts, over a vector table,
     that this function agrees with it **exactly**. If billing ever changes its proration and
     this file does not follow, **that test goes red.** The vectors are frozen into
     `tests/data/proration_vectors_20260712.json` so the **Go** runtime is held to the same
     spec (one law, two runtimes, one vector table).

**This is a DIVERGENCE RISK THAT IS DETECTED, not a divergence that is prevented.** The real
fix is for the proration law to live in ONE place that both houses consume. That is framed as
a decision (B-D13), not silently patched over. Do not "improve" the arithmetic here: it is not
ours. It mirrors billing. If it looks wrong, it is wrong THERE, and it must be changed there
first.

WHY NOT REUSE `LifecycleEmitter._record_cost`
---------------------------------------------
The library already has a cost calculation (`billing/lifecycle.py::_record_cost`) and it
**DISAGREES with billing's** — deliberately:

    | | billing (money)                 | _record_cost (quota cap)      |
    |-| ------------------------------- | ----------------------------- |
    | min billing unit | max(elapsed, 60s)      | none                          |
    | rounding         | ROUND_UP to 1e-6       | none                          |
    | type             | Decimal                | float                         |

`_record_cost` feeds the **monthly-cost quota accumulator**, where billing is explicitly "the
source of truth for charges, this just keeps the quota cap honest". Its approximation is fine
there and **catastrophic here**: using it to settle would charge a *different amount* than a
commit of the same usage, silently, depending only on whether the SNS event happened to be
delivered in time. Money must not depend on the weather.
"""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_UP
from typing import Optional

#: Minimum billable unit, in seconds. Mirrors billing's `MIN_BILLING_SECONDS`.
#: A 3-second sandbox still bills a minute — a deliberate floor, not a rounding artifact.
MIN_BILLING_SECONDS = 60

#: Billing's fallback when a lifecycle event carries no hourly_rate *key at all*.
#: See `settlement_cost()` for why the library never actually reaches this default.
BILLING_DEFAULT_HOURLY_RATE = Decimal("0.10")


def calculate_prorated_cost(
    started_at: datetime,
    stopped_at: datetime,
    hourly_rate: Decimal,
    allocation_fee: Decimal = Decimal("0"),
    min_billing_seconds: int = MIN_BILLING_SECONDS,
) -> Decimal:
    """Total cost = allocation_fee + prorated runtime.

    A LINE-FOR-LINE port of billing `app/core/proration.py:20-44`. Pinned against the real
    function by `tests/test_d12_proration_conformance_20260712.py`.

    The order of operations is load-bearing and is NOT tidied:
      * the elapsed floor is applied BEFORE the division;
      * the runtime cost is quantized ROUND_UP to 1e-6 **before** the fee is added, so the
        fee itself is never rounded.
    Reproducing the *result* is not enough — reproducing the *arithmetic* is, because that is
    what keeps a settled amount identical to a committed amount for the same usage.
    """
    elapsed = (stopped_at - started_at).total_seconds()
    elapsed = max(elapsed, min_billing_seconds)
    hours = Decimal(str(elapsed)) / Decimal("3600")
    runtime_cost = hourly_rate * hours
    total = allocation_fee + runtime_cost.quantize(Decimal("0.000001"), rounding=ROUND_UP)
    return total


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    """Parse a lifecycle event timestamp. Port of billing's `_parse_dt`
    (`app/workers/lifecycle_consumer.py:478-487`) — same tolerance, same failure mode
    (`None`, never an exception), so the two houses agree on what an unparseable
    timestamp *is*."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


class UnsettleableEvent(Exception):
    """The event cannot be turned into a settlement at all (no org, no start time, ...).

    This is the *permanent* failure direction: it is not retried, it is VOIDED and ALERTED —
    the pre-existing behaviour, kept exactly, as the fallback it was always meant to be.
    """


def settlement_cost(event: dict) -> Decimal:
    """Derive the settlement amount from a lifecycle event, the way BILLING would.

    Raises `UnsettleableEvent` when the event cannot yield a cost — which is a genuine
    "this can never settle", i.e. the void+alert path.

    ⚠️ The `hourly_rate` handling deliberately does NOT mirror billing byte-for-byte, and
    here is exactly why (FOUND, reported for billing's owner — see the ticket artifact):

        billing: Decimal(str(event.get("hourly_rate", "0.10")))

    `dict.get(k, default)` returns the default only when the key is **ABSENT**. This library
    ALWAYS SETS the key (`lifecycle.py:189`: `str(hourly_rate) if hourly_rate else None`), so
    for a rate-less event billing receives `hourly_rate=None`, computes `Decimal(str(None))`
    == `Decimal('None')` and raises `decimal.InvalidOperation`. Billing's `"0.10"` default is
    therefore **unreachable from this library's events**, and a rate-less money event CRASHES
    billing's consumer rather than being charged $0.10/h.

    So there is no billing *amount* to agree with in that case — only a crash. We treat a
    missing rate as **zero** and still settle: with QM-02, a $0 settlement is a POSITIVE
    RECORD ("we metered you and charged you nothing"), which is strictly better than both a
    crash and an invented $0.10/h. Where a rate IS present — the entire normal path — the
    arithmetic is identical to billing's, to the last decimal place.
    """
    org_id = event.get("org_id")
    if not org_id:
        raise UnsettleableEvent("no org_id; the usage is unattributable")

    started_at = parse_dt(event.get("started_at"))
    if started_at is None:
        raise UnsettleableEvent("no parseable started_at; a lifetime cannot be prorated")
    stopped_at = parse_dt(event.get("stopped_at")) or datetime.now(timezone.utc)

    raw_rate = event.get("hourly_rate")
    hourly_rate = Decimal(str(raw_rate)) if raw_rate not in (None, "") else Decimal("0")
    raw_fee = event.get("allocation_fee")
    allocation_fee = Decimal(str(raw_fee)) if raw_fee not in (None, "") else Decimal("0")

    cost = calculate_prorated_cost(started_at, stopped_at, hourly_rate, allocation_fee)
    if cost < 0:
        # Billing REFUSES a negative settlement (400) — a negative amount would CREDIT the
        # customer through a path with no credit authorisation whatsoever. Fail here, in the
        # library, toward void+alert rather than toward a refused HTTP call.
        raise UnsettleableEvent("negative settlement cost")
    return cost
