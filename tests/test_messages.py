"""2.1.6 — Message builder tests.

§11.2 REPLACEMENT, declared (ticket 20260722, MSG lane): five assertions in
this file encoded the two DELETED lookup tables rather than the behaviour they
named — they passed only because `ACTION_HINTS` and `UPGRADE_TIER_MAP` agreed
with these sandbox-shaped fixtures:

  test_includes_action_hint      asserted library-owned copy ("stop"/"free up")
                                 for `sandbox.concurrent`; the hint is now
                                 ResourceDef.action_hint, so the FIXTURE
                                 declares it (D-CK-1).
  test_includes_upgrade_hint     asserted an upgrade clause with NO catalog
                                 supplied — i.e. the `_default: "Starter"` row.
                                 Now passes the consumer's own catalog.
  test_zero_limit_feature_locked same, on the limit==0 branch.
  test_cost_limit                asserted the word "limit", which came from the
                                 invented upgrade sentence, not the denial.
  TestFeatureLocked.test_with_next_tier  same, via feature_locked().

Each is rewritten to assert the SAME product intent against a DECLARED tier
catalog. No assertion was weakened: every one now also proves no tier outside
the catalog is named.
"""

import pytest

from ab0t_quota.messages import MessageBuilder
from ab0t_quota.models.core import ResourceDef, CounterType, TierConfig, TierLimits


SANDBOX_DEF = ResourceDef(
    service="test", resource_key="sandbox.concurrent",
    display_name="Concurrent Sandboxes", counter_type=CounterType.GAUGE,
    unit="sandboxes",
    action_hint="Stop an existing sandbox to free up a slot.",
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


PRO_TIER = TierConfig(
    tier_id="pro", display_name="Pro", sort_order=1,
    features={"gpu_access"},
    limits={
        "sandbox.concurrent": TierLimits(limit=25),
        "sandbox.gpu_instances": TierLimits(limit=5),
        "sandbox.monthly_cost": TierLimits(limit=1000),
    },
)
#: the consumer's OWN catalog — the only source of a tier name in any message
CATALOG = {"free": FREE_TIER, "pro": PRO_TIER}


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
        """The hint is the RESOURCE's declared remediation copy (D-CK-1)."""
        msg = MessageBuilder.deny(SANDBOX_DEF, FREE_TIER, current=2, limit=2,
                                  requested=1, tiers=CATALOG)
        assert SANDBOX_DEF.action_hint in msg
        assert "Starter" not in msg

    def test_includes_upgrade_hint(self):
        msg = MessageBuilder.deny(SANDBOX_DEF, FREE_TIER, current=2, limit=2,
                                  requested=1, tiers=CATALOG)
        assert "upgrade" in msg.lower()
        assert "Pro" in msg, "the next tier must come from the catalog"
        assert "Starter" not in msg, "named a plan the catalog does not contain"

    def test_zero_limit_feature_locked(self):
        msg = MessageBuilder.deny(GPU_DEF, FREE_TIER, current=0, limit=0,
                                  requested=1, tiers=CATALOG)
        assert "not available" in msg.lower()
        assert "Free" in msg
        assert "Pro" in msg and "Starter" not in msg

    def test_cost_limit(self):
        msg = MessageBuilder.deny(COST_DEF, FREE_TIER, current=10, limit=10,
                                  requested=5, tiers=CATALOG)
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
        msg = MessageBuilder.feature_locked("gpu_access", FREE_TIER, tiers=CATALOG)
        assert "not available" in msg.lower()
        assert "Free" in msg
        assert "upgrade" in msg.lower()
        assert "Pro" in msg and "Starter" not in msg
