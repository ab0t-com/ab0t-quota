"""P4 — the library gauge reconciler + observed_usage_provider seam + drift alert.

Ticket 20260709_ab0t_quota_systemic_integrity_redesign, tasks P4.1/P4.2/P4.3.

The law being tested is D-33 (which SUPERSEDES D-10's wording) — three layers,
each authoritative over exactly ONE thing:
  * observed_usage_provider -> EXISTENCE (what is actually live)
  * activation ledger       -> IDENTITY and COST
  * Redis counter           -> NOTHING; it is a cache of the level

Consequences the reconciler must implement, and which these tests pin:
  1. No provider configured -> converge the counter to Σ open activations
     (the zero-config self-heal; D-33 §1).
  2. Provider configured AND it disagrees with the ledger about EXISTENCE ->
     that is a BUG, not drift. Converge the counter to the PROVIDER's observed
     set, flag the record for repair, and ALERT. Never silently reconcile it
     away (D-33 §2). This is THE forbidden case (the whole reason the seam is
     load-bearing, D-28).
  3. Provider UNREACHABLE -> do NOTHING and ALERT. Never fall back to the
     ledger (D-31/D-33 §5) — that is precisely the operation that erases
     reality when the record is what is broken.
  4. Recent-activity guard: never force-set a (org, resource) touched inside
     the guard window (the provider lags creation).
  5. Gauges only. Accumulators are NEVER reconciled.
  6. Per-user partitions are reconciled too (QI-06 — a repair tool that repairs
     half the state guarantees org/user divergence).
  7. A bounded per-pass budget (a drift storm must not become a DDB incident).
  8. A kill-switch mirroring the consumer's proven POOL_QUOTA_RECONCILE_ENABLED.
  9. A paired drift_resolved recovery event so a heal never reads as an incident
     and never alert-storms (FUTURE §5 / D-26).

NEGATIVE CONTROL (mandatory, at the bottom): a deliberately-broken reconciler
that converges to the LEDGER when the provider disagrees MUST fail the
provider-wins assertion — proving the guard catches a broken reconciler.

fakeredis[lua] (lupa) is NOT Redis; these greens are logic proofs, not runtime
proofs on a real EVAL. See the phase-4 artifact's not_verified.
"""
from __future__ import annotations

import asyncio

import fakeredis.aioredis
import pytest
import pytest_asyncio

from ab0t_quota.activations import (
    Activation, ActivationState, InMemoryActivationStore, RedisActivationStore,
)
from ab0t_quota.counters.gauge import GaugeCounter
from ab0t_quota.counters.accumulator import AccumulatorCounter
from ab0t_quota.engine import QuotaEngine
from ab0t_quota.models.core import CounterType, ResetPeriod, ResourceDef, TierConfig, TierLimits
from ab0t_quota.providers import StaticTierProvider
from ab0t_quota.registry import ResourceRegistry
from ab0t_quota.reconcile import LibraryReconciler, ReconcileConfig
from ab0t_quota.alerts import DriftAlertManager, AlertDispatcher


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.flushall()
    await r.aclose()


CONCURRENT = ResourceDef(
    service="test", resource_key="sandbox.concurrent",
    display_name="Concurrent", counter_type=CounterType.GAUGE, unit="sandboxes",
)
COST = ResourceDef(
    service="test", resource_key="sandbox.monthly_cost",
    display_name="Monthly Cost", counter_type=CounterType.ACCUMULATOR, unit="USD",
    reset_period=ResetPeriod.MONTHLY, precision=2,
)
TIERS = {
    "free": TierConfig(
        tier_id="free", display_name="Free",
        limits={"sandbox.concurrent": TierLimits(limit=100)},
    ),
}


def _engine(redis, store=None):
    registry = ResourceRegistry()
    registry.register(CONCURRENT, COST)
    return QuotaEngine(
        redis=redis, tier_provider=StaticTierProvider({"org-1": "free"}),
        registry=registry, tiers=TIERS,
        activation_store=store or InMemoryActivationStore(),
    )


class _RecordingDispatcher(AlertDispatcher):
    """Captures every dispatched QuotaAlert for assertion."""
    def __init__(self):
        self.alerts = []

    async def dispatch(self, alert) -> None:
        self.alerts.append(alert)

    def messages(self):
        return [a.message for a in self.alerts]


def _drift_mgr(redis):
    rec = _RecordingDispatcher()
    mgr = DriftAlertManager(redis=redis, dispatchers=[rec], cooldown_seconds=600)
    return mgr, rec


async def _open(store, org_id, resource_key, *, user_id=None, n=1, age_seconds=3600):
    """Put n OPEN activations into the ledger (each spends 1.0 of resource_key).

    Defaults to activations opened `age_seconds` ago (1h) so the recent-activity
    guard does NOT fire — the realistic heal scenario is a crash's over-count
    that has since gone quiet. Pass age_seconds=0 to simulate a just-created
    resource the provider hasn't caught up to yet."""
    from datetime import datetime, timezone, timedelta
    opened = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
    for _ in range(n):
        act = Activation(
            activation_id=f"act_{org_id}_{resource_key}_{_}_{id(object())}",
            org_id=org_id, user_id=user_id, resource_key=resource_key,
            spend={resource_key: 1.0}, state=ActivationState.OPEN.value,
            opened_at=opened,
        )
        await store.put_open(act)


async def _set_gauge(redis, org_id, resource_key, value, *, user=None):
    g = GaugeCounter(redis, org_id, resource_key)
    if user is None:
        await g.reset(value)
    else:
        await g.reset_user(user, value)


async def _gauge(redis, org_id, resource_key, *, user=None):
    g = GaugeCounter(redis, org_id, resource_key)
    return await (g.get_user(user) if user is not None else g.get())


# ===========================================================================
# 1. No provider -> converge to Σ open activations (zero-config self-heal)
# ===========================================================================

async def test_no_provider_converges_counter_to_open_activations(redis):
    """D-33 §1: with NO observed_usage_provider, the ledger is the best proxy
    for existence. A crash-orphaned over-count (counter ahead of the ledger)
    heals DOWN to Σ open activations."""
    store = InMemoryActivationStore()
    eng = _engine(redis, store)
    await _open(store, "org-1", "sandbox.concurrent", n=3)
    await _set_gauge(redis, "org-1", "sandbox.concurrent", 99.0)  # drifted (crash over-count)

    r = LibraryReconciler(eng, observed_usage_provider=None)
    res = await r.reconcile_org("org-1")

    assert await _gauge(redis, "org-1", "sandbox.concurrent") == 3.0
    assert res.skipped is None
    assert res.changes["sandbox.concurrent"]["source"] == "activations"


# ===========================================================================
# 2. THE FORBIDDEN CASE — provider disagrees with the ledger about existence.
#    provider says 3 live, ledger says 1 open -> counter MUST become 3,
#    an alert MUST fire, and the record is flagged for repair.
# ===========================================================================

async def test_provider_wins_on_existence_disagreement_and_alerts(redis):
    store = InMemoryActivationStore()
    eng = _engine(redis, store)
    await _open(store, "org-1", "sandbox.concurrent", n=1)      # ledger says 1
    await _set_gauge(redis, "org-1", "sandbox.concurrent", 1.0)  # counter agrees with ledger

    def provider(org_id):
        return {"sandbox.concurrent": {"total": 3.0, "per_user": {}}}  # reality: 3 live

    mgr, rec = _drift_mgr(redis)
    r = LibraryReconciler(eng, observed_usage_provider=provider, drift_alerts=mgr)
    res = await r.reconcile_org("org-1")

    # counter converges to the PROVIDER's observed existence, not the ledger's.
    assert await _gauge(redis, "org-1", "sandbox.concurrent") == 3.0
    assert res.changes["sandbox.concurrent"]["source"] == "provider"
    # the ledger-vs-reality disagreement is a BUG the record needs repaired.
    assert "sandbox.concurrent" in res.divergences
    # ALERT fired (distinct drift channel).
    assert any("gauge_drift_detected" in m for m in rec.messages())


# ===========================================================================
# 3. Provider unreachable -> do NOTHING and ALERT (D-31). Never converge to
#    the ledger, because the record may be the thing that is broken.
# ===========================================================================

async def test_provider_unreachable_counter_does_not_move_and_alerts(redis):
    store = InMemoryActivationStore()
    eng = _engine(redis, store)
    await _open(store, "org-1", "sandbox.concurrent", n=1)
    await _set_gauge(redis, "org-1", "sandbox.concurrent", 99.0)  # drifted, BUT...

    def provider(org_id):
        raise ConnectionError("product DB unreachable")

    mgr, rec = _drift_mgr(redis)
    r = LibraryReconciler(eng, observed_usage_provider=provider, drift_alerts=mgr)
    res = await r.reconcile_org("org-1")

    # ...the counter must NOT move — not even to the ledger. An IO error may
    # never silently widen a limit or erase a spend (D-31).
    assert await _gauge(redis, "org-1", "sandbox.concurrent") == 99.0
    assert res.skipped == "provider_unreachable"
    assert any("provider_unreachable" in m for m in rec.messages())


# ===========================================================================
# 4. Recent-activity guard — never force-set a just-touched (org, resource).
# ===========================================================================

async def test_recent_activity_guard_skips_forceset(redis):
    store = InMemoryActivationStore()
    eng = _engine(redis, store)
    await _open(store, "org-1", "sandbox.concurrent", n=1)
    await _set_gauge(redis, "org-1", "sandbox.concurrent", 99.0)  # drifted
    # consumer's touch-key (set by every increment/decrement) is live:
    await redis.set("quota:reconcile:recent:org-1", "1", ex=90)

    r = LibraryReconciler(eng, observed_usage_provider=None)
    res = await r.reconcile_org("org-1")

    assert await _gauge(redis, "org-1", "sandbox.concurrent") == 99.0  # untouched
    assert res.skipped == "recent_activity"


async def test_recently_opened_activation_is_recent_activity(redis):
    """Zero-config guard: a just-opened activation (opened_at ~ now) means the
    provider may lag — skip, no consumer touch-key needed."""
    store = InMemoryActivationStore()
    eng = _engine(redis, store)
    await _open(store, "org-1", "sandbox.concurrent", n=1, age_seconds=0)  # just now
    await _set_gauge(redis, "org-1", "sandbox.concurrent", 99.0)

    r = LibraryReconciler(
        eng, observed_usage_provider=None,
        config=ReconcileConfig(activity_guard_seconds=90),
    )
    res = await r.reconcile_org("org-1")
    assert await _gauge(redis, "org-1", "sandbox.concurrent") == 99.0
    assert res.skipped == "recent_activity"


# ===========================================================================
# 5. Per-user partitions ARE reconciled (QI-06).
# ===========================================================================

async def test_per_user_partitions_reconciled(redis):
    store = InMemoryActivationStore()
    eng = _engine(redis, store)
    # ledger empty; provider is authoritative for existence: user-a has 2 live.
    await _set_gauge(redis, "org-1", "sandbox.concurrent", 5.0)          # org drifted
    await _set_gauge(redis, "org-1", "sandbox.concurrent", 5.0, user="user-a")
    await _set_gauge(redis, "org-1", "sandbox.concurrent", 4.0, user="ghost")  # stale user

    def provider(org_id):
        return {"sandbox.concurrent": {"total": 2.0, "per_user": {"user-a": 2.0}}}

    mgr, _rec = _drift_mgr(redis)
    r = LibraryReconciler(eng, observed_usage_provider=provider, drift_alerts=mgr)
    await r.reconcile_org("org-1")

    assert await _gauge(redis, "org-1", "sandbox.concurrent") == 2.0
    assert await _gauge(redis, "org-1", "sandbox.concurrent", user="user-a") == 2.0
    # a stale per-user key not present in the provider's observed set is cleared.
    assert await _gauge(redis, "org-1", "sandbox.concurrent", user="ghost") == 0.0


# ===========================================================================
# 6. Gauges only — accumulators are NEVER reconciled.
# ===========================================================================

async def test_accumulators_never_reconciled(redis):
    store = InMemoryActivationStore()
    eng = _engine(redis, store)
    acc = AccumulatorCounter(redis, "org-1", "sandbox.monthly_cost", ResetPeriod.MONTHLY)
    await acc.increment(12.34)
    before = await acc.get()

    def provider(org_id):
        # even if a provider foolishly returns an accumulator key, it is ignored.
        return {"sandbox.monthly_cost": {"total": 0.0, "per_user": {}},
                "sandbox.concurrent": {"total": 0.0, "per_user": {}}}

    r = LibraryReconciler(eng, observed_usage_provider=provider)
    res = await r.reconcile_org("org-1")

    assert await acc.get() == before               # spend never erased
    assert "sandbox.monthly_cost" not in res.changes


# ===========================================================================
# 7. Bounded per-pass budget — a drift storm is paced, not a DDB incident.
# ===========================================================================

async def test_bounded_per_pass_budget(redis):
    store = InMemoryActivationStore()
    eng = _engine(redis, store)
    orgs = [f"org-{i}" for i in range(5)]
    for o in orgs:
        await _open(store, o, "sandbox.concurrent", n=1)
        await _set_gauge(redis, o, "sandbox.concurrent", 50.0)  # every org drifted

    r = LibraryReconciler(
        eng, observed_usage_provider=None,
        config=ReconcileConfig(max_force_sets_per_pass=2),
    )
    # force=True: a single-process logic test explicitly accepts the in-memory view.
    res = await r.run_once(org_ids=orgs, force=True)

    assert res.force_sets == 2          # budget capped the pass
    assert res.backpressure is True
    healed = 0
    for o in orgs:
        if await _gauge(redis, o, "sandbox.concurrent") == 1.0:
            healed += 1
    assert healed == 2                  # only two orgs got force-set this pass


# ===========================================================================
# 8. Kill-switch (mirrors POOL_QUOTA_RECONCILE_ENABLED).
# ===========================================================================

async def test_kill_switch_disables_reconcile(redis):
    store = InMemoryActivationStore()
    eng = _engine(redis, store)
    await _open(store, "org-1", "sandbox.concurrent", n=1)
    await _set_gauge(redis, "org-1", "sandbox.concurrent", 99.0)

    r = LibraryReconciler(
        eng, observed_usage_provider=None, config=ReconcileConfig(enabled=False),
    )
    res = await r.reconcile_org("org-1")
    assert await _gauge(redis, "org-1", "sandbox.concurrent") == 99.0
    assert res.skipped == "disabled"


# ===========================================================================
# 9. Paired drift_resolved recovery event (FUTURE §5).
# ===========================================================================

async def test_paired_drift_resolved_after_heal(redis):
    store = InMemoryActivationStore()
    eng = _engine(redis, store)
    await _open(store, "org-1", "sandbox.concurrent", n=1)
    await _set_gauge(redis, "org-1", "sandbox.concurrent", 99.0)  # drift

    mgr, rec = _drift_mgr(redis)
    r = LibraryReconciler(eng, observed_usage_provider=None, drift_alerts=mgr)

    await r.reconcile_org("org-1")                                  # pass 1: detect + heal
    assert any("gauge_drift_detected" in m for m in rec.messages())
    assert await _gauge(redis, "org-1", "sandbox.concurrent") == 1.0

    await r.reconcile_org("org-1")                                  # pass 2: value matches
    assert any("gauge_drift_resolved" in m for m in rec.messages())


async def test_drift_detect_is_rate_limited_but_resolve_is_not_suppressed(redis):
    """The resolve must never be suppressed by a prior detect cooldown (mirrors
    Go's over_limit_admitted/resolved keying)."""
    mgr, rec = _drift_mgr(redis)
    # two detects in a row -> the second is suppressed by cooldown.
    await mgr.drift_detected("org-1", "r", observed=3, ledger=1, before=1, after=3, source="provider")
    await mgr.drift_detected("org-1", "r", observed=3, ledger=1, before=1, after=3, source="provider")
    assert sum("gauge_drift_detected" in m for m in rec.messages()) == 1
    # ...but a resolve still fires (separate keyspace).
    fired = await mgr.drift_resolved("org-1", "r", value=1)
    assert fired is True
    assert any("gauge_drift_resolved" in m for m in rec.messages())


# ===========================================================================
# 10. run_once enumerates orgs from the LEDGER's open index (D-10/E3), not a
#     counter snapshot.
# ===========================================================================

async def test_run_once_enumerates_from_ledger_open_index(redis):
    store = InMemoryActivationStore()
    eng = _engine(redis, store)
    await _open(store, "org-a", "sandbox.concurrent", n=2)
    await _open(store, "org-b", "sandbox.concurrent", n=1)
    await _set_gauge(redis, "org-a", "sandbox.concurrent", 40.0)
    await _set_gauge(redis, "org-b", "sandbox.concurrent", 40.0)

    r = LibraryReconciler(eng, observed_usage_provider=None)
    res = await r.run_once(force=True)  # org_ids=None -> enumerate; force: single-process test

    assert await _gauge(redis, "org-a", "sandbox.concurrent") == 2.0
    assert await _gauge(redis, "org-b", "sandbox.concurrent") == 1.0
    assert res.orgs_reconciled == 2


# ===========================================================================
# 11. Background loop (defaults ON, D-28) — a running reconciler heals an
#     orphaned over-count with NO per-request wiring.
# ===========================================================================

async def test_background_loop_heals_orphaned_overcount(redis):
    # D-37: the background loop requires a SHARED (durable) store — an in-memory
    # store would be refused. RedisActivationStore shares the counter's Redis.
    store = RedisActivationStore(redis)
    eng = _engine(redis, store)
    await _open(store, "org-1", "sandbox.concurrent", n=1)        # ledger=1 (old)
    await _set_gauge(redis, "org-1", "sandbox.concurrent", 7.0)   # crash over-count

    r = LibraryReconciler(
        eng, observed_usage_provider=None,
        # D-39: a shared Redis store is durable only under the machine-check; on
        # fakeredis (no CONFIG, like ElastiCache) the operator confirms it.
        config=ReconcileConfig(interval_seconds=0.02, redis_durability_confirmed=True),
    )
    assert r.start() is True                                      # shared+durable → runs
    try:
        # let the loop run at least one pass
        for _ in range(50):
            await asyncio.sleep(0.02)
            if await _gauge(redis, "org-1", "sandbox.concurrent") == 1.0:
                break
    finally:
        await r.stop()
    assert await _gauge(redis, "org-1", "sandbox.concurrent") == 1.0
    assert r._task is None  # stop() cleaned up the task


# ===========================================================================
# 12. D-37 — the reconciler must REFUSE to background-run against a per-process
#     in-memory store, and converge correctly against a SHARED store.
#     Two engines, two in-memory stores, one shared Redis counter is the exact
#     multi-replica config that would under-count. This test class did not exist
#     and is the one all six pattern-instances would have been caught by.
# ===========================================================================

async def test_reconciler_refuses_background_run_on_in_memory_store(redis):
    # Replica A and Replica B each with their OWN in-memory ledger, sharing one
    # Redis counter. Replica A's reconciler would see only A's activations and
    # force the SHARED counter down to A's partial view → under-count (D-31).
    store_a = InMemoryActivationStore()
    store_b = InMemoryActivationStore()
    eng_a = _engine(redis, store_a)
    eng_b = _engine(redis, store_b)
    await _open(store_a, "org-1", "sandbox.concurrent", n=2)   # A sees 2
    await _open(store_b, "org-1", "sandbox.concurrent", n=1)   # B sees 1 (same org!)
    await _set_gauge(redis, "org-1", "sandbox.concurrent", 3.0)  # shared counter = 3 (truth)

    r_a = LibraryReconciler(eng_a, observed_usage_provider=None)
    started = r_a.start()

    assert started is False                     # REFUSED to auto-run
    assert r_a._refused_unsafe is True
    assert r_a._task is None                    # no background task
    assert "in-memory" in r_a.unsafe_capability()
    # crucially: the shared counter was NOT forced down to replica A's partial 2.
    assert await _gauge(redis, "org-1", "sandbox.concurrent") == 3.0
    await r_a.stop()  # safe no-op


async def test_reconciler_runs_and_converges_full_view_on_shared_store(redis):
    # ONE shared RedisActivationStore across both engines — now Σ open = the FULL
    # truth (3), and the reconciler converges the shared counter to it.
    shared = RedisActivationStore(redis)
    eng_a = _engine(redis, shared)
    eng_b = _engine(redis, shared)
    await _open(shared, "org-1", "sandbox.concurrent", n=2)    # via A
    await _open(shared, "org-1", "sandbox.concurrent", n=1)    # via B (same store)
    await _set_gauge(redis, "org-1", "sandbox.concurrent", 99.0)  # drifted

    r_a = LibraryReconciler(
        eng_a, observed_usage_provider=None,
        config=ReconcileConfig(redis_durability_confirmed=True),  # D-39: durable shared store
    )
    assert r_a.start() is True                  # shared+durable store → safe to run
    await r_a.stop()
    res = await r_a.reconcile_org("org-1")
    assert await _gauge(redis, "org-1", "sandbox.concurrent") == 3.0  # FULL view, not partial


async def test_in_memory_store_still_allowed_when_explicitly_opted_in(redis):
    """A single-process dev/test consumer can opt in (the flag exists so the
    default is safe, not so the capability is impossible)."""
    store = InMemoryActivationStore()
    eng = _engine(redis, store)
    r = LibraryReconciler(
        eng, observed_usage_provider=None,
        config=ReconcileConfig(refuse_in_memory_store=False,
                               require_durable_ledger=False,  # full dev opt-in (D-39)
                               interval_seconds=0.02),
    )
    assert r.start() is True
    await r.stop()


# ===========================================================================
# 13. D-36 — a ledger-vs-reality divergence reads as a MONEY incident: N live
#     resources with no activation record = un-billable usage (QB-01 signature).
# ===========================================================================

async def test_divergence_alert_names_unbillable_usage_as_a_money_incident(redis):
    from ab0t_quota.models.core import AlertSeverity
    store = InMemoryActivationStore()
    eng = _engine(redis, store)
    await _open(store, "org-1", "sandbox.concurrent", n=1)       # ledger records 1
    await _set_gauge(redis, "org-1", "sandbox.concurrent", 1.0)

    def provider(org_id):
        return {"sandbox.concurrent": {"total": 3.0, "per_user": {}}}  # 3 actually live

    mgr, rec = _drift_mgr(redis)
    r = LibraryReconciler(eng, observed_usage_provider=provider, drift_alerts=mgr)
    await r.reconcile_org("org-1")

    money = [a for a in rec.alerts if "UN-BILLABLE" in a.message]
    assert money, "divergence must fire a money-incident alert"
    a = money[0]
    assert a.severity == AlertSeverity.CRITICAL           # not a counter nit
    assert "unbillable_live=2" in a.message               # 3 live - 1 recorded = 2
    assert "cannot be settled" in a.message.lower()


# ===========================================================================
# 15. D-39 — the activation LEDGER is authoritative for identity+cost (D-33) and
#     must be DURABLE. Redis is a CACHE (evictable): a lost row → reconciler sees
#     fewer open → converges the shared counter DOWN → under-count (D-31). So the
#     reconciler REFUSES a non-durable ledger, guarding the OPERATION (run_once +
#     the loop), not just the scheduler. Durability is machine-checked by REUSING
#     W-PY-A's check_redis_outbox_durability (not reimplemented).
# ===========================================================================

async def test_run_once_refuses_non_durable_redis_ledger_and_leaves_counter_untouched(redis):
    # fakeredis has no CONFIG (like ElastiCache) → NOT durable unless confirmed.
    store = RedisActivationStore(redis)
    eng = _engine(redis, store)
    await _open(store, "org-1", "sandbox.concurrent", n=1)
    await _set_gauge(redis, "org-1", "sandbox.concurrent", 99.0)  # drifted

    r = LibraryReconciler(eng, observed_usage_provider=None)  # require_durable_ledger default True
    res = await r.run_once()

    assert res.skipped == "ledger_not_durable"
    # A non-durable ledger reconciled would UNDER-count; refusing leaves the
    # (fail-closed) over-count untouched (D-39).
    assert await _gauge(redis, "org-1", "sandbox.concurrent") == 99.0


async def test_run_once_converges_on_confirmed_durable_redis(redis):
    store = RedisActivationStore(redis)
    eng = _engine(redis, store)
    await _open(store, "org-1", "sandbox.concurrent", n=1)
    await _set_gauge(redis, "org-1", "sandbox.concurrent", 99.0)

    r = LibraryReconciler(
        eng, observed_usage_provider=None,
        config=ReconcileConfig(redis_durability_confirmed=True),  # operator asserts durability
    )
    res = await r.run_once()
    assert res.skipped is None
    assert await _gauge(redis, "org-1", "sandbox.concurrent") == 1.0


async def test_run_once_force_bypasses_the_durability_gate(redis):
    store = RedisActivationStore(redis)   # non-durable (no confirm)
    eng = _engine(redis, store)
    await _open(store, "org-1", "sandbox.concurrent", n=1)
    await _set_gauge(redis, "org-1", "sandbox.concurrent", 99.0)

    r = LibraryReconciler(eng, observed_usage_provider=None)
    res = await r.run_once(force=True)    # explicit acknowledgement of a partial view
    assert res.skipped is None
    assert await _gauge(redis, "org-1", "sandbox.concurrent") == 1.0


async def test_background_loop_stops_on_non_durable_ledger(redis):
    store = RedisActivationStore(redis)   # non-durable
    eng = _engine(redis, store)
    await _open(store, "org-1", "sandbox.concurrent", n=1)
    await _set_gauge(redis, "org-1", "sandbox.concurrent", 99.0)

    r = LibraryReconciler(
        eng, observed_usage_provider=None,
        config=ReconcileConfig(interval_seconds=0.02),
    )
    r.start()                             # start() can't await; the loop checks + exits
    for _ in range(50):
        await asyncio.sleep(0.02)
        if r._refused_unsafe:
            break
    assert r._refused_unsafe is True
    assert await _gauge(redis, "org-1", "sandbox.concurrent") == 99.0  # never under-counted
    await r.stop()


async def test_ledger_durability_classifies_stores(redis):
    eng = _engine(redis, InMemoryActivationStore())

    r_mem = LibraryReconciler(eng, activation_store=InMemoryActivationStore())
    durable, reason = await r_mem.ledger_durability()
    assert durable is False and "in-memory" in reason

    r_redis = LibraryReconciler(eng, activation_store=RedisActivationStore(redis))
    durable, reason = await r_redis.ledger_durability()
    assert durable is False and "Redis" in reason        # fakeredis: no CONFIG, unconfirmed

    r_conf = LibraryReconciler(
        eng, activation_store=RedisActivationStore(redis),
        config=ReconcileConfig(redis_durability_confirmed=True))
    durable, reason = await r_conf.ledger_durability()
    assert durable is True                                # operator-confirmed


# ===========================================================================
# 16. D-39 — DDB is the durable default. Self-provision (connect + ensure_table)
#     against DynamoDB Local, prove the reconciler deems it durable and converges.
#     Throwaway table created + deleted per test; never touches a platform table.
# ===========================================================================

async def test_ddb_activation_ledger_self_provisions_and_is_durable():
    import os as _os
    import uuid as _uuid
    aioboto3 = pytest.importorskip("aioboto3")
    from ab0t_quota.activations import connect_ddb_activation_store

    endpoint = _os.getenv("DYNAMODB_ENDPOINT", "http://localhost:8000")
    table = f"test_act_ledger_{_uuid.uuid4().hex[:12]}"
    _os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
    _os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

    try:
        store, aclose = await connect_ddb_activation_store(
            region="us-east-1", endpoint_url=endpoint, table_name=table)
    except Exception:
        pytest.skip("DynamoDB Local not reachable on :8000")
    try:
        await store.ensure_table()                    # self-provision: create + GSI-active wait
        # a fakeredis counter shares the deployment; the DURABLE ledger is DDB.
        r = fakeredis.aioredis.FakeRedis()
        try:
            registry = ResourceRegistry(); registry.register(CONCURRENT, COST)
            eng = QuotaEngine(redis=r, tier_provider=StaticTierProvider({"org-1": "free"}),
                              registry=registry, tiers=TIERS, activation_store=store)
            await _open(store, "org-1", "sandbox.concurrent", n=2)  # 2 open in the DDB ledger
            await _set_gauge(r, "org-1", "sandbox.concurrent", 50.0)

            rec = LibraryReconciler(eng, observed_usage_provider=None)
            durable, reason = await rec.ledger_durability()
            assert durable is True and reason == "DDB"   # NO confirm needed — a real durable store
            res = await rec.run_once()                    # not refused
            assert res.skipped is None
            assert await GaugeCounter(r, "org-1", "sandbox.concurrent").get() == 2.0
        finally:
            await r.flushall(); await r.aclose()
    finally:
        try:
            await store._ddb.delete_table(TableName=table)   # never leave a throwaway table
        except Exception:
            pass
        await aclose()


# ===========================================================================
# 17. D-10 truth.source=provider (P4.4) — the provider-authoritative mode a
#     legacy-increment consumer (no activation ledger) uses. This is the mode
#     sandbox-platform's bespoke reconcile_org_gauges implemented by hand; the
#     library now owns it so the consumer can DELETE its version.
# ===========================================================================

def _provider_reconciler(redis, provider, **cfg):
    eng = _engine(redis, InMemoryActivationStore())
    mgr, rec = _drift_mgr(redis)
    r = LibraryReconciler(
        eng, observed_usage_provider=provider, drift_alerts=mgr,
        config=ReconcileConfig(truth_source="provider", **cfg))
    return r, rec


async def test_provider_mode_forcesets_to_provider_no_divergence_alarm(redis):
    # ledger is EMPTY (consumer never calls acquire); counter drifted high.
    await _set_gauge(redis, "org-1", "sandbox.concurrent", 5.0)

    def provider(org_id):
        return {"sandbox.concurrent": {"total": 1.0, "per_user": {}}}  # product truth: 1 live

    r, rec = _provider_reconciler(redis, provider)
    res = await r.reconcile_org("org-1")

    assert await _gauge(redis, "org-1", "sandbox.concurrent") == 1.0   # force-set to provider
    assert res.divergences == []                                       # NOT a bug — provider IS truth
    # no CRITICAL "un-billable" money incident in provider mode
    assert not any("UN-BILLABLE" in m for m in rec.messages())


async def test_provider_mode_runs_without_a_durable_ledger(redis):
    # provider mode does not read the ledger, so the D-39 durability gate must not
    # refuse an in-memory store — the provider (product store) is the durable truth.
    await _set_gauge(redis, "org-1", "sandbox.concurrent", 5.0)

    def provider(org_id):
        return {"sandbox.concurrent": {"total": 2.0, "per_user": {}}}

    r, _rec = _provider_reconciler(redis, provider)
    durable, reason = await r.ledger_durability()
    assert durable is True and "provider-mode" in reason
    res = await r.run_once(org_ids=["org-1"])          # NOT refused
    assert res.skipped is None
    assert await _gauge(redis, "org-1", "sandbox.concurrent") == 2.0


async def test_provider_mode_enumerates_orgs_from_counter_keyspace(redis):
    # stuck-high orgs (a gauge but zero live) — the population the ledger can't
    # enumerate. run_once(None) must find them from the library's own gauge keys.
    await _set_gauge(redis, "org-a", "sandbox.concurrent", 3.0)
    await _set_gauge(redis, "org-b", "sandbox.concurrent", 7.0)

    def provider(org_id):
        return {"sandbox.concurrent": {"total": 0.0, "per_user": {}}}  # nothing live anymore

    r, _rec = _provider_reconciler(redis, provider)
    res = await r.run_once()                             # enumerate from counter keyspace
    assert res.orgs_reconciled == 2
    assert await _gauge(redis, "org-a", "sandbox.concurrent") == 0.0
    assert await _gauge(redis, "org-b", "sandbox.concurrent") == 0.0


async def test_provider_mode_syncs_and_clears_per_user(redis):
    await _set_gauge(redis, "org-1", "sandbox.concurrent", 5.0)
    await _set_gauge(redis, "org-1", "sandbox.concurrent", 5.0, user="alice")
    await _set_gauge(redis, "org-1", "sandbox.concurrent", 4.0, user="ghost")

    def provider(org_id):
        return {"sandbox.concurrent": {"total": 2.0, "per_user": {"alice": 2.0}}}

    r, _rec = _provider_reconciler(redis, provider)
    await r.reconcile_org("org-1")
    assert await _gauge(redis, "org-1", "sandbox.concurrent") == 2.0
    assert await _gauge(redis, "org-1", "sandbox.concurrent", user="alice") == 2.0
    assert await _gauge(redis, "org-1", "sandbox.concurrent", user="ghost") == 0.0


async def test_provider_mode_recent_activity_guard_still_applies(redis):
    await _set_gauge(redis, "org-1", "sandbox.concurrent", 5.0)
    await redis.set("quota:reconcile:recent:org-1", "1", ex=90)  # a create just happened

    def provider(org_id):
        return {"sandbox.concurrent": {"total": 1.0, "per_user": {}}}

    r, _rec = _provider_reconciler(redis, provider)
    res = await r.reconcile_org("org-1")
    assert res.skipped == "recent_activity"
    assert await _gauge(redis, "org-1", "sandbox.concurrent") == 5.0   # untouched


async def test_provider_mode_requires_a_provider(redis):
    eng = _engine(redis, InMemoryActivationStore())
    with pytest.raises(ValueError):
        LibraryReconciler(eng, observed_usage_provider=None,
                          config=ReconcileConfig(truth_source="provider"))


async def test_engine_bundle_mutation_sets_recent_activity_marker(redis):
    # The library now owns the guard's SET half: a gauge bundle mutation marks
    # recent activity, so a consumer that deletes its bespoke marker keeps the guard.
    from ab0t_quota.models.requests import QuotaIncrementRequest as _Inc  # noqa
    eng = _engine(redis, InMemoryActivationStore())
    await eng.increment_for_bundle(org_id="org-1", bundle_name="sandbox", user_id="u1")
    assert await redis.get("quota:reconcile:recent:org-1") is not None


# ===========================================================================
# 14. setup_quota(observed_usage_provider=...) seam + default-ON wiring (P4.2).
# ===========================================================================

async def test_setup_quota_wires_reconciler_and_provider_seam(tmp_path, monkeypatch):
    """The drop-in accepts observed_usage_provider and starts the reconciler by
    default (D-28). Asserts the seam is wired end-to-end through the lifespan.

    The sync TestClient (which drives the lifespan on its own event loop) runs in
    a worker thread so it does not collide with this test's running loop."""
    import json as _json
    from unittest.mock import patch
    import fakeredis.aioredis as _fr
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ab0t_quota import setup_quota

    cfg = {
        "storage": {"redis_url": "redis://test/0", "persistence_enabled": False},
        "tier_provider": {"type": "static", "default_tier": "starter"},
        "alerts": {"enabled": False},
        "resources": [{
            "service": "test-svc", "resource_key": "thing.concurrent",
            "display_name": "Concurrent Things", "counter_type": "gauge", "unit": "things",
        }],
        "tiers": [{"tier_id": "starter", "display_name": "Starter", "sort_order": 1,
                   "limits": {"thing.concurrent": 5}}],
        # D-39: no DDB in this unit test — use Redis, and confirm durability on the
        # record (fakeredis has no CONFIG, like ElastiCache) so the ledger is durable.
        "activations": {"store": "redis", "redis_durability_confirmed": True},
    }
    p = tmp_path / "quota-config.json"
    p.write_text(_json.dumps(cfg))
    monkeypatch.setenv("QUOTA_CONFIG_PATH", str(p))

    calls = {"n": 0}

    def my_provider(org_id):
        calls["n"] += 1
        return {"thing.concurrent": {"total": 0.0, "per_user": {}}}

    captured = {}

    def _run(injected_store):
        r = _fr.FakeRedis()
        with patch("redis.asyncio.Redis.from_url", side_effect=lambda *a, **k: r):
            app = FastAPI()
            setup_quota(app, enable_paid=False, enable_rate_limit=False,
                        observed_usage_provider=my_provider,
                        activation_store=injected_store)
            with TestClient(app) as client:
                client.get("/api/quotas/tiers")  # drive the lifespan
                captured["reconciler"] = getattr(app.state, "quota_reconciler", None)
                captured["caps"] = dict(getattr(app.state, "quota_capabilities", {}) or {})

    # (a) default path: a DURABLE (confirmed-Redis) ledger -> reconciler runs,
    #     capability populated (not "unknown"), activation_store surfaced.
    await asyncio.to_thread(_run, None)
    reconciler = captured["reconciler"]
    assert reconciler is not None                     # default ON (D-28)
    assert reconciler._provider is my_provider        # provider seam wired
    assert not reconciler._store_is_in_memory()       # durable shared store by default
    assert captured["caps"].get("reconciler", "").startswith("on")   # capability populated
    assert "durable" in captured["caps"].get("activation_store", "")

    # (b) explicit in-memory injection (dev) -> a NON-durable ledger. The reconciler
    #     does NOT auto-run and the capability says OFF, not a lie (D-37/D-39).
    await asyncio.to_thread(_run, InMemoryActivationStore())
    assert captured["caps"].get("reconciler", "").startswith("OFF")
    assert "not durable" in captured["caps"].get("activation_store", "").lower()


# ===========================================================================
# NEGATIVE CONTROL (mandatory) — prove the guard catches a broken reconciler.
# A reconciler that converges to the LEDGER when the provider disagrees MUST
# fail the provider-wins assertion. xfail(strict=True): if it ever PASSES
# (i.e. the broken reconciler somehow satisfies "counter == provider"), that
# is itself a hard failure and this flips red.
# ===========================================================================

class _BrokenReconcilerConvergesToLedger(LibraryReconciler):
    """The forbidden behaviour: always trust the ledger, ignore the provider.
    This is exactly the silent-reconcile that erases reality (D-33 §2/§5)."""
    def _resolve_existence(self, *, ledger_total, provider_observed):
        return ledger_total, "activations", False


@pytest.mark.xfail(strict=True, reason=(
    "NEGATIVE CONTROL: a reconciler that converges to the ledger when the "
    "provider disagrees must NOT satisfy the provider-wins assertion. If this "
    "xfail ever passes, the guard has stopped catching a broken reconciler."))
async def test_negative_control_broken_reconciler_is_caught(redis):
    store = InMemoryActivationStore()
    eng = _engine(redis, store)
    await _open(store, "org-1", "sandbox.concurrent", n=1)      # ledger says 1
    await _set_gauge(redis, "org-1", "sandbox.concurrent", 1.0)

    def provider(org_id):
        return {"sandbox.concurrent": {"total": 3.0, "per_user": {}}}  # reality: 3

    r = _BrokenReconcilerConvergesToLedger(eng, observed_usage_provider=provider)
    await r.reconcile_org("org-1")

    # The REAL requirement (test #2): the counter must equal the provider (3).
    # The broken reconciler leaves it at the ledger (1), so this assertion is
    # EXPECTED to fail — that is the negative control succeeding.
    assert await _gauge(redis, "org-1", "sandbox.concurrent") == 3.0
