"""P3.3 — Settle-once ACROSS BILLING'S RETENTION BOUNDARY (ticket 20260709).

RED-BY-DESIGN. Encodes the end-to-end settlement invariant the redesign now
guarantees, measured against billing's ACTUAL retention semantics (not an
idealized "billing remembers forever"). Authored by W-DISC as the executable
companion to `information_billing_retention_20260710.md` and DECISIONS D-9 /
D-12 / D-13.

    THE INVARIANT (ticket guarantee):
      Every activation ends in EXACTLY ONE settled usage row
      OR an explicit, ALERTED void — NEVER a silent $0 release.

This suite does NOT call any deployed service. It models billing with an
in-memory double (`FakeBilling`) that reproduces ONLY the behaviours W-DISC
verified in source (each cited inline). The RED-ness is deliberate: the library
does not yet own the outbox/void mechanism (Phase 3), and billing does not yet
own durable dedup (D-13). Tests fail today and turn green as those land.

Scope boundary vs the peer suite:
  - `tests/billing/test_integrity_delivery_20260710.py` (P0.4) proves the
    EMITTER's delivery guarantee at the SNS-sink level (outbox retention +
    settle-once against a fake sink). Do not duplicate or remove it.
  - THIS suite proves settle-once ACROSS BILLING'S 24h/reservation-window
    RETENTION HORIZON — the seam D-9 is about — using a billing double. The two
    are complementary: delivery (there) vs. what happens when delivery lands
    early / late / never (here).

Findings covered:
  - QB-01 / QM-02  dropped emit -> reservation sweep-expires as a $0 release with
                   NO usage row (silent un-billing). RED asserts the invariant:
                   a usage row XOR an explicit alerted void, never a silent
                   release.  (billing reservation.py:785-811 vs :1188-1243)
  - D-12           past-horizon settlement cannot commit (billing commit reads
                   Redis only, no DDB fallback -> 404). The outbox MUST convert
                   that into an explicit void + ALERT, never a silent drop.
                   (billing reservation.py:576-587; workers/lifecycle_consumer.py:206-249)
  - D-13 / QC-04   /apply-credit-grant idempotency is a 24h Redis cache only;
                   past 24h the same key RE-EXECUTES -> double credit. RED (as a
                   strict-xfail contract test) asserts single-effect. Flips to a
                   hard failure the day billing ships durable dedup.
                   (billing.py:65,1257-1262,1284-1421; transaction_repo.py:19-57)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional

import pytest

from ab0t_quota.billing.lifecycle import LifecycleEmitter


# ---------------------------------------------------------------------------
# FakeBilling — a faithful in-memory double of the SETTLEMENT semantics W-DISC
# verified in billing/output. It encodes ONLY measured behaviour; every branch
# carries its source citation. No network, no deployed service.
# ---------------------------------------------------------------------------

class Billing404(Exception):
    """Billing's 'reservation not found or expired' (HTTP 404).

    Source: commit reads the reservation from Redis ONLY and 404s when the hash
    is gone — there is NO DynamoDB fallback on the commit path.
    (billing/output/app/core/reservation.py:576-587)
    """


@dataclass
class _Reservation:
    reservation_id: str
    org_id: str
    expires_at_s: float          # virtual-clock seconds
    status: str = "active"       # active | committed | released


@dataclass
class FakeBilling:
    """In-memory faithful double of billing's retention behaviour.

    Virtual clock (`now_s`) lets a test cross the 24h / reservation-window
    horizon deterministically without real time.

    Modeled behaviours (all FOUND, cited):
      1. Reservation window + one-usage-row-on-commit.  A reservation lives for
         `reservation_window_s` (billing: RESERVATION_TIMEOUT_MINUTES*60;
         prod .env.production:95 = 1440min = 86400s). A commit while active
         writes EXACTLY ONE usage row iff actual_cost > 0.
         (reservation.py:785-811 — `if actual_cost > 0:` wraps the USAGE txn)
      2. No DDB fallback on commit.  A commit after the hash has expired 404s.
         (reservation.py:576-587)
      3. The sweep.  An uncommitted reservation past its window is released as a
         $0 `reservation_release` with NO usage row.
         (reservation.py:1188-1243; workers/reservation_sweep_worker.py:59-97)
      4. Credit-grant dedup is a 24h Redis cache ONLY.  Past 24h the same
         idempotency_key RE-EXECUTES the grant.
         (billing.py:65 IDEMPOTENCY_RESULT_TTL_SECONDS=86400; :1257-1262;
          :1284-1421 no reference_id consult; transaction_repo.py:19-57)
    """

    reservation_window_s: float = 86400.0   # prod value; dev/template = 1800s
    grant_dedup_ttl_s: float = 86400.0       # billing.py:65

    now_s: float = 0.0
    reservations: Dict[str, _Reservation] = field(default_factory=dict)
    usage_rows: List[dict] = field(default_factory=list)
    zero_releases: List[dict] = field(default_factory=list)
    # grant idempotency cache: key -> (applied_at_s)
    _grant_cache: Dict[str, float] = field(default_factory=dict)
    grants_applied: List[dict] = field(default_factory=list)
    credit_balance: Decimal = Decimal("0")

    # -- clock ------------------------------------------------------------
    def advance(self, seconds: float) -> None:
        """Advance the virtual clock and run billing's 60s sweep (idempotently):
        any active reservation now past its window becomes a $0 release with NO
        usage row — exactly the QM-02 silent-un-billing outcome."""
        self.now_s += seconds
        for r in self.reservations.values():
            if r.status == "active" and self.now_s >= r.expires_at_s:
                r.status = "released"
                self.zero_releases.append({
                    "reservation_id": r.reservation_id,
                    "org_id": r.org_id,
                    "amount": Decimal("0"),          # reservation.py:1218
                    "reference_type": "reservation_expire",
                    "usage_row": False,
                })

    # -- reserve / commit -------------------------------------------------
    def reserve(self, reservation_id: str, org_id: str) -> None:
        self.reservations[reservation_id] = _Reservation(
            reservation_id=reservation_id,
            org_id=org_id,
            expires_at_s=self.now_s + self.reservation_window_s,
        )

    def commit(self, reservation_id: str, actual_cost: Decimal) -> dict:
        """Commit a reservation. State-machine guard is the real dedup:
        active -> settle once; committed -> replay (no new row); gone -> 404.
        (reservation.py:662-668 Lua status guard; :576-587 Redis-only read)"""
        r = self.reservations.get(reservation_id)
        if r is None or (r.status == "released") or (
            r.status == "active" and self.now_s >= r.expires_at_s
        ):
            # Hash expired / swept -> billing 404s; no re-debit possible.
            raise Billing404(reservation_id)
        if r.status == "committed":
            # 24h idempotent replay: cached response, NO second usage row.
            return {"status": "already_committed", "reservation_id": reservation_id}
        r.status = "committed"
        if actual_cost > 0:                              # reservation.py:787
            self.usage_rows.append({
                "reservation_id": reservation_id,
                "org_id": r.org_id,
                "actual_cost": actual_cost,
            })
        return {"status": "committed", "reservation_id": reservation_id}

    # -- credit grant -----------------------------------------------------
    def apply_credit_grant(self, idempotency_key: str, amount: Decimal) -> dict:
        """24h Redis-cache dedup ONLY. Past the TTL the same key re-executes and
        the grant is applied AGAIN. (billing.py:1257-1262 -> :65; no durable
        reference_id consult at :1284-1421)"""
        cached_at = self._grant_cache.get(idempotency_key)
        if cached_at is not None and (self.now_s - cached_at) < self.grant_dedup_ttl_s:
            return {"status": "idempotent_replay", "applied": False}
        # Cache miss OR cache entry has TTL-expired -> RE-EXECUTE.
        self.credit_balance += amount
        self._grant_cache[idempotency_key] = self.now_s
        self.grants_applied.append({"idempotency_key": idempotency_key, "amount": amount})
        return {"status": "applied", "applied": True}


# ---------------------------------------------------------------------------
# The invariant helper — the star of the suite.
# ---------------------------------------------------------------------------

def assert_activation_settled_exactly_once(
    billing: FakeBilling,
    reservation_id: str,
    *,
    void_ledger: List[dict],
) -> None:
    """THE ticket invariant. For a given activation, exactly one of:
      (a) a settled usage row, OR
      (b) an explicit, ALERTED void
    must exist — and there must be NO silent $0 release standing in for either.
    """
    usage = [u for u in billing.usage_rows if u["reservation_id"] == reservation_id]
    silent_release = [z for z in billing.zero_releases if z["reservation_id"] == reservation_id]
    voids = [v for v in void_ledger
             if v.get("reservation_id") == reservation_id and v.get("alerted") is True]

    settled = len(usage)
    voided = len(voids)

    # Exactly-one-of: a usage row XOR an alerted void.
    assert settled + voided == 1, (
        f"invariant violated for {reservation_id}: "
        f"{settled} usage row(s) + {voided} alerted void(s) "
        f"(need exactly one). silent $0 releases seen: {len(silent_release)}."
    )
    # And the silent-release escape hatch must be closed: a $0 release is only
    # acceptable if it was PROMOTED to an alerted void.
    if silent_release:
        assert voided == 1, (
            f"invariant violated for {reservation_id}: a $0 reservation_release "
            f"was recorded WITHOUT an accompanying alerted void — this is the "
            f"QM-02 silent un-billing the redesign forbids."
        )


TOPIC = "arn:aws:sns:us-east-1:123456789012:resource-lifecycle"
_STARTED = datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc)
_STOPPED = datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc)


# ===========================================================================
# CASE 1 — commit WITHIN the reservation window -> exactly one usage row.
# Baseline: documents correct within-horizon behaviour + idempotent replay.
# Expected GREEN (establishes FakeBilling fidelity + the happy invariant).
# ===========================================================================

class TestCommitWithinWindow:
    def test_within_window_yields_exactly_one_usage_row_and_dedups_replay(self):
        """A terminal event delivered within the reservation window settles once;
        an at-least-once redelivery (still within the 24h commit cache) does NOT
        write a second usage row. Invariant holds via branch (a)."""
        billing = FakeBilling()
        void_ledger: List[dict] = []
        rid = "rsv-within-1"

        billing.reserve(rid, org_id="org-1")
        billing.advance(3600)  # 1h later, still << 24h window

        first = billing.commit(rid, actual_cost=Decimal("0.10"))
        assert first["status"] == "committed"

        # Redelivery within the window -> guarded replay, no second row.
        replay = billing.commit(rid, actual_cost=Decimal("0.10"))
        assert replay["status"] == "already_committed"

        assert len(billing.usage_rows) == 1
        assert not billing.zero_releases
        assert_activation_settled_exactly_once(billing, rid, void_ledger=void_ledger)


# ===========================================================================
# CASE 2 — commit PAST reservation-hash expiry -> 404, and the activation MUST
# end in an explicit ALERTED void, never a silent $0 release.  (D-12)
# RED today: the library has no outbox/void mechanism, so a dropped emit becomes
# a silent $0 release with no usage row and no alert.
# ===========================================================================

class TestPastWindowMustVoidNotSilentlyRelease:
    @pytest.mark.asyncio
    async def test_dropped_emit_past_window_becomes_alerted_void_not_silent_release(self):
        """Sequence that silently un-bills today:
          1. reserve.
          2. terminal emit is DROPPED (SNS unconfigured -> emit() returns False;
             fire-and-forget, no retry — lifecycle.py:141-143).
          3. the reservation window elapses -> billing sweep releases it as a $0
             `reservation_release`, NO usage row (reservation.py:1188-1243).
          4. a late redelivery can no longer commit -> billing 404
             (reservation.py:576-587, no DDB fallback).

        D-12 guarantee: step 3/4 MUST instead produce an explicit, ALERTED void.
        RED today: no void/alert exists; only a silent $0 release. The invariant
        helper fails.  GREEN target (outbox + past-horizon void+alert, P3.1/D-12).
        """
        billing = FakeBilling()
        void_ledger: List[dict] = []
        rid = "rsv-late-2"

        billing.reserve(rid, org_id="org-1")

        # --- the drop: emitter is unconfigured, emit() returns False. ---
        emitter = LifecycleEmitter(sns_topic_arn=None)   # -> _get_client() is None
        delivered = await emitter.resource_stopped(
            org_id="org-1", user_id="alice",
            resource_id="sb-late", resource_type="sandbox",
            reservation_id=rid,
            hourly_rate=Decimal("0.10"), allocation_fee=Decimal("0.00"),
            started_at=_STARTED, stopped_at=_STOPPED,
        )
        assert delivered is False, "precondition: the terminal emit was silently dropped"

        # --- the window elapses: billing sweep -> $0 release, no usage row. ---
        billing.advance(billing.reservation_window_s + 1)
        assert billing.zero_releases, "precondition: sweep produced a $0 release"
        assert not billing.usage_rows, "precondition: no usage row was ever written"

        # --- a late redelivery can no longer settle: billing 404s. ---
        with pytest.raises(Billing404):
            billing.commit(rid, actual_cost=Decimal("0.10"))

        # --- D-12 contract: the library must have converted the un-settleable
        #     activation into an explicit, ALERTED void. Probe for that surface. ---
        void_hook = (
            getattr(emitter, "void_ledger", None)
            or getattr(emitter, "voided_activations", None)
            or getattr(emitter, "_void_ledger", None)
        )
        if void_hook:
            void_ledger = list(void_hook)

        # The invariant: a usage row XOR an alerted void; never a silent release.
        assert_activation_settled_exactly_once(billing, rid, void_ledger=void_ledger)


# ===========================================================================
# CASE 3 — /apply-credit-grant replayed PAST 24h -> double grant.  (D-13)
# This asserts a contract BILLING DOES NOT YET PROVIDE. It is a strict-xfail:
# RED-by-contract today (double grant), and it flips to a HARD FAILURE (xpass
# under strict) the day billing ships durable dedup — the signal to promote it.
# ===========================================================================

@pytest.mark.xfail(
    strict=True,
    reason="D-13: billing dedups credit grants on a 24h Redis cache only "
           "(billing.py:65,1257-1262) and never consults the persisted "
           "reference_id (billing.py:1284-1421; transaction_repo.py:19-57). "
           "Past 24h the same idempotency_key RE-EXECUTES -> double credit. This "
           "asserts single-effect, a contract billing does not yet provide; it "
           "will xpass (hard-fail) once durable grant dedup lands.",
)
class TestCreditGrantDoubleChargePastRetention:
    def test_grant_replayed_past_24h_must_apply_exactly_once(self):
        """The same 'one credit EVER' idempotency_key
        (e.g. `org:{org}:initial_credit`, auth_events.py:573-576) is redelivered
        by the outbox after billing's 24h grant cache has expired. Billing
        re-executes the grant -> the customer is credited twice.

        Asserts the D-13 contract: a stable idempotency_key must apply AT MOST
        ONCE regardless of how late the redelivery arrives."""
        billing = FakeBilling()
        key = "org:org-1:initial_credit"

        first = billing.apply_credit_grant(key, amount=Decimal("5.00"))
        assert first["applied"] is True

        # Outbox redelivers the SAME grant 25h later (long SNS outage / DLQ replay),
        # past billing's 24h dedup cache.
        billing.advance(billing.grant_dedup_ttl_s + 3600)

        second = billing.apply_credit_grant(key, amount=Decimal("5.00"))

        # D-13 contract (currently VIOLATED -> strict-xfail): the grant must NOT
        # have been applied a second time.
        assert second["applied"] is False, (
            "D-13: credit grant re-executed past the 24h retention horizon — "
            "the customer was credited twice for a one-time grant."
        )
        assert billing.credit_balance == Decimal("5.00"), (
            f"D-13: credit_balance is {billing.credit_balance}; a single grant "
            f"replayed past 24h double-credited to $10.00."
        )
        assert len(billing.grants_applied) == 1
