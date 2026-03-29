"""
Alert dispatchers — notify when quota usage crosses thresholds.

The engine calls AlertDispatcher.dispatch() when a check returns
WARNING or CRITICAL severity. Cooldown prevents spamming the same
alert repeatedly.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional

from redis.asyncio import Redis

from .models.core import QuotaAlert, AlertSeverity

logger = logging.getLogger("ab0t_quota.alerts")

# Default: 1 alert per resource per org per hour
DEFAULT_COOLDOWN_SECONDS = 3600


class AlertDispatcher(ABC):
    """Base class for alert delivery."""

    @abstractmethod
    async def dispatch(self, alert: QuotaAlert) -> None:
        """Send an alert. Implementation decides the channel."""


class LogAlertDispatcher(AlertDispatcher):
    """Emit alerts as structured log events (default, always active)."""

    async def dispatch(self, alert: QuotaAlert) -> None:
        log_fn = logger.warning if alert.severity == AlertSeverity.WARNING else logger.error
        log_fn(
            "quota_alert org_id=%s resource_key=%s severity=%s current=%s limit=%s utilization=%s message=%s",
            alert.org_id, alert.resource_key, alert.severity.value,
            alert.current, alert.limit, round(alert.utilization, 3),
            alert.message,
        )


class WebhookAlertDispatcher(AlertDispatcher):
    """POST alert payload to a webhook URL (Slack, PagerDuty, custom)."""

    # Private/loopback CIDRs that must never be webhook targets
    _BLOCKED_HOSTS = frozenset({
        "localhost", "127.0.0.1", "::1", "0.0.0.0",
    })

    def __init__(self, url: str, headers: Optional[dict] = None):
        self._validate_url(url)
        self._url = url
        self._headers = headers or {"Content-Type": "application/json"}

    @classmethod
    def _validate_url(cls, url: str) -> None:
        """Enforce HTTPS and block private/loopback destinations (SSRF protection)."""
        from urllib.parse import urlparse
        import ipaddress

        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise ValueError(f"Webhook URL must use HTTPS scheme, got '{parsed.scheme}'")
        hostname = parsed.hostname or ""
        if hostname in cls._BLOCKED_HOSTS:
            raise ValueError(f"Webhook URL must not target loopback/localhost: {hostname}")
        try:
            addr = ipaddress.ip_address(hostname)
            if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
                raise ValueError(f"Webhook URL must not target private/loopback/reserved IP: {hostname}")
        except ValueError as e:
            if "must not target" in str(e):
                raise
            # hostname is a DNS name, not a raw IP — that's fine

    async def dispatch(self, alert: QuotaAlert) -> None:
        import httpx
        payload = {
            "org_id": alert.org_id,
            "resource": alert.resource_key,
            "severity": alert.severity.value,
            "current": alert.current,
            "limit": alert.limit,
            "utilization": round(alert.utilization, 3),
            "message": alert.message,
            "timestamp": alert.timestamp.isoformat(),
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self._url, json=payload, headers=self._headers)
                if resp.status_code >= 400:
                    logger.error("webhook_alert_failed url=%s status=%d", self._url, resp.status_code)
        except Exception as e:
            logger.error("webhook_alert_error url=%s error=%s", self._url, str(e))


class AlertManager:
    """Manages alert dispatching with cooldown to prevent spam.

    Tracks the last severity alerted per org+resource. Only dispatches when:
    - Severity escalates (WARNING → CRITICAL)
    - Cooldown has expired since last alert at this severity
    """

    def __init__(
        self,
        redis: Redis,
        dispatchers: Optional[list[AlertDispatcher]] = None,
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
    ):
        self._redis = redis
        self._dispatchers = dispatchers or [LogAlertDispatcher()]
        self._cooldown = cooldown_seconds

    _SEVERITY_ORDER = {
        AlertSeverity.WARNING.value: 1,
        AlertSeverity.CRITICAL.value: 2,
        AlertSeverity.EXCEEDED.value: 3,
    }

    async def maybe_alert(self, alert: QuotaAlert) -> bool:
        """Dispatch alert if cooldown allows. Returns True if dispatched."""
        if alert.severity in (AlertSeverity.INFO,):
            return False  # don't alert on INFO

        cache_key = f"quota:alert:{alert.org_id}:{alert.resource_key}"
        last_severity = await self._redis.get(cache_key)

        if last_severity:
            last_sev = last_severity.decode() if isinstance(last_severity, bytes) else last_severity
            if self._SEVERITY_ORDER.get(alert.severity.value, 0) <= self._SEVERITY_ORDER.get(last_sev, 0):
                return False  # already alerted at this or higher severity

        # Atomically claim the right to dispatch this alert.
        # SET NX prevents duplicate dispatches from concurrent requests.
        dispatch_key = f"{cache_key}:dispatch:{alert.severity.value}"
        acquired = await self._redis.set(dispatch_key, "1", ex=60, nx=True)
        if not acquired:
            return False

        # Dispatch to all registered dispatchers
        for dispatcher in self._dispatchers:
            try:
                await dispatcher.dispatch(alert)
            except Exception as e:
                logger.error("alert_dispatch_error dispatcher=%s error=%s", type(dispatcher).__name__, str(e))

        # Record this alert with cooldown TTL
        await self._redis.set(cache_key, alert.severity.value, ex=self._cooldown)
        return True
