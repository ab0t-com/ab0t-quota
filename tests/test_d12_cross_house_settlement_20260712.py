"""D-12, the CALLER leg — the library settles against BILLING'S REAL ENDPOINT.

Ticket: billing/output/tickets/20260712_revenue_chain_integrity (worker W-CHAIN).
Read first: that ticket's `information_d12_durable_settlement_20260712.md` (billing's half)
and `DECISIONS.md` **B-D1** — the decision this file exists to discharge.

WHY THIS FILE IS NOT LIKE THE OTHER TESTS IN THIS REPO
------------------------------------------------------
**B-D1, verbatim:** *"A double can prove what YOUR code does with a contract. Only the real
system can prove the CONTRACT holds."* That decision was written after a cross-house tripwire
(`test_settle_once_billing_retention_20260710.py`) asserted against a `FakeBilling` hard-coded
to billing's OLD semantics — so when billing shipped the contract the tripwire was built to
detect, **it could not tell.** A real cross-house integration test was recorded as **OWED.**

**This is that test.** Nothing here is a double:

  * billing's **REAL** `POST /billing/{org_id}/settle` route, imported from billing's own
    source tree and mounted in a FastAPI app;
  * billing's **REAL** `ReservationManager.settle_activation` — the real four-layer guard, the
    real three-bucket spend-order Lua, the real DynamoDB conditional write;
  * this library's **REAL** `BillingServiceClient` and **REAL** `LifecycleEmitter`, over a real
    HTTP stack (ASGI transport — real request encoding, real status codes, real error mapping);
  * **REAL DynamoDB Local** (a throwaway table per test) and **REAL Redis (:6382)**.

The only stand-in is the *auth dependency* (billing's `BillingTransactionWriter`), which is
overridden with an authorised user. Auth is not the contract under test; the money is.

THE DEFECT
----------
`shared/ab0t-quota` writes a durable intent for every money-bearing lifecycle event. When that
event cannot be delivered before the retry horizon, the drain **voided it and alerted** — on
the premise, stated in the code, that *"a late commit would 404 at billing anyway"*. That
premise was true. **It is now false**: billing has a durable, activation-scoped settlement path.
Until this change, nothing called it. The endpoint existed and **the money was still lost.**

  `test_D12_RED_...` pins the loss AS IT EXISTS IN PRODUCTION TODAY. It is not a
  hypothetical: it constructs the emitter exactly as production does when no settlement
  client is wired, and asserts the money is voided and never charged.

HOW TO RUN (needs BOTH houses' dependencies in one interpreter — billing's venv has them)
-----------------------------------------------------------------------------------------
    cd /home/ubuntu/infra/infra/code/billing/output
    PYTHONPATH=/home/ubuntu/infra/infra/code/shared/ab0t-quota \\
      ./venv/bin/python -m pytest \\
      /home/ubuntu/infra/infra/code/shared/ab0t-quota/tests/test_d12_cross_house_settlement_20260712.py -v

If DynamoDB Local or Redis is down this suite **SKIPS LOUDLY** and D-12's caller leg is
**NOT VERIFIED** — exactly-once is a conditional write and a mock cannot prove it (D-57).
"""
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio

BILLING_ROOT = Path("/home/ubuntu/infra/infra/code/billing/output")

# --- The cross-house import. If billing's source is not on this box, this suite cannot
# --- certify anything and must say so rather than pass vacuously.
_IMPORT_ERROR = None
try:
    if str(BILLING_ROOT) not in sys.path:
        sys.path.insert(0, str(BILLING_ROOT))
    import httpx
    from fastapi import FastAPI

    from app.api.billing import router as billing_router
    from app.auth import BillingTransactionWriter
    from app.core.proration import price_usage as BILLING_PRICE
    from app.dependencies import get_reservation_manager
    from app.workers.lifecycle_consumer import LifecycleConsumer

    # Billing's real-infra money harness. Loaded BY PATH, not by package name: both houses
    # have a top-level `tests` package and `import tests.money_harness_...` resolves to this
    # library's instead of billing's, which is how this suite first skipped itself.
    import importlib.util as _ilu

    _spec = _ilu.spec_from_file_location(
        "billing_money_harness_20260712",
        BILLING_ROOT / "tests" / "money_harness_20260712.py",
    )
    _mh = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mh)

    cleanup_redis_org = _mh.cleanup_redis_org
    create_throwaway_table = _mh.create_throwaway_table
    ddb_buckets = _mh.ddb_buckets
    ddb_local_available = _mh.ddb_local_available
    delete_throwaway_table = _mh.delete_throwaway_table
    make_redis = _mh.make_redis
    make_stack = _mh.make_stack
    real_redis_available = _mh.real_redis_available
    seed_account = _mh.seed_account
    transactions_for_org = _mh.transactions_for_org
    txns_of_type = _mh.txns_of_type
    CrossTenantUser = _mh.CrossTenantUser
except Exception as e:  # pragma: no cover - environment guard
    _IMPORT_ERROR = e

from ab0t_quota.billing.clients import BillingServiceClient
from ab0t_quota.billing.lifecycle import LifecycleEmitter
from ab0t_quota.billing.outbox import OutboxRecord, RedisOutboxStore

pytestmark = pytest.mark.asyncio


def _require_cross_house():
    if _IMPORT_ERROR is not None:
        pytest.skip(
            "CANNOT IMPORT BILLING'S REAL SETTLEMENT ENDPOINT "
            f"({type(_IMPORT_ERROR).__name__}: {_IMPORT_ERROR}). "
            "D-12's CALLER LEG IS **NOT VERIFIED** — this suite refuses to certify the "
            "cross-house contract against a double (B-D1)."
        )


# --- Fixtures: real DynamoDB Local, real Redis, billing's real app ------------------------


@pytest_asyncio.fixture
async def table():
    _require_cross_house()
    if not await ddb_local_available():
        pytest.skip(
            "DynamoDB Local (localhost:8000) not reachable — the exactly-once guarantee this "
            "suite rests on is a CONDITIONAL WRITE and CANNOT be verified against a mock. "
            "A skip here means D-12's caller leg is **NOT VERIFIED**."
        )
    if not await real_redis_available():
        pytest.skip(
            "Real Redis (localhost:6382) not reachable — a skip here means D-12's caller leg "
            "is **NOT VERIFIED** on real infrastructure."
        )
    name = await create_throwaway_table()
    try:
        yield name
    finally:
        await delete_throwaway_table(name)


@pytest_asyncio.fixture
async def stack(table):
    """Billing's REAL ReservationManager over real DDB Local + real Redis."""
    redis_client = await make_redis("real")
    st = make_stack(table, redis_client)
    st._orgs = []
    st._reservations = []
    try:
        yield st
    finally:
        for org in st._orgs:
            await cleanup_redis_org(redis_client, org, st._reservations)
        for key in await redis_client.keys("outbox:*"):
            await redis_client.delete(key)
        await redis_client.aclose()


@pytest_asyncio.fixture
async def billing_app(stack):
    """Billing's REAL router, with its REAL reservation manager. Only auth is stood in."""
    app = FastAPI()
    app.include_router(billing_router, prefix="/billing")

    # Billing's OWN auth stand-in (from its money harness) — not one I invented.
    auth_dep = BillingTransactionWriter.__metadata__[0].dependency
    app.dependency_overrides[auth_dep] = lambda: CrossTenantUser()
    app.dependency_overrides[get_reservation_manager] = lambda: stack.reservations
    return app


def _client_for(app, transport_cls=None):
    """The library's REAL BillingServiceClient, speaking real HTTP to billing's real app."""
    c = BillingServiceClient(base_url="http://billing", api_key="test-key")
    transport = (transport_cls or httpx.ASGITransport)(app=app)
    c.client = httpx.AsyncClient(transport=transport, base_url="http://billing")
    return c


def _emitter(redis, settlement_client):
    """The library's REAL emitter, with a REAL Redis-backed durable outbox.

    `outbox_max_retry_horizon_s=0` makes every pending intent past-horizon on the next drain —
    which is the whole scenario: the event outlived its window.
    """
    return LifecycleEmitter(
        sns_topic_arn=None,               # no SNS: the event is undeliverable, as in the defect
        outbox_enabled=True,
        outbox_max_retry_horizon_s=0.0,
        outbox_store=RedisOutboxStore(redis),
        settlement_client=settlement_client,
    )


def _stopped_event(org_id, reservation_id, *, hours=3, rate="1.00", fee="0",
                   event_type="resource.stopped"):
    """A terminal lifecycle event, shaped exactly as `LifecycleEmitter.emit()` builds it."""
    now = time.time()
    from datetime import datetime, timezone
    started = datetime.fromtimestamp(now - hours * 3600, tz=timezone.utc)
    stopped = datetime.fromtimestamp(now, tz=timezone.utc)
    return {
        "event_type": event_type,
        "org_id": org_id,
        "user_id": "u1",
        "resource_id": "sbx-1",
        "resource_type": "sandbox",
        "reservation_id": reservation_id,
        "hourly_rate": rate,
        "allocation_fee": fee,
        "started_at": started.isoformat(),
        "stopped_at": stopped.isoformat(),
        "reason": "user_action",
        "metadata": {},
    }


async def _pending_intent(stack, event, reservation_id, event_type="resource.stopped"):
    """Write the durable intent the way production does, but STALE — the pod wrote it, SNS was
    down, and time passed. This is precisely the state D-12's revenue died in."""
    store = RedisOutboxStore(stack.redis)
    await store.put_intent(OutboxRecord(
        key=f"{reservation_id}:{event_type}",
        event=event,
        event_type=event_type,
        resource_type="sandbox",
        reservation_id=reservation_id,
        first_ts=time.time() - 86_400,   # a day old: far past any horizon
    ))
    return store


async def _new_org(stack, **buckets):
    org = f"org-{uuid.uuid4().hex[:10]}"
    stack._orgs.append(org)
    await seed_account(stack, org, **buckets)
    return org


# =========================================================================================
# 1. THE RED TEST — the money is LOST today
# =========================================================================================


async def test_D12_RED_today_the_past_horizon_event_is_VOIDED_and_the_money_is_LOST(stack):
    """⭐ THE DEFECT, reproduced against REAL billing.

    This constructs the emitter exactly as production does today: a durable outbox and **no
    settlement client**, because nothing in the library ever called billing's settle endpoint.
    The event is past its horizon.

    Result: the intent is VOIDED, a human is alerted... **and the customer is never charged.**
    The alert is an obituary, not a recovery. THIS IS THE REVENUE LOSS.
    """
    org = await _new_org(stack, balance="100")
    rid = f"res-{uuid.uuid4().hex[:8]}"
    stack._reservations.append(rid)
    event = _stopped_event(org, rid, hours=3, rate="1.00")
    store = await _pending_intent(stack, event, rid)

    em = _emitter(stack.redis, settlement_client=None)   # ← production, before this ticket
    await em.drain()

    # The event is gone from the outbox — voided.
    assert await em.pending_count() == 0
    assert len(em.void_ledger) == 1, "the event must have been voided (this IS today's path)"
    assert em.void_ledger[0]["reservation_id"] == rid

    # AND THE MONEY WAS NEVER TAKEN. $3.00 of real usage, billed to nobody.
    buckets = await ddb_buckets(stack, org)
    assert buckets["balance"] == Decimal("100"), (
        "RED: the customer ran a resource for 3h at $1.00/h and was charged NOTHING. "
        "The revenue is lost and no settlement row exists."
    )
    rows = await transactions_for_org(stack, org)
    assert not txns_of_type(rows, "usage"), "no usage row — the settlement is unauditable"


# =========================================================================================
# 2. THE FIX — the same event now settles, through billing's real endpoint
# =========================================================================================


async def test_D12_the_SAME_event_now_SETTLES_against_billings_REAL_endpoint(stack, billing_app):
    """⭐ THE CLOSE. Identical event, identical horizon — but the library now calls
    `POST /billing/{org_id}/settle`. The $3.00 lands, exactly once, with an auditable row."""
    org = await _new_org(stack, balance="100")
    rid = f"res-{uuid.uuid4().hex[:8]}"
    stack._reservations.append(rid)
    event = _stopped_event(org, rid, hours=3, rate="1.00")
    await _pending_intent(stack, event, rid)

    client = _client_for(billing_app)
    em = _emitter(stack.redis, settlement_client=client)
    await em.drain()

    # Not voided — SETTLED.
    assert em.void_ledger == [], "a settleable event must NEVER be voided"
    assert len(em.settled_ledger) == 1
    assert em.settled_ledger[0]["reservation_id"] == rid
    assert await em.pending_count() == 0, "the durable intent is delivered, not left churning"

    # The money moved, and it is exactly what BILLING'S OWN LAW says it should be.
    # NOTE (B-D13): the library no longer computes this — it sends the inputs. The expectation
    # below comes from billing's REAL `price_usage`, executed. We assert against the other house's
    # law, never against a number we derived ourselves.
    from ab0t_quota.billing.observation import parse_dt
    expected = BILLING_PRICE(
        started_at=parse_dt(event["started_at"]),
        stopped_at=parse_dt(event["stopped_at"]),
        hourly_rate=Decimal("1.00"), allocation_fee=Decimal("0"),
    )
    buckets = await ddb_buckets(stack, org)
    assert buckets["balance"] == Decimal("100") - expected, (
        f"the settlement must debit exactly billing's prorated cost ({expected})"
    )

    # QM-02: a settlement is a POSITIVE RECORD, not the absence of a hold.
    usage = txns_of_type(await transactions_for_org(stack, org), "usage")
    assert len(usage) == 1, "exactly one auditable usage row"

    await client.close()


# =========================================================================================
# 3. EXACTLY ONCE — the guarantee is billing's DynamoDB condition, not our code
# =========================================================================================


async def test_D12_a_REDELIVERED_event_settles_EXACTLY_ONCE(stack, billing_app):
    """The same terminal event drained three times (pod restart, SQS redelivery, operator
    replay). Billing's durable marker refuses the repeats. Debited ONCE."""
    org = await _new_org(stack, balance="100")
    rid = f"res-{uuid.uuid4().hex[:8]}"
    stack._reservations.append(rid)
    event = _stopped_event(org, rid, hours=2, rate="1.00")

    client = _client_for(billing_app)
    em = _emitter(stack.redis, settlement_client=client)

    for _ in range(3):
        await _pending_intent(stack, event, rid)   # re-queue the intent, as a redelivery would
        await em.drain()

    buckets = await ddb_buckets(stack, org)
    assert buckets["balance"] == Decimal("98"), (
        "2h x $1.00 = $2.00, charged ONCE across three drains. A second debit here is a "
        "customer double-charge."
    )
    usage = txns_of_type(await transactions_for_org(stack, org), "usage")
    assert len(usage) == 1, "one settlement, one ledger row — never two"
    assert em.void_ledger == []
    await client.close()


async def test_D12_library_and_BILLINGS_OWN_CONSUMER_dedup_against_EACH_OTHER(stack, billing_app):
    """⭐⭐ THE CROSS-PATH PROOF — and the reason `settlement_key` MUST be `reservation_id`.

    Two settlement paths now exist for the same usage:
      (a) this library, calling /settle when the event outlives its horizon;
      (b) billing's own SQS lifecycle consumer, calling settle_activation when a delivered
          event finds its reservation window closed.

    If the SNS copy of an event we already settled is *later* delivered (SNS is at-least-once;
    a DLQ replay is a normal operator action), BOTH paths run on the SAME usage. They dedup
    only because they use the SAME durable key. Key on anything else — `activation_id`, say —
    and you get two keys, one usage, and **two debits**.

    Here the library settles first, then billing's REAL consumer processes the same event.
    """
    org = await _new_org(stack, balance="100")
    rid = f"res-{uuid.uuid4().hex[:8]}"
    stack._reservations.append(rid)
    event = _stopped_event(org, rid, hours=3, rate="1.00")
    await _pending_intent(stack, event, rid)

    client = _client_for(billing_app)
    em = _emitter(stack.redis, settlement_client=client)
    await em.drain()                                    # (a) the library settles $3.00

    after_library = (await ddb_buckets(stack, org))["balance"]
    assert after_library == Decimal("97")

    # (b) billing's REAL consumer now receives the very same event over SNS/SQS.
    consumer = LifecycleConsumer(sqs_client=None, reservation_manager=stack.reservations)
    await consumer._handle_commit(event)

    buckets = await ddb_buckets(stack, org)
    assert buckets["balance"] == Decimal("97"), (
        "DOUBLE CHARGE: billing's consumer settled the same usage the library already "
        "settled. The two paths did not dedup — check that both key on reservation_id."
    )
    usage = txns_of_type(await transactions_for_org(stack, org), "usage")
    assert len(usage) == 1, "one usage, one row — across two independent settlement paths"
    await client.close()


# =========================================================================================
# 4. THE FAIL DIRECTION — billing is unreachable / times out
# =========================================================================================


class _DeadTransport(httpx.AsyncBaseTransport):
    """Billing is down: connection refused."""

    def __init__(self, app=None):
        pass

    async def handle_async_request(self, request):
        raise httpx.ConnectError("billing is down", request=request)


async def test_D12_billing_UNREACHABLE_leaves_the_event_PENDING_never_voided(stack, billing_app):
    """⭐ THE FAIL DIRECTION. Billing is down when the drain runs.

    The event must NOT be voided. Voiding on a transient network failure would let a billing
    pod restart **consume real revenue** — turning an outage into permanent money loss. It
    stays PENDING in the DURABLE store and is retried.

    Then billing comes back, and the SAME retained event settles.
    """
    org = await _new_org(stack, balance="100")
    rid = f"res-{uuid.uuid4().hex[:8]}"
    stack._reservations.append(rid)
    event = _stopped_event(org, rid, hours=3, rate="1.00")
    await _pending_intent(stack, event, rid)

    dead = _client_for(billing_app, transport_cls=_DeadTransport)
    em = _emitter(stack.redis, settlement_client=dead)
    await em.drain()

    assert em.void_ledger == [], (
        "FAIL DIRECTION VIOLATED: a transient outage VOIDED a money event. A 503 must never "
        "consume revenue."
    )
    assert em.settle_failures == 1
    assert await em.pending_count() == 1, "the event is RETAINED for retry, not discarded"
    assert (await ddb_buckets(stack, org))["balance"] == Decimal("100"), "no money moved"
    await dead.close()

    # Billing recovers. The retained event settles on the next pass — nothing was lost.
    good = _client_for(billing_app)
    em2 = _emitter(stack.redis, settlement_client=good)
    await em2.drain()

    assert (await ddb_buckets(stack, org))["balance"] == Decimal("97"), (
        "the event survived the outage and settled when billing came back"
    )
    assert em2.void_ledger == []
    await good.close()


class _TimeoutAfterLandingTransport(httpx.AsyncBaseTransport):
    """The cruellest case: the request REALLY REACHES billing and the settlement REALLY LANDS,
    then the response is lost on the way back. The caller sees a timeout and cannot know."""

    def __init__(self, app):
        self._inner = httpx.ASGITransport(app=app)

    async def handle_async_request(self, request):
        await self._inner.handle_async_request(request)   # billing settles for real
        raise httpx.ReadTimeout("response lost", request=request)


async def test_D12_a_TIMEOUT_whose_settlement_ACTUALLY_LANDED_does_not_double_charge(
    stack, billing_app,
):
    """⭐ A timeout is NOT evidence that the settlement failed.

    The settlement lands; the response is lost. The library sees 504 and retries — which is
    the ONLY safe move, because it cannot distinguish "never landed" from "landed, response
    lost". The retry is safe **because billing's dedup is durable**: it returns the original
    result and moves no money.

    This is why the client must lean on the endpoint's idempotency rather than invent its own.
    """
    org = await _new_org(stack, balance="100")
    rid = f"res-{uuid.uuid4().hex[:8]}"
    stack._reservations.append(rid)
    event = _stopped_event(org, rid, hours=3, rate="1.00")
    await _pending_intent(stack, event, rid)

    lossy = _client_for(billing_app, transport_cls=_TimeoutAfterLandingTransport)
    em = _emitter(stack.redis, settlement_client=lossy)
    await em.drain()

    # The library believes it failed — and correctly retains the event rather than voiding it.
    assert em.settle_failures == 1
    assert em.void_ledger == []
    assert await em.pending_count() == 1
    # But the money DID move, once.
    assert (await ddb_buckets(stack, org))["balance"] == Decimal("97")
    await lossy.close()

    # The retry now runs against a healthy billing. It must NOT charge a second time.
    good = _client_for(billing_app)
    em2 = _emitter(stack.redis, settlement_client=good)
    await em2.drain()

    assert (await ddb_buckets(stack, org))["balance"] == Decimal("97"), (
        "DOUBLE CHARGE on a retry after a lost response. The durable marker must have "
        "recognised the replay."
    )
    assert em2.settled_ledger[0]["replayed"] is True, (
        "billing must report this as a REPLAY, not a fresh settlement"
    )
    usage = txns_of_type(await transactions_for_org(stack, org), "usage")
    assert len(usage) == 1
    assert em2.void_ledger == []
    await good.close()


# =========================================================================================
# 5. THE TERMINAL-EVENT GATE — the sharpest edge in the change
# =========================================================================================


@pytest.mark.parametrize("event_type", ["resource.started", "resource.heartbeat"])
async def test_D12_a_NON_TERMINAL_event_is_VOIDED_and_NEVER_settled(stack, billing_app, event_type):
    """⭐ Settling a non-terminal event would be WORSE than the defect.

    `resource.started` and `resource.heartbeat` ride the SAME outbox and reach the SAME void
    path, and they carry a reservation_id — so they *look* settleable. Settling one would burn
    `settlement_key=reservation_id` on a partial, wrong amount, and the REAL terminal
    settlement would then be refused as a duplicate: the customer charged the wrong amount AND
    the true settlement lost.

    They must be VOIDED, exactly as before — and the key must remain unburned, which the
    second half of this test proves by settling the real terminal event afterwards.
    """
    org = await _new_org(stack, balance="100")
    rid = f"res-{uuid.uuid4().hex[:8]}"
    stack._reservations.append(rid)

    non_terminal = _stopped_event(org, rid, hours=3, rate="1.00", event_type=event_type)
    await _pending_intent(stack, non_terminal, rid, event_type=event_type)

    client = _client_for(billing_app)
    em = _emitter(stack.redis, settlement_client=client)
    await em.drain()

    assert em.settled_ledger == [], f"{event_type} must NEVER settle — it has no final cost"
    assert len(em.void_ledger) == 1, f"{event_type} past its horizon is correctly voided"
    assert (await ddb_buckets(stack, org))["balance"] == Decimal("100"), "no money moved"

    # THE KEY WAS NOT BURNED: the real terminal event still settles for its full amount.
    terminal = _stopped_event(org, rid, hours=3, rate="1.00")
    await _pending_intent(stack, terminal, rid)
    await em.drain()

    assert (await ddb_buckets(stack, org))["balance"] == Decimal("97"), (
        "the terminal settlement was REFUSED as a duplicate — the non-terminal event burned "
        "the settlement key. This is the failure mode the terminal-event gate exists to stop."
    )
    await client.close()


# =========================================================================================
# 6. THE VOID PATH SURVIVES — for events that genuinely cannot settle
# =========================================================================================


async def test_D12_an_event_with_NO_ORG_ID_is_still_VOIDED_and_alerted(stack, billing_app):
    """Unattributable usage cannot be settled at any horizon, on any retry. The void+alert
    path is kept for exactly this — it is now the fallback, not the only outcome."""
    rid = f"res-{uuid.uuid4().hex[:8]}"
    stack._reservations.append(rid)
    event = _stopped_event("", rid, hours=3, rate="1.00")
    event["org_id"] = None
    await _pending_intent(stack, event, rid)

    client = _client_for(billing_app)
    em = _emitter(stack.redis, settlement_client=client)
    await em.drain()

    assert em.settled_ledger == []
    assert len(em.void_ledger) == 1, "genuinely unsettleable → voided AND alerted"
    assert em.void_ledger[0]["alerted"] is True
    assert "unsettleable" in em.void_ledger[0]["reason"]
    assert await em.pending_count() == 0
    await client.close()


async def test_D12_an_UNKNOWN_ORG_is_VOIDED_not_retried_forever(stack, billing_app):
    """Billing answers 404 (no billing account). A retry will never change that — it is a
    PERMANENT failure, so it voids and alerts rather than churning forever."""
    rid = f"res-{uuid.uuid4().hex[:8]}"
    stack._reservations.append(rid)
    ghost = f"org-{uuid.uuid4().hex[:10]}"      # never seeded — billing has no account for it
    event = _stopped_event(ghost, rid, hours=3, rate="1.00")
    await _pending_intent(stack, event, rid)

    client = _client_for(billing_app)
    em = _emitter(stack.redis, settlement_client=client)
    await em.drain()

    assert em.settled_ledger == []
    assert len(em.void_ledger) == 1, "a 404 is permanent: void + alert, do not churn"
    assert "404" in em.void_ledger[0]["reason"]
    assert em.settle_failures == 0, "a 404 must NOT be counted as a transient failure"
    await client.close()


# =========================================================================================
# 7. THE MONEY LAW — the settlement spends in the three-bucket order
# =========================================================================================


async def test_D12_a_SUB_MINUTE_lifetime_settles_at_billings_60_SECOND_FLOOR(stack, billing_app):
    """⭐ ADDED AFTER NEGATIVE CONTROL NC-5 EXPOSED THIS SUITE AS BLIND.

    NC-5 swapped the settlement arithmetic for the library's *quota-cap* float version (no 60s
    floor, no ROUND_UP) — and **all 11 tests stayed GREEN**, because every scenario used whole
    hours, where the two arithmetics agree. The suite was not pinning the money law at all; it
    only looked like it was.

    A 30-second sandbox is the case that separates them: billing bills the **60-second minimum**
    ($0.016667 at $1.00/h), a naive arithmetic bills 30 seconds ($0.008333).

    ⚠️ **B-D13 changed what this test guards, and it is worth being precise about it.** The library
    no longer HAS an arithmetic to get wrong — it sends the inputs and billing prices them. So this
    no longer guards the library's proration (there is none). It now guards the **boundary**: that
    the lifetime we report is the lifetime billing prices, end to end, through the real endpoint.
    If the library ever mangles a timestamp, rounds a duration, or "helpfully" pre-processes a
    lifetime, the charged amount moves — and this is the scenario that sees it.
    """
    org = await _new_org(stack, balance="100")
    rid = f"res-{uuid.uuid4().hex[:8]}"
    stack._reservations.append(rid)

    from datetime import datetime, timezone
    now = time.time()
    event = _stopped_event(org, rid, rate="1.00")
    event["started_at"] = datetime.fromtimestamp(now - 30, tz=timezone.utc).isoformat()
    event["stopped_at"] = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
    await _pending_intent(stack, event, rid)

    client = _client_for(billing_app)
    em = _emitter(stack.redis, settlement_client=client)
    await em.drain()

    from ab0t_quota.billing.observation import parse_dt
    expected = BILLING_PRICE(
        started_at=parse_dt(event["started_at"]),
        stopped_at=parse_dt(event["stopped_at"]),
        hourly_rate=Decimal("1.00"), allocation_fee=Decimal("0"),
    )
    assert expected == Decimal("0.016667"), "60s floor at $1.00/h, ROUND_UP to 1e-6"
    b = await ddb_buckets(stack, org)
    assert b["balance"] == Decimal("100") - expected, (
        f"a 30s lifetime must settle at billing's 60-SECOND FLOOR ({expected}), not at its "
        f"literal 30 seconds. Charging the literal elapsed time UNDERCHARGES the customer and "
        f"disagrees with what /commit would have taken for the identical usage."
    )
    assert em.void_ledger == []
    await client.close()


async def test_D12_the_settlement_spends_SUBSCRIPTION_CREDIT_before_CASH(stack, billing_app):
    """The settlement goes through billing's ONE shared spend-order Lua, so it cannot charge
    cash while the customer's prepaid credit sits unspent (the `usage_service.py:119` trap,
    B-4). Proven here through the real endpoint, not asserted."""
    org = await _new_org(stack, balance="100", credit_balance="1", subscription_credit="2")
    rid = f"res-{uuid.uuid4().hex[:8]}"
    stack._reservations.append(rid)
    event = _stopped_event(org, rid, hours=4, rate="1.00")   # $4.00: 2 sub + 1 credit + 1 cash
    await _pending_intent(stack, event, rid)

    client = _client_for(billing_app)
    em = _emitter(stack.redis, settlement_client=client)
    await em.drain()

    b = await ddb_buckets(stack, org)
    assert b["subscription_credit"] == Decimal("0"), "subscription credit is spent FIRST"
    assert b["credit_balance"] == Decimal("0"), "then promotional credit"
    assert b["balance"] == Decimal("99"), "cash LAST — only the $1.00 remainder"
    await client.close()


# =========================================================================================
# 8. B-D13 — THE LIBRARY SENDS INPUTS AND COMPUTES NO COST (against billing's REAL model)
# =========================================================================================


async def test_BD13_the_library_sends_INPUTS_and_billing_REALLY_prices_them(stack, billing_app):
    """⭐ B-D13, end to end, through the REAL endpoint.

    The library used to send a pre-computed `actual_cost`, which forced it to carry a **port of
    billing's proration** — one of three implementations of one money law. It now sends what it
    OBSERVED, and billing prices it.

    This asserts the whole boundary at once: the library's payload is accepted by billing's REAL
    pydantic model, priced by billing's REAL law, and the money that moves is billing's number —
    not one the library computed.
    """
    org = await _new_org(stack, balance="100")
    rid = f"res-{uuid.uuid4().hex[:8]}"
    stack._reservations.append(rid)
    event = _stopped_event(org, rid, hours=3, rate="1.00", fee="0.25")
    await _pending_intent(stack, event, rid)

    client = _client_for(billing_app)
    em = _emitter(stack.redis, settlement_client=client)
    await em.drain()

    from ab0t_quota.billing.observation import parse_dt
    expected = BILLING_PRICE(
        started_at=parse_dt(event["started_at"]),
        stopped_at=parse_dt(event["stopped_at"]),
        hourly_rate=Decimal("1.00"), allocation_fee=Decimal("0.25"),
    )
    b = await ddb_buckets(stack, org)
    assert b["balance"] == Decimal("100") - expected, (
        f"billing must charge ITS OWN price for the inputs we reported ({expected})"
    )
    # And the cost we recorded is BILLING'S answer, read off the response — not our computation.
    assert em.settled_ledger[0]["actual_cost"] is not None
    assert Decimal(em.settled_ledger[0]["actual_cost"]) == expected
    await client.close()


async def test_BD13_the_library_has_NO_proration_left_to_get_wrong(stack, billing_app):
    """The structural guarantee. `ab0t_quota.proration` is ARCHIVED: there is no second
    implementation of the money law left in this library to drift from billing's.

    If someone reintroduces it, this test says so — and says why it must not come back."""
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("ab0t_quota.proration")

    # And the observation carries the INPUTS, with no cost field to be wrong.
    from ab0t_quota.billing.observation import observe
    obs = observe(_stopped_event("org-1", "res-1", hours=3, rate="1.00"))
    payload = obs.to_settlement_payload()
    # The INPUTS, and only the inputs. `allocation_fee` is present because the event carries a
    # real value ("0" is a value — an explicit zero fee, not an absent one). Absence is tested in
    # test_BD13_a_RATE_LESS_event_..., where the key is genuinely missing.
    assert set(payload) == {"started_at", "stopped_at", "hourly_rate", "allocation_fee"}, payload
    assert "actual_cost" not in payload, (
        "the library sent a COST. B-D13 is regressed — billing prices usage, the caller reports "
        "it. A caller that can compute a cost can compute it WRONG."
    )


async def test_BD13_a_RATE_LESS_event_settles_at_ZERO_runtime_and_the_key_is_OMITTED(
    stack, billing_app,
):
    """⭐ Both traps, closed together.

    1. **We must not INVENT a price.** Billing prices a rate-less runtime at **ZERO** and alerts
       (`settle_missing_hourly_rate`) — a fabricated price is worse than no price (D-36). The
       library must not ship a competing `$0.10/hr` fallback, and must not raise a second alert:
       that policy lives in exactly one place.
    2. **We must not recreate B-D14.** The bug we just killed was an *always-present key whose
       value was sometimes `None`*, against a `.get(k, default)` that only defaults on an ABSENT
       key. So the payload **omits** `hourly_rate` rather than sending `null`.

    The allocation fee is still owed — provisioning really happened — and it still lands.
    """
    org = await _new_org(stack, balance="100")
    rid = f"res-{uuid.uuid4().hex[:8]}"
    stack._reservations.append(rid)
    event = _stopped_event(org, rid, hours=3, rate="1.00", fee="0.25")
    event["hourly_rate"] = None          # the rate-less event: a pricing-config hole
    await _pending_intent(stack, event, rid)

    # The key is OMITTED, never null.
    from ab0t_quota.billing.observation import observe
    payload = observe(event).to_settlement_payload()
    assert "hourly_rate" not in payload, (
        f"a missing rate must be OMITTED, not sent as null — that is the B-D14 landmine, "
        f"against any `.get(k, default)` on the other side. payload={payload}"
    )

    client = _client_for(billing_app)
    em = _emitter(stack.redis, settlement_client=client)
    await em.drain()

    # It SETTLES (a rate-less event is a config gap, not an unsettleable event), and the RUNTIME
    # prices at ZERO — only the $0.25 allocation fee is charged. No invented rate.
    assert em.void_ledger == [], "a rate-less event must still settle, not be voided"
    b = await ddb_buckets(stack, org)
    assert b["balance"] == Decimal("100") - Decimal("0.25"), (
        f"runtime with no rate costs ZERO and the fee is still owed. If this charged ~$3, "
        f"someone invented a rate. balance={b['balance']}"
    )
    await client.close()


async def test_BD13_the_INPUTS_are_PERSISTED_on_the_ledger_row_not_just_consumed(
    stack, billing_app,
):
    """⭐ **A cost you cannot re-derive is a cost you cannot audit.**

    This is the lesson billing paid for twice in this ticket. `/commit`'s usage row records
    `actual_cost` but **not what it was charged FOR** — no duration, no rate. So when a real
    pricing defect was found (B-D21: `elapsed/3600` first, then `ROUND_UP` on the residue its own
    division created — billing `$0.060001` for an exact `$0.06`), **the exposure could not be
    computed from the ledger.** The blocker was not a missing mechanism. It was a **missing
    record**.

    Sending the inputs (B-D13) only fixes that if the inputs are **KEPT**. So: after a settlement
    lands, the ledger row must show billing's working — the lifetime and the rate it priced — and
    the cost must be **re-derivable from the row alone**, with no access to the original event.

    Asserted against the REAL row in REAL DynamoDB.
    """
    org = await _new_org(stack, balance="100")
    rid = f"res-{uuid.uuid4().hex[:8]}"
    stack._reservations.append(rid)
    event = _stopped_event(org, rid, hours=3, rate="1.00", fee="0.25")
    await _pending_intent(stack, event, rid)

    client = _client_for(billing_app)
    em = _emitter(stack.redis, settlement_client=client)
    await em.drain()

    usage = txns_of_type(await transactions_for_org(stack, org), "usage")
    assert len(usage) == 1
    meta = usage[0].get("metadata", {}).get("M", {})

    def _m(k):
        return meta.get(k, {}).get("S")

    for field in ("started_at", "stopped_at", "hourly_rate", "allocation_fee"):
        assert _m(field), (
            f"the ledger row does not record {field!r}. The cost cannot be re-derived from the "
            f"record, which is exactly the hole that made B-D21's exposure incomputable. "
            f"metadata={list(meta)}"
        )

    # THE REAL TEST: re-derive the charge from the ROW ALONE, using billing's law. If this works,
    # an auditor can reprice history without the original event — which is the whole point.
    from ab0t_quota.billing.observation import parse_dt
    rederived = BILLING_PRICE(
        started_at=parse_dt(_m("started_at")),
        stopped_at=parse_dt(_m("stopped_at")),
        hourly_rate=Decimal(_m("hourly_rate")),
        allocation_fee=Decimal(_m("allocation_fee")),
    )
    assert rederived == Decimal(_m("actual_cost")), (
        f"the row's own inputs do not reprice to the amount charged "
        f"({rederived} != {_m('actual_cost')}) — the ledger cannot prove its own arithmetic"
    )
    await client.close()


# =========================================================================================
# 9. ⚠️ A /settle 409 IS NOT A SUCCESS — D-12's loss, re-entering via the ERROR CONTRACT
# =========================================================================================


async def test_D12_RED_a_409_on_a_STILL_LIVE_reservation_must_NOT_be_acked_as_settled(
    stack, billing_app,
):
    """⭐⭐ **THE BUG: a refused settlement was booked as a settled one.**

    The library acked **any** `/settle` 409 as success — it marked the outbox row delivered and
    dropped the event. But billing returns **ONE OPAQUE 409 by design**: distinct codes would
    build a **cross-tenant enumeration oracle**, because its precheck reads Redis *before* it
    checks tenancy. That single code covers three different worlds
    (`app/core/reservation.py::_settle_precheck`):

      * `reservation_still_live:use_commit`   — **THE MONEY IS NOT TAKEN**
      * `org_mismatch`                        — not ours; nothing settled
      * `already_committed:ledger_row_exists`  — the money IS booked

    **Two of the three mean the settlement did not land.** Acking them retired the durable row
    and **discarded the revenue** — through the one surface nobody thought of as a money path.

    This test drives the most dangerous of the three against billing's REAL endpoint: a
    reservation that is **still live**. Settle is refused (correctly — `/commit` should take it),
    and if we ack that refusal, then when the commit never lands (which is **exactly the D-12
    scenario**) the usage is **never billed**.

    **Ambiguity is not success** (D-49: "not obviously a failure" is not "definitely a success").
    The event must stay **PENDING**.
    """
    org = await _new_org(stack, balance="100")
    rid = f"res-{uuid.uuid4().hex[:8]}"
    stack._reservations.append(rid)

    # A LIVE reservation: /settle must refuse it with an opaque 409 (use /commit instead).
    await stack.reservations.create_reservation(
        org_id=org, user_id="u1", tool_id="sandbox",
        estimated_cost=Decimal("10"), session_id="s1",
        reservation_id=rid, actor_user_id="test",
    )

    event = _stopped_event(org, rid, hours=3, rate="1.00")
    await _pending_intent(stack, event, rid)

    client = _client_for(billing_app)
    em = _emitter(stack.redis, settlement_client=client)
    await em.drain()

    # It really was refused — and no money moved (the reservation is live; commit's job).
    assert em.settled_ledger == [], "nothing should have settled — billing refused it"

    # ⚠️ THE ASSERTION THAT MATTERS. The event must NOT have been acked away.
    assert await em.pending_count() == 1, (
        "REVENUE LOST: a 409 was acked as 'settled' and the durable intent was retired. That "
        "409 meant 'the reservation is still live' — the money has NOT been taken. If the "
        "commit never lands (D-12's whole premise), this usage is now billed to NOBODY, and "
        "the outbox row that would have recovered it is gone."
    )
    assert em.void_ledger == [], (
        "a 409 must not be VOIDED either — the money may yet be owed; we simply cannot confirm"
    )
    assert em.settle_failures == 1, "the ambiguous refusal is counted as a failure, not a success"
    await client.close()


async def test_D12_a_COMMITTED_reservations_409_still_retries_and_is_MONEY_SAFE(
    stack, billing_app,
):
    """The other side of the same 409: `/commit` really DID take the money.

    We still cannot tell it apart from "still live" (one opaque code), so we still retry. This
    proves that retrying is **free** — the point that makes "never ack an ambiguous refusal"
    affordable:

      * **no money moves** on the retry (billing refuses it again — commit already booked it);
      * the customer is **not double-charged**;
      * the event stays pending until a human retires it, which is **loud and safe**.

    ⚠️ The cost of correctness here is a row that retries forever. That is the finding framed in
    **B-D24**: billing has no way to say *"this usage IS accounted for"* affirmatively without
    re-opening the enumeration oracle. Until it can, we are **deliberately wrong in the safe
    direction.**
    """
    org = await _new_org(stack, balance="100")
    rid = f"res-{uuid.uuid4().hex[:8]}"
    stack._reservations.append(rid)

    await stack.reservations.create_reservation(
        org_id=org, user_id="u1", tool_id="sandbox",
        estimated_cost=Decimal("10"), session_id="s1",
        reservation_id=rid, actor_user_id="test",
    )
    # /commit takes the money for real.
    await stack.reservations.commit_reservation(
        reservation_id=rid, actual_cost=Decimal("3"),
        usage_record_id="u-1", actor_user_id="test",
    )
    after_commit = (await ddb_buckets(stack, org))["balance"]
    assert after_commit == Decimal("97")

    event = _stopped_event(org, rid, hours=3, rate="1.00")
    await _pending_intent(stack, event, rid)

    client = _client_for(billing_app)
    em = _emitter(stack.redis, settlement_client=client)
    await em.drain()
    await em.drain()   # and again — retrying an already-settled event must stay free

    assert (await ddb_buckets(stack, org))["balance"] == Decimal("97"), (
        "DOUBLE CHARGE: retrying a 409 moved money. Retrying must be free — billing's durable "
        "dedup is what makes 'never ack an ambiguous refusal' affordable."
    )
    usage = txns_of_type(await transactions_for_org(stack, org), "usage")
    assert len(usage) == 1, "one usage row: commit's. The retries fabricated nothing."
    assert em.void_ledger == []
    await client.close()
