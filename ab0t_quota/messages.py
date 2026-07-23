"""Plain-English message templates for quota responses.

All user-facing messages go through here. No technical jargon — no
"quota_exceeded", no raw resource keys. These read like a helpful product,
not an error log.

THE CONFIG IS KING (ticket 20260722, D-CK-1/D-CK-2/D-CK-3). Every NOUN in a
sentence comes from the consumer's own `quota-config.json`:

  resource name  ResourceDef.display_name        unit  ResourceDef.unit
  remediation    ResourceDef.action_hint         plan  TierConfig.display_name
  upgrade target next TierConfig by sort_order    CTA  TierConfig.upgrade_url
  severity       TierLimits.warning/critical_threshold

Anything config does not supply is OMITTED — never invented, never left
dangling. Until 0.6.3 this module carried two lookup tables keyed on ONE
consumer's vocabulary (`ACTION_HINTS`) and ONE consumer's tier ladder
(`UPGRADE_TIER_MAP`), so a consumer with a `free`/`pro` catalog was told to
"upgrade to Starter" — a plan that does not exist in their product.

The SENTENCE stays library-owned (D-CK-2: no `messages` config section this
release); config supplies the facts. Copy lives in `Templates` — the shape
ported from the Go runtime's `messages/builder.go`, which got this right from
day one — so overriding it is a one-object change, not an edit to this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Union

from .models.core import ResourceDef, TierConfig, TierLimits

#: A tier catalog: the consumer's own `tiers[]`, keyed by tier_id or as a
#: plain iterable. `None` means "no catalog supplied" and yields a correct,
#: quieter message — never a guessed ladder.
TierCatalog = Union[Mapping[str, TierConfig], Iterable[TierConfig], None]

# Library-wide threshold defaults live in ONE place (TierLimits). Referencing
# them here rather than re-typing 0.80/0.95 is what makes D-CK-3 structural.
_DEFAULT_WARNING = TierLimits.model_fields["warning_threshold"].default
_DEFAULT_CRITICAL = TierLimits.model_fields["critical_threshold"].default


@dataclass(frozen=True)
class Templates:
    """The library's voice, in one overridable object (Go parity).

    Placeholders are `str.format` names; every value is supplied from config.
    A consumer wanting different wording passes a `Templates(...)` to the
    MessageBuilder methods — there is deliberately no config section for copy
    (D-CK-2), so there is no way to declare a template the renderer cannot
    fill.
    """
    deny: str = "You've reached the maximum of {limit} {unit} on the {tier} plan."
    deny_unavailable: str = "{resource} is not available on the {tier} plan."
    deny_unavailable_no_plan: str = "{resource} is not available on your current plan."
    upgrade: str = "Upgrade to {next_tier} for a higher limit."
    upgrade_alternative: str = "Or upgrade to {next_tier} for a higher limit."
    upgrade_unlock: str = "Upgrade to {next_tier} to unlock this feature."
    upgrade_link: str = "See {upgrade_url}."
    warning: str = ("You're using {used} of {limit} {unit} ({pct}%). "
                    "Consider upgrading if you need more.")
    critical: str = ("Almost at your limit: {used} of {limit} {unit} ({pct}%). "
                     "You'll be blocked from creating more soon.")
    allow: str = "{used} of {limit} {unit} used"
    allow_unlimited: str = "{resource}: unlimited"
    burst: str = ("You're over your {resource_lower} limit of {limit} {unit} "
                  "({used}/{limit}). Burst allowance is active — usage above "
                  "the limit may incur overage charges.")
    feature_locked: str = "This feature is not available on the {tier} plan."
    feature_locked_no_plan: str = "This feature is not available on your current plan."


DEFAULT_TEMPLATES = Templates()


class MessageBuilder:
    """Generates user-friendly messages for quota results.

    Every method takes the consumer's tier catalog as an OPTIONAL keyword —
    existing positional call sites are unchanged, and a caller that supplies
    no catalog simply gets no upgrade clause.
    """

    @staticmethod
    def deny(
        resource_def: ResourceDef,
        tier: TierConfig,
        current: float,
        limit: float,
        requested: float,
        *,
        tiers: TierCatalog = None,
        templates: Templates = DEFAULT_TEMPLATES,
    ) -> str:
        """Message for a hard denial (429)."""
        next_tier = _next_tier_for_resource(tier, tiers, resource_def.resource_key, limit)

        # Limit is 0 — the feature is not on this plan at all.
        if limit == 0:
            if next_tier is not None:
                return _join(
                    templates.deny_unavailable.format(
                        resource=resource_def.display_name, tier=tier.display_name),
                    templates.upgrade_unlock.format(next_tier=next_tier.display_name),
                    _link(templates, tier),
                )
            return templates.deny_unavailable_no_plan.format(
                resource=resource_def.display_name)

        action = (resource_def.action_hint or "").strip()
        upgrade = ""
        if next_tier is not None:
            tmpl = templates.upgrade_alternative if action else templates.upgrade
            upgrade = tmpl.format(next_tier=next_tier.display_name)

        return _join(
            templates.deny.format(limit=_fmt(limit),
                                  unit=_unit(resource_def.unit, limit),
                                  tier=tier.display_name),
            action,
            upgrade,
            _link(templates, tier) if upgrade else "",
        )

    @staticmethod
    def warning(
        resource_def: ResourceDef,
        tier: TierConfig,
        current: float,
        limit: float,
        after: float,
        *,
        warning_threshold: Optional[float] = None,
        critical_threshold: Optional[float] = None,
        templates: Templates = DEFAULT_TEMPLATES,
    ) -> str:
        """Message for a warning (allowed but approaching limit).

        D-CK-3: the CRITICAL wording fires at the tier's configured
        `critical_threshold`, the same value the enforcement path uses. A
        library that honours a threshold when denying and hardcodes 0.95 when
        speaking is not config-driven.
        """
        critical = _DEFAULT_CRITICAL if critical_threshold is None else critical_threshold
        utilization = (after / limit) if limit > 0 else 0.0
        fields = {
            "used": _fmt(after),
            "limit": _fmt(limit),
            "unit": _unit(resource_def.unit, after),
            "pct": int(utilization * 100),
            "resource": resource_def.display_name,
        }
        if utilization >= critical:
            return templates.critical.format(**fields)
        return templates.warning.format(**fields)

    @staticmethod
    def allow(
        resource_def: ResourceDef,
        current: float,
        limit: Optional[float],
        after: float,
        *,
        templates: Templates = DEFAULT_TEMPLATES,
    ) -> str:
        """Message for a clean allow (under limit)."""
        if limit is None:
            return templates.allow_unlimited.format(resource=resource_def.display_name)
        return templates.allow.format(used=_fmt(after), limit=_fmt(limit),
                                      unit=_unit(resource_def.unit, after))

    @staticmethod
    def burst(
        resource_def: ResourceDef,
        tier: TierConfig,
        current: float,
        limit: float,
        after: float,
        *,
        templates: Templates = DEFAULT_TEMPLATES,
    ) -> str:
        """Message for burst allowance (over limit but within burst cap)."""
        return templates.burst.format(
            resource_lower=resource_def.display_name.lower(),
            limit=_fmt(limit), unit=_unit(resource_def.unit, limit),
            used=_fmt(after))

    @staticmethod
    def feature_locked(
        feature: str,
        tier: TierConfig,
        *,
        tiers: TierCatalog = None,
        templates: Templates = DEFAULT_TEMPLATES,
    ) -> str:
        """Message when a tier-gated feature is not available.

        The upgrade target is the lowest tier in the consumer's own catalog
        that actually declares the feature. No tier declares it ⇒ no upgrade
        is promised.
        """
        next_tier = _next_tier_with_feature(tier, tiers, feature)
        head = templates.feature_locked.format(tier=tier.display_name)
        if next_tier is None:
            return head
        return _join(head,
                     templates.upgrade_unlock.format(next_tier=next_tier.display_name),
                     _link(templates, tier))


# ---------------------------------------------------------------------------
# Config readers — the only place a "next tier" is decided
# ---------------------------------------------------------------------------

def _catalog(tiers: TierCatalog) -> list[TierConfig]:
    if tiers is None:
        return []
    values = tiers.values() if isinstance(tiers, Mapping) else tiers
    return [t for t in values if isinstance(t, TierConfig)]


def _higher_tiers(tier: TierConfig, tiers: TierCatalog) -> list[TierConfig]:
    """Tiers above `tier` in the consumer's OWN catalog, lowest first."""
    return sorted(
        (t for t in _catalog(tiers)
         if t.tier_id != tier.tier_id and t.sort_order > tier.sort_order),
        key=lambda t: (t.sort_order, t.tier_id),
    )


def _next_tier_for_resource(
    tier: TierConfig,
    tiers: TierCatalog,
    resource_key: str,
    current_limit: Optional[float],
) -> Optional[TierConfig]:
    """The lowest higher tier that genuinely grants MORE of this resource.

    "Upgrade for a higher limit" must be true. A higher tier with the same
    (or a smaller) limit is not an upgrade for this resource, so it is not
    offered — the same class of false claim as naming a plan that does not
    exist.
    """
    for candidate in _higher_tiers(tier, tiers):
        limits = candidate.get_limit(resource_key)
        if limits.limit is None:  # unlimited
            return candidate
        if current_limit is None:
            continue
        if limits.limit > current_limit:
            return candidate
    return None


def _next_tier_with_feature(
    tier: TierConfig, tiers: TierCatalog, feature: str
) -> Optional[TierConfig]:
    for candidate in _higher_tiers(tier, tiers):
        if feature in (candidate.features or set()):
            return candidate
    return None


def _link(templates: Templates, tier: TierConfig) -> str:
    """The tier's OWN upgrade URL, when it declared one."""
    url = (tier.upgrade_url or "").strip()
    return templates.upgrade_link.format(upgrade_url=url) if url else ""


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _join(*parts: str) -> str:
    """Join present clauses with a single space; absent clauses leave no scar."""
    return " ".join(p.strip() for p in parts if p and p.strip())


def _unit(unit: str, count: float) -> str:
    """Config declares the unit label in its PLURAL form ("sandboxes",
    "requests", "seats"). At exactly one, print the singular — 0.6.2 emitted
    "1 widgets". Units with no plural marker ("USD", "GB") are left alone;
    a consumer whose label does not follow English plural rules can declare
    the form they want and see it verbatim at every count but one.
    """
    if count != 1 or not unit:
        return unit
    lowered = unit.lower()
    for suffix in ("ches", "shes", "xes", "zes", "sses"):
        if lowered.endswith(suffix):
            return unit[:-2]
    if lowered.endswith("ies") and len(unit) > 3:
        return unit[:-3] + "y"
    if lowered.endswith("s") and not lowered.endswith("ss"):
        return unit[:-1]
    return unit


def _fmt(val: float) -> str:
    """Format a number for display: integers as int, decimals with 2 places."""
    if val == int(val):
        return f"{int(val)}"
    return f"{val:.2f}"
