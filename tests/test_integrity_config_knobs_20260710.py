"""P0.6 — Config-knob red suite (ticket 20260709_ab0t_quota_systemic_integrity_redesign).

RED-BY-DESIGN. Asserts the documented behaviour of enforcement knobs and the
explicit-outcome contract for unknown bundles/tiers, so each test FAILS on the
current Python runtime (which ignores the knobs / fails silently).

Findings covered:
  - QP-01  The Python runtime ignores enforcement.enabled / shadow_mode /
           global_kill_switch. The ONLY effect of `enabled:false` is a startup
           log warning (setup.py:226-229); shadow_mode and global_kill_switch
           appear NOWHERE in the engine/middleware (grep-verified). The Go
           engine honours all three (engine.go:49-64,164). The documented
           semantics the fix must mirror (P1.5):
             * global_kill_switch=true -> DENY everything ("global_kill_switch")
             * enabled=false           -> ALLOW everything ("enforcement_disabled")
             * shadow_mode=true        -> a would-be DENY becomes an ALLOW
                                          ("shadow_would_deny"), logged not enforced
  - QP-02  Silent fail-open / fail-wrong cliffs:
             * Unknown bundle name -> trivially allowed with no log
               (engine.py:282-286). A typo in resource_bundles disables
               enforcement for that create path.
             * Unknown tier id -> silent fallback to `free` (engine.py:107-109);
               a mis-mapped PAYING org is denied paid capacity. Go returns an
               explicit `tier_not_in_config` instead.

BEHAVIOURAL, not construction-coupled (repaired 2026-07-10 per V-BATCH GATE-A +
DECISIONS D-15). The knob is driven through the REAL config path
(`config.load_enforcement`, the seam `setup.py` uses), and the assertions are on
`check()` BEHAVIOUR (deny/allow), NOT on a missing kwarg. That way a fix that
merely ACCEPTS `enforcement=` but leaves it inert cannot pass — the test only
greens when the engine actually honours the knob.

Green target (Go parity, engine.go:49-64,164): global_kill_switch → DENY
('global_kill_switch'); enabled=false → ALLOW ('enforcement_disabled');
shadow_mode → a would-be DENY becomes SHADOW_ALLOW ('shadow_would_deny').

Default-outcome note (QP-02 unknown bundle): fail-closed (deny) in enforce mode
per D-14; forced allow_warn under shadow_mode.
"""
from __future__ import annotations

import fakeredis.aioredis
import pytest
import pytest_asyncio

from ab0t_quota.config import load_enforcement
from ab0t_quota.engine import QuotaEngine
from ab0t_quota.models.core import CounterType, ResourceDef, TierConfig, TierLimits
from ab0t_quota.models.requests import QuotaCheckRequest, QuotaIncrementRequest
from ab0t_quota.providers import StaticTierProvider
from ab0t_quota.registry import ResourceRegistry


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.flushall()
    await r.aclose()


RESOURCE = ResourceDef(
    service="test",
    resource_key="sandbox.concurrent",
    display_name="Concurrent Sandboxes",
    counter_type=CounterType.GAUGE,
    unit="sandboxes",
)

TIERS = {
    "free": TierConfig(
        tier_id="free", display_name="Free",
        limits={"sandbox.concurrent": TierLimits(limit=10)},
    ),
}


def _engine(redis, *, org_tier: str = "free", enforcement=None, bundles=None) -> QuotaEngine:
    registry = ResourceRegistry()
    registry.register(RESOURCE)
    kwargs = dict(
        redis=redis,
        tier_provider=StaticTierProvider({"org-1": org_tier}),
        registry=registry,
        tiers=TIERS,
    )
    if bundles is not None:
        kwargs["resource_bundles"] = bundles
    if enforcement is not None:
        # Drive the knob through the REAL config parser (the seam setup.py uses),
        # so the test exercises config -> engine plumbing, not a bare kwarg.
        kwargs["enforcement"] = load_enforcement({"enforcement": enforcement})
    return QuotaEngine(**kwargs)


# ---------------------------------------------------------------------------
# QP-01 — enforcement knobs
# ---------------------------------------------------------------------------

class TestEnforcementKnobs:
    @pytest.mark.asyncio
    async def test_global_kill_switch_denies_everything(self, redis):
        """global_kill_switch=true must fail closed: even a well-under-limit
        request is DENIED with reason 'global_kill_switch'.

        RED today: the knob is inert (the engine does not accept it and would
        not honour it) — an under-limit request is ALLOWED.
        """
        engine = _engine(
            redis,
            enforcement={"enabled": True, "shadow_mode": False, "global_kill_switch": True},
        )
        result = await engine.check(
            QuotaCheckRequest(org_id="org-1", resource_key="sandbox.concurrent")
        )
        assert result.denied is True, (
            "QP-01: global_kill_switch is inert in Python — the emergency kill "
            "switch allowed an under-limit request instead of failing closed."
        )

    @pytest.mark.asyncio
    async def test_enforcement_disabled_allows_over_limit(self, redis):
        """enabled=false must allow everything without computing limits.

        RED today: the knob only logs at startup; the engine still hard-denies
        an over-limit request.
        """
        engine = _engine(
            redis,
            enforcement={"enabled": False, "shadow_mode": False, "global_kill_switch": False},
        )
        # Drive the gauge to the limit so a normal check would DENY.
        for _ in range(10):
            await engine.increment(
                QuotaIncrementRequest(org_id="org-1", resource_key="sandbox.concurrent")
            )
        result = await engine.check(
            QuotaCheckRequest(org_id="org-1", resource_key="sandbox.concurrent")
        )
        assert result.denied is False, (
            "QP-01: enforcement.enabled=false is inert in Python — an over-limit "
            "request was still denied. `enabled:false` only logs a startup warning."
        )

    @pytest.mark.asyncio
    async def test_shadow_mode_converts_deny_to_allow(self, redis):
        """shadow_mode=true must convert a would-be DENY into an ALLOW (logged
        as 'shadow_would_deny'), so the mandated 'ship shadow first' rollout
        does not silently hard-enforce.

        RED today: shadow_mode appears nowhere in the Python engine; an
        over-limit request is hard-denied.
        """
        engine = _engine(
            redis,
            enforcement={"enabled": True, "shadow_mode": True, "global_kill_switch": False},
        )
        for _ in range(10):
            await engine.increment(
                QuotaIncrementRequest(org_id="org-1", resource_key="sandbox.concurrent")
            )
        result = await engine.check(
            QuotaCheckRequest(org_id="org-1", resource_key="sandbox.concurrent")
        )
        assert result.denied is False, (
            "QP-01: shadow_mode is inert in Python — an over-limit request was "
            "hard-denied instead of shadow-allowed. Rollout in shadow silently "
            "enforces for real."
        )


# ---------------------------------------------------------------------------
# QP-02 — silent fail-open / fail-wrong cliffs
# ---------------------------------------------------------------------------

class TestUnknownBundleAndTier:
    @pytest.mark.asyncio
    async def test_unknown_bundle_is_not_silently_allowed(self, redis):
        """A typo'd / undeclared bundle name must NOT silently disable
        enforcement. Recommended default: fail closed (deny) in enforce mode.

        RED today: check_for_bundle('typo') returns allowed=True with no log
        (engine.py:283-286).
        """
        engine = _engine(redis, bundles={"sandbox": ["sandbox.concurrent"]})
        result = await engine.check_for_bundle("org-1", "sandbox_typo")
        assert result.allowed is False, (
            "QP-02: an unknown/typo bundle name was silently allowed — a typo in "
            "resource_bundles disables enforcement for that create path with no "
            "signal. (Recommended default: deny-in-enforce; see the framed decision.)"
        )

    @pytest.mark.asyncio
    async def test_unknown_tier_is_surfaced_not_silently_free(self, redis):
        """A paying org mapped to a tier id that is not in the config must
        surface a config error (Go returns `tier_not_in_config`), not silently
        fall back to `free` — which would DENY the paying org's paid capacity.

        RED today: the engine silently coerces the unknown tier to `free`
        (engine.py:107-109), so result.tier_id == 'free'.
        """
        engine = _engine(redis, org_tier="enterprise_v2")  # not present in TIERS
        result = await engine.check(
            QuotaCheckRequest(org_id="org-1", resource_key="sandbox.concurrent")
        )
        assert result.tier_id != "free", (
            "QP-02: an unknown tier id was silently coerced to 'free' — a "
            "mis-mapped paying org is denied paid capacity instead of the config "
            "error being surfaced (Go returns 'tier_not_in_config')."
        )
