"""Typed configuration errors — the §5 error contract of the dependency-
resolution design (tickets/20260721_shared_lib_declared_not_discovered/
design_dependency_resolution_20260721.md).

One error, one cause, one remedy that works. The message names the config key
and the accepted namespaced env var(s) verbatim; the `previously` line states
what pre-0.7 resolution would silently have used (secrets redacted). Stable
QUOTA-CFG-nnn codes are runbook identifiers. No message may name a specific
consumer's file layout (DOC-09).
"""
from __future__ import annotations

from typing import Optional, Sequence

__all__ = ["QuotaConfigError"]


class QuotaConfigError(RuntimeError):
    """A dependency the deployment needs was not declared (or the config
    document itself is missing/invalid). Raised BEFORE any network, Redis, or
    AWS call."""

    def __init__(
        self,
        *,
        name: str,                       # human name: "Redis counter store URL"
        config_key: str,                 # dotted: "storage.redis_url"
        code: str,                       # "QUOTA-CFG-001"
        state: str,                      # "declared null" | "key absent" | "file missing" | ...
        env_names: Sequence[str] = (),
        previously: Optional[str] = None,
        remedy: str = "",
        docs_anchor: str = "",
    ):
        self.name = name
        self.config_key = config_key
        self.code = code
        self.state = state
        self.env_names = tuple(env_names)
        self.remedy = remedy

        env_line = ", ".join(self.env_names) + "        (not set)" if self.env_names \
            else "(none — this value is never read from the environment)"
        lines = [
            f"ab0t-quota config error [{code}] — {name} is not declared.",
            "",
            f"  config key : {config_key}        ({state})",
            f"  env        : {env_line}",
        ]
        if previously:
            lines.append(f"  previously : {previously}")
        if remedy:
            lines.append(f"  remedy     : {remedy}")
        lines.append("")
        anchor = f"#{docs_anchor}" if docs_anchor else ""
        lines.append(
            f"  docs: docs/requirements.md{anchor} · "
            f"verify before deploy: python -m ab0t_quota preflight"
        )
        super().__init__("\n".join(lines))
