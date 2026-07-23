"""MSG lane (ticket 20260722_end_customer_experience_defects,
TICKET_config_is_king.md §5d D-CK-1 / D-CK-3) — the config is king.

RED-first. Every test here failed on its OWN assertion against 0.6.2's
`messages.py`, whose two lookup tables encoded ONE consumer's vocabulary
(`ACTION_HINTS`) and ONE consumer's tier ladder (`UPGRADE_TIER_MAP`).
Reproduced wrong strings, verbatim, before the fix:

  free/pro catalog   "You've reached the maximum of 1 widgets on the Free plan.
                      Or upgrade to Starter for a higher limit."
                     -> names a plan absent from the catalog; dangling "Or";
                        "1 widgets"
  top tier (pro)     "You've reached the maximum of 50 widgets on the Pro plan.
                      Or upgrade to Enterprise for a higher limit."
                     -> invents a tier above the consumer's highest
  zero limit         "Widgets are not available on the Free plan. Upgrade to
                      Starter to unlock this feature."
  feature_locked     "This feature is not available on the Free plan. Upgrade
                      to Starter to unlock it."

The law under test: every noun in a customer-facing sentence comes from THAT
consumer's config — tier `display_name` reached by `sort_order`, resource
`display_name`, `unit`, `action_hint` (D-CK-1) — and anything the config does
not supply is OMITTED, never invented and never left dangling.
"""
from __future__ import annotations

import re

import pytest

from ab0t_quota.messages import MessageBuilder
from ab0t_quota.models.core import (
    AlertSeverity,
    CounterType,
    QuotaState,
    ResourceDef,
    TierConfig,
    TierLimits,
)

# --- a NON-sandbox consumer: generic SaaS vocabulary, two tiers ------------
WIDGETS = ResourceDef(
    service="acme", resource_key="widgets.active", display_name="Widgets",
    counter_type=CounterType.GAUGE, unit="widgets",
    action_hint="Archive a widget to free up a slot.",
)
WIDGETS_NO_HINT = ResourceDef(
    service="acme", resource_key="widgets.active", display_name="Widgets",
    counter_type=CounterType.GAUGE, unit="widgets",
)
SEATS = ResourceDef(
    service="acme", resource_key="seats.active", display_name="Seats",
    counter_type=CounterType.GAUGE, unit="seats",
)

FREE = TierConfig(
    tier_id="free", display_name="Free", sort_order=0, upgrade_url="/billing/upgrade",
    features={"widgets"},
    limits={"widgets.active": TierLimits(limit=1), "seats.active": TierLimits(limit=0)},
)
PRO = TierConfig(
    tier_id="pro", display_name="Pro", sort_order=1, features={"widgets", "sso"},
    limits={"widgets.active": TierLimits(limit=50), "seats.active": TierLimits(limit=25)},
)
TWO_TIER = {"free": FREE, "pro": PRO}

SOLO = TierConfig(
    tier_id="solo", display_name="Solo", sort_order=0,
    limits={"widgets.active": TierLimits(limit=1)},
)
SINGLE_TIER = {"solo": SOLO}

# Every tier name any hardcoded ladder ever knew. None may appear in a message
# built from a catalog that does not contain it.
_INVENTED = ("Starter", "Enterprise", "Pro", "Free", "Solo")


def _tiers_named(msg: str, catalog: dict) -> list[str]:
    known = {t.display_name for t in catalog.values()}
    return [n for n in _INVENTED if re.search(rf"\b{n}\b", msg) and n not in known]


# ---------------------------------------------------------------------------
# 1. No tier name that is not in THIS consumer's catalog (ticket §5 control 1)
# ---------------------------------------------------------------------------

def test_two_tier_catalog_never_names_a_plan_it_does_not_have():
    msg = MessageBuilder.deny(WIDGETS, FREE, 1, 1, 1, tiers=TWO_TIER)
    assert _tiers_named(msg, TWO_TIER) == [], f"invented a plan: {msg!r}"
    assert "Pro" in msg, f"the real next tier must be offered: {msg!r}"


def test_highest_tier_offers_no_upgrade_at_all():
    msg = MessageBuilder.deny(WIDGETS, PRO, 50, 50, 1, tiers=TWO_TIER)
    assert _tiers_named(msg, TWO_TIER) == [], f"invented a plan above the top: {msg!r}"
    assert "upgrade" not in msg.lower(), f"offered an upgrade that does not exist: {msg!r}"


def test_single_tier_config_does_not_dangle():
    msg = MessageBuilder.deny(WIDGETS, SOLO, 1, 1, 1, tiers=SINGLE_TIER)
    assert _tiers_named(msg, SINGLE_TIER) == []
    assert " Or " not in msg and not msg.strip().endswith("Or"), \
        f"dangling connector with no upgrade clause: {msg!r}"
    # the sentence must still read correctly end-to-end
    assert msg.endswith("."), msg


def test_no_catalog_supplied_omits_the_upgrade_clause():
    """A caller that passes no catalog gets a correct quieter message — never
    a guessed ladder (the old default was `_default: "Starter"`)."""
    msg = MessageBuilder.deny(WIDGETS, FREE, 1, 1, 1)
    assert _tiers_named(msg, {"free": FREE}) == [], msg
    assert "upgrade" not in msg.lower(), msg


def test_next_tier_must_actually_offer_more_of_this_resource():
    """`seats.active` is 0 on free and 25 on pro -> Pro qualifies. If no
    higher tier grants more, no upgrade is promised."""
    flat = TierConfig(tier_id="flat", display_name="Flat", sort_order=1,
                      limits={"widgets.active": TierLimits(limit=1)})
    catalog = {"free": FREE, "flat": flat}
    msg = MessageBuilder.deny(WIDGETS, FREE, 1, 1, 1, tiers=catalog)
    assert "upgrade" not in msg.lower(), \
        f"promised 'a higher limit' from a tier with the same limit: {msg!r}"


# ---------------------------------------------------------------------------
# 2. Action hints come from config (D-CK-1), never from a library table
# ---------------------------------------------------------------------------

def test_action_hint_is_read_from_the_resource_def():
    msg = MessageBuilder.deny(WIDGETS, FREE, 1, 1, 1)
    assert "Archive a widget to free up a slot." in msg, msg


def test_absent_action_hint_omits_the_sentence_cleanly():
    msg = MessageBuilder.deny(WIDGETS_NO_HINT, FREE, 1, 1, 1)
    assert "Archive" not in msg
    assert "  " not in msg, f"whitespace scar from an omitted clause: {msg!r}"
    assert " Or " not in msg, f"connector survived its missing antecedent: {msg!r}"


def test_sandbox_vocabulary_is_no_longer_reachable():
    """The library must not know what a sandbox is. A resource key that used
    to hit ACTION_HINTS now gets exactly what its config declares."""
    legacy = ResourceDef(service="acme", resource_key="sandbox.concurrent",
                         display_name="Boxes", counter_type=CounterType.GAUGE,
                         unit="boxes")
    msg = MessageBuilder.deny(legacy, FREE, 1, 1, 1)
    assert "sandbox" not in msg.lower(), f"library-owned sandbox copy leaked: {msg!r}"


# ---------------------------------------------------------------------------
# 3. Units pluralise correctly
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("limit,expected", [(1, "1 widget"), (2, "2 widgets")])
def test_units_pluralise(limit, expected):
    msg = MessageBuilder.deny(WIDGETS, FREE, limit, limit, 1)
    assert expected in msg, msg
    if limit == 1:
        assert "1 widgets" not in msg, msg


def test_singular_only_unit_is_left_alone():
    usd = ResourceDef(service="acme", resource_key="spend.monthly",
                      display_name="Monthly Spend", counter_type=CounterType.ACCUMULATOR,
                      unit="USD", reset_period="monthly", precision=2)
    msg = MessageBuilder.deny(usd, FREE, 1, 1, 1)
    assert "1 USD" in msg, msg


# ---------------------------------------------------------------------------
# 4. Zero-limit / feature-locked paths obey the same law
# ---------------------------------------------------------------------------

def test_zero_limit_uses_the_real_next_tier():
    msg = MessageBuilder.deny(SEATS, FREE, 0, 0, 1, tiers=TWO_TIER)
    assert _tiers_named(msg, TWO_TIER) == [], msg
    assert "Pro" in msg and "Seats" in msg, msg


def test_feature_locked_names_a_tier_that_has_the_feature():
    msg = MessageBuilder.feature_locked("sso", FREE, tiers=TWO_TIER)
    assert _tiers_named(msg, TWO_TIER) == [], msg
    assert "Pro" in msg, msg


def test_feature_locked_omits_upgrade_when_no_tier_has_it():
    msg = MessageBuilder.feature_locked("time_travel", FREE, tiers=TWO_TIER)
    assert "upgrade" not in msg.lower(), msg
    assert "not available" in msg.lower()


# ---------------------------------------------------------------------------
# 5. D-CK-3 — the configured thresholds win in EVERY path
# ---------------------------------------------------------------------------

def test_warning_wording_follows_the_configured_critical_threshold():
    """A consumer whose critical_threshold is 0.5 is CRITICAL at 60% — the
    enforcement path already agrees (engine.py:1266,1273); the sentence did
    not (it hardcoded >= 95)."""
    msg = MessageBuilder.warning(WIDGETS, FREE, 0, 10, 6.0,
                                 warning_threshold=0.30, critical_threshold=0.50)
    assert "almost" in msg.lower(), \
        f"wording ignored the configured critical_threshold: {msg!r}"


def test_warning_wording_respects_a_raised_threshold_too():
    msg = MessageBuilder.warning(WIDGETS, FREE, 0, 10, 9.6,
                                 warning_threshold=0.90, critical_threshold=0.99)
    assert "almost" not in msg.lower(), \
        f"called 96% critical when the config says 99%: {msg!r}"


def test_quota_state_severity_follows_configured_thresholds():
    st = QuotaState(org_id="o", resource_key="widgets.active", current=6.0,
                    limit=10.0, tier_id="free",
                    warning_threshold=0.30, critical_threshold=0.50)
    assert st.severity is AlertSeverity.CRITICAL, \
        f"severity ignored the configured thresholds (got {st.severity})"

    st2 = QuotaState(org_id="o", resource_key="widgets.active", current=9.6,
                     limit=10.0, tier_id="free",
                     warning_threshold=0.90, critical_threshold=0.99)
    assert st2.severity is AlertSeverity.WARNING, \
        f"severity hardcoded 0.95 over the configured 0.99 (got {st2.severity})"


def test_quota_state_defaults_are_the_schema_defaults_not_literals():
    """Unspecified thresholds keep today's behaviour, sourced from TierLimits
    (the one place the default is declared) rather than a second copy."""
    assert QuotaState.model_fields["warning_threshold"].default == \
        TierLimits.model_fields["warning_threshold"].default
    assert QuotaState.model_fields["critical_threshold"].default == \
        TierLimits.model_fields["critical_threshold"].default
    st = QuotaState(org_id="o", resource_key="widgets.active", current=8.5, limit=10.0)
    assert st.severity is AlertSeverity.WARNING


# ---------------------------------------------------------------------------
# 6. The engine threads the catalog + thresholds through (a message built
#    without them is the defect, not a caller mistake)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_engine_denial_message_uses_the_configured_catalog():
    import fakeredis.aioredis
    from ab0t_quota.engine import QuotaEngine
    from ab0t_quota.models.requests import QuotaCheckRequest, QuotaIncrementRequest
    from ab0t_quota.providers import StaticTierProvider
    from ab0t_quota.registry import ResourceRegistry

    registry = ResourceRegistry()
    registry.register(WIDGETS)
    engine = QuotaEngine(
        redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
        tier_provider=StaticTierProvider({"org-1": "free"}, default_tier="free"),
        registry=registry,
        tiers=TWO_TIER,
    )
    await engine.increment(QuotaIncrementRequest(
        org_id="org-1", resource_key="widgets.active", amount=1))
    res = await engine.check(QuotaCheckRequest(org_id="org-1",
                                               resource_key="widgets.active"))
    assert res.decision.value == "deny", res
    assert _tiers_named(res.message, TWO_TIER) == [], res.message
    assert "Pro" in res.message, res.message
    assert "Archive a widget" in res.message, res.message
