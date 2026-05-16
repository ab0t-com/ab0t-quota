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
import re
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

# ---------------------------------------------------------------------------
# Env-var interpolation in config strings
# ---------------------------------------------------------------------------
# The lib supports ${VAR} and ${VAR:-default} substitution in config string
# values so per-environment values (table name, redis URL, mesh URL, etc.)
# can live in env vars without forking the config JSON per environment.
#
# IMPORTANT — namespaced. This is a shared, public client library: consumers
# drop it into their own services with their own env. Generic ${VAR}
# expansion against the entire OS environment would be a dark pattern —
# a typo like ${USER} or ${PATH} could leak unrelated OS values into the
# resolved config, and a malicious or buggy config string could try to
# read consumer secrets (${DATABASE_URL}, ${API_KEY}). Both are bad.
#
# Therefore: ONLY env vars whose names start with the QUOTA_ prefix are
# eligible for substitution. References to any other env var fall through
# to the inline default (or empty string) and emit a one-line warning so
# the consumer sees clearly that their reference was rejected.
#
# If a consumer needs to thread a non-QUOTA_ env var (e.g. AWS_REGION)
# into the config, they should re-export it under the QUOTA_ namespace
# in their .env file (e.g. `QUOTA_REGION=$AWS_REGION`) and reference
# ${QUOTA_REGION:-us-east-1} in quota-config.json.
_QUOTA_ENV_NAMESPACE = "QUOTA_"
_ENV_PATTERN = re.compile(r'\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}')


def _interpolate_env(value):
    """Replace ${QUOTA_VAR} / ${QUOTA_VAR:-default} in strings; recurse
    into dicts/lists. Non-string leaves pass through unchanged.

    Namespaced — only env vars whose names start with `QUOTA_` are read.
    Any reference outside that namespace is logged as a warning and
    resolved to the inline default (or empty string if no default given).
    See module docstring for rationale.

    Empty env values are treated as 'unset' (matches shell ${VAR:-default}
    semantics) so the default kicks in.
    """
    if isinstance(value, str):
        def sub(m):
            var, default = m.group(1), m.group(2) or ""
            if not var.startswith(_QUOTA_ENV_NAMESPACE):
                logger.warning(
                    "quota-config references env var ${%s} outside the %s* "
                    "namespace; ignoring and using default=%r. To inject a "
                    "non-QUOTA_ value, re-export it as QUOTA_%s in your env.",
                    var, _QUOTA_ENV_NAMESPACE, default, var,
                )
                return default
            return os.environ.get(var) or default
        return _ENV_PATTERN.sub(sub, value)
    if isinstance(value, dict):
        return {k: _interpolate_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_env(v) for v in value]
    return value


def load_config(path: Optional[str] = None) -> dict:
    """Load quota config from file. Returns raw dict with QUOTA_* env-var
    references expanded (see _interpolate_env for the namespace contract)."""
    search = path or os.getenv("QUOTA_CONFIG_PATH")
    if search:
        p = Path(search)
        if p.exists():
            logger.info("Loading quota config from %s", p)
            return _interpolate_env(json.loads(p.read_text()))
        logger.warning("Quota config not found at %s, using defaults", p)
        return {}

    for candidate in CONFIG_SEARCH_PATHS:
        p = Path(candidate)
        if p.exists():
            logger.info("Loading quota config from %s", p)
            return _interpolate_env(json.loads(p.read_text()))

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
            default_per_user_fraction=tier_data.get("default_per_user_fraction"),
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


def load_resource_bundles(config: Optional[dict] = None) -> dict[str, list[str]]:
    """Load resource-bundle definitions from config.

    Bundles are a generic, consumer-defined naming layer over resource_keys.
    Each entry maps a name (whatever the consumer chooses) to the list of
    resource_keys consumed when one of those things is created. The library
    has no opinion on what bundles represent — they're whatever the consumer
    wants to dispatch by:

      "resource_bundles": {
        "my_thing":            ["my.concurrent_things"],
        "my_premium_thing":    ["my.concurrent_things", "my.premium_slots"]
      }

    Returns {} when no bundles are declared.
    """
    if not config or "resource_bundles" not in config:
        return {}

    raw = config["resource_bundles"] or {}
    if not isinstance(raw, dict):
        logger.warning("resource_bundles must be an object, got %s — ignoring", type(raw).__name__)
        return {}

    bundles: dict[str, list[str]] = {}
    for name, keys in raw.items():
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
            logger.warning("resource_bundles.%s must be a list of strings — skipping", name)
            continue
        bundles[name] = list(keys)

    logger.info("Loaded %d resource bundles from config", len(bundles))
    return bundles
