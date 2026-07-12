"""E2 (DECISIONS D-24 Option B / D-25) — the legacy increment over-admission
SAFETY NET must be asserted, not merely emitted.

D-24 B says: increment() counts at the fact and NEVER refuses; over-admission is
made an OBSERVABLE fact via an `over_limit_admitted` event. An unasserted event is
not observable — so this suite pins that a legacy increment crossing the limit
(a) COUNTS (never refuses) and (b) FIRES `over_limit_admitted` carrying org,
resource_key, and the resulting level, at BOTH org and per-user scope, and (c) does
NOT fire while under the limit.
"""
from __future__ import annotations

import logging

import fakeredis.aioredis
import pytest
import pytest_asyncio

from ab0t_quota.alerts import AlertDispatcher, AlertManager
from ab0t_quota.engine import QuotaEngine
from ab0t_quota.models.core import CounterType, ResourceDef, TierConfig, TierLimits
from ab0t_quota.models.requests import QuotaIncrementRequest
from ab0t_quota.providers import StaticTierProvider
from ab0t_quota.registry import ResourceRegistry


class _Recorder(AlertDispatcher):
    def __init__(self):
        self.alerts = []

    async def dispatch(self, alert) -> None:
        self.alerts.append(alert)


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.flushall()
    await r.aclose()


RK = "sandbox.concurrent"


def _engine(redis, *, limit=1, per_user=None):
    reg = ResourceRegistry()
    reg.register(ResourceDef(service="t", resource_key=RK, display_name="C",
                             counter_type=CounterType.GAUGE, unit="s"))
    return QuotaEngine(
        redis=redis, tier_provider=StaticTierProvider({"org-1": "free"}),
        registry=reg,
        tiers={"free": TierConfig(tier_id="free", display_name="F",
                                  limits={RK: TierLimits(limit=limit, per_user_limit=per_user)})},
    )


class TestOverLimitAdmitted:
    @pytest.mark.asyncio
    async def test_counts_past_limit_and_fires_event(self, redis, caplog):
        engine = _engine(redis, limit=1)
        rec = _Recorder()
        engine.set_alert_manager(AlertManager(redis, dispatchers=[rec]))

        with caplog.at_level(logging.WARNING, logger="ab0t_quota"):
            v1 = await engine.increment(QuotaIncrementRequest(org_id="org-1", resource_key=RK))
            v2 = await engine.increment(QuotaIncrementRequest(org_id="org-1", resource_key=RK))

        # (a) counts at the fact — never refuses
        assert v1 == 1.0
        assert v2 == 2.0, "legacy increment must COUNT past the limit, not refuse"

        # (b) fires over_limit_admitted carrying org, resource, resulting level
        oa = [a for a in rec.alerts if a.message == "over_limit_admitted"]
        assert oa, "crossing the limit must fire over_limit_admitted"
        assert oa[0].resource_key == RK
        assert oa[0].current == 2.0 and oa[0].limit == 1.0
        assert oa[0].org_id == "org-1"
        # the log line carries org + resource + level (the observable fact)
        assert any(
            "over_limit_admitted" in r.getMessage() and "org=org-1" in r.getMessage()
            and "resource=" + RK in r.getMessage() and "level=2.0" in r.getMessage()
            for r in caplog.records
        ), "over_limit_admitted log must carry org, resource, and resulting level"

    @pytest.mark.asyncio
    async def test_no_event_while_under_limit(self, redis):
        engine = _engine(redis, limit=5)
        rec = _Recorder()
        engine.set_alert_manager(AlertManager(redis, dispatchers=[rec]))
        await engine.increment(QuotaIncrementRequest(org_id="org-1", resource_key=RK))  # 1/5
        assert [a for a in rec.alerts if a.message == "over_limit_admitted"] == []

    @pytest.mark.asyncio
    async def test_fires_at_per_user_scope(self, redis):
        engine = _engine(redis, limit=10, per_user=1)
        rec = _Recorder()
        engine.set_alert_manager(AlertManager(redis, dispatchers=[rec]))
        # org limit 10 (fine), per-user 1: second create for alice crosses the USER limit
        await engine.increment(QuotaIncrementRequest(org_id="org-1", resource_key=RK, user_id="alice"))
        await engine.increment(QuotaIncrementRequest(org_id="org-1", resource_key=RK, user_id="alice"))
        oa = [a for a in rec.alerts if a.message == "over_limit_admitted"]
        assert oa, "crossing the per-user limit must fire over_limit_admitted"
        assert oa[0].current == 2.0 and oa[0].limit == 1.0

    @pytest.mark.asyncio
    async def test_bundle_path_also_fires(self, redis):
        # increment_for_bundle routes through increment(), so it inherits the event.
        reg = ResourceRegistry()
        reg.register(ResourceDef(service="t", resource_key=RK, display_name="C",
                                 counter_type=CounterType.GAUGE))
        engine = QuotaEngine(
            redis=redis, tier_provider=StaticTierProvider({"org-1": "free"}), registry=reg,
            tiers={"free": TierConfig(tier_id="free", display_name="F",
                                      limits={RK: TierLimits(limit=1)})},
            resource_bundles={"box": [RK]})
        rec = _Recorder()
        engine.set_alert_manager(AlertManager(redis, dispatchers=[rec]))
        await engine.increment_for_bundle("org-1", "box")   # 1
        await engine.increment_for_bundle("org-1", "box")   # 2 — crosses
        assert [a for a in rec.alerts if a.message == "over_limit_admitted"], \
            "bundle over-admission must also be observable"
