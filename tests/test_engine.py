"""2.1.3 — Engine tests."""

import pytest
import pytest_asyncio
import fakeredis.aioredis
from datetime import datetime, timedelta, timezone

from ab0t_quota.engine import QuotaEngine
from ab0t_quota.models.core import (
    ResourceDef, CounterType, TierConfig, TierLimits, QuotaOverride,
    AlertSeverity, ResetPeriod,
)
from ab0t_quota.models.requests import (
    QuotaCheckRequest, QuotaIncrementRequest, QuotaDecrementRequest,
    QuotaBatchCheckRequest, QuotaResetRequest,
)
from ab0t_quota.models.responses import QuotaDecision
from ab0t_quota.registry import ResourceRegistry
from ab0t_quota.providers import StaticTierProvider


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TIERS = {
    "free": TierConfig(
        tier_id="free", display_name="Free", sort_order=0,
        features={"basic"},
        upgrade_url="/upgrade",
        limits={
            "sandbox.concurrent": TierLimits(limit=2),
            "sandbox.monthly_cost": TierLimits(limit=10.00),
            "sandbox.gpu": TierLimits(limit=0),
            "api.requests": TierLimits(limit=100),
            "sandbox.with_burst": TierLimits(limit=5, burst_allowance=2),
            "sandbox.per_user": TierLimits(limit=6, per_user_limit=3),
        },
    ),
    "pro": TierConfig(
        tier_id="pro", display_name="Pro", sort_order=2,
        features={"basic", "gpu_access", "priority_support"},
        limits={
            "sandbox.concurrent": TierLimits(limit=25),
            "sandbox.monthly_cost": TierLimits(limit=1000.00),
            "sandbox.gpu": TierLimits(limit=5),
            "api.requests": TierLimits(limit=50000),
        },
    ),
}

RESOURCES = [
    ResourceDef(service="test", resource_key="sandbox.concurrent", display_name="Concurrent Sandboxes", counter_type=CounterType.GAUGE, unit="sandboxes"),
    ResourceDef(service="test", resource_key="sandbox.monthly_cost", display_name="Monthly Spend", counter_type=CounterType.ACCUMULATOR, unit="USD", reset_period=ResetPeriod.MONTHLY, precision=2),
    ResourceDef(service="test", resource_key="sandbox.gpu", display_name="GPU Instances", counter_type=CounterType.GAUGE, unit="instances"),
    ResourceDef(service="test", resource_key="api.requests", display_name="API Requests/Hr", counter_type=CounterType.RATE, unit="requests", window_seconds=3600),
    ResourceDef(service="test", resource_key="sandbox.with_burst", display_name="Burst Test", counter_type=CounterType.GAUGE, unit="items"),
    ResourceDef(service="test", resource_key="sandbox.per_user", display_name="Per-User Test", counter_type=CounterType.GAUGE, unit="items"),
]


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.flushall()
    await r.aclose()


@pytest_asyncio.fixture
async def engine(redis):
    registry = ResourceRegistry()
    registry.register(*RESOURCES)
    provider = StaticTierProvider({"org-free": "free", "org-pro": "pro"})
    return QuotaEngine(redis=redis, tier_provider=provider, registry=registry, tiers=TIERS)


# ---------------------------------------------------------------------------
# check()
# ---------------------------------------------------------------------------

class TestCheck:
    @pytest.mark.asyncio
    async def test_allow(self, engine):
        result = await engine.check(QuotaCheckRequest(org_id="org-free", resource_key="sandbox.concurrent"))
        assert result.decision == QuotaDecision.ALLOW
        assert result.allowed is True
        assert result.current == 0
        assert result.limit == 2

    @pytest.mark.asyncio
    async def test_deny_at_limit(self, engine):
        # Fill to limit
        await engine.increment(QuotaIncrementRequest(org_id="org-free", resource_key="sandbox.concurrent", delta=2))
        result = await engine.check(QuotaCheckRequest(org_id="org-free", resource_key="sandbox.concurrent"))
        assert result.decision == QuotaDecision.DENY
        assert result.denied is True
        assert result.current == 2
        assert result.limit == 2

    @pytest.mark.asyncio
    async def test_deny_zero_limit(self, engine):
        """GPU limit=0 on free tier → deny with feature-locked message."""
        result = await engine.check(QuotaCheckRequest(org_id="org-free", resource_key="sandbox.gpu"))
        assert result.denied is True
        assert "not available" in result.message.lower()

    @pytest.mark.asyncio
    async def test_unlimited(self, engine):
        """Pro tier has no limit on sandbox.with_burst (not in pro tier limits → unlimited)."""
        result = await engine.check(QuotaCheckRequest(org_id="org-pro", resource_key="sandbox.concurrent"))
        assert result.allowed is True
        assert result.limit == 25

    @pytest.mark.asyncio
    async def test_warning_at_threshold(self, engine):
        """At 80%+ → ALLOW_WARNING."""
        # 2 limit, put 1 in → 50%, then check with 1 more → would be 100%
        await engine.increment(QuotaIncrementRequest(org_id="org-free", resource_key="sandbox.concurrent", delta=1))
        result = await engine.check(QuotaCheckRequest(org_id="org-free", resource_key="sandbox.concurrent"))
        # After would be 2/2 = 100% → CRITICAL
        assert result.decision == QuotaDecision.ALLOW_WARNING
        assert result.severity == AlertSeverity.CRITICAL

    @pytest.mark.asyncio
    async def test_burst_allowance(self, engine):
        """sandbox.with_burst: limit=5, burst=2. At 5, allow with warning. At 8, deny."""
        await engine.increment(QuotaIncrementRequest(org_id="org-free", resource_key="sandbox.with_burst", delta=5))
        # At limit but within burst
        result = await engine.check(QuotaCheckRequest(org_id="org-free", resource_key="sandbox.with_burst"))
        assert result.decision == QuotaDecision.ALLOW_WARNING
        assert result.severity == AlertSeverity.CRITICAL
        assert "burst" in result.message.lower()

        # Over burst
        await engine.increment(QuotaIncrementRequest(org_id="org-free", resource_key="sandbox.with_burst", delta=2))
        result = await engine.check(QuotaCheckRequest(org_id="org-free", resource_key="sandbox.with_burst"))
        assert result.denied is True


# ---------------------------------------------------------------------------
# Override
# ---------------------------------------------------------------------------

class TestOverride:
    @pytest.mark.asyncio
    async def test_override_increases_limit(self, redis):
        registry = ResourceRegistry()
        registry.register(*RESOURCES)
        provider = StaticTierProvider({"org-1": "free"})

        override = QuotaOverride(org_id="org-1", resource_key="sandbox.concurrent", limit=10, reason="VIP")

        async def load_override(org_id, resource_key):
            if org_id == "org-1" and resource_key == "sandbox.concurrent":
                return override
            return None

        engine = QuotaEngine(redis=redis, tier_provider=provider, registry=registry, tiers=TIERS, override_loader=load_override)

        await engine.increment(QuotaIncrementRequest(org_id="org-1", resource_key="sandbox.concurrent", delta=5))
        result = await engine.check(QuotaCheckRequest(org_id="org-1", resource_key="sandbox.concurrent"))
        assert result.allowed is True  # tier limit is 2 but override is 10
        assert result.limit == 10
        assert result.has_override is True

    @pytest.mark.asyncio
    async def test_expired_override_falls_back(self, redis):
        registry = ResourceRegistry()
        registry.register(*RESOURCES)
        provider = StaticTierProvider({"org-1": "free"})

        override = QuotaOverride(
            org_id="org-1", resource_key="sandbox.concurrent", limit=10,
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),  # expired
        )

        async def load_override(org_id, resource_key):
            return override

        engine = QuotaEngine(redis=redis, tier_provider=provider, registry=registry, tiers=TIERS, override_loader=load_override)

        await engine.increment(QuotaIncrementRequest(org_id="org-1", resource_key="sandbox.concurrent", delta=2))
        result = await engine.check(QuotaCheckRequest(org_id="org-1", resource_key="sandbox.concurrent"))
        assert result.denied is True  # falls back to tier limit of 2
        assert result.limit == 2


# ---------------------------------------------------------------------------
# Per-user sub-quota
# ---------------------------------------------------------------------------

class TestPerUser:
    @pytest.mark.asyncio
    async def test_per_user_deny(self, engine):
        """sandbox.per_user: org limit=6, per_user=3. User fills 3 → denied."""
        for _ in range(3):
            await engine.increment(QuotaIncrementRequest(
                org_id="org-free", resource_key="sandbox.per_user", delta=1, user_id="alice",
            ))
        result = await engine.check(QuotaCheckRequest(
            org_id="org-free", resource_key="sandbox.per_user", user_id="alice",
        ))
        assert result.denied is True
        assert result.denied_level == "user"
        assert result.user_current == 3
        assert result.user_limit == 3

    @pytest.mark.asyncio
    async def test_per_user_other_user_allowed(self, engine):
        """Alice at limit but Bob can still create."""
        for _ in range(3):
            await engine.increment(QuotaIncrementRequest(
                org_id="org-free", resource_key="sandbox.per_user", delta=1, user_id="alice",
            ))
        result = await engine.check(QuotaCheckRequest(
            org_id="org-free", resource_key="sandbox.per_user", user_id="bob",
        ))
        assert result.allowed is True
        assert result.user_current == 0

    @pytest.mark.asyncio
    async def test_org_level_still_enforced(self, engine):
        """Even though per-user limit not hit, org limit denies."""
        # 3 each for alice and bob = 6 total = at org limit
        for _ in range(3):
            await engine.increment(QuotaIncrementRequest(
                org_id="org-free", resource_key="sandbox.per_user", delta=1, user_id="alice",
            ))
        for _ in range(3):
            await engine.increment(QuotaIncrementRequest(
                org_id="org-free", resource_key="sandbox.per_user", delta=1, user_id="bob",
            ))
        # Charlie has 0 personal but org is at 6/6
        result = await engine.check(QuotaCheckRequest(
            org_id="org-free", resource_key="sandbox.per_user", user_id="charlie",
        ))
        assert result.denied is True
        assert result.denied_level == "org"


# ---------------------------------------------------------------------------
# batch_check()
# ---------------------------------------------------------------------------

class TestBatchCheck:
    @pytest.mark.asyncio
    async def test_all_pass(self, engine):
        from ab0t_quota.models.requests import QuotaCheckItem
        result = await engine.batch_check(QuotaBatchCheckRequest(
            org_id="org-free",
            checks=[
                QuotaCheckItem(resource_key="sandbox.concurrent"),
                QuotaCheckItem(resource_key="sandbox.with_burst"),
            ],
        ))
        assert result.allowed is True
        assert len(result.denied_resources) == 0

    @pytest.mark.asyncio
    async def test_one_fails(self, engine):
        from ab0t_quota.models.requests import QuotaCheckItem
        await engine.increment(QuotaIncrementRequest(org_id="org-free", resource_key="sandbox.concurrent", delta=2))
        result = await engine.batch_check(QuotaBatchCheckRequest(
            org_id="org-free",
            checks=[
                QuotaCheckItem(resource_key="sandbox.concurrent"),  # at limit → deny
                QuotaCheckItem(resource_key="sandbox.with_burst"),  # fine
            ],
        ))
        assert result.allowed is False
        assert "sandbox.concurrent" in result.denied_resources


# ---------------------------------------------------------------------------
# increment / decrement
# ---------------------------------------------------------------------------

class TestIncrementDecrement:
    @pytest.mark.asyncio
    async def test_increment_updates(self, engine):
        val = await engine.increment(QuotaIncrementRequest(org_id="org-free", resource_key="sandbox.concurrent", delta=1))
        assert val == 1.0

    @pytest.mark.asyncio
    async def test_decrement_updates(self, engine):
        await engine.increment(QuotaIncrementRequest(org_id="org-free", resource_key="sandbox.concurrent", delta=3))
        val = await engine.decrement(QuotaDecrementRequest(org_id="org-free", resource_key="sandbox.concurrent", delta=1))
        assert val == 2.0

    @pytest.mark.asyncio
    async def test_decrement_non_gauge_raises(self, engine):
        with pytest.raises(TypeError):
            await engine.decrement(QuotaDecrementRequest(org_id="org-free", resource_key="sandbox.monthly_cost", delta=1))

    @pytest.mark.asyncio
    async def test_reset(self, engine):
        await engine.increment(QuotaIncrementRequest(org_id="org-free", resource_key="sandbox.concurrent", delta=5))
        await engine.reset(QuotaResetRequest(org_id="org-free", resource_key="sandbox.concurrent", new_value=1, reason="drift fix", admin_user_id="admin-1"))
        result = await engine.check(QuotaCheckRequest(org_id="org-free", resource_key="sandbox.concurrent"))
        assert result.current == 1.0

    @pytest.mark.asyncio
    async def test_reset_requires_admin_user_id(self):
        """M2: admin_user_id is mandatory on QuotaResetRequest."""
        from pydantic import ValidationError
        with pytest.raises(ValidationError, match="admin_user_id"):
            QuotaResetRequest(org_id="org-1", resource_key="sandbox.concurrent", new_value=0, reason="test")


# ---------------------------------------------------------------------------
# get_usage()
# ---------------------------------------------------------------------------

class TestGetUsage:
    @pytest.mark.asyncio
    async def test_returns_all_resources(self, engine):
        await engine.increment(QuotaIncrementRequest(org_id="org-free", resource_key="sandbox.concurrent", delta=1))
        usage = await engine.get_usage("org-free")
        assert usage.org_id == "org-free"
        assert usage.tier_id == "free"
        assert len(usage.resources) == len(RESOURCES)
        sandbox_item = next(r for r in usage.resources if r.resource_key == "sandbox.concurrent")
        assert sandbox_item.current == 1
        assert sandbox_item.limit == 2


# ---------------------------------------------------------------------------
# Feature gating
# ---------------------------------------------------------------------------

class TestFeatureGating:
    @pytest.mark.asyncio
    async def test_free_has_basic(self, engine):
        assert await engine.check_feature("org-free", "basic") is True

    @pytest.mark.asyncio
    async def test_free_lacks_gpu(self, engine):
        assert await engine.check_feature("org-free", "gpu_access") is False

    @pytest.mark.asyncio
    async def test_pro_has_gpu(self, engine):
        assert await engine.check_feature("org-pro", "gpu_access") is True
