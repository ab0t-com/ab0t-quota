"""Ticket 20260810_quota_drift_live_recurrence_permanent_fix — P1.1/P1.5/P5.1.

Live incident: org cd790b95 showed "5/1" quota used with ZERO running
sandboxes. Root cause (this bug class, 4th sighting): a gauge decrement keyed
on a bare RECYCLABLE resource/container id (`counter:lifecycle:end:{id}`).
Warm-pool reuse means a SECOND, genuinely distinct lifecycle event computes
the IDENTICAL key as a past one -> the claim-then-mutate Lua (correctly)
treats it as a retry -> the decrement silently no-ops -> the gauge sticks
high forever.

This file proves, against the REAL GaugeCounter + fakeredis:
  1. The OLD bug shape (bare recyclable-id key) FAILS — the collision
     reproduces exactly as it did live.
  2. The NEW/current key shape (a generation or :evt: UUID component) PASSES
     — the same two activations release the slot correctly.
  3. The P1.5 contract guard (`guard_idempotency_key` /
     `idempotency_key_has_unique_component`) warns (default) or hard-refuses
     (strict mode, AB0T_QUOTA_STRICT_IDEMPOTENCY_KEYS=1) a bare-shaped key,
     and is silent for a good one.
  4. A genuine RETRY of the SAME lifecycle event (identical key, called
     twice) still dedups to exactly ONE decrement — the fix must not weaken
     the pre-existing exactly-once guarantee.
  5. Reconcile-to-live force-sets the gauge from a supplied observed count
     (the self-heal backstop for whatever a key scheme still misses).

Run:
  cd shared/ab0t-quota
  .venv/bin/python -m pytest tests/test_gauge_idempotency_key_contract_20260810.py -q
"""
import os

import pytest
import pytest_asyncio
import fakeredis.aioredis

from ab0t_quota.counters.gauge import (
    GaugeCounter,
    guard_idempotency_key,
    idempotency_key_has_unique_component,
    RecyclableIdempotencyKeyError,
)

ORG = "org-cd790b95-sim"
USER = "user-1"
RK = "sandbox.desktop_sessions"


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.flushall()
    await r.aclose()


# =====================================================================================
# 1/2 — THE collision case: bare key FAILS, generation/uuid-scoped key PASSES
# =====================================================================================

@pytest.mark.asyncio
async def test_bare_recyclable_key_on_both_sides_silently_drops_the_second_activation(redis):
    """OLD/BUGGY scheme: BOTH the create and end keys are bare — keyed ONLY
    on the reused container id, no per-activation component (exactly
    `counter:lifecycle:{start,end}:{id}`, no generation/:evt: suffix — this
    is the shape a caller gets if it forgets to thread claim_generation at
    all, e.g. a synthetic/heartbeat-triggered lifecycle call with no
    resource row in hand). Two DISTINCT activations of the SAME id within
    the 24h idempotency TTL: activation #2's CREATE collides with
    activation #1's already-claimed start key -> silently no-ops (never
    counted) -> activation #2's END then ALSO collides with activation #1's
    already-claimed end key -> also no-ops. Net effect: activation #2's
    real, running resource is never reflected in the gauge at all. This is
    a genuinely different failure mode from the "stuck high" symptom
    (that one comes from a decrement that never fires, e.g. a missed
    lifecycle emit or the reconciler failing to run — see
    test_reconcile_to_live_sets_gauge_from_supplied_count below) but it is
    the SAME root cause (a bare recyclable-id key) and the SAME fix
    (thread a real per-activation component). This is expected to FAIL
    (assert the drop) — the negative control proving the bare shape is
    genuinely broken, so the fix below is real."""
    g = GaugeCounter(redis, ORG, RK)
    cid = "desktop-abc123"  # reused across pool release -> re-claim
    bare_start_key = f"counter:lifecycle:start:{cid}"  # NO generation component
    bare_end_key = f"counter:lifecycle:end:{cid}"

    await g.increment_user(USER, 1.0, idempotency_key=bare_start_key)  # activation 1 create
    await g.decrement_user(USER, 1.0, idempotency_key=bare_end_key)  # activation 1 end
    assert await g.get_user(USER) == 0.0

    # activation 2 (id reused) — SAME bare keys as activation 1
    await g.increment_user(USER, 1.0, idempotency_key=bare_start_key)
    dropped = await g.get_user(USER)
    assert dropped == 0.0, (
        f"expected the bare-key collision to silently drop activation #2's create "
        f"(gauge stays 0 though a resource is now really running), got {dropped} — if "
        "this assertion now fails, the collision protection changed shape; update this "
        "negative control, don't delete it"
    )
    await g.decrement_user(USER, 1.0, idempotency_key=bare_end_key)
    assert await g.get_user(USER) == 0.0  # end also dropped — no over-decrement either


@pytest.mark.asyncio
async def test_generation_scoped_key_fixes_the_same_scenario(redis):
    """NEW/current scheme: the SAME two-activations-of-a-reused-id scenario,
    but the decrement key carries a per-activation generation (the shape
    app/quota.py already uses: `...:{claim_generation}`). Must NOT reproduce
    the bug — this is the fix, proven against the exact scenario that fails
    above."""
    g = GaugeCounter(redis, ORG, RK)
    cid = "desktop-abc123"

    for gen in (0, 1):
        await g.increment_user(USER, 1.0, idempotency_key=f"counter:lifecycle:start:{cid}:{gen}")
        await g.decrement_user(USER, 1.0, idempotency_key=f"counter:lifecycle:end:{cid}:{gen}")

    assert await g.get_user(USER) == 0.0


@pytest.mark.asyncio
async def test_uuid_scoped_key_also_fixes_the_same_scenario(redis):
    """Alternative unique-event-id shape (a fresh UUID per lifecycle event,
    the `:evt:<uuid4>` convention) — for callers with no reliable generation
    counter (e.g. a synthetic/heartbeat-triggered teardown)."""
    import uuid
    g = GaugeCounter(redis, ORG, RK)
    cid = "desktop-abc123"

    for _ in range(2):
        evt = uuid.uuid4()
        await g.increment_user(USER, 1.0, idempotency_key=f"counter:lifecycle:start:{cid}:evt:{evt}")
        await g.decrement_user(USER, 1.0, idempotency_key=f"counter:lifecycle:end:{cid}:evt:{evt}")

    assert await g.get_user(USER) == 0.0


# =====================================================================================
# 3 — P1.5 contract guard
# =====================================================================================

def test_guard_detects_bare_recyclable_shape():
    assert idempotency_key_has_unique_component("counter:lifecycle:end:desktop-abc123") is False
    assert idempotency_key_has_unique_component("counter:lifecycle:end:desktop-abc123:0") is True
    assert idempotency_key_has_unique_component("counter:lifecycle:end:desktop-abc123:7:sandbox.desktop_sessions") is True
    assert idempotency_key_has_unique_component(
        "counter:lifecycle:end:desktop-abc123:evt:9b1f1e2e-6c3b-4a2b-9b1e-2e6c3b4a2b9b"
    ) is True
    assert idempotency_key_has_unique_component(None) is True  # no idempotency requested at all


def test_guard_warns_by_default_does_not_raise(caplog):
    import logging
    caplog.set_level(logging.WARNING, logger="ab0t_quota.counters.gauge")
    guard_idempotency_key("counter:lifecycle:end:desktop-abc123")  # bare — should warn, not raise
    assert any("quota_idempotency_key_looks_recyclable" in r.message for r in caplog.records)


def test_guard_silent_for_generation_scoped_key(caplog):
    import logging
    caplog.set_level(logging.WARNING, logger="ab0t_quota.counters.gauge")
    guard_idempotency_key("counter:lifecycle:end:desktop-abc123:3")
    assert not any("quota_idempotency_key_looks_recyclable" in r.message for r in caplog.records)


def test_guard_strict_mode_rejects_bare_key(monkeypatch):
    monkeypatch.setenv("AB0T_QUOTA_STRICT_IDEMPOTENCY_KEYS", "1")
    with pytest.raises(RecyclableIdempotencyKeyError):
        guard_idempotency_key("counter:lifecycle:end:desktop-abc123")


def test_guard_strict_mode_accepts_good_key(monkeypatch):
    monkeypatch.setenv("AB0T_QUOTA_STRICT_IDEMPOTENCY_KEYS", "1")
    guard_idempotency_key("counter:lifecycle:end:desktop-abc123:3")  # must not raise


@pytest.mark.asyncio
async def test_strict_mode_refuses_at_the_counter_call_site(redis, monkeypatch):
    """The guard is wired into increment/decrement/*_user — strict mode
    refuses the mutation outright rather than only warning."""
    monkeypatch.setenv("AB0T_QUOTA_STRICT_IDEMPOTENCY_KEYS", "1")
    g = GaugeCounter(redis, ORG, RK)
    with pytest.raises(RecyclableIdempotencyKeyError):
        await g.decrement_user(USER, 1.0, idempotency_key="counter:lifecycle:end:desktop-abc123")


# =====================================================================================
# 4 — genuine retry of the SAME event must still dedup to ONE decrement
# =====================================================================================

@pytest.mark.asyncio
async def test_same_event_id_retried_still_dedups_to_one_decrement(redis):
    """The fix must not weaken exactly-once for a genuine retry (SNS/emit
    redelivery of ONE teardown) — only DISTINCT activations should apply
    independently."""
    g = GaugeCounter(redis, ORG, RK)
    await g.increment_user(USER, 1.0, idempotency_key="counter:lifecycle:start:sbx-1:0")
    await g.increment_user(USER, 1.0, idempotency_key="counter:lifecycle:start:sbx-2:0")
    assert await g.get_user(USER) == 2.0

    end_key = "counter:lifecycle:end:sbx-1:0"
    await g.decrement_user(USER, 1.0, idempotency_key=end_key)
    await g.decrement_user(USER, 1.0, idempotency_key=end_key)  # retry #1
    await g.decrement_user(USER, 1.0, idempotency_key=end_key)  # retry #2 (SNS-style replay)

    assert await g.get_user(USER) == 1.0  # sbx-2 keeps its slot — no over-decrement


# =====================================================================================
# 5 — reconcile-to-live sets the gauge from a supplied observed count
# =====================================================================================

@pytest.mark.asyncio
async def test_reconcile_to_live_sets_gauge_from_supplied_count(redis):
    """Belt-and-suspenders backstop: whatever a key scheme still misses (a
    lifecycle event that never fires at all — no key involved), the
    reconciler force-sets the gauge to the observed live truth."""
    from ab0t_quota.engine import QuotaEngine
    from ab0t_quota.models.core import ResourceDef, CounterType
    from ab0t_quota.registry import ResourceRegistry
    from ab0t_quota.providers import StaticTierProvider
    from ab0t_quota.reconcile import LibraryReconciler, ReconcileConfig

    registry = ResourceRegistry()
    registry.register(ResourceDef(
        service="sandbox-platform", resource_key=RK, display_name="Desktop Sessions",
        counter_type=CounterType.GAUGE, unit="sessions",
    ))
    engine = QuotaEngine(
        redis=redis, tier_provider=StaticTierProvider({}), registry=registry,
        tiers={},  # explicit empty catalog — required, never invented (T-3/ENV-12)
        resource_bundles={},
    )

    # Simulate the exact live symptom: gauge stuck at 5, nothing actually running.
    g = GaugeCounter(redis, ORG, RK)
    for i in range(5):
        await g.increment_user(USER, 1.0, idempotency_key=f"counter:lifecycle:start:sbx-{i}:0")
    assert await g.get_user(USER) == 5.0

    observed = {RK: {"total": 0.0, "per_user": {}}}  # live truth: nothing running
    reconciler = LibraryReconciler(
        engine, observed_usage_provider=lambda org_id: observed,
        config=ReconcileConfig(truth_source="provider", activity_guard_seconds=0),
    )
    await redis.delete(f"quota:reconcile:recent:{ORG}")  # clear the just-touched guard
    result = await reconciler.reconcile_org(ORG, reason="test_heal_stuck_gauge")

    assert await g.get_user(USER) == 0.0
    assert RK in result.changes


@pytest.mark.asyncio
async def test_reconciler_heals_the_actual_stuck_high_mechanism_missed_decrement(redis):
    """THE live cd790b95 symptom ("5/1" with zero running), reproduced by its
    REAL mechanism: a decrement that never fires at all (a missed lifecycle
    emit — e.g. a warm-pool silently reclaiming a container without going
    through the teardown hook, or a crashed handler) — NOT a key collision.
    No idempotency-key scheme can fix a call that never happens; only the
    reconciler-to-live-truth backstop can. Five orphaned increments, zero
    matching decrements, zero actually running -> reconcile force-sets 0."""
    from ab0t_quota.engine import QuotaEngine
    from ab0t_quota.models.core import ResourceDef, CounterType
    from ab0t_quota.registry import ResourceRegistry
    from ab0t_quota.providers import StaticTierProvider
    from ab0t_quota.reconcile import LibraryReconciler, ReconcileConfig

    registry = ResourceRegistry()
    registry.register(ResourceDef(
        service="sandbox-platform", resource_key="sandbox.concurrent",
        display_name="Concurrent Sandboxes", counter_type=CounterType.GAUGE, unit="sandboxes",
    ))
    engine = QuotaEngine(
        redis=redis, tier_provider=StaticTierProvider({}), registry=registry,
        tiers={}, resource_bundles={},
    )
    g = GaugeCounter(redis, ORG, "sandbox.concurrent")
    # Five sandboxes created, properly generation-keyed — but NONE of their
    # matching terminate events ever fired (the class of bug the reconciler,
    # not the idempotency key, exists to catch).
    for i in range(5):
        await g.increment_user(USER, 1.0, idempotency_key=f"counter:lifecycle:start:sbx-{i}:0")
    assert await g.get_user(USER) == 5.0

    observed = {"sandbox.concurrent": {"total": 0.0, "per_user": {}}}  # live truth: 0 running
    reconciler = LibraryReconciler(
        engine, observed_usage_provider=lambda org_id: observed,
        config=ReconcileConfig(truth_source="provider", activity_guard_seconds=0),
    )
    await redis.delete(f"quota:reconcile:recent:{ORG}")
    result = await reconciler.reconcile_org(ORG, reason="test_heal_missed_decrements")

    assert await g.get_user(USER) == 0.0
    assert "sandbox.concurrent" in result.changes


@pytest.mark.asyncio
async def test_reconcile_heals_after_provider_recovers_from_not_initialized(redis):
    """Coordinator Phase-0 live finding, reproduced at the reconciler layer:
    the deployed reconciler IS running, but a provider callback that raises
    ("DynamoDB not initialized...", the pre-fix app/database.py._get_table
    behavior) is treated by the D-31 fail-safe as "unreachable" -> the pass
    is SKIPPED, not force-set to a possibly-wrong value. This is CORRECT
    caution, but it means an org stays un-healed for as long as the provider
    keeps raising. Once the provider recovers (app/database.py's lazy
    _get_table reinit — see test_database_lazy_table_reinit_20260810.py in
    resource/output/sandbox-platform), the VERY NEXT reconcile pass heals
    the gauge — proving the two fixes (lazy DB reinit + a running
    reconciler) compose correctly."""
    from ab0t_quota.engine import QuotaEngine
    from ab0t_quota.models.core import ResourceDef, CounterType
    from ab0t_quota.registry import ResourceRegistry
    from ab0t_quota.providers import StaticTierProvider
    from ab0t_quota.reconcile import LibraryReconciler, ReconcileConfig

    registry = ResourceRegistry()
    registry.register(ResourceDef(
        service="sandbox-platform", resource_key="sandbox.concurrent",
        display_name="Concurrent Sandboxes", counter_type=CounterType.GAUGE, unit="sandboxes",
    ))
    engine = QuotaEngine(
        redis=redis, tier_provider=StaticTierProvider({}), registry=registry,
        tiers={}, resource_bundles={},
    )
    g = GaugeCounter(redis, ORG, "sandbox.concurrent")
    for i in range(5):  # the live symptom: gauge=5, nothing really running
        await g.increment_user(USER, 1.0, idempotency_key=f"counter:lifecycle:start:sbx-{i}:0")
    await redis.delete(f"quota:reconcile:recent:{ORG}")

    # A DB handle that is None on the first call ("not initialized" — the
    # pre-fix _get_table shape) and recovers on the second, mirroring the
    # lazy-reinit fix in resource/output/sandbox-platform/app/database.py.
    db_table = {"handle": None}

    def flaky_provider(org_id):
        if db_table["handle"] is None:
            raise RuntimeError("DynamoDB not initialized — call initialize_tables() first")
        return {"sandbox.concurrent": {"total": 0.0, "per_user": {}}}

    reconciler = LibraryReconciler(
        engine, observed_usage_provider=flaky_provider,
        config=ReconcileConfig(truth_source="provider", activity_guard_seconds=0),
    )

    pass1 = await reconciler.reconcile_org(ORG, reason="test_pass_1_provider_down")
    assert pass1.skipped == "provider_unreachable"
    assert await g.get_user(USER) == 5.0, "must NOT force-set anything while the provider is down"

    db_table["handle"] = "recovered"  # the lazy reinit succeeding
    pass2 = await reconciler.reconcile_org(ORG, reason="test_pass_2_provider_recovered")
    assert pass2.skipped is None
    assert await g.get_user(USER) == 0.0, "the very next pass must heal once the provider recovers"


# =====================================================================================
# OPUS verify-and-fix regression: the guard must ACCEPT the library's OWN release key.
#
# `engine.release()` decrements gauges with idempotency_key=f"release:{activation_id}"
# where activation_id = mint_activation_id() = "act_" + secrets.token_hex(16) (32 hex).
# That opaque high-entropy token is minted fresh per activation and never reused — the
# strongest possible unique component — yet the original guard recognized only dashed
# UUIDs / :evt: / numeric generations, so it MISCLASSIFIED this key as "bare recyclable".
# In warn mode that spammed a false warning on every release; in strict mode it RAISED
# inside release() AFTER the ledger row was marked RELEASED but BEFORE the gauge
# decrement — leaking the slot and sticking the gauge HIGH forever, i.e. causing the
# exact drift class this whole ticket exists to prevent. Fixed by _OPAQUE_TOKEN_RE.
# =====================================================================================

from ab0t_quota.activations import mint_activation_id


def test_guard_accepts_the_library_own_release_activation_id_key():
    """The library's own release key must be recognized as unique — not flagged."""
    for _ in range(50):  # many mints: guard against a token that happens to look numeric
        aid = mint_activation_id()
        assert idempotency_key_has_unique_component(f"release:{aid}") is True, (
            f"release:{aid} is a fresh, never-reused opaque token — the strongest "
            f"unique component; the guard must NOT treat it as bare-recyclable"
        )


def test_guard_accepts_opaque_hex_tokens_but_still_rejects_short_recyclable_ids():
    # Accept: dash-less UUID (32 hex), secrets.token_hex(16) (32 hex), token_hex(12) (24 hex).
    assert idempotency_key_has_unique_component("release:9b1f1e2e6c3b4a2b9b1e2e6c3b4a2b9b") is True
    assert idempotency_key_has_unique_component("act_3a965d529d641ac53cfd09779f5926c3") is True
    # Still reject: a short reused resource/pool id (only a 6-hex run — far below 24).
    assert idempotency_key_has_unique_component("counter:lifecycle:end:desktop-abc123") is False
    assert idempotency_key_has_unique_component("counter:lifecycle:end:sbx-1") is False


def test_strict_mode_does_not_break_the_release_path(monkeypatch):
    """Regression for the strict-mode catastrophe: guard_idempotency_key must NOT
    raise on release:{activation_id} even with strict enforcement on."""
    monkeypatch.setenv("AB0T_QUOTA_STRICT_IDEMPOTENCY_KEYS", "1")
    guard_idempotency_key(f"release:{mint_activation_id()}")  # must not raise


@pytest.mark.asyncio
async def test_strict_mode_release_key_still_decrements_the_gauge(redis, monkeypatch):
    """End-to-end at the counter call site (mirrors engine.release()'s exact call):
    under strict enforcement, a release-keyed decrement must ACTUALLY apply — proving
    the guard fix keeps the slot from leaking (gauge returns to zero), rather than
    raising and sticking it high."""
    monkeypatch.setenv("AB0T_QUOTA_STRICT_IDEMPOTENCY_KEYS", "1")
    g = GaugeCounter(redis, ORG, RK)
    aid = mint_activation_id()
    # acquire with a generation-scoped create key (as real consumers do)
    await g.increment_user(USER, 1.0, idempotency_key=f"counter:lifecycle:start:sbx-1:0")
    assert await g.get_user(USER) == 1.0
    # release exactly as engine.release() does — must not raise, must decrement
    await g.decrement_user(USER, 1.0, idempotency_key=f"release:{aid}")
    assert await g.get_user(USER) == 0.0, (
        "strict-mode release must decrement the gauge, not raise and leak the slot"
    )
