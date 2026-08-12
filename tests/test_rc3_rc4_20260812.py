"""RC3 (durable tier-catalog publish) + RC4 (sustained tier-lookup-failure
detection) — ticket 20260810_billing_resilience_and_erroneous_tier_downgrade
TICKET.md P4/P5; verify_fabel_5_report_20260812 Hole H7. 2026-08-12.

RC3 contract: a FAILED startup catalog publish schedules a background
retry-until-acked loop (capped backoff). The catalog is DERIVED state (a pure
function of consumer config), so process-lifetime retry + republish-on-restart
IS the durable contract. The capabilities dict always reflects the state
("retrying"/"published") so an unpublished catalog is a visible degradation.

RC4 contract: LKG serving keeps customers safe during a billing outage, but
must never make the outage SILENT — consecutive per-org fetch failures are
tracked (reset on success) and escalate to an ERROR-level
`tier_lookup_failing_sustained` line at the threshold and every multiple.
Detection-only: what is served never changes.
"""
import asyncio
import logging

import pytest

import ab0t_quota.setup as setup_mod
from ab0t_quota.providers import AuthServiceTierProvider


# --------------------------------------------------------------------------- #
# RC4 — sustained-failure escalation (detection-only)
# --------------------------------------------------------------------------- #
def _failing_provider(threshold=3):
    async def fetch(org_id):
        raise RuntimeError("billing down")

    p = AuthServiceTierProvider(fetch_fn=fetch, redis=None, default_tier="free")
    p.SUSTAINED_FAILURE_THRESHOLD = threshold
    return p


@pytest.mark.asyncio
async def test_rc4_streak_counts_and_escalates_at_threshold(caplog):
    p = _failing_provider(threshold=3)
    with caplog.at_level(logging.WARNING, logger="ab0t_quota.providers"):
        for _ in range(3):
            tier = await p.get_tier("org1")
            assert tier == "free"  # serving unchanged (uncached default, no LKG)
    assert p.consecutive_failures("org1") == 3
    errors = [r for r in caplog.records
              if r.levelno >= logging.ERROR
              and "tier_lookup_failing_sustained" in r.getMessage()]
    assert len(errors) == 1  # fires exactly at the threshold, not before


@pytest.mark.asyncio
async def test_rc4_refires_at_every_threshold_multiple_not_every_request(caplog):
    p = _failing_provider(threshold=3)
    with caplog.at_level(logging.ERROR, logger="ab0t_quota.providers"):
        for _ in range(9):
            await p.get_tier("org1")
    errors = [r for r in caplog.records
              if "tier_lookup_failing_sustained" in r.getMessage()]
    assert len(errors) == 3  # at 3, 6, 9 — never per-request spam


@pytest.mark.asyncio
async def test_rc4_success_resets_the_streak():
    calls = {"n": 0}

    async def flaky(org_id):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("blip")
        return "pro"

    p = AuthServiceTierProvider(fetch_fn=flaky, redis=None, default_tier="free")
    p.SUSTAINED_FAILURE_THRESHOLD = 3
    await p.get_tier("org1")
    await p.get_tier("org1")
    assert p.consecutive_failures("org1") == 2
    assert await p.get_tier("org1") == "pro"
    assert p.consecutive_failures("org1") == 0


@pytest.mark.asyncio
async def test_rc4_streaks_are_per_org():
    p = _failing_provider(threshold=10)
    await p.get_tier("org1")
    await p.get_tier("org1")
    await p.get_tier("org2")
    assert p.consecutive_failures("org1") == 2
    assert p.consecutive_failures("org2") == 1


# --------------------------------------------------------------------------- #
# RC3 — catalog retry-until-acked
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_rc3_retries_until_acked_and_updates_capabilities(monkeypatch):
    attempts = []

    async def fake_publish(service_name, tiers, *, registry=None, bundles=None):
        attempts.append(service_name)
        return len(attempts) >= 3  # fail, fail, succeed

    monkeypatch.setattr(setup_mod, "_publish_tier_catalog", fake_publish)
    monkeypatch.setattr(setup_mod, "CATALOG_RETRY_BACKOFF_SECONDS", (0,))
    caps = {"tier_catalog": "retrying(startup publish failed)"}

    await asyncio.wait_for(
        setup_mod._retry_publish_tier_catalog_until_acked(
            "svc-x", {}, registry=None, bundles=None, caps=caps),
        timeout=5,
    )
    assert len(attempts) == 3
    assert caps["tier_catalog"] == "published(after 3 retries)"


@pytest.mark.asyncio
async def test_rc3_survives_a_raising_publish_and_keeps_retrying(monkeypatch):
    attempts = []

    async def raising_then_ok(service_name, tiers, *, registry=None, bundles=None):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("unexpected")  # must not kill the loop
        return True

    monkeypatch.setattr(setup_mod, "_publish_tier_catalog", raising_then_ok)
    monkeypatch.setattr(setup_mod, "CATALOG_RETRY_BACKOFF_SECONDS", (0,))
    caps = {}
    await asyncio.wait_for(
        setup_mod._retry_publish_tier_catalog_until_acked(
            "svc-x", {}, registry=None, bundles=None, caps=caps),
        timeout=5,
    )
    assert len(attempts) == 2
    assert caps["tier_catalog"].startswith("published")


@pytest.mark.asyncio
async def test_rc3_cancellation_is_clean(monkeypatch):
    """Shutdown mid-retry: the task cancels without swallowing CancelledError."""
    async def never_ok(service_name, tiers, *, registry=None, bundles=None):
        return False

    monkeypatch.setattr(setup_mod, "_publish_tier_catalog", never_ok)
    monkeypatch.setattr(setup_mod, "CATALOG_RETRY_BACKOFF_SECONDS", (0,))
    task = asyncio.create_task(
        setup_mod._retry_publish_tier_catalog_until_acked(
            "svc-x", {}, registry=None, bundles=None, caps={}))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
