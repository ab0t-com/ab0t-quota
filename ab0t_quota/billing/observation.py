"""What the library OBSERVED. Not what it costs — that is billing's business.

Ticket: billing/output/tickets/20260712_revenue_chain_integrity (**B-D13**, the caller's half).

THIS MODULE REPLACES A MONEY LAW WITH A REPORT
----------------------------------------------
`ab0t_quota/proration.py` used to live here in spirit: a **faithful port of billing's
proration**, because `/settle` took a pre-computed `actual_cost` and every caller therefore had
to reimplement billing's arithmetic. Three implementations of one money law existed — billing's,
this library's, and the Go library's — guarded by a frozen vector table.

**A copy kept in sync is still a copy.** Billing has since changed `/settle` to accept the
**INPUTS** (`started_at`, `stopped_at`, `hourly_rate`, `allocation_fee`) and price them itself
through the one law it owns (`app/core/proration.py::price_usage`). So the library's proration is
**deleted**, not synchronised — archived to `ab0t_quota/.archive/` with a citation.

  **A caller that cannot compute a cost cannot compute it wrong.**

The boundary is now the correct one between two houses:
  * the **library** reports what it saw — when the resource started, when it stopped, the rate it
    was quoted;
  * **billing** decides what that costs.

⚠️ THE TRAP THIS MODULE MUST NOT FALL INTO
-------------------------------------------
Deleting the local proration must not turn into the library *inferring* a cost some other way, or
shipping a "fallback" price. **A fabricated price is worse than no price**: nothing is honest,
recoverable and alertable; a fabricated number is an overcharge we cannot defend (D-36).

So there is **no arithmetic in this file at all** — no rate default, no minimum, no rounding. A
missing rate is reported as **missing**. Billing's law prices a rate-less runtime at **ZERO and
alerts** (`price_usage`; `reservation.py::settle_activation` → `settle_missing_hourly_rate`).
That decision is **billing's, and it is made in exactly one place** — this library does not
duplicate it, does not second-guess it, and does not raise a competing alert.

⚠️ B-D14, AND NOT RECREATING IT
--------------------------------
The last landmine here was an **always-present key whose value was sometimes `None`**: this
library always set `hourly_rate` (to `None` when there was no price), and billing's
`event.get("hourly_rate", "0.10")` only defaults on an **ABSENT** key — so it evaluated
`Decimal(str(None))` → `InvalidOperation` → the money event **DLQ'd**, and the `"0.10"` fallback
was unreachable.

`to_settlement_payload()` therefore **OMITS a key it has no value for**, rather than sending an
explicit `null`. Billing's pydantic model accepts either, but any *future* consumer doing
`.get(k, default)` — the exact bug we just killed — is safe against an omitted key and is broken
by a null one. **Send absence as absence.**
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional


class UnsettleableEvent(Exception):
    """The event cannot be turned into a settlement at all (no org, no start time, a lifetime
    that runs backwards).

    This is the **permanent** failure direction: not retried — **voided and alerted**, which is
    the pre-existing behaviour, kept as the fallback it was always meant to be.
    """


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    """Parse a lifecycle-event timestamp. Mirrors billing's `_parse_dt`
    (`app/workers/lifecycle_consumer.py`) — same tolerance, same failure mode (`None`, never an
    exception), so both houses agree on what an unparseable timestamp *is*.

    This is parsing, not pricing. It stays.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _money(raw: Any) -> Optional[Decimal]:
    """Read a money field off an event. **`None` when there is no value** — never a default.

    Deliberately mirrors billing's `proration.py::coerce_money`, which returns `None` rather than
    inventing `0` or `0.10`, *"so the CALLER must decide what a missing rate means, out loud"*.
    Our answer, out loud: we report it missing and let billing's one law price and alert it.
    """
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        # A rate we cannot even parse is NOT a rate of zero — that would silently under-bill.
        # It is a broken event: unsettleable, void + alert.
        raise UnsettleableEvent("unparseable money field on the lifecycle event")


@dataclass(frozen=True)
class SettlementObservation:
    """What happened, as this library saw it. **Contains no cost, by design.**"""

    org_id: str
    started_at: datetime
    stopped_at: datetime
    resource_id: str
    hourly_rate: Optional[Decimal] = None      # None == "we were never quoted a rate"
    allocation_fee: Optional[Decimal] = None

    def to_settlement_payload(self) -> Dict[str, Any]:
        """The body of `POST /billing/{org_id}/settle`, minus the keys billing's caller supplies
        (`settlement_key`, `reservation_id`).

        Timestamps are ISO-8601; money is a **string** (the house Decimal-as-string convention —
        a float would put binary-fraction error into a number about to be debited from a
        customer). **A value we do not have is OMITTED, not sent as `null`** (B-D14, above).
        """
        body: Dict[str, Any] = {
            "started_at": self.started_at.isoformat(),
            "stopped_at": self.stopped_at.isoformat(),
        }
        if self.hourly_rate is not None:
            body["hourly_rate"] = str(self.hourly_rate)
        if self.allocation_fee is not None:
            body["allocation_fee"] = str(self.allocation_fee)
        return body


def observe(event: dict) -> SettlementObservation:
    """Extract the settlement observation from a lifecycle event.

    Raises `UnsettleableEvent` when the event can never yield a settlement — a genuine
    "this can never settle at any horizon, on any retry", i.e. the void+alert path.

    Note what is NOT a reason to refuse: **a missing hourly_rate.** That is a pricing-config gap,
    not an unsettleable event — the allocation fee may still be owed, and billing prices the
    runtime at zero and *alerts*. Refusing here would re-create a revenue-loss path (B-D9) and
    would also hide the very gap billing's alert exists to surface.
    """
    org_id = event.get("org_id")
    if not org_id:
        raise UnsettleableEvent("no org_id; the usage is unattributable")

    started_at = parse_dt(event.get("started_at"))
    if started_at is None:
        raise UnsettleableEvent("no parseable started_at; a lifetime cannot be priced")

    # Billing's consumer defaults a missing stop to "now" — the resource stopped, we just were
    # not told precisely when. Mirrored, so the two houses price the same lifetime.
    stopped_at = parse_dt(event.get("stopped_at")) or datetime.now(timezone.utc)

    if stopped_at < started_at:
        # Billing 400s this ("Settlement lifetime is invalid"). Catch it here so a broken event
        # is voided + alerted rather than burning a round-trip to be refused.
        raise UnsettleableEvent("lifetime runs backwards (stopped_at < started_at)")

    return SettlementObservation(
        org_id=org_id,
        started_at=started_at,
        stopped_at=stopped_at,
        resource_id=event.get("resource_id") or "unknown",
        hourly_rate=_money(event.get("hourly_rate")),
        allocation_fee=_money(event.get("allocation_fee")),
    )
