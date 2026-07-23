"""Config document schema (T-19) — makes the config's SHAPE checkable before
any I/O, and publishes the artifact the example config already points at
(`"$schema": "quota-config-schema.json"`).

Scope, deliberately narrow (design §3 + T-19's recorded verification):
  * the `storage` block is STRICT — unknown keys and wrong types are config
    errors ("a typo must never silently change enforcement", D-14/D-48;
    a mistyped `redis_ur` was silently ignored before this module);
  * comment keys (`$…` / `_…`) are always allowed;
  * every OTHER top-level block stays permissive (forward-compat — Go's
    `Extra` map behaves the same), validated by its own loader as today.

`null ≠ absent ≠ invalid` is the RESOLVER's job on the parsed dict
(ab0t_quota/resolve.py); this module only rejects *invalid*.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional

from .errors import QuotaConfigError

# storage.<key> -> accepted python types (None allowed for every Optional).
_STORAGE_KEYS: dict[str, tuple] = {
    "redis_url": (str,),
    "redis_password": (str,),
    "redis_key_prefix": (str,),
    "dynamodb_table": (str,),
    "dynamodb_region": (str,),
    "dynamodb_endpoint": (str,),
    "persistence_enabled": (bool,),
    "persistence_sync_interval_seconds": (int, float),
    "redis_cluster_confirmed_disabled": (bool,),
    "redis_durability_confirmed": (bool,),
    "redis_scripting_confirmed": (bool,),   # T-9: D-73 hatch (unverifiable probe only)
    "ddb_pitr_confirmed": (bool,),
    "auto_create_tables": (bool,),          # T-6: opt-in table creation (default false)
    "connect_retry_seconds": (int, float),  # D-2 lever
    "keyspace_version": (int,),             # K-1: 1|2 — READ-authoritative key shape
    "keyspace_dual_write": (bool,),         # K-1: maintain BOTH shapes during migration
}

_ENGINE_MODES = ("local", "byo_redis", "bridge")


def _is_comment(key: str) -> bool:
    return key.startswith("$") or key.startswith("_")


def validate_config(config: Mapping[str, Any]) -> None:
    """Raise QuotaConfigError [QUOTA-CFG-006] on an invalid document shape.
    Collects ALL violations so the operator fixes the file once."""
    problems: list[str] = []

    storage = config.get("storage")
    if storage is not None and not isinstance(storage, Mapping):
        problems.append("storage: must be an object")
    elif isinstance(storage, Mapping):
        for key, value in storage.items():
            if _is_comment(key):
                continue
            if key not in _STORAGE_KEYS:
                problems.append(
                    f"storage.{key}: unknown key (a typo here would silently change "
                    f"behaviour). Allowed: {', '.join(sorted(_STORAGE_KEYS))}")
            elif value is not None:
                expected = _STORAGE_KEYS[key]
                # bool is an int subclass — a bool never satisfies a number slot
                ok = isinstance(value, expected) and not (
                    isinstance(value, bool) and bool not in expected)
                if not ok:
                    problems.append(
                        f"storage.{key}: expected "
                        f"{'/'.join(t.__name__ for t in expected)}, "
                        f"got {type(value).__name__}")
        # Announced in the 0.6.x CHANGELOG, enforced by Go at boot, and the
        # Python keyspace is hard-fixed to `quota:` — a custom prefix was
        # silently IGNORED here (Gate E cross-lane finding).
        # K-1 (keyspace spec §3.1): only defined versions — an unknown version
        # must never silently fall back to a shape (it would orphan counters).
        ksv = storage.get("keyspace_version")
        if ksv is not None and not (isinstance(ksv, int)
                                    and not isinstance(ksv, bool) and ksv in (1, 2)):
            problems.append(
                f"storage.keyspace_version: must be 1 or 2 (got {ksv!r}) — an "
                f"unknown keyspace version would orphan every live counter")
        # K-9: setup_quota now consumes keyspace_version/keyspace_dual_write
        # (local/byo_redis). Bridge mode still refuses a declared state in
        # setup_quota itself (D-KS-8 — never a silent no-op).
        prefix = storage.get("redis_key_prefix")
        if prefix is not None and prefix != "quota":
            problems.append(
                f"storage.redis_key_prefix: custom prefixes are not allowed "
                f"(got {prefix!r}; must be \"quota\" or omitted) — a custom "
                f"value forks the keyspace, breaks cross-runtime sharing, and "
                f"Python's keyspace is fixed to quota: (it was silently ignored)")

    mode = config.get("engine_mode")
    if mode is not None and mode not in _ENGINE_MODES:
        problems.append(f"engine_mode: {mode!r} is not one of {_ENGINE_MODES} "
                        f"(an unknown mode must never silently fall back)")

    tiers = config.get("tiers")
    if tiers is not None and not isinstance(tiers, list):
        problems.append("tiers: must be an array of tier objects")

    # `resources` stays permissive on shape (ResourceDef validates the rest at
    # load), but the customer-facing copy fields are checked here: a mistyped
    # `action_hints` would silently cost every 429 its remediation sentence,
    # which is the class of defect D-CK-1 exists to close.
    resources = config.get("resources")
    if resources is not None and not isinstance(resources, list):
        problems.append("resources: must be an array of resource objects")
    elif isinstance(resources, list):
        for i, res in enumerate(resources):
            if not isinstance(res, Mapping):
                problems.append(f"resources[{i}]: must be an object")
                continue
            for field in ("display_name", "action_hint", "description", "unit"):
                val = res.get(field)
                if val is not None and not isinstance(val, str):
                    problems.append(
                        f"resources[{i}].{field}: expected string, "
                        f"got {type(val).__name__}")

    if problems:
        raise QuotaConfigError(
            name="quota config document", config_key="(document)",
            code="QUOTA-CFG-006",
            state=f"{len(problems)} shape violation(s)",
            env_names=("QUOTA_CONFIG_PATH",),
            remedy="fix every violation listed below, then re-run:\n               - "
                   + "\n               - ".join(problems),
            docs_anchor="config",
        )


def generate_schema() -> dict:
    """The committed quota-config-schema.json content — generated from the
    same tables `validate_config` enforces, so the artifact cannot drift
    silently (the sync test regenerates and byte-compares)."""
    def _t(types: tuple) -> list:
        names = []
        for t in types:
            names.append({str: "string", bool: "boolean", int: "number",
                          float: "number"}[t])
        out = list(dict.fromkeys(names)) + ["null"]
        return out

    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$id": "quota-config-schema.json",
        "title": "ab0t-quota configuration (quota-config.json)",
        "description": "DECLARED, NOT DISCOVERED: the library uses exactly what "
                       "this file (plus documented QUOTA_*/AB0T_* env vars) "
                       "declares. See docs/requirements.md.",
        "type": "object",
        "properties": {
            "service_name": {"type": ["string", "null"]},
            "engine_mode": {"enum": list(_ENGINE_MODES) + [None]},
            "offline": {"type": ["boolean", "null"]},
            "storage": {
                "type": "object",
                "properties": {k: {"type": _t(v)} for k, v in sorted(_STORAGE_KEYS.items())},
                "additionalProperties": False,
                "patternProperties": {"^[$_]": {}},
            },
            "tiers": {"type": ["array", "null"]},
            "resources": {
                "type": ["array", "null"],
                "items": {
                    "type": "object",
                    "required": ["service", "resource_key", "display_name",
                                 "counter_type"],
                    "properties": {
                        "service": {"type": "string"},
                        "resource_key": {"type": "string"},
                        "display_name": {
                            "type": "string",
                            "description": "Human-readable name shown in "
                                           "dashboards and 429 responses",
                        },
                        "description": {
                            "type": ["string", "null"],
                            "description": "Longer description for admin UIs "
                                           "and docs (never shown to end "
                                           "customers)",
                        },
                        "action_hint": {
                            "type": ["string", "null"],
                            "description": "End-customer remediation copy shown "
                                           "in 429 responses — what the user can "
                                           "do right now. Absent = the sentence "
                                           "is omitted, never invented (D-CK-1)",
                        },
                        "counter_type": {"enum": ["gauge", "rate", "accumulator"]},
                        "unit": {"type": ["string", "null"]},
                        "window_seconds": {"type": ["number", "null"]},
                        "reset_period": {
                            "enum": ["daily", "weekly", "monthly", None]},
                        "precision": {"type": ["number", "null"]},
                    },
                    "patternProperties": {"^[$_]": {}},
                },
            },
        },
        "additionalProperties": True,
    }


def write_schema(path: str) -> None:  # pragma: no cover - release tooling
    with open(path, "w") as f:
        json.dump(generate_schema(), f, indent=2, sort_keys=False)
        f.write("\n")
