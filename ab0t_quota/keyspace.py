"""Keyspace versioning (F-1 + D-23) — the ONE home of counter key shape.

Spec: tickets/20260721_keyspace_versioning/SPEC_keyspace_versioning_20260721.md
v1: quota:{org}:{rk}<suffix>            (today; untagged, cluster-unsafe)
v2: quota:v2:{<svc>/<org>}:{rk}<suffix> (braces literal — Redis hash tag)
Four legal states: (1,False) → (1,True) → (2,True) → (2,False).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# Latch TTL the flip gate must outwait (mirrors gauge/accumulator _IDEM_TTL).
IDEM_TTL_SECONDS = 86400

_VERSIONS = (1, 2)
# Tag delimiters + separators; a scope containing one would corrupt the key.
_FORBIDDEN = set("{}/:")
_VERSIONISH = re.compile(r"^v[0-9]+$")


class KeyspaceConfigError(ValueError):
    """Illegal keyspace declaration (bad version / missing service scope)."""


class KeyspaceScopeError(ValueError):
    """A service/org value that cannot be embedded in a key — refused loudly,
    never mangled (spec §2.3 charset guard; checked per-request)."""


def validate_scope(value: Optional[str], what: str) -> str:
    if not value:
        raise KeyspaceScopeError(f"{what} must be a non-empty string for keyspace v2 keys")
    bad = _FORBIDDEN.intersection(value)
    if bad:
        raise KeyspaceScopeError(
            f"{what} {value!r} contains forbidden character(s) {sorted(bad)} — "
            "'{', '}', '/', ':' would corrupt the hash-tagged v2 key shape")
    if _VERSIONISH.match(value):
        raise KeyspaceScopeError(
            f"{what} {value!r} matches ^v[0-9]+$ — reserved as a keyspace version "
            "discriminator (spec §2.4); it would be misparsed as a version segment")
    return value


def marker_key(service: str) -> str:
    """Migration marker, one per service scope (spec §3.1)."""
    return f"quota:keyspace:meta:{service}"


@dataclass(frozen=True)
class Keyspace:
    """Declared keyspace state. Default = today's behaviour, bit-identical.

    ``version`` names the READ-AUTHORITATIVE shape; ``dual_write`` maintains
    BOTH shapes during migration. The primary key set is the authoritative
    shape; ``secondary`` is the other shape (only meaningful when dual).
    """
    service: Optional[str] = None
    version: int = 1
    dual_write: bool = False

    def __post_init__(self):
        if self.version not in _VERSIONS:
            raise KeyspaceConfigError(
                f"storage.keyspace_version must be one of {_VERSIONS}, got {self.version!r}")
        if self.service is not None:
            validate_scope(self.service, "service_name")
        if (self.version == 2 or self.dual_write) and not self.service:
            raise KeyspaceConfigError(
                "keyspace v2 keys carry a service segment: declare service_name "
                "(spec §3.2) before setting keyspace_version=2 or keyspace_dual_write")

    # ---------------------------------------------------------------- shapes

    def _prefix(self, org_id: str, resource_key: str, version: int) -> str:
        if version == 1:
            return f"quota:{org_id}:{resource_key}"
        validate_scope(org_id, "org_id")
        return f"quota:v2:{{{self.service}/{org_id}}}:{resource_key}"

    def _org_prefix(self, org_id: str, version: int) -> str:
        if version == 1:
            return f"quota:{org_id}"
        validate_scope(org_id, "org_id")
        return f"quota:v2:{{{self.service}/{org_id}}}"

    def gauge_key(self, org, rk, version=None):
        return f"{self._prefix(org, rk, version or self.version)}:gauge"

    def user_key(self, org, rk, uid, version=None):
        return f"{self._prefix(org, rk, version or self.version)}:gauge:user:{uid}"

    def seq_user_key(self, org, rk, uid, version=None):
        return f"{self._prefix(org, rk, version or self.version)}:gauge:seq:user:{uid}"

    def idem_key(self, org, rk, key, version=None):
        p = self._prefix(org, rk, version or self.version)
        return f"{p}:idem:{key}" if key else f"{p}:idem:__unused__"

    def idem_gen_key(self, org, rk, key, version=None):
        p = self._prefix(org, rk, version or self.version)
        return f"{p}:idemgen:{key}" if key else f"{p}:idemgen:__unused__"

    def acc_key(self, org, rk, period, version=None):
        return f"{self._prefix(org, rk, version or self.version)}:acc:{period}"

    def rate_key(self, org, rk, version=None):
        return f"{self._prefix(org, rk, version or self.version)}:rate"

    def recent_key(self, org, version=None):
        """Reconciler recent-activity guard. v2 gains the svc scope (spec §7 #5)."""
        v = version or self.version
        if v == 1:
            return f"quota:reconcile:recent:{org}"
        return f"{self._org_prefix(org, v)}:reconcile:recent"

    # ------------------------------------------------------------- dual side

    @property
    def secondary_version(self) -> Optional[int]:
        """The non-authoritative shape maintained during dual, else None."""
        if not self.dual_write:
            return None
        return 2 if self.version == 1 else 1

    @property
    def primary_is_v2(self) -> bool:
        return self.version == 2


def parse_counter_key(key: str):
    """Parse a counter key of EITHER shape into
    ``(version, service, org_id, resource_key, kind)`` where kind is
    'gauge' | 'user' | 'acc' — or None for non-counter keys (idem, alert, …).
    Discriminator (spec §2.4): after 'quota:', a 'v2' segment followed by
    '{svc/org}' opens a versioned key; anything else is v1.
    """
    if not key.startswith("quota:"):
        return None
    rest = key[len("quota:"):]
    service = None
    version = 1
    if rest.startswith("v2:{"):
        end = rest.find("}")
        if end < 0:
            return None
        tag = rest[len("v2:{"):end]
        if "/" not in tag:
            return None
        service, org_id = tag.split("/", 1)
        rest = rest[end + 1:]
        if not rest.startswith(":"):
            return None
        parts = rest[1:].split(":")
        version = 2
    else:
        parts = rest.split(":")
        if len(parts) < 3:
            return None
        org_id = parts[0]
        if _VERSIONISH.match(org_id):
            # A v1-shaped key with a version-ish org can only be a foreign /
            # corrupt writer (the charset guard refuses creating it) — refuse
            # to misattribute it (spec §2.4 / V6 planted offender).
            return None
        parts = parts[1:]
    # parts = [resource_key, suffix, ...]; resource keys contain '.' (ResourceDef
    # regex) which excludes 'reconcile'/'alert'/version-marker segments.
    if len(parts) < 2 or "." not in parts[0]:
        return None
    resource_key, suffix = parts[0], parts[1]
    if suffix == "gauge":
        if len(parts) >= 4 and parts[2] == "user":
            return (version, service, org_id, resource_key, "user")
        if len(parts) == 2:
            return (version, service, org_id, resource_key, "gauge")
        return None  # :gauge:seq:user:* — generation keys are not snapshotted
    if suffix == "acc" and len(parts) >= 3:
        return (version, service, org_id, resource_key, "acc")
    return None
