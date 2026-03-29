"""
Quota configuration — loads tier definitions and resource registry from a
config file or environment, with hardcoded defaults as fallback.

Config file location (checked in order):
  1. QUOTA_CONFIG_PATH env var
  2. ./quota-config.json (cwd)
  3. /etc/ab0t/quota-config.json
  4. Built-in defaults from tiers.py

The config file is the operator-facing interface. Changing tiers, limits,
features, and resource definitions should NOT require a code deploy.
"""

from __future__ import annotations

import json
import os
import logging
from pathlib import Path
from typing import Optional

from .models.core import TierConfig, TierLimits, ResourceDef, CounterType, ResetPeriod
from .tiers import DEFAULT_TIERS

logger = logging.getLogger("ab0t_quota.config")

CONFIG_SEARCH_PATHS = [
    "quota-config.json",
    "/etc/ab0t/quota-config.json",
]


def load_config(path: Optional[str] = None) -> dict:
    """Load quota config from file. Returns raw dict."""
    search = path or os.getenv("QUOTA_CONFIG_PATH")
    if search:
        p = Path(search)
        if p.exists():
            logger.info("Loading quota config from %s", p)
            return json.loads(p.read_text())
        logger.warning("Quota config not found at %s, using defaults", p)
        return {}

    for candidate in CONFIG_SEARCH_PATHS:
        p = Path(candidate)
        if p.exists():
            logger.info("Loading quota config from %s", p)
            return json.loads(p.read_text())

    logger.info("No quota config file found, using built-in defaults")
    return {}


def load_tiers(config: Optional[dict] = None) -> dict[str, TierConfig]:
    """Load tier definitions from config, falling back to defaults."""
    if not config or "tiers" not in config:
        return DEFAULT_TIERS

    tiers = {}
    for tier_data in config["tiers"]:
        limits = {}
        for key, limit_data in tier_data.get("limits", {}).items():
            if isinstance(limit_data, (int, float)):
                limits[key] = TierLimits(limit=limit_data)
            elif limit_data is None:
                limits[key] = TierLimits(limit=None)
            elif isinstance(limit_data, dict):
                limits[key] = TierLimits(**limit_data)

        tiers[tier_data["tier_id"]] = TierConfig(
            tier_id=tier_data["tier_id"],
            display_name=tier_data.get("display_name", tier_data["tier_id"].title()),
            description=tier_data.get("description"),
            sort_order=tier_data.get("sort_order", 0),
            limits=limits,
            features=set(tier_data.get("features", [])),
            upgrade_url=tier_data.get("upgrade_url"),
        )

    logger.info("Loaded %d tiers from config", len(tiers))
    return tiers


def load_resources(config: Optional[dict] = None) -> list[ResourceDef]:
    """Load resource definitions from config."""
    if not config or "resources" not in config:
        return []

    resources = []
    for r in config["resources"]:
        resources.append(ResourceDef(
            service=r["service"],
            resource_key=r["resource_key"],
            display_name=r["display_name"],
            counter_type=CounterType(r["counter_type"]),
            unit=r.get("unit", "units"),
            window_seconds=r.get("window_seconds"),
            reset_period=ResetPeriod(r["reset_period"]) if r.get("reset_period") else None,
            precision=r.get("precision", 0),
        ))

    logger.info("Loaded %d resource definitions from config", len(resources))
    return resources
