"""Tests for ab0t_quota.config loaders."""

from decimal import Decimal

from ab0t_quota.config import load_resource_bundles, load_tiers
from ab0t_quota.models.core import (
    BillingModel,
    CreditDestination,
    CreditLifecycle,
    CreditTrigger,
)


class TestLoadResourceBundles:
    def test_returns_empty_when_missing(self):
        assert load_resource_bundles({}) == {}
        assert load_resource_bundles(None) == {}
        assert load_resource_bundles({"other_key": 1}) == {}

    def test_loads_simple_map(self):
        cfg = {"resource_bundles": {
            "single": ["thing.a"],
            "multi":  ["thing.a", "thing.b"],
        }}
        bundles = load_resource_bundles(cfg)
        assert bundles == {"single": ["thing.a"], "multi": ["thing.a", "thing.b"]}

    def test_skips_invalid_entries_keeps_valid_ones(self):
        cfg = {"resource_bundles": {
            "good":      ["thing.a"],
            "not_list":  "thing.a",       # wrong shape — string instead of list
            "non_str":   ["thing.a", 123], # non-string element
        }}
        bundles = load_resource_bundles(cfg)
        assert bundles == {"good": ["thing.a"]}

    def test_rejects_non_object_root(self):
        cfg = {"resource_bundles": ["this", "should", "be", "a", "dict"]}
        assert load_resource_bundles(cfg) == {}

    def test_returns_independent_lists(self):
        """Mutating the loaded dict shouldn't affect the source config."""
        src = {"resource_bundles": {"x": ["thing.a"]}}
        bundles = load_resource_bundles(src)
        bundles["x"].append("thing.b")
        assert src["resource_bundles"]["x"] == ["thing.a"]


class TestLoadTiersBillingFields:
    """Regression tests for T0a — load_tiers() must preserve the billing
    fields added under the parent ticket (20260516_paid_plan_balance_model_gap).

    Before T0a, load_tiers() silently dropped billing_model / price /
    credit_grant / initial_credit, so the runtime tier_registry was missing
    the policy that handle_subscription_invoice_paid() needs to fire grants.
    These tests pin that the fields survive the loader.

    Ticket: 20260516_auto_credit_invoice_paid_wiring (T0a / AC0a).
    """

    def _bare_tier(self, **overrides):
        """Minimal valid tier dict; tests override the fields they care about."""
        base = {
            "tier_id": "test_tier",
            "display_name": "Test",
            "limits": {},
        }
        base.update(overrides)
        return base

    def test_billing_model_defaults_to_capacity_only_when_absent(self):
        """Backward-compat: consumers that don't declare billing_model get
        the safe default (no money side-effects)."""
        cfg = {"tiers": [self._bare_tier()]}
        tiers = load_tiers(cfg)
        assert tiers["test_tier"].billing_model == BillingModel.CAPACITY_ONLY
        assert tiers["test_tier"].price is None
        assert tiers["test_tier"].credit_grant is None

    def test_billing_model_subscription_with_credits_survives(self):
        """Paid-subscription tier with full credit_grant block preserves
        all the policy: model + price + grant trigger + lifecycle + destination."""
        cfg = {"tiers": [self._bare_tier(
            tier_id="starter",
            sort_order=1,
            billing_model="subscription_with_credits",
            price={"amount_per_period": 19.00, "currency": "USD", "period": "month"},
            credit_grant={
                "trigger": "subscription_invoice_paid",
                "amount_per_period": 19.00,
                "currency": "USD",
                "lifecycle": "use_it_or_lose_it",
                "destination": "subscription_credit",
                "reset_on_downgrade": True,
                "reset_on_upgrade": False,
            },
        )]}
        tier = load_tiers(cfg)["starter"]
        assert tier.billing_model == BillingModel.SUBSCRIPTION_WITH_CREDITS
        assert tier.price is not None
        assert tier.price.amount_per_period == Decimal("19.00")
        assert tier.credit_grant is not None
        assert tier.credit_grant.trigger == CreditTrigger.SUBSCRIPTION_INVOICE_PAID
        assert tier.credit_grant.amount_per_period == Decimal("19.00")
        assert tier.credit_grant.lifecycle == CreditLifecycle.USE_IT_OR_LOSE_IT
        assert tier.credit_grant.destination == CreditDestination.SUBSCRIPTION_CREDIT
        assert tier.credit_grant.reset_on_downgrade is True
        assert tier.credit_grant.reset_on_upgrade is False

    def test_rollover_capped_preserves_rollover_max_periods(self):
        """Enterprise-style tier with rollover_capped lifecycle must keep
        rollover_max_periods — the cap is the whole point of this lifecycle."""
        cfg = {"tiers": [self._bare_tier(
            tier_id="enterprise",
            sort_order=3,
            billing_model="subscription_with_credits",
            price={"amount_per_period": 200.00, "currency": "USD", "period": "month"},
            credit_grant={
                "trigger": "subscription_invoice_paid",
                "amount_per_period": 200.00,
                "currency": "USD",
                "lifecycle": "rollover_capped",
                "rollover_max_periods": 3,
                "destination": "subscription_credit",
            },
        )]}
        tier = load_tiers(cfg)["enterprise"]
        assert tier.credit_grant.lifecycle == CreditLifecycle.ROLLOVER_CAPPED
        assert tier.credit_grant.rollover_max_periods == 3

    def test_initial_credit_back_compat_synthesizes_signup_grant(self):
        """Free-tier legacy shape — initial_credit alone — is synthesized
        into a CreditGrant by the TierConfig validator. The loader must
        preserve initial_credit so the validator sees it."""
        cfg = {"tiers": [self._bare_tier(
            tier_id="free",
            billing_model="consumption_only",
            initial_credit=10.00,
        )]}
        tier = load_tiers(cfg)["free"]
        assert tier.initial_credit == Decimal("10.00")
        assert tier.credit_grant is not None
        assert tier.credit_grant.trigger == CreditTrigger.SIGNUP
        assert tier.credit_grant.amount_per_period == Decimal("10.00")
        assert tier.credit_grant.lifecycle == CreditLifecycle.PERSISTENT
        assert tier.credit_grant.destination == CreditDestination.CREDIT_BALANCE

    def test_explicit_credit_grant_wins_over_initial_credit(self):
        """If both are set, the explicit credit_grant takes precedence —
        initial_credit is for legacy configs that haven't migrated yet."""
        cfg = {"tiers": [self._bare_tier(
            tier_id="hybrid",
            billing_model="consumption_only",
            initial_credit=5.00,
            credit_grant={
                "trigger": "signup",
                "amount_per_period": 20.00,
                "currency": "USD",
                "lifecycle": "persistent",
                "destination": "credit_balance",
            },
        )]}
        tier = load_tiers(cfg)["hybrid"]
        assert tier.credit_grant.amount_per_period == Decimal("20.00")

    def test_sandbox_platform_real_config_loads_all_four_tiers(self):
        """End-to-end: the actual sandbox-platform quota-config.json loads
        with every tier's policy intact. This is the canonical real-world
        regression guard — if this breaks, every paying customer is affected.
        """
        import json
        from pathlib import Path
        cfg_path = Path(__file__).resolve().parents[3] / \
            "resource/output/sandbox-platform/quota-config.json"
        if not cfg_path.exists():
            import pytest
            pytest.skip(f"sandbox-platform config not at {cfg_path}")

        cfg = json.loads(cfg_path.read_text())
        tiers = load_tiers(cfg)

        assert set(tiers.keys()) >= {"free", "starter", "pro", "enterprise"}

        free = tiers["free"]
        assert free.billing_model == BillingModel.CONSUMPTION_ONLY
        assert free.credit_grant is not None
        assert free.credit_grant.trigger == CreditTrigger.SIGNUP
        assert free.credit_grant.destination == CreditDestination.CREDIT_BALANCE

        for tier_id in ("starter", "pro"):
            t = tiers[tier_id]
            assert t.billing_model == BillingModel.SUBSCRIPTION_WITH_CREDITS, tier_id
            assert t.price is not None, tier_id
            assert t.credit_grant.trigger == CreditTrigger.SUBSCRIPTION_INVOICE_PAID, tier_id
            assert t.credit_grant.lifecycle == CreditLifecycle.USE_IT_OR_LOSE_IT, tier_id
            assert t.credit_grant.destination == CreditDestination.SUBSCRIPTION_CREDIT, tier_id
            assert t.credit_grant.reset_on_downgrade is True, tier_id
            assert t.credit_grant.reset_on_upgrade is False, tier_id

        ent = tiers["enterprise"]
        assert ent.billing_model == BillingModel.SUBSCRIPTION_WITH_CREDITS
        assert ent.credit_grant.lifecycle == CreditLifecycle.ROLLOVER_CAPPED
        assert ent.credit_grant.rollover_max_periods is not None
        assert ent.credit_grant.rollover_max_periods > 0
