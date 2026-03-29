"""2.1.6 — Message builder tests."""

import pytest

from ab0t_quota.messages import MessageBuilder
from ab0t_quota.models.core import ResourceDef, CounterType, TierConfig, TierLimits


SANDBOX_DEF = ResourceDef(
    service="test", resource_key="sandbox.concurrent",
    display_name="Concurrent Sandboxes", counter_type=CounterType.GAUGE,
    unit="sandboxes",
)
GPU_DEF = ResourceDef(
    service="test", resource_key="sandbox.gpu_instances",
    display_name="GPU Instances", counter_type=CounterType.GAUGE,
    unit="instances",
)
COST_DEF = ResourceDef(
    service="test", resource_key="sandbox.monthly_cost",
    display_name="Monthly Compute Spend", counter_type=CounterType.ACCUMULATOR,
    unit="USD", reset_period="monthly", precision=2,
)

FREE_TIER = TierConfig(
    tier_id="free", display_name="Free", sort_order=0,
    features=set(), upgrade_url="/upgrade",
    limits={
        "sandbox.concurrent": TierLimits(limit=2),
        "sandbox.gpu_instances": TierLimits(limit=0),
        "sandbox.monthly_cost": TierLimits(limit=10),
    },
)


class TestDenyMessages:
    def test_no_technical_jargon(self):
        msg = MessageBuilder.deny(SANDBOX_DEF, FREE_TIER, current=2, limit=2, requested=1)
        assert "quota" not in msg.lower()
        assert "sandbox.concurrent" not in msg
        assert "error" not in msg.lower()

    def test_includes_limit_and_tier(self):
        msg = MessageBuilder.deny(SANDBOX_DEF, FREE_TIER, current=2, limit=2, requested=1)
        assert "2" in msg
        assert "Free" in msg

    def test_includes_action_hint(self):
        msg = MessageBuilder.deny(SANDBOX_DEF, FREE_TIER, current=2, limit=2, requested=1)
        assert "stop" in msg.lower() or "free up" in msg.lower()

    def test_includes_upgrade_hint(self):
        msg = MessageBuilder.deny(SANDBOX_DEF, FREE_TIER, current=2, limit=2, requested=1)
        assert "upgrade" in msg.lower() or "Starter" in msg

    def test_zero_limit_feature_locked(self):
        msg = MessageBuilder.deny(GPU_DEF, FREE_TIER, current=0, limit=0, requested=1)
        assert "not available" in msg.lower()
        assert "Free" in msg

    def test_cost_limit(self):
        msg = MessageBuilder.deny(COST_DEF, FREE_TIER, current=10, limit=10, requested=5)
        assert "10" in msg
        assert "spending" in msg.lower() or "limit" in msg.lower()


class TestWarningMessages:
    def test_80_percent(self):
        msg = MessageBuilder.warning(SANDBOX_DEF, FREE_TIER, current=1, limit=2, after=1.6)
        assert "80%" in msg or "using" in msg.lower()
        assert "upgrad" in msg.lower()  # "upgrading" or "upgrade"

    def test_95_percent(self):
        msg = MessageBuilder.warning(SANDBOX_DEF, FREE_TIER, current=1, limit=2, after=1.95)
        assert "almost" in msg.lower() or "97%" in msg
        assert "blocked" in msg.lower() or "limit" in msg.lower()


class TestAllowMessages:
    def test_under_limit(self):
        msg = MessageBuilder.allow(SANDBOX_DEF, current=1, limit=5, after=2)
        assert "2" in msg and "5" in msg

    def test_unlimited(self):
        msg = MessageBuilder.allow(SANDBOX_DEF, current=1, limit=None, after=2)
        assert "unlimited" in msg.lower()


class TestBurstMessages:
    def test_burst_zone(self):
        msg = MessageBuilder.burst(SANDBOX_DEF, FREE_TIER, current=5, limit=5, after=6)
        assert "over" in msg.lower()
        assert "burst" in msg.lower()
        assert "overage" in msg.lower()


class TestFeatureLocked:
    def test_with_next_tier(self):
        msg = MessageBuilder.feature_locked("gpu_access", FREE_TIER)
        assert "not available" in msg.lower()
        assert "Free" in msg
        assert "upgrade" in msg.lower()
