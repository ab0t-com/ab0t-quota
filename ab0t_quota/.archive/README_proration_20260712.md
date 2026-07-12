# ARCHIVED: `ab0t_quota/proration.py` — the library's copy of billing's money law

**Archived:** 2026-07-12 · **By:** W-CHAIN · **Ticket:** `billing/output/tickets/20260712_revenue_chain_integrity`
**Decision:** **B-D13** (accepted).

## Why it existed
`POST /billing/{org_id}/settle` originally took a **pre-computed `actual_cost`**. That single
field pushed the cost law across the mesh boundary: every caller had to reimplement billing's
proration (60s floor, ROUND_UP to 1e-6, fee added after quantization). **Three** implementations
of one money law existed — billing's, this file, and `ab0t-quota-go/proration/`.

It was guarded by a frozen cross-house vector table, because a forced duplicate can only be made
**detectable**, never **safe**.

## Why it is gone
Billing changed `/settle` to accept the **INPUTS** and price them itself through the one law it
owns (`app/core/proration.py::price_usage`). So this is **deleted, not synchronised.**

> **A copy kept in sync is still a copy.**
> **A caller that cannot compute a cost cannot compute it wrong.**

The library now reports **what it observed** (`ab0t_quota/billing/observation.py`); billing decides
what it costs.

## Kept for the audit trail
`proration_20260712_superseded_by_BD13.py` is the file as it shipped, intact. It is **not
imported by anything** and must not be revived: reviving it re-opens the drift it was archived to
close. If you need the arithmetic, it lives — and only lives — in
`billing/output/app/core/proration.py`.
