# Superseded by B-D13 (2026-07-12, W-CHAIN)

`test_d12_proration_conformance_20260712.py` and `proration_vectors_20260712.json` existed to make
a **forced duplicate of billing's money law** detectable: `/settle` took a pre-computed
`actual_cost`, so the library had to reimplement billing's proration, and the vector table was the
tripwire that would fire if the two drifted.

**Billing now takes the INPUTS and prices them itself.** The library's proration is archived
(`ab0t_quota/.archive/`), so there is no longer a second implementation to keep in sync — and
therefore nothing for these to guard.

**Retired in place, not deleted** (never delete a test; the audit trail shows what was guarded and
why it went). Superseded by `tests/test_d12_settlement_contract_20260712.py`, which pins the thing
that CAN still be wrong: **the request payload**, against billing's REAL pydantic model and REAL
`price_usage`.
