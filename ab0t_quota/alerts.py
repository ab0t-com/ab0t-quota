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

from .models.core import QuotaAlert, AlertSeverity, QuotaMetric

logger = logging.getLogger("ab0t_quota.alerts")
metrics_logger = logging.getLogger("ab0t_quota.metrics")

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


# ---------------------------------------------------------------------------
# Metric dispatchers (F9/P4.1, ticket 20260810_quota_drift_live_recurrence_permanent_fix)
# ---------------------------------------------------------------------------
#
# WHY THESE EXIST, SEPARATE FROM AlertDispatcher: an alert is cooldown-gated
# (never spam a human) and free-text. A metric is the opposite — never
# cooldown-gated (a dashboard needs every observation to chart drift over
# time) and strictly numeric/low-cardinality, so a downstream sink can
# threshold or graph it. Reusing AlertDispatcher for both would force one of
# the two contracts to lose (either the human gets spammed, or the metric
# gets holes). This module previously logged `gauge_drift_detected` /
# `gauge_drift_resolved` ONLY as part of a QuotaAlert's message string — a
# human can read that, but nothing can chart it. A MetricDispatcher fixes
# that without touching the alert path at all.

class MetricDispatcher(ABC):
    """Base class for metric emission — a numeric SINK, distinct from
    AlertDispatcher's human-facing notification. Multiple dispatchers may be
    registered (e.g. a scrapeable Redis counter AND a structured log line)."""

    @abstractmethod
    async def emit(self, metric: QuotaMetric) -> None:
        """Record one metric observation. Implementation decides the sink."""


class LogMetricDispatcher(MetricDispatcher):
    """Emit metrics as a structured, grep/scrape-able log line (default,
    always active). Distinct logger + line SHAPE (`name=... key=value`) from
    QuotaAlert's message-based line, so a log-based metrics pipeline (Datadog
    log-metrics, CloudWatch Logs Insights, a `journalctl | grep` cron, or a
    Prometheus log exporter) can parse it without string-matching a sentence
    meant for a human."""

    async def emit(self, metric: QuotaMetric) -> None:
        metrics_logger.info(
            "quota_metric name=%s org_id=%s resource_key=%s type=%s observed=%s ledger=%s "
            "counter_before=%s counter_after=%s delta=%s source=%s",
            metric.name, metric.org_id, metric.resource_key, metric.resource_type,
            metric.observed, metric.ledger, metric.counter_before, metric.counter_after,
            metric.delta, metric.source,
        )


class RedisCounterMetricDispatcher(MetricDispatcher):
    """The "if there is no metrics sink" fallback (F9/P4.1). Every consumer
    already has the engine's Redis — this needs no new infrastructure, no
    statsd agent, no CloudWatch IAM grant. It increments plain counters an
    operator (or a future statsd/CloudWatch bridge) can read directly with
    GET — a `docker exec ... redis-cli GET quota:metrics:drift_detected:total`
    already works.

    Keys (all under ``key_prefix``, default ``"quota:metrics"``):
      * ``{prefix}:drift_detected:total``               — INCR, all-time count.
      * ``{prefix}:drift_detected:{org}:{resource_key}`` — INCR, per (org, resource).
      * ``{prefix}:drift_resolved:total``                — INCR, all-time count.
      * ``{prefix}:drift:{org}:{resource_key}:sustained`` — consecutive detects
        with no intervening resolve for this (org, resource); reset to 0 on a
        resolve. Mirrors ``DriftAlertManager``'s own sustained-pass tracker —
        this copy is the one an external scraper reads without importing the
        library.

    A dispatcher failure (a flaky Redis op) is caught and logged, never
    raised — recording a metric must never break the reconcile pass."""

    def __init__(self, redis: Redis, key_prefix: str = "quota:metrics"):
        self._redis = redis
        self._prefix = key_prefix

    def _sustained_key(self, org_id: str, resource_key: str) -> str:
        return f"{self._prefix}:drift:{org_id}:{resource_key}:sustained"

    async def emit(self, metric: QuotaMetric) -> None:
        try:
            if metric.name == "quota.drift_detected":
                await self._redis.incr(f"{self._prefix}:drift_detected:total")
                await self._redis.incr(
                    f"{self._prefix}:drift_detected:{metric.org_id}:{metric.resource_key}")
                await self._redis.incr(self._sustained_key(metric.org_id, metric.resource_key))
            elif metric.name == "quota.drift_resolved":
                await self._redis.incr(f"{self._prefix}:drift_resolved:total")
                await self._redis.delete(self._sustained_key(metric.org_id, metric.resource_key))
        except Exception as e:
            logger.error("redis_counter_metric_dispatch_error metric=%s org=%s resource=%s "
                         "error=%s", metric.name, metric.org_id, metric.resource_key, e)


async def drift_metrics_snapshot(
    redis: Redis, *, metrics_prefix: str = "quota:metrics",
    sustained_threshold: int = 3, max_scan: int = 500,
) -> dict:
    """Read ``RedisCounterMetricDispatcher``'s scrapeable counters (F9/P4.1,
    ticket 20260810_quota_drift_live_recurrence_permanent_fix).

    This is the "expose a counter the platform can scrape" fallback: a
    consumer wires it into whatever HTTP surface they already have (setup.py
    mounts it on the library's own ``/quota/capabilities`` for every consumer
    automatically). Two cheap O(1) totals, plus a BOUNDED scan (never more
    than ``max_scan`` keys — a drift storm must not turn a status probe into
    its own incident, same ethos as the reconciler's own per-pass budget) for
    which (org, resource) pairs are CURRENTLY sustaining drift at or above
    ``sustained_threshold`` consecutive re-heals — i.e. exactly the set the
    ``quota_drift_sustained`` alert has fired (or will fire) for.
    """
    async def _get_int(key: str) -> int:
        v = await redis.get(key)
        return int(v) if v else 0

    snap: dict = {
        "drift_detected_total": await _get_int(f"{metrics_prefix}:drift_detected:total"),
        "drift_resolved_total": await _get_int(f"{metrics_prefix}:drift_resolved:total"),
        "sustained_threshold": sustained_threshold,
        "currently_sustaining": [],
        "scan_truncated": False,
    }
    scanned = 0
    suffix = ":sustained"
    key_root = f"{metrics_prefix}:drift:"
    try:
        async for key in redis.scan_iter(match=f"{key_root}*{suffix}", count=100):
            scanned += 1
            if scanned > max_scan:
                snap["scan_truncated"] = True
                break
            raw = await redis.get(key)
            count = int(raw) if raw else 0
            if count < sustained_threshold:
                continue
            k = key.decode() if isinstance(key, bytes) else str(key)
            body = k[len(key_root):-len(suffix)]  # "{org_id}:{resource_key}"
            org_id, _, resource_key = body.rpartition(":")
            snap["currently_sustaining"].append({
                "org_id": org_id, "resource_key": resource_key,
                "consecutive_passes": count,
            })
    except Exception as e:
        logger.error("drift_metrics_snapshot_scan_failed error=%s", e)
    return snap


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


class DriftAlertManager:
    """Alerts for the GAUGE reconciler (P4.3, ticket 20260709).

    This is a DISTINCT channel from ``AlertManager`` (which is escalation-only
    and emits no de-escalation). It exists because two second-order hazards
    (FUTURE §5 / D-26) bite a reconciler that reuses the escalation-only path:

      1. A drift that self-heals leaves NO "resolved" trail — so an on-call
         engineer sees a scary "drift detected" and never the "all clear".
      2. A reconciler force-set can ALERT-STORM: every pass re-fires.

    So this manager fires a PAIRED event set — ``gauge_drift_detected`` and
    ``gauge_drift_resolved`` — with the resolve keyed SEPARATELY from the detect
    cooldown, so a *heal* is never suppressed by a prior *detect* (this is the
    exact shape Go ships for ``over_limit_admitted`` / ``over_limit_resolved``).
    A ``detected`` is rate-limited by a cooldown; a ``resolved`` fires whenever
    an ``active`` marker exists, regardless of that cooldown.

    A reconciliation-induced transition therefore never reads as a fresh
    limit-incident: distinct message tags (``gauge_drift_*``), distinct
    keyspace, distinct cooldown.
    """

    def __init__(
        self,
        redis: Redis,
        dispatchers: Optional[list[AlertDispatcher]] = None,
        cooldown_seconds: int = 600,
        key_prefix: str = "quota:reconcile:drift",
        metric_dispatchers: Optional[list[MetricDispatcher]] = None,
        sustained_alert_threshold: int = 3,
    ):
        self._redis = redis
        self._dispatchers = dispatchers or [LogAlertDispatcher()]
        self._cooldown = cooldown_seconds
        self._prefix = key_prefix
        # F9/P4.1: metric_dispatchers defaults to a log line + a Redis counter —
        # both need zero extra config/infrastructure, so "no metrics sink wired"
        # never means "no observability" (the P4.1 fallback promise).
        self._metric_dispatchers = (
            list(metric_dispatchers) if metric_dispatchers is not None
            else [LogMetricDispatcher(), RedisCounterMetricDispatcher(redis)]
        )
        # Sustained-drift threshold: consecutive reconcile passes that had to
        # re-heal the SAME (org, resource) with no intervening resolve. A
        # single heal is expected (a crash, a slow provider); the SAME gauge
        # needing a heal N passes running is the signature of a RECURRING bug
        # (e.g. the F1 idempotency-key collision) — that is the case a metric
        # alone does not surface unless something is watching it, so the
        # library also raises a dedicated CRITICAL alert once the threshold
        # is crossed (proves the wiring end-to-end even with zero external
        # metrics infrastructure).
        self._sustained_threshold = sustained_alert_threshold

    def _active_key(self, org_id: str, resource_key: str) -> str:
        return f"{self._prefix}:{org_id}:{resource_key}:active"

    def _detect_cd_key(self, org_id: str, resource_key: str) -> str:
        return f"{self._prefix}:{org_id}:{resource_key}:detect_cd"

    def _unreachable_cd_key(self, org_id: str) -> str:
        return f"{self._prefix}:{org_id}:provider_unreachable_cd"

    async def _dispatch(self, alert: QuotaAlert) -> None:
        for dispatcher in self._dispatchers:
            try:
                await dispatcher.dispatch(alert)
            except Exception as e:  # a broken dispatcher must never break reconcile
                logger.error("drift_alert_dispatch_error dispatcher=%s error=%s",
                             type(dispatcher).__name__, str(e))

    async def _emit_metric(self, metric: QuotaMetric) -> None:
        """Fan the metric out to every configured MetricDispatcher. NEVER
        rate-limited (unlike ``_dispatch``'s alerts) — a metrics sink needs
        every observation, and a broken dispatcher must never break reconcile."""
        for dispatcher in self._metric_dispatchers:
            try:
                await dispatcher.emit(metric)
            except Exception as e:
                logger.error("drift_metric_dispatch_error dispatcher=%s error=%s",
                             type(dispatcher).__name__, str(e))

    def _sustained_key(self, org_id: str, resource_key: str) -> str:
        return f"{self._prefix}:{org_id}:{resource_key}:sustained_passes"

    def _sustained_cd_key(self, org_id: str, resource_key: str) -> str:
        return f"{self._prefix}:{org_id}:{resource_key}:sustained_cd"

    async def _track_sustained(self, org_id: str, resource_key: str) -> int:
        """INCR the consecutive-detect counter for (org, resource); returns the
        new count. TTL'd generously so an abandoned org's counter doesn't
        live forever."""
        key = self._sustained_key(org_id, resource_key)
        count = await self._redis.incr(key)
        await self._redis.expire(key, max(self._cooldown * 8, 3600))
        return int(count)

    async def _clear_sustained(self, org_id: str, resource_key: str) -> None:
        await self._redis.delete(self._sustained_key(org_id, resource_key))

    async def _maybe_sustained_alert(
        self, org_id: str, resource_key: str, *, count: int,
        observed: float, ledger: float, tier_id: str = "",
    ) -> bool:
        """Once ``count`` (consecutive re-heals with no intervening resolve)
        reaches the threshold, raise a distinct CRITICAL alert — a sustained
        drift is a RECURRING bug (this bug class's 4th sighting was exactly
        this: a reconciler that heals but keeps re-drifting), not a one-off
        blip a single heal already handles quietly. Rate-limited by its own
        cooldown key (separate from the plain detect cooldown) so it re-fires
        periodically while the condition persists, not once and never again."""
        if count < self._sustained_threshold:
            return False
        cd = self._sustained_cd_key(org_id, resource_key)
        if not await self._redis.set(cd, "1", ex=self._cooldown, nx=True):
            return False  # rate-limited: already fired inside the cooldown
        await self._dispatch(QuotaAlert(
            org_id=org_id, resource_key=resource_key,
            severity=AlertSeverity.CRITICAL,
            current=observed, limit=ledger,
            utilization=(observed / ledger) if ledger else 0.0,
            tier_id=tier_id,
            message=(
                f"quota_drift_sustained org={org_id} resource={resource_key} "
                f"consecutive_passes={count} observed={observed} ledger={ledger} — "
                f"this gauge has drifted and been re-healed {count} consecutive "
                f"reconcile passes with no intervening resolve. This reads as a "
                f"RECURRING bug (e.g. an idempotency-key collision on a reused "
                f"resource id), not a one-off blip — investigate the WRITE path, "
                f"not just the counter."
            ),
        ))
        return True

    # ---- D-75: infrastructure invariants (the guards' own boundary: TIME) ----------
    # Every guard we own (D-32 durability, D-71 topology, D-72 eviction, D-73 scripting,
    # D-76 DDB) verified the world ONCE, at boot, and then trusted it forever. A
    # `CONFIG SET maxmemory-policy allkeys-lru` at 3am — or a managed failover onto a
    # differently-configured replica — is invisible to all of them, and the counter becomes
    # silently evictable (under-count → phantom headroom → over-admission, D-31). The
    # re-check that catches it needs a HUMAN at the other end (D-40: an event with no sink
    # is not observability), and a PAIRED restore so an on-call engineer sees the all-clear
    # (D-26) rather than a scary violation that never closes.

    def _invariant_active_key(self, name: str) -> str:
        return f"{self._prefix}:invariant:{name}:active"

    def _invariant_cd_key(self, name: str) -> str:
        return f"{self._prefix}:invariant:{name}:detect_cd"

    async def invariant_violated(self, name: str, detail: str) -> bool:
        """An infrastructure invariant the library VERIFIED AT STARTUP has changed
        underneath us. Rate-limited by the detect cooldown; sets the persistent `active`
        marker so a later restore can pair with it. Returns True if dispatched."""
        await self._redis.set(self._invariant_active_key(name), detail[:200],
                              ex=max(self._cooldown * 8, 3600))
        if not await self._redis.set(self._invariant_cd_key(name), "1",
                                     ex=self._cooldown, nx=True):
            return False  # rate-limited: already fired inside the cooldown
        await self._dispatch(QuotaAlert(
            org_id="_platform", resource_key=name, tier_id="",
            severity=AlertSeverity.CRITICAL,
            current=0.0, limit=0.0, utilization=1.0,
            message=(f"infrastructure_invariant_violated capability={name} detail={detail} "
                     f"— the library verified this at STARTUP and it has CHANGED at runtime "
                     f"(D-75). Health is degraded; the service keeps serving. Drain or fix."),
        ))
        return True

    async def invariant_restored(self, name: str) -> bool:
        """The invariant is safe again. Fires whenever an `active` marker exists, regardless
        of the detect cooldown — a heal must never be swallowed by a prior violation."""
        active_key = self._invariant_active_key(name)
        if not await self._redis.get(active_key):
            return False
        await self._redis.delete(active_key)
        await self._redis.delete(self._invariant_cd_key(name))
        await self._dispatch(QuotaAlert(
            org_id="_platform", resource_key=name, tier_id="",
            severity=AlertSeverity.WARNING,
            current=0.0, limit=0.0, utilization=0.0,
            message=f"infrastructure_invariant_restored capability={name} (D-75)",
        ))
        return True

    async def drift_detected(
        self, org_id: str, resource_key: str, *,
        observed: float, ledger: float, before: float, after: float,
        source: str, tier_id: str = "", resource_type: str = "",
    ) -> bool:
        """Reconciler force-set the counter (a real drift). Rate-limited by the
        detect cooldown; sets the persistent ``active`` marker so a later
        ``drift_resolved`` can pair with it. Returns True if the (cooldown-gated)
        human ALERT was dispatched.

        F9/P4.1 (ticket 20260810): ALSO emits a structured ``quota.drift_detected``
        METRIC on EVERY call — never cooldown-gated, unlike the alert below, since
        a metrics sink needs full-fidelity data to chart drift over time — and
        tracks consecutive sustained passes, raising a distinct CRITICAL alert
        once ``sustained_alert_threshold`` is crossed (the signal that this is a
        RECURRING bug, not a one-off blip)."""
        delta = abs(after - before)
        await self._emit_metric(QuotaMetric(
            name="quota.drift_detected", org_id=org_id, resource_key=resource_key,
            resource_type=resource_type,
            observed=observed, ledger=ledger, counter_before=before,
            counter_after=after, delta=delta, source=source,
        ))
        sustained = await self._track_sustained(org_id, resource_key)
        await self._maybe_sustained_alert(
            org_id, resource_key, count=sustained, observed=observed,
            ledger=ledger, tier_id=tier_id)

        # Persist the active marker (long-lived; cleared only by a resolve) so a
        # heal can always find it. Its TTL is generous — a drift that never heals
        # should stay "active".
        await self._redis.set(self._active_key(org_id, resource_key), source,
                              ex=max(self._cooldown * 8, 3600))
        cd = self._detect_cd_key(org_id, resource_key)
        if not await self._redis.set(cd, "1", ex=self._cooldown, nx=True):
            return False  # rate-limited: a detect already fired inside the cooldown
        await self._dispatch(QuotaAlert(
            org_id=org_id, resource_key=resource_key,
            severity=AlertSeverity.WARNING,
            current=after, limit=observed,
            utilization=(after / observed) if observed else 0.0,
            tier_id=tier_id,
            message=(f"gauge_drift_detected source={source} observed={observed} "
                     f"ledger={ledger} counter_before={before} counter_after={after}"),
        ))
        return True

    async def drift_resolved(
        self, org_id: str, resource_key: str, *, value: float, tier_id: str = "",
        resource_type: str = "",
    ) -> bool:
        """The (org, resource) value now matches its authoritative level. Fires
        ONLY if a drift was previously active, then clears the markers. NOT
        suppressed by the detect cooldown — a resolve must never be swallowed by
        a prior admit. Returns True if dispatched.

        F9/P4.1 (ticket 20260810): the consecutive-sustained-passes counter is
        always cleared here (the streak breaks the moment we're back in sync),
        and a ``quota.drift_resolved`` METRIC (delta=0.0) is emitted paired 1:1
        with the human ``gauge_drift_resolved`` alert — same trigger condition
        (an active drift existed), so a metrics sink and an on-call human always
        see the SAME "all clear" transitions, never a metric-only or alert-only one."""
        active_key = self._active_key(org_id, resource_key)
        was_active = await self._redis.get(active_key)
        await self._clear_sustained(org_id, resource_key)
        if not was_active:
            return False
        await self._redis.delete(active_key)
        await self._redis.delete(self._detect_cd_key(org_id, resource_key))
        await self._redis.delete(self._sustained_cd_key(org_id, resource_key))
        await self._emit_metric(QuotaMetric(
            name="quota.drift_resolved", org_id=org_id, resource_key=resource_key,
            resource_type=resource_type,
            observed=value, ledger=value, counter_before=value, counter_after=value,
            delta=0.0, source="",
        ))
        await self._dispatch(QuotaAlert(
            org_id=org_id, resource_key=resource_key,
            severity=AlertSeverity.WARNING,
            current=value, limit=value, utilization=1.0, tier_id=tier_id,
            message=f"gauge_drift_resolved value={value}",
        ))
        return True

    async def divergence_detected(
        self, org_id: str, resource_key: str, *, provider: float, ledger: float,
        tier_id: str = "",
    ) -> bool:
        """D-33 §2 / D-36: the provider (reality) and the ledger (record) disagree
        about EXISTENCE. This is a BUG, not drift, and it must read as a MONEY
        incident — not a counter nit.

        When the provider sees MORE live than the ledger records, those resources
        have **no activation row**, so their usage **cannot be settled** —
        un-billable usage, QB-01's signature. That is CRITICAL. When the ledger
        over-states (phantom rows for resources no longer live), it is a WARNING
        over-count. Rate-limited under the drift detect cooldown (and marks the
        pair active so a later resolve fires)."""
        await self._redis.set(self._active_key(org_id, resource_key), "divergence",
                              ex=max(self._cooldown * 8, 3600))
        cd = self._detect_cd_key(org_id, resource_key)
        if not await self._redis.set(cd, "1", ex=self._cooldown, nx=True):
            return False
        unbillable = provider - ledger
        if unbillable > 0:
            severity = AlertSeverity.CRITICAL
            msg = (f"gauge_drift_detected ledger_reality_divergence UN-BILLABLE "
                   f"provider={provider} ledger={ledger} unbillable_live={unbillable} — "
                   f"{unbillable:g} resource(s) live with NO activation record; their "
                   f"usage CANNOT be settled (QB-01 signature). The record lost a row.")
        else:
            severity = AlertSeverity.WARNING
            msg = (f"gauge_drift_detected ledger_reality_divergence PHANTOM_RECORDS "
                   f"provider={provider} ledger={ledger} phantom_rows={-unbillable:g} — "
                   f"the ledger records resources that are no longer live (over-count).")
        await self._dispatch(QuotaAlert(
            org_id=org_id, resource_key=resource_key, severity=severity,
            current=provider, limit=ledger,
            utilization=(provider / ledger) if ledger else 0.0,
            tier_id=tier_id, message=msg,
        ))
        return True

    async def provider_unreachable(self, org_id: str, *, error: str = "") -> bool:
        """The observed_usage_provider raised. Per D-31 the reconciler did
        NOTHING for this org; this makes that visible + loud. Rate-limited so a
        sustained provider outage does not alert-storm."""
        cd = self._unreachable_cd_key(org_id)
        if not await self._redis.set(cd, "1", ex=self._cooldown, nx=True):
            return False
        await self._dispatch(QuotaAlert(
            org_id=org_id, resource_key="*",
            severity=AlertSeverity.CRITICAL,
            current=0.0, limit=0.0, utilization=0.0, tier_id="",
            message=("observed_usage_provider_unreachable — reconciler skipped this "
                     "org and did NOT converge to the ledger (D-31); an IO error may "
                     "never erase reality"),
        ))
        return True

    async def missing_observation(self, org_id: str, resource_key: str) -> bool:
        """D-51: the provider returned NO observation for this (org, resource) —
        the key was absent, not an explicit zero. Absence means UNKNOWN, never an
        affirmative value, so the reconciler left the counter alone and fails
        closed. Rate-limited under the detect cooldown so an incomplete provider
        does not alert-storm."""
        cd = self._detect_cd_key(org_id, resource_key)
        if not await self._redis.set(cd, "1", ex=self._cooldown, nx=True):
            return False
        await self._dispatch(QuotaAlert(
            org_id=org_id, resource_key=resource_key,
            severity=AlertSeverity.WARNING,
            current=0.0, limit=0.0, utilization=0.0, tier_id="",
            message=("gauge_reconcile_missing_observation — the provider returned NO "
                     "observation for this key (absence != zero, D-51); the counter "
                     "was left untouched rather than silently wiped to 0. If the "
                     "provider legitimately omits ⇒ zero, set empty_provider=converge."),
        ))
        return True

    async def per_user_sum_mismatch(
        self, org_id: str, resource_key: str, *, total: float, per_user_sum: float,
    ) -> bool:
        """D-53: the provider's per_user values do not sum to its org total — a
        CONSUMER bug. The reconciler converged from the numbers it was given AND
        alerts (never silently picks a side). Rate-limited under the detect
        cooldown."""
        cd = self._detect_cd_key(org_id, resource_key) + ":pus"
        if not await self._redis.set(cd, "1", ex=self._cooldown, nx=True):
            return False
        await self._dispatch(QuotaAlert(
            org_id=org_id, resource_key=resource_key,
            severity=AlertSeverity.WARNING,
            current=per_user_sum, limit=total,
            utilization=(per_user_sum / total) if total else 0.0, tier_id="",
            message=(f"gauge_reconcile_per_user_sum_mismatch total={total} "
                     f"sum_per_user={per_user_sum} — the provider is inconsistent "
                     f"(a consumer bug); converged from its numbers and alerting."),
        ))
        return True

    async def stale_open_activations(
        self, org_id: str, *, count: int, older_than_s: float,
    ) -> bool:
        """D2 (QB-03): N activations have been OPEN past the stale threshold with no
        release — the missed-decrement alarm for the leak that shipped three times.
        A leaked slot denies capacity (fail-closed) and its usage may accrue
        UNBILLED, so it reads as a money incident. Rate-limited per org."""
        cd = f"{self._prefix}:{org_id}:stale_open_cd"
        if not await self._redis.set(cd, "1", ex=self._cooldown, nx=True):
            return False
        await self._dispatch(QuotaAlert(
            org_id=org_id, resource_key="*",
            severity=AlertSeverity.WARNING,
            current=float(count), limit=0.0, utilization=0.0, tier_id="",
            message=(f"gauge_reconcile_stale_open_activations count={count} "
                     f"older_than_s={older_than_s:g} — {count} activation(s) OPEN with no "
                     f"release past the threshold; likely a MISSED DECREMENT (QB-03). A "
                     f"leaked slot denies capacity and its usage may accrue UNBILLED."),
        ))
        return True
