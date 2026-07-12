"""P2.0 — the precedence law, encoded as a test (ticket 20260709, DECISIONS D-10).

This is the FIRST Phase-2 task and it gates everything after it. Four mechanisms
can each claim "the count" for a gauge: the live Redis counter, the DDB snapshot,
the open-activation ledger, and the consumer's observed_usage_provider. Without a
declared order, the redesign would re-create the drift class one layer up (QI-07).

The law (D-10): the activation ledger is authoritative; the counter is a CACHE of
Σ open activations; the snapshot is a snapshot OF the ledger; the provider is a
reconciliation INPUT only, for gauges, only when activations are absent.

THE required test (D-10, verbatim): "given contradictory values in all four, the
system converges to Σ open activations (or the provider's observed set when
activations are absent) and logs which source won."
"""
from __future__ import annotations

import logging

import fakeredis.aioredis
import pytest
import pytest_asyncio

from ab0t_quota.activations import (
    Activation, ActivationState, InMemoryActivationStore, converge_gauge,
    resolve_gauge_level,
)
from ab0t_quota.counters.gauge import GaugeCounter
from ab0t_quota.counters.accumulator import AccumulatorCounter
from ab0t_quota.models.core import ResetPeriod

RK = "sandbox.concurrent"


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.flushall()
    await r.aclose()


async def _open(store, org, rk, n):
    """Open n activations each spending 1.0 of rk."""
    for i in range(n):
        await store.put_open(Activation(
            activation_id=f"act-{org}-{rk}-{i}", org_id=org, user_id=None,
            resource_key=rk, spend={rk: 1.0}, state=ActivationState.OPEN.value,
        ))


class TestPrecedenceLaw:
    @pytest.mark.asyncio
    async def test_converges_to_sum_open_activations_over_all_contradictions(self, redis, caplog):
        """All four sources DISAGREE. The ledger (Σ open activations = 3) must win —
        NOT the live counter (99), NOT the snapshot (42), NOT the provider (7)."""
        store = InMemoryActivationStore()
        await _open(store, "org-1", RK, 3)          # ledger says 3

        counter = GaugeCounter(redis, "org-1", RK)
        await counter.reset(99.0)                    # live counter lies: 99
        # (a snapshot of 42 would, pre-law, be restored over the counter; the law
        #  makes the snapshot a snapshot OF the ledger, so it cannot resurrect 42.)

        with caplog.at_level(logging.INFO, logger="ab0t_quota.activations"):
            # D-35: converge_gauge is now the MECHANISM (counter := Σ open); it makes
            # no source choice, so it takes no provider argument. A contradictory
            # provider value cannot enter here — the provider-vs-ledger LAW is the
            # reconciler's (reconcile._resolve_existence). The ledger (3) still wins
            # over the counter (99) / snapshot (42).
            value, source = await converge_gauge(
                activation_store=store, org_id="org-1", resource_key=RK,
                counter=counter,
            )

        assert value == 3.0, "must converge to Σ open activations, not the counter/snapshot/provider"
        assert source == "activations"
        assert await counter.get() == 3.0, "counter is a CACHE of Σ open activations"
        # Logs WHICH source won.
        assert any("source=activations" in r.getMessage() for r in caplog.records), \
            "convergence must log which source won"

    @pytest.mark.asyncio
    async def test_converge_gauge_is_mechanism_only_no_provider_choice(self, redis, caplog):
        """D-35 (SUPERSEDES this test's original premise): converge_gauge is the
        MECHANISM — counter := Σ open activations, no source choice. With no open
        activations it converges to 0 and logs source=activations. The
        provider-wins-on-existence LAW moved to the reconciler and is proven in
        tests/test_reconcile_20260710.py (test_provider_wins_on_existence_...,
        test_per_user_partitions_reconciled). Two implementations of "which source
        wins" WAS the hazard (FUTURE §1); there is now exactly one, and it is not
        here. (Original name test_provider_wins_only_when_activations_absent —
        renamed because converge_gauge no longer chooses the provider.)"""
        store = InMemoryActivationStore()          # no activations
        counter = GaugeCounter(redis, "org-1", RK)
        await counter.reset(99.0)                    # counter still lies

        with caplog.at_level(logging.INFO, logger="ab0t_quota.activations"):
            value, source = await converge_gauge(
                activation_store=store, org_id="org-1", resource_key=RK,
                counter=counter,
            )
        assert value == 0.0
        assert source == "activations"
        assert await counter.get() == 0.0
        assert any("source=activations" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_no_activations_no_provider_is_zero(self, redis):
        """No activations and no observation → the justified level is zero (the
        counter cannot assert a value nothing justifies)."""
        store = InMemoryActivationStore()
        counter = GaugeCounter(redis, "org-1", RK)
        await counter.reset(99.0)
        value, source = await converge_gauge(
            activation_store=store, org_id="org-1", resource_key=RK,
            counter=counter,   # D-35: no provider arg — converge_gauge is mechanism-only
        )
        assert value == 0.0 and source == "activations"
        assert await counter.get() == 0.0

    def test_counter_and_snapshot_are_never_authoritative(self):
        """The pure law: neither the live counter nor the snapshot can win. The
        resolver takes no counter/snapshot argument — they are DERIVATIONS, so a
        contradictory value simply has no way to enter the decision."""
        # activations in use → activations win regardless of any observed value.
        v, s = resolve_gauge_level(open_activation_sum=3.0, provider_observed=999.0,
                                   activations_in_use=True)
        assert (v, s) == (3.0, "activations")

    @pytest.mark.asyncio
    async def test_converge_derives_per_user_partitions_from_ledger(self, redis):
        """P2.5: per-user partitions are DERIVED from the ledger (activations carry
        user_id + spend), not resurrected from a snapshot. Two users' open
        activations → org total AND each user partition reconstructed."""
        store = InMemoryActivationStore()
        for uid, n in (("alice", 2), ("bob", 1)):
            for i in range(n):
                await store.put_open(Activation(
                    activation_id=f"act-{uid}-{i}", org_id="org-1", user_id=uid,
                    resource_key=RK, spend={RK: 1.0}, state=ActivationState.OPEN.value))
        counter = GaugeCounter(redis, "org-1", RK)
        await counter.reset(99.0)                # drifted org value
        await counter.reset_user("alice", 99.0)  # drifted user value

        value, source = await converge_gauge(
            activation_store=store, org_id="org-1", resource_key=RK, counter=counter)
        assert value == 3.0 and source == "activations"
        assert await counter.get() == 3.0
        assert await counter.get_user("alice") == 2.0, "per-user derived from ledger"
        assert await counter.get_user("bob") == 1.0

    @pytest.mark.asyncio
    async def test_accumulators_are_never_reconciled(self, redis):
        """Correctness, not a knob (D-10): accumulators (deltas) are never
        reconciled — only gauges (levels). converge_gauge is gauge-only; an
        accumulator has no reset-to-a-level semantics that would be safe to apply,
        so the money accumulator is left strictly alone."""
        store = InMemoryActivationStore()
        acc = AccumulatorCounter(redis, "org-1", "sandbox.monthly_cost", ResetPeriod.MONTHLY)
        await acc.increment(0.30)
        before = await acc.get()
        # We deliberately do NOT call converge_gauge on an accumulator anywhere in
        # the library. Assert the money total is untouched by any gauge convergence.
        gauge = GaugeCounter(redis, "org-1", RK)
        await converge_gauge(activation_store=store, org_id="org-1", resource_key=RK,
                             counter=gauge)   # D-35: mechanism-only, no provider arg
        assert await acc.get() == before, "accumulator (money) must never be reconciled"
