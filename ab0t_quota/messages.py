"""
Plain-English message templates for quota responses.

All user-facing messages go through here. No technical jargon — no
"quota_exceeded", no "sandbox.concurrent". These read like a helpful
product, not an error log.
"""

from __future__ import annotations

from typing import Optional
from .models.core import ResourceDef, TierConfig


# ---------------------------------------------------------------------------
# Per-resource action hints (what the user can do RIGHT NOW)
# ---------------------------------------------------------------------------

ACTION_HINTS: dict[str, str] = {
    "sandbox.concurrent":       "Stop an existing sandbox to free up a slot.",
    "sandbox.monthly_cost":     "Wait until next month when your spending limit resets.",
    "sandbox.gpu_instances":    "Stop an existing GPU sandbox first.",
    "sandbox.browser_sessions": "Close a browser session to free up a slot.",
    "sandbox.desktop_sessions": "Close a desktop session to free up a slot.",
    "resource.cpu_cores":       "Terminate or scale down an existing allocation.",
    "resource.allocations":     "Terminate an existing allocation first.",
    "resource.monthly_cost":    "Wait until next month when your spending limit resets.",
    "auth.users_per_org":       "Remove an existing member first.",
    "auth.teams_per_org":       "Delete an existing team first.",
    "auth.api_keys_per_org":    "Revoke an existing API key first.",
    "api.requests_per_hour":    "Wait a moment and try again.",
}

# Next tier that unlocks more of a resource (for upgrade messaging)
UPGRADE_TIER_MAP: dict[str, dict[str, str]] = {
    "free": {
        "sandbox.gpu_instances":    "Starter",
        "sandbox.desktop_sessions": "Starter",
        "_default":                 "Starter",
    },
    "starter": {
        "_default": "Pro",
    },
    "pro": {
        "_default": "Enterprise",
    },
}


class MessageBuilder:
    """Generates user-friendly messages for quota results."""

    @staticmethod
    def deny(
        resource_def: ResourceDef,
        tier: TierConfig,
        current: float,
        limit: float,
        requested: float,
    ) -> str:
        """Message for a hard denial (429)."""
        name = resource_def.display_name.lower()
        unit = resource_def.unit
        tier_name = tier.display_name

        # Special case: limit is 0 (feature not available on tier)
        if limit == 0:
            next_tier = _get_next_tier(tier.tier_id, resource_def.resource_key)
            if next_tier:
                return (
                    f"{resource_def.display_name} are not available on the {tier_name} plan. "
                    f"Upgrade to {next_tier} to unlock this feature."
                )
            return f"{resource_def.display_name} are not available on your current plan."

        # Standard denial
        action = ACTION_HINTS.get(resource_def.resource_key, "")
        next_tier = _get_next_tier(tier.tier_id, resource_def.resource_key)

        parts = [
            f"You've reached the maximum of {_fmt(limit)} {unit} "
            f"on the {tier_name} plan.",
        ]
        if action:
            parts.append(action)
        if next_tier:
            parts.append(f"Or upgrade to {next_tier} for a higher limit.")

        return " ".join(parts)

    @staticmethod
    def warning(
        resource_def: ResourceDef,
        tier: TierConfig,
        current: float,
        limit: float,
        after: float,
    ) -> str:
        """Message for a warning (allowed but approaching limit)."""
        unit = resource_def.unit
        pct = int((after / limit) * 100) if limit > 0 else 0

        if pct >= 95:
            return (
                f"Almost at your limit: {_fmt(after)} of {_fmt(limit)} {unit} "
                f"({pct}%). You'll be blocked from creating more soon."
            )
        return (
            f"You're using {_fmt(after)} of {_fmt(limit)} {unit} ({pct}%). "
            f"Consider upgrading if you need more."
        )

    @staticmethod
    def allow(
        resource_def: ResourceDef,
        current: float,
        limit: Optional[float],
        after: float,
    ) -> str:
        """Message for a clean allow (under limit)."""
        unit = resource_def.unit
        if limit is None:
            return f"{resource_def.display_name}: unlimited"
        return f"{_fmt(after)} of {_fmt(limit)} {unit} used"

    @staticmethod
    def burst(
        resource_def: ResourceDef,
        tier: TierConfig,
        current: float,
        limit: float,
        after: float,
    ) -> str:
        """Message for burst allowance (over limit but within burst cap)."""
        unit = resource_def.unit
        return (
            f"You're over your {resource_def.display_name.lower()} limit of "
            f"{_fmt(limit)} {unit} ({_fmt(after)}/{_fmt(limit)}). "
            f"Burst allowance is active — usage above the limit may incur "
            f"overage charges."
        )

    @staticmethod
    def feature_locked(
        feature: str,
        tier: TierConfig,
    ) -> str:
        """Message when a tier-gated feature is not available."""
        next_tier = _get_next_tier(tier.tier_id, "_default")
        if next_tier:
            return f"This feature is not available on the {tier.display_name} plan. Upgrade to {next_tier} to unlock it."
        return f"This feature is not available on your current plan."


def _get_next_tier(current_tier_id: str, resource_key: str) -> Optional[str]:
    tier_map = UPGRADE_TIER_MAP.get(current_tier_id, {})
    return tier_map.get(resource_key) or tier_map.get("_default")


def _fmt(val: float) -> str:
    """Format a number for display: integers as int, decimals with 2 places."""
    if val == int(val):
        return f"{int(val)}"
    return f"{val:.2f}"
