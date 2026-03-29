"""2.1.1 — Model validation tests."""

import pytest
from pydantic import ValidationError

from ab0t_quota.models.core import (
    ResourceDef, CounterType, ResetPeriod, TierLimits, TierConfig, QuotaState, AlertSeverity,
)
from ab0t_quota.models.responses import QuotaResult, QuotaDecision, QuotaBatchResult


# ---------------------------------------------------------------------------
# ResourceDef validation
# ---------------------------------------------------------------------------

class TestResourceDef:
    def test_rate_requires_window(self):
        with pytest.raises(ValidationError, match="window_seconds"):
            ResourceDef(
                service="test",
                resource_key="api.requests",
                display_name="Requests",
                counter_type=CounterType.RATE,
                # window_seconds missing
            )

    def test_rate_with_window_ok(self):
        r = ResourceDef(
            service="test",
            resource_key="api.requests",
            display_name="Requests",
            counter_type=CounterType.RATE,
            window_seconds=3600,
        )
        assert r.window_seconds == 3600

    def test_accumulator_requires_reset(self):
        with pytest.raises(ValidationError, match="reset_period"):
            ResourceDef(
                service="test",
                resource_key="cost.monthly",
                display_name="Monthly Cost",
                counter_type=CounterType.ACCUMULATOR,
                # reset_period missing
            )

    def test_accumulator_with_reset_ok(self):
        r = ResourceDef(
            service="test",
            resource_key="cost.monthly",
            display_name="Monthly Cost",
            counter_type=CounterType.ACCUMULATOR,
            reset_period=ResetPeriod.MONTHLY,
            precision=2,
        )
        assert r.reset_period == ResetPeriod.MONTHLY

    def test_gauge_no_extras_needed(self):
        r = ResourceDef(
            service="sandbox",
            resource_key="sandbox.concurrent",
            display_name="Concurrent Sandboxes",
            counter_type=CounterType.GAUGE,
        )
        assert r.counter_type == CounterType.GAUGE

    def test_resource_key_pattern(self):
        with pytest.raises(ValidationError):
            ResourceDef(
                service="test",
                resource_key="INVALID-KEY",  # uppercase, dash
                display_name="Bad",
                counter_type=CounterType.GAUGE,
            )

    def test_fully_qualified_key(self):
        r = ResourceDef(
            service="sandbox",
            resource_key="sandbox.concurrent",
            display_name="Concurrent",
            counter_type=CounterType.GAUGE,
        )
        assert r.fully_qualified_key == "sandbox:sandbox.concurrent"


# ---------------------------------------------------------------------------
# TierLimits validation
# ---------------------------------------------------------------------------

class TestTierLimits:
    def test_defaults(self):
        t = TierLimits(limit=10)
        assert t.warning_threshold == 0.80
        assert t.critical_threshold == 0.95
        assert t.is_unlimited is False

    def test_unlimited(self):
        t = TierLimits()
        assert t.is_unlimited is True

    def test_threshold_bounds(self):
        with pytest.raises(ValidationError):
            TierLimits(limit=10, warning_threshold=1.5)  # > 1.0

    def test_per_user_limit(self):
        t = TierLimits(limit=10, per_user_limit=3)
        assert t.per_user_limit == 3


# ---------------------------------------------------------------------------
# QuotaResult computed properties
# ---------------------------------------------------------------------------

class TestQuotaResult:
    def _make(self, decision, current=3, requested=1, limit=5):
        return QuotaResult(
            decision=decision,
            resource_key="sandbox.concurrent",
            current=current,
            requested=requested,
            limit=limit,
            tier_id="starter",
            tier_display="Starter",
            severity=AlertSeverity.INFO,
            message="test",
        )

    def test_allowed(self):
        r = self._make(QuotaDecision.ALLOW)
        assert r.allowed is True
        assert r.denied is False
        assert r.warning is False

    def test_denied(self):
        r = self._make(QuotaDecision.DENY)
        assert r.allowed is False
        assert r.denied is True

    def test_warning(self):
        r = self._make(QuotaDecision.ALLOW_WARNING)
        assert r.allowed is True
        assert r.warning is True
        assert r.denied is False

    def test_unlimited(self):
        r = self._make(QuotaDecision.UNLIMITED, limit=None)
        assert r.allowed is True
        assert r.remaining is None
        assert r.utilization is None

    def test_remaining(self):
        r = self._make(QuotaDecision.ALLOW, current=3, requested=1, limit=5)
        assert r.remaining == 1  # 5 - 3 - 1

    def test_utilization(self):
        r = self._make(QuotaDecision.ALLOW, current=4, requested=1, limit=5)
        assert r.utilization == 0.8  # 4/5

    def test_to_api_error(self):
        r = self._make(QuotaDecision.DENY, current=5, requested=1, limit=5)
        err = r.to_api_error()
        assert err["error"] == "quota_exceeded"
        assert err["resource"] == "sandbox.concurrent"
        assert err["limit"] == 5
        assert err["tier"] == "starter"


# ---------------------------------------------------------------------------
# QuotaBatchResult
# ---------------------------------------------------------------------------

class TestQuotaBatchResult:
    def test_all_allowed(self):
        results = [
            QuotaResult(
                decision=QuotaDecision.ALLOW, resource_key="a.b",
                current=1, requested=1, limit=5, tier_id="free",
                tier_display="Free", severity=AlertSeverity.INFO, message="ok",
            ),
            QuotaResult(
                decision=QuotaDecision.ALLOW, resource_key="c.d",
                current=2, requested=1, limit=10, tier_id="free",
                tier_display="Free", severity=AlertSeverity.INFO, message="ok",
            ),
        ]
        batch = QuotaBatchResult(allowed=True, results=results)
        assert batch.allowed is True
        assert batch.first_denial is None
        assert batch.denied_resources == []

    def test_one_denied(self):
        results = [
            QuotaResult(
                decision=QuotaDecision.ALLOW, resource_key="a.b",
                current=1, requested=1, limit=5, tier_id="free",
                tier_display="Free", severity=AlertSeverity.INFO, message="ok",
            ),
            QuotaResult(
                decision=QuotaDecision.DENY, resource_key="c.d",
                current=10, requested=1, limit=10, tier_id="free",
                tier_display="Free", severity=AlertSeverity.EXCEEDED, message="denied",
            ),
        ]
        batch = QuotaBatchResult(
            allowed=False,
            results=results,
            denied_resources=["c.d"],
        )
        assert batch.allowed is False
        assert batch.first_denial.resource_key == "c.d"


# ---------------------------------------------------------------------------
# QuotaState severity
# ---------------------------------------------------------------------------

class TestQuotaState:
    def test_info(self):
        s = QuotaState(org_id="o", resource_key="a.b", current=3, limit=10, tier_id="free")
        assert s.severity == AlertSeverity.INFO

    def test_warning(self):
        s = QuotaState(org_id="o", resource_key="a.b", current=8.5, limit=10, tier_id="free")
        assert s.severity == AlertSeverity.WARNING

    def test_critical(self):
        s = QuotaState(org_id="o", resource_key="a.b", current=9.6, limit=10, tier_id="free")
        assert s.severity == AlertSeverity.CRITICAL

    def test_exceeded(self):
        s = QuotaState(org_id="o", resource_key="a.b", current=11, limit=10, tier_id="free")
        assert s.severity == AlertSeverity.EXCEEDED

    def test_unlimited(self):
        s = QuotaState(org_id="o", resource_key="a.b", current=100, limit=None, tier_id="free")
        assert s.severity == AlertSeverity.INFO
        assert s.utilization is None
        assert s.remaining is None
