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
# Per-user sub-quota DERIVED from default_per_user_fraction
# ---------------------------------------------------------------------------

class TestDefaultPerUserFraction:
    """When a tier sets default_per_user_fraction, every GAUGE without an
    explicit per_user_limit gets one derived = ceil(limit * fraction)."""

    def _engine_with_fraction(self, redis, *, fraction):
        registry = ResourceRegistry()
        # Just one gauge resource at limit=10
        registry.register(ResourceDef(
            service="test", resource_key="sandbox.concurrent",
            display_name="Concurrent", counter_type=CounterType.GAUGE, unit="sandboxes",
        ))
        tiers = {
            "starter": TierConfig(
                tier_id="starter", display_name="Starter", sort_order=1,
                default_per_user_fraction=fraction,
                limits={"sandbox.concurrent": TierLimits(limit=10)},
            ),
        }
        provider = StaticTierProvider({"org-1": "starter"})
        return QuotaEngine(redis=redis, tier_provider=provider, registry=registry, tiers=tiers)

    @pytest.mark.asyncio
    async def test_fraction_caps_user_at_half_of_org(self, redis):
        """fraction=0.5, org limit=10 → each user capped at 5."""
        engine = self._engine_with_fraction(redis, fraction=0.5)
        for _ in range(5):
            await engine.increment(QuotaIncrementRequest(
                org_id="org-1", resource_key="sandbox.concurrent", delta=1, user_id="alice",
            ))
        # 6th alice request should fail at user level — not org (org is at 5/10)
        result = await engine.check(QuotaCheckRequest(
            org_id="org-1", resource_key="sandbox.concurrent", user_id="alice",
        ))
        assert result.denied is True
        assert result.denied_level == "user"
        assert result.user_current == 5
        assert result.user_limit == 5

    @pytest.mark.asyncio
    async def test_fraction_other_user_still_allowed(self, redis):
        """Alice maxed at 5/5 personal; Bob can still create up to his own 5."""
        engine = self._engine_with_fraction(redis, fraction=0.5)
        for _ in range(5):
            await engine.increment(QuotaIncrementRequest(
                org_id="org-1", resource_key="sandbox.concurrent", delta=1, user_id="alice",
            ))
        result = await engine.check(QuotaCheckRequest(
            org_id="org-1", resource_key="sandbox.concurrent", user_id="bob",
        ))
        assert result.allowed is True
        assert result.user_limit == 5
        assert result.user_current == 0

    @pytest.mark.asyncio
    async def test_explicit_per_user_overrides_fraction(self, redis):
        """If a TierLimits sets per_user_limit explicitly, fraction is ignored."""
        registry = ResourceRegistry()
        registry.register(ResourceDef(
            service="test", resource_key="sandbox.concurrent",
            display_name="Concurrent", counter_type=CounterType.GAUGE, unit="sandboxes",
        ))
        tiers = {
            "starter": TierConfig(
                tier_id="starter", display_name="Starter",
                default_per_user_fraction=0.5,  # would derive 5
                limits={"sandbox.concurrent": TierLimits(limit=10, per_user_limit=2)},  # explicit 2 wins
            ),
        }
        provider = StaticTierProvider({"org-1": "starter"})
        engine = QuotaEngine(redis=redis, tier_provider=provider, registry=registry, tiers=tiers)
        for _ in range(2):
            await engine.increment(QuotaIncrementRequest(
                org_id="org-1", resource_key="sandbox.concurrent", delta=1, user_id="alice",
            ))
        result = await engine.check(QuotaCheckRequest(
            org_id="org-1", resource_key="sandbox.concurrent", user_id="alice",
        ))
        assert result.denied is True
        assert result.denied_level == "user"
        assert result.user_limit == 2  # explicit, not derived 5

    @pytest.mark.asyncio
    async def test_no_fraction_means_no_per_user_enforcement(self, redis):
        """default_per_user_fraction=None means a single user can fill the org."""
        engine = self._engine_with_fraction(redis, fraction=None) if False else None
        # Build directly without fraction
        registry = ResourceRegistry()
        registry.register(ResourceDef(
            service="test", resource_key="sandbox.concurrent",
            display_name="Concurrent", counter_type=CounterType.GAUGE, unit="sandboxes",
        ))
        tiers = {
            "free": TierConfig(
                tier_id="free", display_name="Free",
                limits={"sandbox.concurrent": TierLimits(limit=10)},
            ),
        }
        provider = StaticTierProvider({"org-1": "free"})
        engine = QuotaEngine(redis=redis, tier_provider=provider, registry=registry, tiers=tiers)
        for _ in range(9):
            await engine.increment(QuotaIncrementRequest(
                org_id="org-1", resource_key="sandbox.concurrent", delta=1, user_id="alice",
            ))
        result = await engine.check(QuotaCheckRequest(
            org_id="org-1", resource_key="sandbox.concurrent", user_id="alice",
        ))
        assert result.allowed is True  # no per-user cap
        assert result.user_limit is None

    @pytest.mark.asyncio
    async def test_fraction_skipped_for_unlimited_tier(self, redis):
        """Enterprise (limit=None) shouldn't derive a per-user cap from fraction."""
        registry = ResourceRegistry()
        registry.register(ResourceDef(
            service="test", resource_key="sandbox.concurrent",
            display_name="Concurrent", counter_type=CounterType.GAUGE, unit="sandboxes",
        ))
        tiers = {
            "enterprise": TierConfig(
                tier_id="enterprise", display_name="Enterprise",
                default_per_user_fraction=0.5,  # ignored — limit is None
                limits={"sandbox.concurrent": TierLimits(limit=None)},
            ),
        }
        provider = StaticTierProvider({"org-1": "enterprise"})
        engine = QuotaEngine(redis=redis, tier_provider=provider, registry=registry, tiers=tiers)
        for _ in range(100):
            await engine.increment(QuotaIncrementRequest(
                org_id="org-1", resource_key="sandbox.concurrent", delta=1, user_id="alice",
            ))
        result = await engine.check(QuotaCheckRequest(
            org_id="org-1", resource_key="sandbox.concurrent", user_id="alice",
        ))
        assert result.allowed is True
        assert result.decision == QuotaDecision.UNLIMITED

    @pytest.mark.asyncio
    async def test_fraction_floors_at_one(self, redis):
        """ceil(1 * 0.1) would be 1; we never derive 0 because that blocks all users."""
        engine = self._engine_with_fraction(redis, fraction=0.1)
        # org limit=10, fraction=0.1 → derived per-user = ceil(1.0) = 1
        # So alice can take 1, then is denied
        await engine.increment(QuotaIncrementRequest(
            org_id="org-1", resource_key="sandbox.concurrent", delta=1, user_id="alice",
        ))
        result = await engine.check(QuotaCheckRequest(
            org_id="org-1", resource_key="sandbox.concurrent", user_id="alice",
        ))
        assert result.denied is True
        assert result.user_limit == 1


class TestResourceBundles:
    """check_for_bundle / increment_for_bundle / decrement_for_bundle.

    The bundle map is generic — bundle names mean whatever the consumer
    decides. These tests use neutral names ("single", "multi") to make
    that explicit.
    """

    def _engine_with_bundles(self, redis):
        registry = ResourceRegistry()
        registry.register(
            ResourceDef(service="x", resource_key="thing.a",
                        display_name="A", counter_type=CounterType.GAUGE, unit="x"),
            ResourceDef(service="x", resource_key="thing.b",
                        display_name="B", counter_type=CounterType.GAUGE, unit="x"),
            ResourceDef(service="x", resource_key="thing.cost",
                        display_name="Cost", counter_type=CounterType.ACCUMULATOR,
                        unit="USD", reset_period=ResetPeriod.MONTHLY),
        )
        tiers = {
            "free": TierConfig(
                tier_id="free", display_name="Free",
                limits={
                    "thing.a": TierLimits(limit=2),
                    "thing.b": TierLimits(limit=1),
                    "thing.cost": TierLimits(limit=10.0),
                },
            ),
        }
        provider = StaticTierProvider({"org-1": "free"})
        bundles = {
            "single": ["thing.a"],
            "multi":  ["thing.a", "thing.b"],
            "with_cost": ["thing.a", "thing.cost"],
        }
        return QuotaEngine(
            redis=redis, tier_provider=provider, registry=registry,
            tiers=tiers, resource_bundles=bundles,
        )

    @pytest.mark.asyncio
    async def test_single_resource_bundle_passes(self, redis):
        engine = self._engine_with_bundles(redis)
        result = await engine.check_for_bundle("org-1", "single")
        assert result.allowed is True
        assert len(result.results) == 1
        assert result.results[0].resource_key == "thing.a"

    @pytest.mark.asyncio
    async def test_multi_resource_bundle_batch_checks(self, redis):
        engine = self._engine_with_bundles(redis)
        result = await engine.check_for_bundle("org-1", "multi")
        assert result.allowed is True
        assert {r.resource_key for r in result.results} == {"thing.a", "thing.b"}

    @pytest.mark.asyncio
    async def test_bundle_denies_if_any_resource_at_limit(self, redis):
        """thing.b limit=1; fill it; multi-bundle check should deny."""
        engine = self._engine_with_bundles(redis)
        await engine.increment(QuotaIncrementRequest(
            org_id="org-1", resource_key="thing.b", delta=1,
        ))
        result = await engine.check_for_bundle("org-1", "multi")
        assert result.allowed is False
        assert "thing.b" in result.denied_resources
        assert "thing.a" not in result.denied_resources  # only b is at limit

    @pytest.mark.asyncio
    async def test_unknown_bundle_is_no_op_allow(self, redis):
        """Library doesn't know consumer-specific bundle names; unknown → allow."""
        engine = self._engine_with_bundles(redis)
        result = await engine.check_for_bundle("org-1", "definitely-not-declared")
        assert result.allowed is True
        assert result.results == []
        assert result.denied_resources == []

    @pytest.mark.asyncio
    async def test_increment_for_bundle_bumps_all(self, redis):
        engine = self._engine_with_bundles(redis)
        new_vals = await engine.increment_for_bundle("org-1", "multi")
        assert new_vals == {"thing.a": 1.0, "thing.b": 1.0}

    @pytest.mark.asyncio
    async def test_decrement_for_bundle_skips_non_gauges(self, redis):
        """Bundle with [gauge, accumulator] — decrement only touches the gauge."""
        engine = self._engine_with_bundles(redis)
        await engine.increment_for_bundle("org-1", "with_cost")
        # Only gauge incremented to 1; accumulator also incremented to 1.0
        # decrement should only touch the gauge
        new_vals = await engine.decrement_for_bundle("org-1", "with_cost")
        assert new_vals == {"thing.a": 0.0}  # only gauge decremented
        # Accumulator value unchanged from increment
        from ab0t_quota.counters.factory import create_counter
        from ab0t_quota.models.core import ResetPeriod as RP
        cost_def = ResourceDef(
            service="x", resource_key="thing.cost", display_name="Cost",
            counter_type=CounterType.ACCUMULATOR, unit="USD", reset_period=RP.MONTHLY,
        )
        counter = create_counter(redis, "org-1", cost_def)
        assert await counter.get() == 1.0  # not decremented

    @pytest.mark.asyncio
    async def test_increment_idempotency_namespaced_per_resource(self, redis):
        engine = self._engine_with_bundles(redis)
        # Call twice with the same idempotency_key — second call no-ops on each resource
        await engine.increment_for_bundle("org-1", "multi", idempotency_key="op-1")
        await engine.increment_for_bundle("org-1", "multi", idempotency_key="op-1")
        from ab0t_quota.counters.factory import create_counter
        a_def = ResourceDef(service="x", resource_key="thing.a", display_name="A",
                            counter_type=CounterType.GAUGE, unit="x")
        b_def = ResourceDef(service="x", resource_key="thing.b", display_name="B",
                            counter_type=CounterType.GAUGE, unit="x")
        a = create_counter(redis, "org-1", a_def)
        b = create_counter(redis, "org-1", b_def)
        assert await a.get() == 1.0
        assert await b.get() == 1.0

    @pytest.mark.asyncio
    async def test_set_resource_bundles_after_construction(self, redis):
        """setup_quota() needs to load bundles after the engine is built."""
        registry = ResourceRegistry()
        registry.register(ResourceDef(
            service="x", resource_key="thing.a", display_name="A",
            counter_type=CounterType.GAUGE, unit="x",
        ))
        engine = QuotaEngine(
            redis=redis,
            tier_provider=StaticTierProvider({"org-1": "free"}),
            registry=registry,
            tiers={"free": TierConfig(tier_id="free", display_name="Free",
                                       limits={"thing.a": TierLimits(limit=5)})},
        )
        assert engine.bundle_resources("foo") == []
        engine.set_resource_bundles({"foo": ["thing.a"]})
        assert engine.bundle_resources("foo") == ["thing.a"]
        result = await engine.check_for_bundle("org-1", "foo")
        assert result.allowed is True
        assert len(result.results) == 1


class TestTierConfigDerivation:
    """Unit-test TierConfig.derive_per_user_limit in isolation."""

    def test_explicit_wins(self):
        tier = TierConfig(tier_id="t", display_name="T", default_per_user_fraction=0.5)
        tl = TierLimits(limit=10, per_user_limit=3)
        assert tier.derive_per_user_limit(tl) == 3

    def test_derives_from_fraction(self):
        tier = TierConfig(tier_id="t", display_name="T", default_per_user_fraction=0.5)
        tl = TierLimits(limit=10)
        assert tier.derive_per_user_limit(tl) == 5

    def test_ceils_up(self):
        tier = TierConfig(tier_id="t", display_name="T", default_per_user_fraction=0.4)
        tl = TierLimits(limit=10)
        assert tier.derive_per_user_limit(tl) == 4
        tl2 = TierLimits(limit=11)
        assert tier.derive_per_user_limit(tl2) == 5  # ceil(4.4)

    def test_floors_at_one(self):
        tier = TierConfig(tier_id="t", display_name="T", default_per_user_fraction=0.1)
        tl = TierLimits(limit=1)
        assert tier.derive_per_user_limit(tl) == 1

    def test_returns_none_without_fraction(self):
        tier = TierConfig(tier_id="t", display_name="T")
        assert tier.derive_per_user_limit(TierLimits(limit=10)) is None

    def test_returns_none_for_unlimited(self):
        tier = TierConfig(tier_id="t", display_name="T", default_per_user_fraction=0.5)
        assert tier.derive_per_user_limit(TierLimits(limit=None)) is None

    def test_validates_fraction_bounds(self):
        # Pydantic should reject fraction <= 0 or > 1
        with pytest.raises(Exception):
            TierConfig(tier_id="t", display_name="T", default_per_user_fraction=0)
        with pytest.raises(Exception):
            TierConfig(tier_id="t", display_name="T", default_per_user_fraction=1.5)
        with pytest.raises(Exception):
            TierConfig(tier_id="t", display_name="T", default_per_user_fraction=-0.1)


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
