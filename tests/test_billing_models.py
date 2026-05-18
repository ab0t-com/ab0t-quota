"""Tier-config schema for billing models + subscription-credit handler.

Covers ticket 20260516_paid_plan_balance_model_gap Phases 1.1, 2.2b,
2.3, and the 3.2 library helper (reset_subscription_credit_on_tier_change).
"""
import pytest
from decimal import Decimal
from unittest.mock import patch

from ab0t_quota.models.core import (
    BillingModel, CreditTrigger, CreditLifecycle, CreditDestination,
    BillingPeriod, Price, CreditGrant, TierConfig,
)
from ab0t_quota.billing.subscription_credit import (
    handle_subscription_invoice_paid,
    reset_subscription_credit_on_tier_change,
)
from ab0t_quota.billing.clients import BillingServiceError


# =============================================================================
# Phase 1.1 — TierConfig schema + validators
# =============================================================================

class TestBillingModelDefaults:
    """Library defaults match context_03 D1/D2/D3/D5."""

    def test_default_is_capacity_only(self):
        t = TierConfig(tier_id="free", display_name="Free")
        assert t.billing_model == BillingModel.CAPACITY_ONLY
        assert t.price is None
        assert t.credit_grant is None

    def test_credit_grant_default_lifecycle_is_use_it_or_lose_it(self):
        g = CreditGrant(
            trigger=CreditTrigger.SUBSCRIPTION_INVOICE_PAID,
            amount_per_period=Decimal("10"),
        )
        # D2 default
        assert g.lifecycle == CreditLifecycle.USE_IT_OR_LOSE_IT

    def test_credit_grant_default_destination_is_subscription_credit(self):
        g = CreditGrant(
            trigger=CreditTrigger.SUBSCRIPTION_INVOICE_PAID,
            amount_per_period=Decimal("10"),
        )
        assert g.destination == CreditDestination.SUBSCRIPTION_CREDIT

    def test_credit_grant_default_reset_flags(self):
        g = CreditGrant(
            trigger=CreditTrigger.SUBSCRIPTION_INVOICE_PAID,
            amount_per_period=Decimal("10"),
        )
        # D3 — reset_on_upgrade default false
        assert g.reset_on_upgrade is False
        # Default reset_on_downgrade is true (less surprising — voluntary)
        assert g.reset_on_downgrade is True


class TestInitialCreditBackCompat:
    """Legacy `initial_credit: <Decimal>` synthesizes a CreditGrant."""

    def test_initial_credit_synthesizes_signup_grant(self):
        t = TierConfig(
            tier_id="free", display_name="Free",
            initial_credit=Decimal("10"),
        )
        assert t.credit_grant is not None
        assert t.credit_grant.trigger == CreditTrigger.SIGNUP
        assert t.credit_grant.amount_per_period == Decimal("10")
        assert t.credit_grant.lifecycle == CreditLifecycle.PERSISTENT
        assert t.credit_grant.destination == CreditDestination.CREDIT_BALANCE

    def test_explicit_credit_grant_wins_over_initial_credit(self):
        explicit = CreditGrant(
            trigger=CreditTrigger.MANUAL, amount_per_period=Decimal("5"),
        )
        t = TierConfig(
            tier_id="free", display_name="Free",
            initial_credit=Decimal("99"),
            credit_grant=explicit,
        )
        assert t.credit_grant.trigger == CreditTrigger.MANUAL
        assert t.credit_grant.amount_per_period == Decimal("5")


class TestCrossFieldValidation:
    """billing_model + price + credit_grant combinations must validate."""

    def test_subscription_with_credits_requires_price(self):
        with pytest.raises(Exception, match="requires `price`"):
            TierConfig(
                tier_id="starter", display_name="Starter",
                billing_model=BillingModel.SUBSCRIPTION_WITH_CREDITS,
            )

    def test_subscription_with_credits_requires_credit_grant(self):
        with pytest.raises(Exception, match="requires `credit_grant`"):
            TierConfig(
                tier_id="starter", display_name="Starter",
                billing_model=BillingModel.SUBSCRIPTION_WITH_CREDITS,
                price=Price(amount_per_period=Decimal("10")),
            )

    def test_subscription_with_credits_requires_invoice_paid_trigger(self):
        with pytest.raises(Exception, match="subscription_invoice_paid"):
            TierConfig(
                tier_id="starter", display_name="Starter",
                billing_model=BillingModel.SUBSCRIPTION_WITH_CREDITS,
                price=Price(amount_per_period=Decimal("10")),
                credit_grant=CreditGrant(
                    trigger=CreditTrigger.MANUAL,
                    amount_per_period=Decimal("10"),
                ),
            )

    def test_unlock_only_must_not_have_credit_grant(self):
        with pytest.raises(Exception, match="must NOT have `credit_grant`"):
            TierConfig(
                tier_id="x", display_name="X",
                billing_model=BillingModel.SUBSCRIPTION_UNLOCK_ONLY,
                price=Price(amount_per_period=Decimal("10")),
                credit_grant=CreditGrant(
                    trigger=CreditTrigger.SUBSCRIPTION_INVOICE_PAID,
                    amount_per_period=Decimal("10"),
                ),
            )

    def test_consumption_only_must_not_have_price(self):
        with pytest.raises(Exception, match="must NOT have `price`"):
            TierConfig(
                tier_id="x", display_name="X",
                billing_model=BillingModel.CONSUMPTION_ONLY,
                price=Price(amount_per_period=Decimal("1")),
            )


class TestExperimentalBillingModelGate:
    """Experimental billing models (overage / seat_based / metered) must
    fail load by default. Opt in via env var; loud WARNING on use.

    Rationale: enum advertises them for schema forward-compat, but no
    runtime path exists. A silent no-op tier would break the drop-in
    promise ("declared config actually does something").
    """

    EXPERIMENTAL = [
        BillingModel.SUBSCRIPTION_WITH_OVERAGE,
        BillingModel.SEAT_BASED,
        BillingModel.METERED,
    ]

    @pytest.mark.parametrize("bm", EXPERIMENTAL)
    def test_experimental_rejected_without_opt_in(self, bm, monkeypatch):
        monkeypatch.delenv(
            "AB0T_QUOTA_ALLOW_EXPERIMENTAL_BILLING_MODELS", raising=False
        )
        with pytest.raises(Exception, match="experimental"):
            TierConfig(
                tier_id="x", display_name="X",
                billing_model=bm,
                price=Price(amount_per_period=Decimal("10")),
            )

    @pytest.mark.parametrize("bm", EXPERIMENTAL)
    def test_experimental_accepted_with_opt_in(self, bm, monkeypatch):
        monkeypatch.setenv(
            "AB0T_QUOTA_ALLOW_EXPERIMENTAL_BILLING_MODELS", "true"
        )
        # Should not raise.
        t = TierConfig(
            tier_id="x", display_name="X",
            billing_model=bm,
            price=Price(amount_per_period=Decimal("10")),
        )
        assert t.billing_model == bm

    @pytest.mark.parametrize(
        "flag_val,should_allow",
        [
            ("true", True), ("True", True), ("TRUE", True),
            ("1", True), ("yes", True), ("on", True),
            ("false", False), ("0", False), ("no", False),
            ("", False), ("anything-else", False),
        ],
    )
    def test_opt_in_flag_parsing(self, flag_val, should_allow, monkeypatch):
        monkeypatch.setenv(
            "AB0T_QUOTA_ALLOW_EXPERIMENTAL_BILLING_MODELS", flag_val
        )
        if should_allow:
            TierConfig(
                tier_id="x", display_name="X",
                billing_model=BillingModel.METERED,
                price=Price(amount_per_period=Decimal("1")),
            )
        else:
            with pytest.raises(Exception, match="experimental"):
                TierConfig(
                    tier_id="x", display_name="X",
                    billing_model=BillingModel.METERED,
                    price=Price(amount_per_period=Decimal("1")),
                )

    def test_supported_models_unaffected(self, monkeypatch):
        """Non-experimental models load regardless of flag state."""
        monkeypatch.delenv(
            "AB0T_QUOTA_ALLOW_EXPERIMENTAL_BILLING_MODELS", raising=False
        )
        # capacity_only (default) — always loads
        TierConfig(tier_id="free", display_name="Free")
        # subscription_unlock_only with price — always loads
        TierConfig(
            tier_id="pro", display_name="Pro",
            billing_model=BillingModel.SUBSCRIPTION_UNLOCK_ONLY,
            price=Price(amount_per_period=Decimal("20")),
        )
        # consumption_only — always loads
        TierConfig(
            tier_id="payg", display_name="PAYG",
            billing_model=BillingModel.CONSUMPTION_ONLY,
        )

    def test_error_lists_supported_alternatives(self, monkeypatch):
        """Error message must guide the consumer to a supported choice."""
        monkeypatch.delenv(
            "AB0T_QUOTA_ALLOW_EXPERIMENTAL_BILLING_MODELS", raising=False
        )
        with pytest.raises(Exception) as exc:
            TierConfig(
                tier_id="x", display_name="X",
                billing_model=BillingModel.SEAT_BASED,
                price=Price(amount_per_period=Decimal("10")),
            )
        msg = str(exc.value)
        assert "AB0T_QUOTA_ALLOW_EXPERIMENTAL_BILLING_MODELS" in msg
        assert "capacity_only" in msg
        assert "subscription_with_credits" in msg


class TestCreditGrantValidators:
    def test_rollover_capped_requires_max(self):
        with pytest.raises(Exception, match="rollover_max_periods is required"):
            CreditGrant(
                trigger=CreditTrigger.SUBSCRIPTION_INVOICE_PAID,
                amount_per_period=Decimal("10"),
                lifecycle=CreditLifecycle.ROLLOVER_CAPPED,
            )

    def test_rollover_max_forbidden_on_other_lifecycles(self):
        with pytest.raises(Exception, match="only applies"):
            CreditGrant(
                trigger=CreditTrigger.SUBSCRIPTION_INVOICE_PAID,
                amount_per_period=Decimal("10"),
                lifecycle=CreditLifecycle.USE_IT_OR_LOSE_IT,
                rollover_max_periods=3,
            )


# =============================================================================
# Phase 2.2b — handle_subscription_invoice_paid
# =============================================================================

@pytest.fixture
def tier_registry():
    starter = TierConfig(
        tier_id="starter", display_name="Starter",
        billing_model=BillingModel.SUBSCRIPTION_WITH_CREDITS,
        price=Price(amount_per_period=Decimal("10")),
        credit_grant=CreditGrant(
            trigger=CreditTrigger.SUBSCRIPTION_INVOICE_PAID,
            amount_per_period=Decimal("10"),
        ),
    )
    unlock = TierConfig(
        tier_id="unlock", display_name="Unlock",
        billing_model=BillingModel.SUBSCRIPTION_UNLOCK_ONLY,
        price=Price(amount_per_period=Decimal("10")),
    )
    free = TierConfig(
        tier_id="free", display_name="Free",
        initial_credit=Decimal("10"),  # trigger=signup
    )
    return {"starter": starter, "unlock": unlock, "free": free}


@pytest.fixture
def plan_resolver():
    async def resolver(plan_id):
        return {
            "plan-starter": "starter",
            "plan-unlock": "unlock",
            "plan-free": "free",
        }.get(plan_id)
    return resolver


class FakeBillingClient:
    """Stub for BillingServiceClient — records calls + canned response."""

    def __init__(self, raise_status=None):
        self.calls = []
        self.raise_status = raise_status

    async def apply_credit_grant(self, **kwargs):
        self.calls.append(("grant", kwargs))
        if self.raise_status is not None:
            raise BillingServiceError(self.raise_status, {"error": "stub"})
        return {"org_id": kwargs["org_id"], "ok": True}

    async def reset_subscription_credit(self, **kwargs):
        self.calls.append(("reset", kwargs))
        if self.raise_status is not None:
            raise BillingServiceError(self.raise_status, {"error": "stub"})
        return {"org_id": kwargs["org_id"], "amount": "29"}


@pytest.mark.asyncio
class TestHandleSubscriptionInvoicePaid:
    async def test_happy_path(self, tier_registry, plan_resolver):
        invoice = {"id": "in_1", "metadata": {
            "org_id": "org-x", "plan_id": "plan-starter",
        }}
        bc = FakeBillingClient()
        result = await handle_subscription_invoice_paid(
            invoice,
            tier_registry=tier_registry,
            plan_to_tier=plan_resolver,
            billing_client=bc,
        )
        assert result["status"] == "applied"
        assert len(bc.calls) == 1
        _, kwargs = bc.calls[0]
        assert kwargs["amount"] == 10.0
        assert kwargs["destination"] == "subscription_credit"
        assert kwargs["lifecycle"] == "use_it_or_lose_it"
        assert kwargs["idempotency_key"] == "invoice:in_1:credit_grant"
        # Phase 3.2 — passes source + source_tier
        assert kwargs["source"] == "in_1"
        assert kwargs["source_tier"] == "starter"

    async def test_pre_2_1_invoice_no_metadata_skipped(self, tier_registry, plan_resolver):
        invoice = {"id": "in_2"}
        bc = FakeBillingClient()
        result = await handle_subscription_invoice_paid(
            invoice, tier_registry=tier_registry,
            plan_to_tier=plan_resolver, billing_client=bc,
        )
        assert result["status"] == "skipped_no_metadata"
        assert len(bc.calls) == 0

    async def test_plan_unknown_skipped(self, tier_registry, plan_resolver):
        invoice = {"id": "in_3", "metadata": {
            "org_id": "org-x", "plan_id": "plan-mystery",
        }}
        bc = FakeBillingClient()
        result = await handle_subscription_invoice_paid(
            invoice, tier_registry=tier_registry,
            plan_to_tier=plan_resolver, billing_client=bc,
        )
        assert result["status"] == "skipped_no_tier"

    async def test_tier_registry_drift_skipped(self, tier_registry):
        async def res_drift(plan_id):
            return "ghost-tier"
        invoice = {"id": "in_4", "metadata": {
            "org_id": "org-x", "plan_id": "plan-starter",
        }}
        bc = FakeBillingClient()
        result = await handle_subscription_invoice_paid(
            invoice, tier_registry=tier_registry,
            plan_to_tier=res_drift, billing_client=bc,
        )
        assert result["status"] == "skipped_no_tier"

    async def test_unlock_only_tier_skipped(self, tier_registry, plan_resolver):
        invoice = {"id": "in_5", "metadata": {
            "org_id": "org-x", "plan_id": "plan-unlock",
        }}
        bc = FakeBillingClient()
        result = await handle_subscription_invoice_paid(
            invoice, tier_registry=tier_registry,
            plan_to_tier=plan_resolver, billing_client=bc,
        )
        assert result["status"] == "skipped_no_grant"

    async def test_wrong_trigger_skipped(self, tier_registry, plan_resolver):
        """Free tier has trigger=signup, NOT invoice_paid → skip."""
        invoice = {"id": "in_6", "metadata": {
            "org_id": "org-x", "plan_id": "plan-free",
        }}
        bc = FakeBillingClient()
        result = await handle_subscription_invoice_paid(
            invoice, tier_registry=tier_registry,
            plan_to_tier=plan_resolver, billing_client=bc,
        )
        assert result["status"] == "skipped_wrong_trigger"

    async def test_billing_transient_503(self, tier_registry, plan_resolver):
        bc = FakeBillingClient(raise_status=503)
        invoice = {"id": "in_7", "metadata": {
            "org_id": "org-x", "plan_id": "plan-starter",
        }}
        result = await handle_subscription_invoice_paid(
            invoice, tier_registry=tier_registry,
            plan_to_tier=plan_resolver, billing_client=bc,
        )
        assert result["status"] == "deferred_transient"

    async def test_billing_permanent_400(self, tier_registry, plan_resolver):
        bc = FakeBillingClient(raise_status=400)
        invoice = {"id": "in_8", "metadata": {
            "org_id": "org-x", "plan_id": "plan-starter",
        }}
        result = await handle_subscription_invoice_paid(
            invoice, tier_registry=tier_registry,
            plan_to_tier=plan_resolver, billing_client=bc,
        )
        assert result["status"] == "failed_permanent"

    async def test_metadata_in_subscription_details_fallback(self, tier_registry, plan_resolver):
        """Stripe sometimes nests metadata under subscription_details."""
        invoice = {
            "id": "in_9",
            "subscription_details": {"metadata": {
                "org_id": "org-y", "plan_id": "plan-starter",
            }},
        }
        bc = FakeBillingClient()
        result = await handle_subscription_invoice_paid(
            invoice, tier_registry=tier_registry,
            plan_to_tier=plan_resolver, billing_client=bc,
        )
        assert result["status"] == "applied"
        assert bc.calls[0][1]["org_id"] == "org-y"


# =============================================================================
# Phase 3.2 — reset_subscription_credit_on_tier_change
# =============================================================================

@pytest.fixture
def hierarchy():
    """Tier registry with sort_order ranks for downgrade detection."""
    def make(tid, sort, has_grant=True, reset_flag=True):
        return TierConfig(
            tier_id=tid, display_name=tid.title(), sort_order=sort,
            billing_model=(
                BillingModel.SUBSCRIPTION_WITH_CREDITS if has_grant
                else BillingModel.CAPACITY_ONLY
            ),
            price=Price(amount_per_period=Decimal("10")) if has_grant else None,
            credit_grant=CreditGrant(
                trigger=CreditTrigger.SUBSCRIPTION_INVOICE_PAID,
                amount_per_period=Decimal("10"),
                reset_on_downgrade=reset_flag,
            ) if has_grant else None,
        )
    return {
        "free": make("free", 0, has_grant=False),
        "starter": make("starter", 1),
        "pro": make("pro", 2),
        "pro_no_reset": make("pro_no_reset", 2, reset_flag=False),
        "enterprise": make("enterprise", 3),
    }


@pytest.mark.asyncio
class TestDowngradeReset:
    async def test_happy_path_pro_to_starter(self, hierarchy):
        bc = FakeBillingClient()
        result = await reset_subscription_credit_on_tier_change(
            "o", old_tier_id="pro", new_tier_id="starter",
            tier_registry=hierarchy, billing_client=bc,
        )
        assert result["status"] == "reset"
        assert bc.calls[0][1]["expected_source_tier"] == "pro"

    async def test_upgrade_not_a_downgrade(self, hierarchy):
        bc = FakeBillingClient()
        result = await reset_subscription_credit_on_tier_change(
            "o", old_tier_id="starter", new_tier_id="pro",
            tier_registry=hierarchy, billing_client=bc,
        )
        assert result["status"] == "skipped_not_downgrade"
        assert len(bc.calls) == 0

    async def test_same_tier_not_a_downgrade(self, hierarchy):
        bc = FakeBillingClient()
        result = await reset_subscription_credit_on_tier_change(
            "o", old_tier_id="pro", new_tier_id="pro",
            tier_registry=hierarchy, billing_client=bc,
        )
        assert result["status"] == "skipped_not_downgrade"

    async def test_no_grant_on_old_tier(self, hierarchy):
        bc = FakeBillingClient()
        result = await reset_subscription_credit_on_tier_change(
            "o", old_tier_id="free", new_tier_id="starter",
            tier_registry=hierarchy, billing_client=bc,
        )
        assert result["status"] == "skipped_no_grant"

    async def test_reset_on_downgrade_false_policy(self, hierarchy):
        bc = FakeBillingClient()
        result = await reset_subscription_credit_on_tier_change(
            "o", old_tier_id="pro_no_reset", new_tier_id="starter",
            tier_registry=hierarchy, billing_client=bc,
        )
        assert result["status"] == "skipped_policy"

    async def test_unknown_old_tier(self, hierarchy):
        bc = FakeBillingClient()
        result = await reset_subscription_credit_on_tier_change(
            "o", old_tier_id="ghost", new_tier_id="starter",
            tier_registry=hierarchy, billing_client=bc,
        )
        assert result["status"] == "skipped_unknown_old"

    async def test_safety_check_rejection_409(self, hierarchy):
        bc = FakeBillingClient(raise_status=409)
        result = await reset_subscription_credit_on_tier_change(
            "o", old_tier_id="pro", new_tier_id="starter",
            tier_registry=hierarchy, billing_client=bc,
        )
        assert result["status"] == "skipped_safety_check"

    async def test_transient_503(self, hierarchy):
        bc = FakeBillingClient(raise_status=503)
        result = await reset_subscription_credit_on_tier_change(
            "o", old_tier_id="pro", new_tier_id="starter",
            tier_registry=hierarchy, billing_client=bc,
        )
        assert result["status"] == "deferred_transient"

    async def test_permanent_failure_400(self, hierarchy):
        bc = FakeBillingClient(raise_status=400)
        result = await reset_subscription_credit_on_tier_change(
            "o", old_tier_id="pro", new_tier_id="starter",
            tier_registry=hierarchy, billing_client=bc,
        )
        assert result["status"] == "failed"
