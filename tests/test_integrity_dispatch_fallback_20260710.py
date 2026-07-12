"""P1.8 / QC-05 — the no-store dispatch fallback must be SHARED + loud, not a
fresh per-dispatch InMemoryLedgerStore (ticket 20260709).

RED-BY-DESIGN (until P1.8). `_dispatch_handler` today does
`store = ledger_store or InMemoryLedgerStore()` (auth_events.py) — a BRAND NEW
in-memory store per dispatch when no store is supplied. Result: delivery dedup,
retry bookkeeping and replay observability are all silently no-ops, with no
warning, for any consumer that mounts `make_router` without a store. Two
deliveries of the SAME event both run the handler body.

Fix (P1.8): a module-level SHARED fallback store + a loud one-time warning.
GREEN target: two no-store dispatches of the same event share dedup → the
handler body runs exactly once.
"""
from __future__ import annotations

import pytest

from ab0t_quota import auth_events
from ab0t_quota.handler_ledger import idempotent


@pytest.mark.asyncio
async def test_no_store_dispatch_shares_dedup_across_dispatches():
    # Reset the shared fallback if the fix has introduced one (test hygiene).
    if hasattr(auth_events, "_reset_fallback_ledger_store"):
        auth_events._reset_fallback_ledger_store()

    runs = {"n": 0}

    @idempotent(handler="qc05_grant")
    async def h(event, ctx):
        runs["n"] += 1
        return ctx.success(side_effect_id="sid")

    event = {"event_type": "user.created", "event_id": "qc05-e1", "data": {"user_id": "u1"}}

    # No ledger_store supplied — the documented manual make_router path.
    await auth_events._dispatch_handler(h, event, None)
    await auth_events._dispatch_handler(h, event, None)

    assert runs["n"] == 1, (
        f"QC-05: handler body ran {runs['n']} times across two no-store dispatches "
        f"of the same event — the per-dispatch InMemoryLedgerStore means delivery "
        f"dedup is a silent no-op. The fallback store must be shared."
    )
