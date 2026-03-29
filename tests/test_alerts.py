"""2.1.7 — Alert dispatcher tests."""

import pytest
import pytest_asyncio
import fakeredis.aioredis

from ab0t_quota.alerts import AlertManager, LogAlertDispatcher, WebhookAlertDispatcher
from ab0t_quota.models.core import QuotaAlert, AlertSeverity


def _make_alert(severity=AlertSeverity.WARNING, resource="sandbox.concurrent", org="org-1"):
    return QuotaAlert(
        org_id=org,
        resource_key=resource,
        severity=severity,
        current=8,
        limit=10,
        utilization=0.8,
        tier_id="free",
        message="test alert",
    )


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.flushall()
    await r.aclose()


class TestAlertManager:
    @pytest.mark.asyncio
    async def test_dispatches_warning(self, redis):
        dispatched = []

        class TrackingDispatcher(LogAlertDispatcher):
            async def dispatch(self, alert):
                dispatched.append(alert)

        mgr = AlertManager(redis=redis, dispatchers=[TrackingDispatcher()], cooldown_seconds=3600)
        alert = _make_alert(AlertSeverity.WARNING)
        result = await mgr.maybe_alert(alert)
        assert result is True
        assert len(dispatched) == 1

    @pytest.mark.asyncio
    async def test_cooldown_prevents_repeat(self, redis):
        dispatched = []

        class TrackingDispatcher(LogAlertDispatcher):
            async def dispatch(self, alert):
                dispatched.append(alert)

        mgr = AlertManager(redis=redis, dispatchers=[TrackingDispatcher()], cooldown_seconds=3600)
        alert = _make_alert(AlertSeverity.WARNING)
        await mgr.maybe_alert(alert)
        result = await mgr.maybe_alert(alert)  # same severity → suppressed
        assert result is False
        assert len(dispatched) == 1

    @pytest.mark.asyncio
    async def test_severity_escalation_triggers_new_alert(self, redis):
        dispatched = []

        class TrackingDispatcher(LogAlertDispatcher):
            async def dispatch(self, alert):
                dispatched.append(alert)

        mgr = AlertManager(redis=redis, dispatchers=[TrackingDispatcher()], cooldown_seconds=3600)
        await mgr.maybe_alert(_make_alert(AlertSeverity.WARNING))
        result = await mgr.maybe_alert(_make_alert(AlertSeverity.CRITICAL))  # escalation
        assert result is True
        assert len(dispatched) == 2
        assert dispatched[1].severity == AlertSeverity.CRITICAL

    @pytest.mark.asyncio
    async def test_info_not_dispatched(self, redis):
        dispatched = []

        class TrackingDispatcher(LogAlertDispatcher):
            async def dispatch(self, alert):
                dispatched.append(alert)

        mgr = AlertManager(redis=redis, dispatchers=[TrackingDispatcher()])
        result = await mgr.maybe_alert(_make_alert(AlertSeverity.INFO))
        assert result is False
        assert len(dispatched) == 0

    @pytest.mark.asyncio
    async def test_different_resources_not_cooldown_shared(self, redis):
        dispatched = []

        class TrackingDispatcher(LogAlertDispatcher):
            async def dispatch(self, alert):
                dispatched.append(alert)

        mgr = AlertManager(redis=redis, dispatchers=[TrackingDispatcher()], cooldown_seconds=3600)
        await mgr.maybe_alert(_make_alert(AlertSeverity.WARNING, resource="a.b"))
        result = await mgr.maybe_alert(_make_alert(AlertSeverity.WARNING, resource="c.d"))
        assert result is True
        assert len(dispatched) == 2


class TestWebhookAlertDispatcherValidation:
    """H1: Webhook URL must be HTTPS and must not target private/loopback addresses."""

    def test_https_url_accepted(self):
        d = WebhookAlertDispatcher(url="https://hooks.slack.com/services/T123/B456/abc")
        assert d._url.startswith("https://")

    def test_http_url_rejected(self):
        with pytest.raises(ValueError, match="HTTPS"):
            WebhookAlertDispatcher(url="http://example.com/webhook")

    def test_no_scheme_rejected(self):
        with pytest.raises(ValueError, match="HTTPS"):
            WebhookAlertDispatcher(url="example.com/webhook")

    def test_localhost_rejected(self):
        with pytest.raises(ValueError, match="loopback"):
            WebhookAlertDispatcher(url="https://localhost/webhook")

    def test_127_0_0_1_rejected(self):
        with pytest.raises(ValueError, match="loopback"):
            WebhookAlertDispatcher(url="https://127.0.0.1/webhook")

    def test_ipv6_loopback_rejected(self):
        with pytest.raises(ValueError, match="loopback"):
            WebhookAlertDispatcher(url="https://[::1]/webhook")

    def test_private_ip_10_rejected(self):
        with pytest.raises(ValueError, match="private"):
            WebhookAlertDispatcher(url="https://10.0.0.1/webhook")

    def test_private_ip_192_168_rejected(self):
        with pytest.raises(ValueError, match="private"):
            WebhookAlertDispatcher(url="https://192.168.1.1/webhook")

    def test_private_ip_172_16_rejected(self):
        with pytest.raises(ValueError, match="private"):
            WebhookAlertDispatcher(url="https://172.16.0.1/webhook")

    def test_link_local_rejected(self):
        with pytest.raises(ValueError, match="private|loopback"):
            WebhookAlertDispatcher(url="https://169.254.169.254/latest/meta-data")

    def test_public_dns_accepted(self):
        d = WebhookAlertDispatcher(url="https://hooks.pagerduty.com/v2/events")
        assert d._url == "https://hooks.pagerduty.com/v2/events"

    def test_public_ip_accepted(self):
        d = WebhookAlertDispatcher(url="https://8.8.8.8/webhook")
        assert d._url == "https://8.8.8.8/webhook"
