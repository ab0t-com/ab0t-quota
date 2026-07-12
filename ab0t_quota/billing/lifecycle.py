"""Resource lifecycle event emitter for billing proration.

Any mesh service that uses ab0t-quota[billing] can emit lifecycle events
so the billing service handles proration automatically.

Usage:
    from ab0t_quota.billing.lifecycle import LifecycleEmitter

    emitter = LifecycleEmitter(
        sns_topic_arn="arn:aws:sns:us-east-1:...:resource-lifecycle",
        aws_endpoint_url="http://localhost:4566",  # LocalStack for dev
    )

    # On resource stop/delete:
    await emitter.resource_stopped(
        org_id="org_123",
        user_id="user_456",
        resource_id="browser_abc",
        resource_type="browser",
        reservation_id="reservation_xyz",
        hourly_rate=Decimal("0.10"),
        allocation_fee=Decimal("0.01"),
        started_at=container.created_at,
        reason="user_stopped",
    )

When constructed with `engine=...`, the emitter ALSO increments the
service's monthly-cost accumulator on resource.stopped/resource.deleted
events. This closes the silent monthly-cost-cap bypass: tier limits like
"$10/month on free" actually enforce, with no extra wiring from the
consumer service. See setup_quota() for the wired-up default.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..engine import QuotaEngine

logger = logging.getLogger("ab0t_quota.billing.lifecycle")


class LifecycleEmitter:
    """Emits resource lifecycle events to SNS for billing proration."""

    # Lifecycle events that should drive cost recording (terminal events
    # carry the final duration; heartbeats are intentionally excluded to
    # avoid double-counting against the stopped event's total cost).
    _COST_RECORDING_EVENTS = frozenset({"resource.stopped", "resource.deleted"})

    def __init__(
        self,
        sns_topic_arn: Optional[str] = None,
        aws_endpoint_url: Optional[str] = None,
        aws_region: Optional[str] = None,
        *,
        engine: Optional["QuotaEngine"] = None,
        cost_resource_key: Optional[str] = None,
        outbox_enabled: bool = True,
        outbox_max_retry_horizon_s: float = 900.0,
        outbox_past_horizon: str = "void_and_alert",
        outbox_store: Any = None,
        settlement_client: Any = None,
    ):
        # Topic ARN: prefer the mesh-namespaced name (consumer-facing
        # convention from setup_quota); fall back to the legacy name for
        # backward compat with services on older configs.
        self._topic_arn = (
            sns_topic_arn
            or os.getenv("AB0T_MESH_SNS_LIFECYCLE_TOPIC_ARN")
            or os.getenv("SNS_LIFECYCLE_TOPIC_ARN")
        )
        self._endpoint = aws_endpoint_url or os.getenv("AWS_ENDPOINT_URL")
        self._client = None

        # Optional quota integration. When set, terminal lifecycle events
        # auto-increment the configured monthly-cost accumulator before
        # publishing SNS. Increment failures are logged + best-effort —
        # billing remains the authoritative source of truth.
        self._engine = engine
        self._cost_resource_key = cost_resource_key

        # --- Durable outbox (QB-01 / D-11 / D-12 / D-29) ------------------
        # A terminal money event's intent is written to a DURABLE store
        # (Redis/DDB — external, survives a process crash) BEFORE the SNS
        # publish, marked delivered on success, and voided+alerted past the
        # retry horizon. drain() reads PENDING intents FROM THE STORE, so a pod
        # restart resumes delivery instead of silently un-billing (D-29). An
        # in-process list would evaporate on the crash it exists to survive.
        self._outbox_enabled = outbox_enabled
        self._outbox_max_retry_horizon_s = outbox_max_retry_horizon_s
        # void_and_alert (default) | drop (never allowed in prod)
        self._outbox_past_horizon = outbox_past_horizon
        # Durable store. If none supplied, degrade to in-memory but flag it LOUD
        # (setup_quota escalates to a startup ERROR — never a silent RAM fallback).
        from .outbox import InMemoryOutboxStore
        self._outbox_degraded = outbox_store is None
        self._outbox_store = outbox_store if outbox_store is not None else InMemoryOutboxStore()
        if self._outbox_degraded and outbox_enabled:
            logger.warning(
                "lifecycle outbox has NO durable store — degrading to in-memory. "
                "Money events will be LOST on restart. Wire a Redis/DDB outbox store."
            )
        self.void_ledger: list = []             # in-RAM alert mirror: [{reservation_id, event_type, alerted, reason}]

        # --- The settlement fallback (D-12, the CALLER leg) ------------------
        # Billing shipped a durable, activation-scoped settlement path
        # (POST /billing/{org_id}/settle) precisely so that a money event which
        # can no longer be COMMITTED can still be SETTLED. Until this client was
        # wired, NOTHING CALLED IT: the library kept voiding-and-alerting past
        # the horizon, and the revenue was still lost — a mechanism without a
        # guarantee (the parent's D-64 class).
        #
        # An undeliverable money event is now SETTLED, and only voided if it
        # genuinely CANNOT be settled. `None` (the default) preserves the old
        # void-and-alert behaviour exactly, so a consumer that has not wired
        # billing is unaffected.
        # Ticket: billing/output/tickets/20260712_revenue_chain_integrity (D-12).
        self._settlement_client = settlement_client
        # Observability mirrors. `settled_ledger` is the money this used to lose.
        self.settled_ledger: list = []          # [{reservation_id, org_id, actual_cost, replayed}]
        self.settle_failures: int = 0           # transient failures; the event stays PENDING

        self._drain_task = None                 # background drain worker (started by setup_quota)
        # D-50: loop liveness. A drain loop that is permanently backing off is a
        # dead worker inside a healthy process — money events stop draining (QB-01)
        # while /health stays green. Track the fail-streak so drain_worker_liveness()
        # can surface it and /quota/health can fail on it.
        self._drain_fail_streak = 0
        self._drain_worker_ever_started = False
        # Billing kill (D-32 Claim 3): refuse to emit money events onto an
        # ephemeral store rather than silently lose them (a loud OFF beats QB-01).
        self._billing_disabled = False
        self._billing_off_reason: Optional[str] = None

        # Extract region from ARN or env
        if self._topic_arn and len(self._topic_arn.split(":")) > 3:
            self._region = self._topic_arn.split(":")[3]
        else:
            self._region = aws_region or os.getenv("AWS_REGION", "us-east-1")

    def _get_client(self):
        if self._client is None:
            if not self._topic_arn:
                return None
            import boto3
            kwargs = {"region_name": self._region}
            if self._endpoint:
                kwargs["endpoint_url"] = self._endpoint
            self._client = boto3.client("sns", **kwargs)
        return self._client

    async def emit(
        self,
        event_type: str,
        org_id: str,
        user_id: str,
        resource_id: str,
        resource_type: str,
        reservation_id: Optional[str] = None,
        instance_type: Optional[str] = None,
        hourly_rate: Optional[Decimal] = None,
        allocation_fee: Optional[Decimal] = None,
        started_at: Optional[datetime] = None,
        stopped_at: Optional[datetime] = None,
        reason: str = "user_action",
        metadata: Optional[Dict[str, Any]] = None,
        activation_id: Optional[str] = None,
    ) -> bool:
        """Emit a lifecycle event. Returns True if published, False if not configured.

        When the emitter was constructed with `engine=...`, terminal events
        (resource.stopped / resource.deleted) ALSO increment the configured
        monthly-cost accumulator before publishing SNS. Increment failure
        is logged but never blocks the SNS publish — billing is authoritative.
        """
        # Auto-record cost for terminal events when wired to a quota engine.
        # Runs first; failures are best-effort (billing service is the
        # source of truth for charges, this just keeps the quota cap honest).
        if (
            self._engine is not None
            and self._cost_resource_key
            and event_type in self._COST_RECORDING_EVENTS
        ):
            await self._record_cost(
                org_id=org_id,
                resource_id=resource_id,
                hourly_rate=hourly_rate,
                allocation_fee=allocation_fee,
                started_at=started_at,
                stopped_at=stopped_at,
                activation_id=activation_id,
            )

        event = {
            "event_type": event_type,
            "org_id": org_id,
            "user_id": user_id,
            "resource_id": resource_id,
            "resource_type": resource_type,
            "reservation_id": reservation_id,
            "instance_type": instance_type,
            "hourly_rate": str(hourly_rate) if hourly_rate else None,
            "allocation_fee": str(allocation_fee) if allocation_fee else "0",
            "started_at": started_at.isoformat() if started_at else None,
            "stopped_at": (stopped_at or datetime.now(timezone.utc)).isoformat(),
            "reason": reason,
            "metadata": metadata or {},
            "emitted_at": datetime.now(timezone.utc).isoformat(),
        }

        # Billing kill (D-32 Claim 3): with no durable store, a money event must
        # be REFUSED loudly, never emitted onto an ephemeral store where it can be
        # silently lost. Quota-without-billing is a known, visible state;
        # quota-with-silently-lost-billing IS QB-01.
        if reservation_id and self._billing_disabled:
            logger.error(
                "lifecycle money event REFUSED (billing OFF: %s) reservation_id=%s event_type=%s "
                "— refusing to emit onto a non-durable outbox.",
                self._billing_off_reason, reservation_id, event_type,
            )
            return False

        # Money-bearing events (those with a reservation_id settlement key) go
        # through the durable outbox: write intent FIRST, then publish, then mark
        # delivered (D-29 / DISCUSSION §2 Option E). A crash at any point leaves a
        # PENDING intent in the external store that drain() resumes.
        if reservation_id and self._outbox_enabled:
            return await self._emit_via_outbox(event, event_type, resource_type, reservation_id)

        # Non-money events (no reservation_id): best-effort, no durable intent.
        client = self._get_client()
        if client is None:
            return False
        return await self._publish(client, event, event_type, resource_type)

    async def _emit_via_outbox(self, event: dict, event_type: str,
                               resource_type: str, reservation_id: str) -> bool:
        import time
        from .outbox import OutboxRecord
        key = self._outbox_key(reservation_id, event_type)
        # 1. Durable intent BEFORE any publish attempt.
        await self._outbox_store.put_intent(OutboxRecord(
            key=key, event=event, event_type=event_type, resource_type=resource_type,
            reservation_id=reservation_id, first_ts=time.time(),
        ))
        client = self._get_client()
        if client is None:
            # Undeliverable over SNS. That is NOT the same as unsettleable: the
            # settlement path is a direct HTTP call to billing and needs no SNS
            # at all. SETTLE it; void only if it genuinely cannot settle (D-12).
            await self._settle_or_void(event, reservation_id, event_type,
                                       key=key, reason="no_sns_configured")
            return False
        # 2. Publish. 3. Mark delivered on success; else leave PENDING for drain.
        if await self._publish(client, event, event_type, resource_type):
            await self._outbox_store.mark_delivered(key)
            return True
        await self._outbox_store.bump_attempt(key)
        logger.warning("lifecycle_outbox_pending reservation_id=%s event_type=%s "
                       "(publish failed; durable intent retained for drain)",
                       reservation_id, event_type)
        return False

    async def _publish(self, client, event: dict, event_type: str, resource_type: str) -> bool:
        """Publish one event to SNS. Returns True on success, False on exception."""
        try:
            import asyncio
            await asyncio.to_thread(
                client.publish,
                TopicArn=self._topic_arn,
                Message=json.dumps(event, default=str),
                MessageAttributes={
                    "event_type": {"DataType": "String", "StringValue": event_type},
                    "resource_type": {"DataType": "String", "StringValue": resource_type},
                },
            )
            logger.debug("lifecycle_event_emitted: %s %s", event_type, event.get("resource_id"))
            return True
        except Exception as e:
            logger.warning("lifecycle_event_failed: %s %s %s", event_type, event.get("resource_id"), e)
            return False

    def _outbox_key(self, reservation_id: str, event_type: str) -> str:
        return f"{reservation_id}:{event_type}"

    def set_outbox_store(self, store, *, degraded: bool = False) -> None:
        """Late-bind the durable outbox store (D-32 Claim 1). The store is
        self-provisioned asynchronously in setup_quota's lifespan, after the
        emitter is constructed in the sync phase."""
        self._outbox_store = store
        self._outbox_degraded = degraded

    def disable_billing(self, reason: str) -> None:
        """Refuse to emit money lifecycle events (D-32 Claim 3). Called when no
        durable outbox is obtainable and allow_ephemeral is false."""
        self._billing_disabled = True
        self._billing_off_reason = reason
        logger.error(
            "BILLING DISABLED: %s — money lifecycle events will be REFUSED (not emitted onto an "
            "ephemeral store). A service that loudly won't bill beats one that quietly loses money.",
            reason,
        )

    def billing_status(self) -> str:
        return f"OFF — {self._billing_off_reason}" if self._billing_disabled else "ON"

    async def _settle_or_void(self, event: dict, reservation_id: str, event_type: str,
                              *, key: str, reason: str) -> None:
        """An undeliverable money event: SETTLE it against billing; void only if it
        genuinely cannot be settled. **This is D-12's caller leg — the close of the
        revenue-loss path.**

        THE FAIL DIRECTION (D-31), stated plainly:

        > **It fails toward RETRYING, never toward DISCARDING; and toward NOT DEBITING,
        > never toward DEBITING TWICE.**

        Three outcomes, and the reasoning for each:

        * **Settled / already-accounted (HTTP 200 / 409)** → mark the intent DELIVERED.
          A 409 means billing's own `/commit` already took this money (or the reservation
          is still live and commit will). The books are correct; this is success, not a
          loss. Voiding here would raise a false money incident.

        * **Transient failure (5xx, timeout, billing unreachable)** → **leave the intent
          PENDING** and bump its attempt count. **Do NOT void.** A network blip must never
          be allowed to consume a settlement: voiding on a 503 would lose real revenue to a
          restart of the billing pod. The next drain pass retries — forever, if need be.
          The retry cannot double-charge, because billing's dedup is a **durable DynamoDB
          conditional write** on `settlement_key` with **no TTL**; and it is precisely the
          right move on a TIMEOUT, where we genuinely do not know whether the settlement
          landed: if it did, the retry returns the original result (`replayed=true`) and
          moves no money; if it did not, the retry lands it. Both readings are safe, which
          is why we lean on billing's idempotency rather than inventing client-side dedup.

        * **Permanently unsettleable (no org_id, no start time, a 4xx that is not 409)** →
          **void + alert**, exactly as before. The pre-existing loud path is kept; it is
          now the fallback it was always meant to be, not the only outcome.
        """
        outcome, detail = await self._try_settle(event, reservation_id, event_type)

        if outcome == "settled":
            # ⚠️ THE ONLY THING THAT MAY RETIRE A MONEY EVENT IS AN AFFIRMATIVE ANSWER.
            #
            # `settled` means billing returned 200 — it either took the money now, or told us
            # (`replayed=true`) that it had already taken it. Both POSITIVELY say the usage is
            # accounted for.
            #
            # `already_accounted` used to be a second way in here, inferred from a 409. It is
            # GONE: billing's 409 is deliberately opaque and also covers "the reservation is
            # still live" and "org mismatch" — neither of which means the money was taken.
            # Acking those DISCARDED the settlement. Ambiguity is not success (D-49).
            await self._outbox_store.mark_delivered(key)
            return

        if outcome == "not_applicable":
            # No settlement was ATTEMPTED (no settlement client wired, or a non-terminal
            # event). Nothing about this event's situation has changed, so it voids with its
            # ORIGINAL reason, byte-for-byte as it did before this ticket. Enriching the reason
            # here would break peer tests for no behavioural gain.
            await self._void(reservation_id, event_type, key=key, reason=reason)
            return

        if outcome == "retry":
            # Transient. The event stays PENDING in the DURABLE store, so it survives a
            # restart and the next drain pass tries again. Never voided on a blip.
            self.settle_failures += 1
            await self._outbox_store.bump_attempt(key)
            logger.error(
                "lifecycle_settle_RETRY reservation_id=%s event_type=%s detail=%s — "
                "settlement did not land (transient). The event is RETAINED as PENDING and "
                "will be retried; it is NOT voided and NOT lost. Revenue is at risk only if "
                "this persists — a permanently rising pending_count IS a money incident.",
                reservation_id, event_type, detail,
            )
            return

        # A settlement WAS attempted and PERMANENTLY refused. Void + alert — the original
        # loud path, now carrying the refusal detail (information it never had before).
        await self._void(reservation_id, event_type, key=key,
                         reason=f"{reason}:unsettleable:{detail}")

    async def _try_settle(self, event: dict, reservation_id: str, event_type: str):
        """Attempt the durable settlement. Returns `(outcome, detail)` where outcome is
        one of `settled` | `already_accounted` | `retry` | `unsettleable`.

        ⚠️ THE MOST DANGEROUS THING IN THIS FILE IS THE TERMINAL-EVENT GATE BELOW. ⚠️
        """
        if self._settlement_client is None:
            # No billing wired: nothing has changed for this consumer. Void exactly as before.
            return "not_applicable", "no_settlement_client"

        # ------------------------------------------------------------------
        # ⚠️ ONLY TERMINAL EVENTS MAY SETTLE. This gate is LOAD-BEARING.
        #
        # `resource.started` and `resource.heartbeat` travel through this SAME
        # outbox and reach this SAME void path. They carry a reservation_id, so
        # they LOOK settleable. They are not:
        #
        #   * a settlement is keyed on `reservation_id`, and billing's dedup is
        #     DURABLE AND ETERNAL. Settling a `resource.started` would BURN the
        #     key on a partial, wrong (start→now) amount — and the REAL
        #     `resource.stopped` settlement that follows would then be REFUSED
        #     as a duplicate.
        #   * net effect: the customer is charged the WRONG amount, AND the true
        #     settlement is lost. That is worse than the defect being fixed.
        #
        # Only `resource.stopped` / `resource.deleted` carry a final lifetime,
        # and they are the only events billing's own consumer commits on
        # (`_COST_RECORDING_EVENTS`). A lost heartbeat is not lost revenue — it
        # is a missed *extension*; a lost `started` is not lost revenue either.
        # Both are correctly VOIDED, exactly as before.
        # ------------------------------------------------------------------
        if event_type not in self._COST_RECORDING_EVENTS:
            return "not_applicable", "not_a_terminal_cost_event"

        from .clients import BillingServiceError
        from .observation import UnsettleableEvent, observe

        # B-D13: we report what we OBSERVED. We do not price it.
        #
        # This block used to compute `actual_cost` with a PORT OF BILLING'S PRORATION that lived
        # in this library — one of three implementations of one money law. `/settle` now takes
        # the INPUTS and billing prices them with the single law it owns. The library's
        # proration is ARCHIVED, not synchronised: a copy kept in sync is still a copy.
        #
        # ⚠️ DO NOT REINTRODUCE A COST HERE — not a rate default, not a minimum, not a
        # "fallback" price. A fabricated price is worse than no price (D-36). A missing rate is
        # reported as MISSING; billing prices its runtime at ZERO and ALERTS
        # (`settle_missing_hourly_rate`), in exactly one place, and we do not compete with it.
        try:
            obs = observe(event)
        except UnsettleableEvent as e:
            # No org_id / no start time / a lifetime that runs backwards. It can never settle,
            # at any horizon, on any retry. Void + alert is the honest outcome.
            return "unsettleable", str(e)
        except Exception as e:  # an event shape we did not anticipate — never guess at money
            return "unsettleable", f"observation_failed:{type(e).__name__}"

        org_id = obs.org_id  # observe() has already proven this is present
        try:
            result = await self._settlement_client.settle_activation(
                org_id=org_id,
                # THE KEY. `reservation_id` — the same key billing's own SQS lifecycle
                # consumer settles under (lifecycle_consumer.py:425). That is what makes
                # THIS path and THAT path dedup against EACH OTHER at billing's DynamoDB
                # condition: if the SNS copy of this event is ever delivered, billing
                # refuses the second settlement. A different key here (e.g. activation_id)
                # would be TWO keys for ONE usage — a double charge.
                settlement_key=reservation_id,
                # The INPUTS, not a cost (B-D13). Billing prices them.
                observation=obs,
                usage_record_id=f"lifecycle:{obs.resource_id}",
                reservation_id=reservation_id,
            )
        except BillingServiceError as e:
            # Dispatch on STATUS CODE, never on the error's text. Billing's own consumer
            # matched on `str(e)` — which is the EMPTY STRING for an HTTPException on the
            # pinned starlette — and its entire revenue-loss alarm was therefore dead code
            # for months (B-D11). Never branch money-path control flow on a message.
            if e.status_code == 409:
                # ==========================================================
                # ⚠️ A 409 IS **NOT** A SUCCESS. THIS ARM USED TO ACK IT.
                #
                # It used to `mark_delivered` the outbox row on any 409, on the
                # reasoning that "the books must already be correct". THEY MIGHT
                # NOT BE. Billing returns ONE OPAQUE 409 (deliberately — distinct
                # codes would build a cross-tenant enumeration oracle, because the
                # precheck reads Redis BEFORE it checks tenancy). That single code
                # covers, from `_settle_precheck`:
                #
                #   * `reservation_still_live:use_commit` — THE MONEY IS NOT TAKEN.
                #     /commit is expected to take it. If that commit never lands —
                #     which is EXACTLY the D-12 scenario — the usage is unbilled.
                #   * `org_mismatch`                      — not ours; nothing settled.
                #   * `already_committed:ledger_row_exists` — the money IS booked.
                #
                # So a 409 is AMBIGUOUS, and acking it retired the outbox row and
                # DISCARDED the settlement — D-12's revenue loss, re-entering
                # through the error contract, on the one surface nobody thought of
                # as a money path.
                #
                # **Ambiguity is not success.** (D-49: we treated "not obviously a
                # failure" as "definitely a success" — a fail-open, one more time.)
                #
                # So it RETRIES, exactly like a 5xx. Retrying a genuinely-settled
                # event is FREE: billing's dedup is a DynamoDB conditional write, so
                # the money moves once and the retry returns 200/replayed — an
                # AFFIRMATIVE answer, which is the only thing allowed to retire the
                # row. Only a response that positively says the money is accounted
                # for may ack.
                #
                # ⚠️ OWED (framed in DECISIONS B-D24): billing cannot currently give
                # that affirmative signal for an already-committed reservation
                # without re-opening the enumeration oracle. Until it can, a
                # commit-settled event retries until a human retires it. That is
                # LOUD and SAFE, and it is the correct direction to be wrong in.
                # ==========================================================
                logger.error(
                    "lifecycle_settle_REFUSED_409 reservation_id=%s org_id=%s — billing "
                    "refused the settlement with an OPAQUE 409. This does NOT mean the money "
                    "was taken: it also covers 'reservation still live' and 'org mismatch'. "
                    "The event is RETAINED as PENDING and will be retried; it is NOT acked "
                    "and NOT lost. Retrying is safe (billing's dedup is durable).",
                    reservation_id, org_id,
                )
                return "retry", "http_409_ambiguous_refusal"
            if e.status_code >= 500 or e.status_code in (408, 429):
                # 503 unreachable / 504 timeout / 5xx / rate-limited → TRANSIENT.
                return "retry", f"http_{e.status_code}"
            # 400 (negative cost), 403 (authz), 404 (no billing account for this org):
            # a retry will not change any of these. Permanent → void + alert.
            return "unsettleable", f"http_{e.status_code}"
        except Exception as e:
            # An unexpected client/transport error is NOT evidence that the settlement
            # failed to land — it may have landed and the response lost. Retry (safe: the
            # durable key dedups) rather than voiding money we may already have charged.
            return "retry", f"{type(e).__name__}"

        result = result if isinstance(result, dict) else {}
        replayed = bool(result.get("replayed", False))
        # The cost is BILLING'S ANSWER, read back off the response — not something we computed.
        # We no longer know what the usage costs, and that is the point (B-D13). Recording our
        # own guess here would quietly reintroduce the second implementation of the money law.
        actual_cost = result.get("actual_cost")
        self.settled_ledger.append({
            "reservation_id": reservation_id,
            "org_id": org_id,
            "actual_cost": actual_cost,      # what BILLING charged
            "replayed": replayed,
        })
        logger.info(
            "lifecycle_SETTLED_past_window reservation_id=%s org_id=%s actual_cost=%s "
            "replayed=%s — revenue that would previously have been VOIDED AND LOST has "
            "been settled durably against billing.",
            reservation_id, org_id, actual_cost, replayed,
        )
        return "settled", "ok"

    async def _void(self, reservation_id: str, event_type: str, *, key: str, reason: str) -> None:
        """Record an explicit, ALERTED void (D-12). Never silent, never dropped:
        settlement will not occur for this activation, so the books must show a
        void — not a phantom $0 release. Marks the durable intent voided AND
        appends to the in-RAM alert mirror (`void_ledger`)."""
        if self._outbox_past_horizon == "drop":  # never allowed in prod
            logger.error("lifecycle_event_DROPPED reservation_id=%s event_type=%s reason=%s "
                         "(outbox.past_horizon=drop — money event lost)", reservation_id, event_type, reason)
            await self._outbox_store.mark_delivered(key)  # remove; the drop is logged above
            return
        await self._outbox_store.mark_voided(key, reason)
        self.void_ledger.append({
            "reservation_id": reservation_id,
            "event_type": event_type,
            "alerted": True,
            "reason": reason,
        })
        logger.error("lifecycle_event_VOIDED reservation_id=%s event_type=%s reason=%s — "
                     "ALERT: activation will not settle; usage may be unbilled. Manual "
                     "reconciliation required.", reservation_id, event_type, reason)

    async def drain(self, max_per_pass: int = 100) -> int:
        """Drain PENDING intents FROM THE DURABLE STORE (D-29). Returns the number
        delivered this pass. Events past the retry horizon are voided + alerted
        (D-12). Bounded per pass (`max_per_pass`): a delivery storm after an SNS
        outage must not become a self-inflicted throughput incident (FUTURE §3).

        Reads from the store, not from RAM — so a fresh process (post-restart)
        with an empty in-memory state still resumes every un-delivered event."""
        import time
        delivered = 0
        client = self._get_client()
        pending = await self._outbox_store.list_pending(limit=max_per_pass)
        if len(pending) >= max_per_pass:
            logger.warning("outbox_drain_budget_reached max_per_pass=%d "
                           "(more may remain; deferred to next pass)", max_per_pass)
        for rec in pending:
            if (time.time() - rec.first_ts) > self._outbox_max_retry_horizon_s:
                # ============================================================
                # D-12 — THIS IS WHERE THE REVENUE USED TO DIE.
                #
                # This branch used to read:
                #
                #     # Past the horizon: a late commit would 404 at billing
                #     # anyway (D-12). Void + alert; do not keep churning.
                #     await self._void(..., reason="past_retry_horizon")
                #
                # The premise was true and is now FALSE. A late COMMIT does
                # still 404 — but billing now has a durable, activation-scoped
                # SETTLEMENT path that needs no live reservation hash
                # (POST /billing/{org_id}/settle). "Commit can't take it" no
                # longer implies "nothing can take it".
                #
                # So the money is SETTLED. The void/alert survives untouched as
                # the FALLBACK for events that genuinely cannot settle.
                # Ticket: billing/output/tickets/20260712_revenue_chain_integrity
                # ============================================================
                await self._settle_or_void(rec.event, rec.reservation_id, rec.event_type,
                                           key=rec.key, reason="past_retry_horizon")
                continue
            if client is None:
                continue  # still nowhere to deliver; try again next pass
            if await self._publish(client, rec.event, rec.event_type, rec.resource_type):
                await self._outbox_store.mark_delivered(rec.key)
                delivered += 1
            else:
                await self._outbox_store.bump_attempt(rec.key)
        return delivered

    async def pending_count(self) -> int:
        """Number of undelivered intents currently in the durable store."""
        return len(await self._outbox_store.list_pending(limit=10_000))

    # --- Background drain worker (started by setup_quota's lifespan) --------

    def start_drain_worker(self, interval_seconds: float = 30.0, max_per_pass: int = 100):
        """Start a library-owned background task that drains the outbox every
        `interval_seconds`. Mirrors QuotaStore.start_sync_worker. Returns the
        asyncio.Task (or the existing one if already running)."""
        import asyncio
        if self._drain_task is not None and not self._drain_task.done():
            return self._drain_task
        self._drain_worker_ever_started = True
        self._drain_task = asyncio.create_task(
            self._drain_loop(interval_seconds, max_per_pass),
            name="ab0t_quota_outbox_drain",
        )
        logger.info("outbox_drain_worker_started interval=%ss max_per_pass=%d",
                    interval_seconds, max_per_pass)
        return self._drain_task

    async def stop_drain_worker(self):
        """Cancel the background drain task. Safe to call if not running."""
        import asyncio
        task = self._drain_task
        self._drain_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        logger.info("outbox_drain_worker_stopped")

    @staticmethod
    def _drain_kill_switched() -> bool:
        """Runtime kill-switch, mirroring the consumer's proven
        POOL_QUOTA_RECONCILE_ENABLED pattern. Flip without a redeploy."""
        return os.getenv("AB0T_QUOTA_OUTBOX_DRAIN_ENABLED", "true").strip().lower() in (
            "false", "0", "no", "off",
        )

    #: consecutive-failure count at which the drain worker is considered a DEAD
    #: worker (permanently backing off) — surfaced by drain_worker_liveness().
    _DRAIN_UNHEALTHY_STREAK = 3

    async def _drain_loop(self, interval_seconds: float, max_per_pass: int):
        """Run drain passes forever with backoff on repeated failure."""
        import asyncio
        self._drain_fail_streak = 0
        while True:
            try:
                await asyncio.sleep(interval_seconds)
                if not self._outbox_enabled or self._drain_kill_switched():
                    continue  # disabled — do nothing this pass (runtime-flippable)
                delivered = await self.drain(max_per_pass=max_per_pass)
                self._drain_fail_streak = 0   # healthy again
                # NOTE (W-T2, ticket 20260709): this used to read `self.outbox`,
                # an in-process dict that D-29 REMOVED when the outbox became
                # durable — so every pass raised AttributeError, was caught below,
                # and the loop lived permanently in error-backoff. The scheduler
                # boundary (D-40 row #1) the direct-drain tests never crossed.
                # Report `remaining` from the DURABLE store, and only when a pass
                # did work (idle passes stay silent + cheap).
                if delivered:
                    logger.info("outbox_drain_pass delivered=%d remaining=%d voided=%d",
                                delivered, await self.pending_count(), len(self.void_ledger))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._drain_fail_streak = min(self._drain_fail_streak + 1, 5)
                # D-50 rule 2: a loop backing off FOREVER must be LOUD, not a
                # repeating warning nobody reads. Escalate to ERROR at the
                # unhealthy threshold so a dead drain (QB-01 re-opened) is visible
                # in the logs AND via drain_worker_liveness()/quota_health.
                if self._drain_fail_streak >= self._DRAIN_UNHEALTHY_STREAK:
                    logger.error("outbox_drain_worker_UNHEALTHY: %s (fail_streak=%d) — "
                                 "money events are NOT draining (QB-01 risk); the loop is "
                                 "backing off. This must reach a human.", e, self._drain_fail_streak)
                else:
                    logger.warning("outbox_drain_pass_error: %s (backoff x%d)", e, self._drain_fail_streak)
                # Back off so a persistent failure doesn't tight-loop.
                await asyncio.sleep(min(interval_seconds * self._drain_fail_streak, 120))

    def drain_worker_liveness(self) -> tuple[bool, str]:
        """D-50: is the background drain loop LIVE? Money-critical — a dead drain
        silently re-opens QB-01 behind a healthy /health. Returns (healthy, detail).

        Healthy when: billing is disabled or the outbox is off (no drain required —
        a known, visible state reflected in the billing capability); or the worker
        was never started here (manual-drain mode); or it is running with a
        fail-streak below the unhealthy threshold. Unhealthy when a STARTED worker
        is no longer running (died/stopped) or is permanently backing off."""
        if self._billing_disabled or not self._outbox_enabled:
            return True, "drain not required (billing off / outbox disabled)"
        if not self._drain_worker_ever_started:
            return True, "drain worker not managed here (manual drain)"
        task = self._drain_task
        if task is None or task.done():
            return False, "drain worker was started but is no longer running"
        if self._drain_fail_streak >= self._DRAIN_UNHEALTHY_STREAK:
            return False, f"drain worker backing off (fail_streak={self._drain_fail_streak}) — money events not draining"
        return True, "on"

    async def _record_cost(
        self,
        *,
        org_id: str,
        resource_id: str,
        hourly_rate: Optional[Decimal],
        allocation_fee: Optional[Decimal],
        started_at: Optional[datetime],
        stopped_at: Optional[datetime],
        activation_id: Optional[str] = None,
    ) -> None:
        """Increment the monthly-cost accumulator for this resource's lifetime.

        Idempotent on ACTIVATION IDENTITY, not on the resource_id (QB-02, P3.2).
        A single resource_id can live multiple lifetimes in one month
        (stop->resume->stop, pool re-claim); each lifetime is a DISTINCT
        activation and its cost must be recorded once. The old key
        ``cost:lifecycle:{resource_id}`` collided across lifetimes and silently
        dropped every activation after the first — under-counting the money cap.

        The activation identity is, in order of preference:
          1. an explicit ``activation_id`` (from engine.acquire()), else
          2. the natural key ``(resource_id, started_at)`` — the lifetime's start
             uniquely identifies it, so two lifetimes get two keys while a genuine
             replay of the SAME terminal event (same start) stays deduped.
        Heartbeat events are intentionally NOT recorded here.
        """
        if hourly_rate is None and allocation_fee is None:
            return  # nothing to charge; pricing not configured for this resource
        if started_at is None:
            logger.debug("cost_record_skipped: no started_at for %s", resource_id)
            return

        end = stopped_at or datetime.now(timezone.utc)
        # Defend against tz-naive datetimes from older callers
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)

        seconds = max(0.0, (end - started_at).total_seconds())
        hours = Decimal(str(seconds / 3600.0))
        rate = hourly_rate or Decimal("0")
        alloc = allocation_fee or Decimal("0")
        cost = float(hours * rate + alloc)
        if cost <= 0:
            return  # zero-cost resources don't move the accumulator

        # Lazy imports to avoid circular dep with engine module
        from ..models.requests import QuotaIncrementRequest

        # Activation-scoped settlement key (QB-02). Explicit activation_id wins;
        # otherwise the lifetime's start disambiguates distinct lifetimes of a
        # reused resource_id while still deduping a genuine replay.
        if activation_id:
            settle_key = f"cost:activation:{activation_id}"
        else:
            settle_key = f"cost:lifecycle:{resource_id}:{started_at.isoformat()}"

        try:
            await self._engine.increment(QuotaIncrementRequest(
                org_id=org_id,
                resource_key=self._cost_resource_key,
                delta=cost,
                # Idempotency on the ACTIVATION, not the resource_id: replay-safe
                # (same activation) AND reuse-safe (distinct lifetimes record once
                # each) — QB-02.
                idempotency_key=settle_key,
            ))
            logger.debug(
                "cost_recorded org=%s resource=%s delta=%.4f key=%s",
                org_id, resource_id, cost, self._cost_resource_key,
            )
        except Exception as e:
            # Quota-side failure must never block the SNS publish that
            # reaches the authoritative billing pipeline.
            logger.warning(
                "cost_record_failed org=%s resource=%s error=%s",
                org_id, resource_id, str(e),
            )

    # Convenience methods
    async def resource_started(self, **kwargs) -> bool:
        return await self.emit(event_type="resource.started", **kwargs)

    async def resource_stopped(self, **kwargs) -> bool:
        return await self.emit(event_type="resource.stopped", **kwargs)

    async def resource_deleted(self, **kwargs) -> bool:
        return await self.emit(event_type="resource.deleted", **kwargs)

    async def resource_heartbeat(self, **kwargs) -> bool:
        return await self.emit(event_type="resource.heartbeat", **kwargs)
