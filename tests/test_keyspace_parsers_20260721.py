"""K-4 — the two parsers that go blind on v2 keys (spec §7 rows 6-7, V6).

RED today: reconcile provider-mode enumeration (reconcile.py) and the snapshot
worker's `_parse_quota_key` (persistence.py) parse v2 keys as garbage/None —
reconciliation and recovery snapshots silently stop at the flip.
Environment: fakeredis[lua], mocked DDB table (no real infra).
"""
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
import pytest_asyncio

from ab0t_quota.keyspace import Keyspace
from ab0t_quota.models.core import CounterType, ResourceDef, TierConfig, TierLimits
from ab0t_quota.persistence import QuotaStore
from ab0t_quota.providers import StaticTierProvider
from ab0t_quota.registry import ResourceRegistry

SVC = "sandbox-platform"
ORG = "org-1"
RK = "sandbox.concurrent"
TAG = "{" + f"{SVC}/{ORG}" + "}"
KS2 = Keyspace(service=SVC, version=2)

GAUGE = ResourceDef(service=SVC, resource_key=RK, display_name="sb",
                    counter_type=CounterType.GAUGE, unit="sandboxes")


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.flushall()
    await r.aclose()


def _engine(redis, ks):
    from ab0t_quota.engine import QuotaEngine
    reg = ResourceRegistry()
    reg.register(GAUGE)
    tiers = {"free": TierConfig(tier_id="free", display_name="Free",
                                limits={RK: TierLimits(limit=10)})}
    return QuotaEngine(redis=redis, tier_provider=StaticTierProvider(),
                       registry=reg, tiers=tiers, keyspace=ks)


# ------------------------------------------------------ persistence parser

def test_parse_quota_key_v2_shapes():
    p = QuotaStore._parse_quota_key
    assert p(f"quota:v2:{TAG}:{RK}:gauge") == (ORG, RK, "gauge")
    assert p(f"quota:v2:{TAG}:{RK}:gauge:user:u9") == (ORG, RK, "user")
    assert p(f"quota:v2:{TAG}:{RK}:acc:2026-07") == (ORG, RK, "acc")
    # non-counter v2 keys stay excluded
    assert p(f"quota:v2:{TAG}:{RK}:idem:k") is None
    assert p(f"quota:v2:{TAG}:{RK}:gauge:seq:user:u9") is None
    assert p(f"quota:v2:{TAG}:reconcile:recent") is None


def test_parse_never_misattributes_versionish_org():
    """Planted offender (spec V6): a v1-shaped key whose org segment is 'v2'
    can only come from an out-of-repo writer (the charset guard refuses
    creating it) — it must be REFUSED, not misparsed as org='v2'."""
    assert QuotaStore._parse_quota_key("quota:v2:some.rk:gauge") is None


def test_parse_v1_still_works():
    p = QuotaStore._parse_quota_key
    assert p(f"quota:{ORG}:{RK}:gauge") == (ORG, RK, "gauge")
    assert p(f"quota:{ORG}:{RK}:gauge:user:u9") == (ORG, RK, "user")
    assert p(f"quota:{ORG}:{RK}:acc:2026-07") == (ORG, RK, "acc")
    assert p(f"quota:{ORG}:{RK}:idem:k") is None


@pytest.mark.asyncio
async def test_snapshot_all_sees_v2_gauge(redis):
    """A v2-only gauge must be snapshotted — today the SCAN parses it as None
    and the recovery cache silently stops at the flip."""
    await redis.set(f"quota:v2:{TAG}:{RK}:gauge", "5")
    reg = ResourceRegistry()
    reg.register(GAUGE)
    store = QuotaStore(table_name="test", region="us-east-1")
    store.snapshot_counter = AsyncMock()
    n = await store.snapshot_all(redis, reg)
    assert n == 1, "v2 gauge was not snapshotted — recovery cache blind on v2"
    args = store.snapshot_counter.call_args
    assert args.args[0] == ORG and args.args[1] == RK and args.args[2] == 5.0
    # the v2 row must be service-scoped so two services' same rk cannot collide
    assert args.kwargs.get("service") == SVC


# ------------------------------------------------------ reconcile enumeration

@pytest.mark.asyncio
async def test_provider_enumeration_finds_v2_gauge(redis):
    from ab0t_quota.reconcile import LibraryReconciler, ReconcileConfig
    await redis.set(f"quota:v2:{TAG}:{RK}:gauge", "3")
    rec = LibraryReconciler(
        _engine(redis, KS2),
        observed_usage_provider=lambda org: {},
        config=ReconcileConfig(truth_source="provider"),
    )
    orgs = await rec._enumerate_orgs()
    assert orgs == [ORG], f"v2 gauge invisible to provider-mode enumeration: {orgs}"


@pytest.mark.asyncio
async def test_provider_enumeration_filters_foreign_service(redis):
    """Bridge shape: another service's v2 gauge for the same org must NOT be
    enumerated by this service's reconciler (cross-tenant independence)."""
    from ab0t_quota.reconcile import LibraryReconciler, ReconcileConfig
    other = "{" + f"other-svc/{ORG}" + "}"
    await redis.set(f"quota:v2:{other}:{RK}:gauge", "3")
    rec = LibraryReconciler(
        _engine(redis, KS2),
        observed_usage_provider=lambda org: {},
        config=ReconcileConfig(truth_source="provider"),
    )
    assert await rec._enumerate_orgs() == []


@pytest.mark.asyncio
async def test_recent_activity_guard_not_blind_on_v2(redis):
    """(2,false): the engine marks activity under the v2 shape only. A guard
    still reading the v1 literal treats in-flight traffic as 'not recent' and
    force-sets against it — the guard must read the engine's shape."""
    from ab0t_quota.reconcile import LibraryReconciler, ReconcileConfig
    eng = _engine(redis, KS2)
    await eng._mark_gauge_activity(ORG)
    rec = LibraryReconciler(
        eng, observed_usage_provider=lambda org: {},
        config=ReconcileConfig(truth_source="provider"),
    )
    assert await rec._is_recent_activity(ORG, []) is True


@pytest.mark.asyncio
async def test_recent_activity_guard_dual_reads_both(redis):
    """During dual the guard must also see a marker set by a pre-flip (v1)
    replica — mixed fleets are the normal case (spec §5)."""
    from ab0t_quota.reconcile import LibraryReconciler, ReconcileConfig
    await redis.set(f"quota:reconcile:recent:{ORG}", "1", ex=90)
    rec = LibraryReconciler(
        _engine(redis, Keyspace(service=SVC, version=2, dual_write=True)),
        observed_usage_provider=lambda org: {},
        config=ReconcileConfig(truth_source="provider"),
    )
    assert await rec._is_recent_activity(ORG, []) is True
