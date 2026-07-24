"""DECLARED, NOT DISCOVERED — the one dependency resolver (pack 20260721, T-1).

Every infrastructure dependency is resolved exactly once, before any client
object is constructed and before any network, Redis, or AWS call, from
DECLARED sources only:

    kwarg  >  config (non-null)  >  namespaced env  >  (error | off | SDK | default)

There is no tier beyond that: no generic env var is ever read for a value, and
no endpoint/region/credential/catalog is ever invented. A set namespaced env
var BEATS an explicit config ``null`` (a consumer-set QUOTA_* variable is a
declaration, not discovery); ``null`` with no namespaced env set is a hard
config error for a REQUIRED dependency and "declared off" for an OPTIONAL one.

Design: tickets/20260721_shared_lib_declared_not_discovered/
design_dependency_resolution_20260721.md §2 (contract), §5 (errors), §6
(provenance + redaction). Decisions D-5 (precedence, memory://), O3 (explicit
empty honoured).

Error codes (stable runbook identifiers):
    QUOTA-CFG-001  Redis counter store URL not declared
    QUOTA-CFG-002  config file missing
    QUOTA-CFG-003  config file malformed
    QUOTA-CFG-004  tier catalog not declared
    QUOTA-CFG-005  memory:// is not a Python engine mode
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

from .errors import QuotaConfigError

logger = logging.getLogger("ab0t_quota.resolve")

_ABSENT = object()   # module-private sentinel: "key not present" ≠ None ("explicit null")

__all__ = [
    "Provenance", "Requirement", "Resolved", "ResolutionPlan",
    "resolve_dependency", "resolve_dependencies",
    "resolve_ddb_region", "resolve_ddb_endpoint", "resolve_sns_topic",
    "offline_mode", "redact_url", "strip_url_password",
]


class Requirement(str, Enum):
    REQUIRED = "required"    # absent/null(+no env) ⇒ QuotaConfigError before any I/O
    OPTIONAL = "optional"    # absent ⇒ unset (feature off); null ⇒ declared-off; both logged
    SDK = "sdk"              # the AWS SDK owns resolution; we never read, only log the outcome
    INTERNAL = "internal"    # library-namespaced identifier/tunable with a documented default


class Provenance(str, Enum):
    KWARG = "kwarg"                 # consumer code passed it explicitly
    CONFIG = "config"               # declared non-null in quota-config.json
    ENV = "env"                     # a documented namespaced env var
    DECLARED_OFF = "declared-off"   # explicit null, optional dependency
    UNSET = "unset"                 # absent, optional dependency
    SDK = "sdk"                     # deferred to the AWS SDK's own resolution
    DEFAULT = "default"             # INTERNAL-kind documented default


def redact_url(url: str) -> str:
    """Strip userinfo from a URL: scheme/host/port/path stay (what an operator
    needs to recognise the wrong endpoint), credentials never render."""
    try:
        parts = urlsplit(url)
    except Exception:
        return "(unrenderable url)"
    if "@" not in (parts.netloc or ""):
        return url
    host = parts.netloc.rsplit("@", 1)[1]
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def strip_url_password(url: str) -> Tuple[str, Optional[str]]:
    """Return (url-without-password, embedded-password-or-None). The username
    (ACL user) is kept. Used to implement D-5(a) explicitly instead of relying
    on redis-py's kwarg-vs-URL merge (which the old comment mis-stated, E-03)."""
    parts = urlsplit(url)
    if parts.password is None:
        return url, None
    host = parts.netloc.rsplit("@", 1)[1]
    userinfo = parts.username or ""
    netloc = f"{userinfo}@{host}" if userinfo else host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment)), parts.password


@dataclass(frozen=True)
class Resolved:
    name: str            # human name: "Redis counter store URL"
    config_key: str      # dotted: "storage.redis_url"
    value: Any           # None when off / unset / sdk-deferred
    provenance: Provenance
    source: str          # "config:storage.redis_url" | "env:QUOTA_REDIS_URL" | "kwarg" | …
    secret: bool = False

    @property
    def declared(self) -> bool:
        return self.provenance in (Provenance.KWARG, Provenance.CONFIG, Provenance.ENV)

    def display(self) -> str:
        """Redacted rendering for logs/capabilities — never raises, never leaks."""
        if self.secret:
            return "(secret: set)" if self.value else "(secret: not set)"
        if self.provenance is Provenance.DECLARED_OFF:
            return "(declared off)"
        if self.provenance is Provenance.UNSET:
            return "(unset)"
        if self.provenance is Provenance.SDK:
            return "(SDK-resolved: aws default chain)"
        if isinstance(self.value, str) and "://" in self.value:
            return redact_url(self.value)
        return repr(self.value)


def _walk(config: Optional[Mapping[str, Any]], dotted: str):
    """Three-way read of a dotted key: ('declared', v) | ('null', None) |
    ('absent', None). Membership checks only — never `.get(k) or …` truthiness."""
    node: Any = config or {}
    parts = dotted.split(".")
    for i, part in enumerate(parts):
        if not isinstance(node, Mapping) or part not in node:
            return "absent", None
        node = node[part]
        if node is None:
            return ("null", None) if i == len(parts) - 1 else ("absent", None)
    return "declared", node


def _env_lookup(names: Sequence[str]):
    """First SET, NON-EMPTY variable from the explicit tuple only. The resolver
    holds no generic-name list to fall back to. Empty string == unset (matches
    `${VAR:-default}` interpolation semantics, config.py)."""
    for n in names:
        v = os.environ.get(n)
        if v:
            return n, v
    return None, None


def resolve_dependency(
    config: Optional[Mapping[str, Any]],
    *,
    name: str,
    config_key: str,
    env: Tuple[str, ...] = (),
    requirement: Requirement = Requirement.REQUIRED,
    kwarg: Any = _ABSENT,
    default: Any = _ABSENT,
    secret: bool = False,
    validator: Optional[Callable[[Any], None]] = None,
    deprecated_env: Tuple[str, ...] = (),
    code: str = "QUOTA-CFG-000",
    remedy: str = "",
    docs_anchor: str = "",
    previously: Optional[Callable[[], str]] = None,
) -> Resolved:
    """Resolve one dependency per the §2.2 precedence. Raises QuotaConfigError;
    performs no I/O. `deprecated_env` is the documented-transition tier
    (DISCUSSION.md §5): the library's OWN legacy names, honoured with a loud
    deprecation warning until 0.8.0 — never a foreign generic harvest."""

    def _validated(value):
        if validator is not None and value is not None:
            validator(value)
        return value

    if kwarg is not _ABSENT and kwarg is not None:
        return Resolved(name, config_key, _validated(kwarg), Provenance.KWARG, "kwarg", secret)

    state, cfg_value = _walk(config, config_key)
    # An empty STRING is 'unset', not 'declared as empty'. config.py resolves an
    # unset `${QUOTA_VAR}` (no inline default) to "", and _env_lookup already
    # treats "" as unset — without this the two halves of one resolver disagree,
    # and `"redis_url": "${QUOTA_REDIS_URL}"` with the var unset flows an empty
    # string into the redis constructor, surfacing as redis-py's `ValueError:
    # Redis URL must specify one of the following schemes` instead of a typed
    # error naming storage.redis_url.
    # Strings ONLY — O3 (explicit empty collection is a declaration, e.g.
    # `tiers: []` means zero tiers) is deliberately untouched.
    if state == "declared" and isinstance(cfg_value, str) and cfg_value.strip() == "":
        state, cfg_value = "null", None
    if state == "declared":
        return Resolved(name, config_key, _validated(cfg_value), Provenance.CONFIG,
                        f"config:{config_key}", secret)

    # Namespaced env: a declaration — by the decided precedence it also applies
    # when the config value is explicit-null.
    env_name, env_value = _env_lookup(env)
    if env_name:
        return Resolved(name, config_key, _validated(env_value), Provenance.ENV,
                        f"env:{env_name}", secret)

    dep_name, dep_value = _env_lookup(deprecated_env)
    if dep_name:
        logger.warning(
            "DEPRECATED source %s used for %s (our own legacy name) — move to %s "
            "before 0.8.0", dep_name, name, env[0] if env else config_key)
        return Resolved(name, config_key, _validated(dep_value), Provenance.ENV,
                        f"env:{dep_name} (DEPRECATED)", secret)

    # Terminal states — error, off, SDK, or a documented internal default.
    if requirement is Requirement.REQUIRED:
        state_text = {"null": "declared null", "absent": "key absent"}[state]
        raise QuotaConfigError(
            name=name, config_key=config_key, code=code,
            state=f"quota-config: {state_text}", env_names=env,
            previously=previously() if previously else None,
            remedy=remedy, docs_anchor=docs_anchor,
        )
    if requirement is Requirement.OPTIONAL:
        if state == "null":
            return Resolved(name, config_key, None, Provenance.DECLARED_OFF, "declared-off", secret)
        return Resolved(name, config_key, None, Provenance.UNSET, "unset", secret)
    if requirement is Requirement.SDK:
        return Resolved(name, config_key, None, Provenance.SDK, "sdk:aws-default-chain", secret)
    # INTERNAL — a default is permitted ONLY here (asserted).
    assert default is not _ABSENT, f"INTERNAL dependency {config_key} needs a documented default"
    return Resolved(name, config_key, default, Provenance.DEFAULT, "default", secret)


# ---------------------------------------------------------------------------
# Per-dependency specs shared by the plan and by direct helper callers
# ---------------------------------------------------------------------------

def _ssrf_endpoint_validator(value: str) -> None:
    """The state store's SSRF allowlist (persistence.py), applied at resolution
    time so EVERY store — money stores included — gets it uniformly (ENV-14)."""
    from .persistence import QuotaStore
    QuotaStore._validate_endpoint_url(value)


def resolve_ddb_region(config) -> Resolved:
    return resolve_dependency(
        config, name="DynamoDB region", config_key="storage.dynamodb_region",
        requirement=Requirement.SDK,
    )


def resolve_ddb_endpoint(config) -> Resolved:
    return resolve_dependency(
        config, name="DynamoDB endpoint override", config_key="storage.dynamodb_endpoint",
        env=("QUOTA_DYNAMODB_ENDPOINT",), deprecated_env=("DYNAMODB_ENDPOINT",),
        requirement=Requirement.OPTIONAL, validator=_ssrf_endpoint_validator,
    )


def _redis_previously() -> str:
    # Report-only read of the generic name, licensed by the error contract
    # (§5.1/§6.3): the message may say a generic variable IS PRESENT and show
    # its redacted host — that is the value a migrating consumer is losing.
    generic = os.environ.get("REDIS_URL")
    if generic:
        return (f"a generic REDIS_URL is present in this environment "
                f"({redact_url(generic)} — userinfo redacted). Versions before 0.7 "
                f"would have connected to it. This version will not: that variable "
                f"belongs to whichever service defines it, not to this library.")
    return ("redis://localhost:6379/0 (an invented default). Versions before 0.7 "
            "would have fabricated it. This version will not.")


class ResolutionPlan(Mapping):
    """Immutable name→Resolved map. Everything downstream — client construction,
    the D-71…D-77 gates, required_money_loops, capabilities — reads the plan,
    never the environment."""

    def __init__(self, rows: dict):
        self._rows = dict(rows)

    def __getitem__(self, key): return self._rows[key]
    def __iter__(self): return iter(self._rows)
    def __len__(self): return len(self._rows)

    def provenance_block(self) -> str:
        """The §6.1 startup block (fixes ENV-11). Emitted BEFORE any connection
        is attempted, so it is present even when a later gate refuses."""
        lines = ["AB0T_QUOTA RESOLVED DEPENDENCIES — declared, not discovered"]
        width = max(len(k) for k in self._rows) if self._rows else 0
        for key, row in self._rows.items():
            lines.append(f"  {key.ljust(width)} = {row.display():<45} source={row.source}")
        return "\n".join(lines)


def resolve_dependencies(config: Optional[Mapping[str, Any]], *, mode: str = "local") -> ResolutionPlan:
    """Build the full mode-aware plan — the ONLY function setup/CLI/preflight
    call. Requiredness is mode-scoped: bridge mode never demands a Redis."""
    rows: dict = {}

    if mode in ("local", "byo_redis"):
        rows["redis_url"] = resolve_dependency(
            config, name="Redis counter store URL", config_key="storage.redis_url",
            env=("QUOTA_REDIS_URL",), requirement=Requirement.REQUIRED,
            code="QUOTA-CFG-001", docs_anchor="redis", previously=_redis_previously,
            remedy=('set storage.redis_url in quota-config.json, or export '
                    'QUOTA_REDIS_URL. No Redis in this deployment? engine_mode: '
                    '"bridge" provisions nothing (quota enforcement only today — '
                    'no billing routes; see docs).'),
        )
        if rows["redis_url"].value == "memory://":
            # D-5(b), Python divergence stated on the record: the counter is
            # multi-key Lua; there is no in-memory implementation to fall back to.
            raise QuotaConfigError(
                name="Redis counter store URL", config_key="storage.redis_url",
                code="QUOTA-CFG-005", state="declared memory://", env_names=("QUOTA_REDIS_URL",),
                remedy=('Python has no in-memory counter backend. For a no-Redis '
                        'deployment use engine_mode: "bridge" (quota enforcement '
                        'only today); for in-process dev the Go runtime supports '
                        '"memory://".'),
                docs_anchor="redis",
            )
        rows["redis_password"] = resolve_dependency(
            config, name="Redis counter store password", config_key="storage.redis_password",
            env=("QUOTA_REDIS_PASSWORD",), requirement=Requirement.OPTIONAL, secret=True,
        )
        # ENV-03/13 fire at plan time, BEFORE any client object is constructed;
        # load_tiers/QuotaEngine carry their own guards for direct callers.
        tiers_state, tiers_value = _walk(config, "tiers")
        if tiers_state != "declared":
            raise QuotaConfigError(
                name="tier catalog", config_key="tiers", code="QUOTA-CFG-004",
                state=f"quota-config: {'declared null' if tiers_state == 'null' else 'key absent'}",
                env_names=(),
                previously=("a built-in 4-tier catalog (free/starter/pro/enterprise with "
                            "sandbox-shaped limits) would have been enforced against your "
                            "customers, and — with service_name set — published to the "
                            "billing service under your name. This version will not invent "
                            "policy."),
                remedy=('add a "tiers" array to quota-config.json (see '
                        'quota-config.example.json). For tests/local dev only: '
                        'ab0t_quota.tiers.DEFAULT_TIERS may be passed explicitly to '
                        'QuotaEngine(tiers=...).'),
                docs_anchor="tiers",
            )
        rows["tiers"] = Resolved(
            "tier catalog", "tiers",
            f"{len(tiers_value)} declared" if isinstance(tiers_value, (list, dict)) else "declared",
            Provenance.CONFIG, "config:tiers")
        rows["dynamodb_region"] = resolve_ddb_region(config)
        rows["dynamodb_endpoint"] = resolve_ddb_endpoint(config)
        # K-9 (keyspace spec §3.1): declared config or the documented default
        # (1,false) — never an env var, never discovery. Value semantics are
        # guarded by config_schema (QUOTA-CFG-006) + Keyspace.__post_init__.
        rows["keyspace_version"] = resolve_dependency(
            config, name="counter keyspace version",
            config_key="storage.keyspace_version",
            requirement=Requirement.INTERNAL, default=1,
        )
        rows["keyspace_dual_write"] = resolve_dependency(
            config, name="counter keyspace dual-write",
            config_key="storage.keyspace_dual_write",
            requirement=Requirement.INTERNAL, default=False,
        )
        rows["auth_url"] = resolve_dependency(
            config, name="auth service URL (tier-pinning)", config_key="auth.url",
            env=("AB0T_AUTH_AUTH_URL",), requirement=Requirement.OPTIONAL,
        )
        rows["stripe_webhook_secret"] = resolve_dependency(
            config, name="Stripe webhook signing secret", config_key="(env-only)",
            env=("AB0T_QUOTA_STRIPE_WEBHOOK_SECRET",), requirement=Requirement.OPTIONAL,
            secret=True,
        )
        rows["sns_lifecycle_topic"] = resolve_sns_topic(config)

    return ResolutionPlan(rows)


def resolve_sns_topic(config, *, kwarg=None) -> Resolved:
    """SNS lifecycle topic: OPTIONAL; the generic SNS_LIFECYCLE_TOPIC_ARN is
    the library's OWN legacy documented name (lifecycle.py) — a documented
    TRANSITION, never a foreign harvest (ENV-06, DISCUSSION.md §5 tier 3)."""
    return resolve_dependency(
        config, name="SNS lifecycle topic ARN", config_key="outbox.sns_topic_arn",
        env=("AB0T_MESH_SNS_LIFECYCLE_TOPIC_ARN",),
        deprecated_env=("SNS_LIFECYCLE_TOPIC_ARN",),
        requirement=Requirement.OPTIONAL, kwarg=kwarg if kwarg is not None else _ABSENT,
    )


def check_deprecated_generic_env(config: Optional[Mapping[str, Any]] = None) -> None:
    """D-10 (SIGNED): call out the silent-off trap at startup — a deprecated
    generic var set while its namespaced replacement is undeclared. ERROR for
    removed names (AB0T_QUOTA_SUPPRESS_DEPRECATION_ERRORS=true downgrades to
    WARNING); DEPRECATED warning for transition-tier names. Presence only —
    values are never read. Retires with the migration window (0.8.0), like D-9."""
    suppress = os.environ.get(
        "AB0T_QUOTA_SUPPRESS_DEPRECATION_ERRORS", "").strip().lower() in ("1", "true", "yes", "on")
    # Membership tests are written as LITERALS so the Gate-C census attributes
    # each licensed name at its read site (never an evasive variable-name form).
    removed = (
        ("STRIPE_WEBHOOK_SECRET" in os.environ, "STRIPE_WEBHOOK_SECRET",
         "AB0T_QUOTA_STRIPE_WEBHOOK_SECRET" in os.environ, "AB0T_QUOTA_STRIPE_WEBHOOK_SECRET",
         None, "Stripe webhook verification is OFF (the route refuses as unconfigured)"),
        ("REDIS_URL" in os.environ, "REDIS_URL",
         "QUOTA_REDIS_URL" in os.environ, "QUOTA_REDIS_URL",
         "storage.redis_url", "the Redis counter store must be declared"),
        ("REDIS_PASSWORD" in os.environ, "REDIS_PASSWORD",
         "QUOTA_REDIS_PASSWORD" in os.environ, "QUOTA_REDIS_PASSWORD",
         "storage.redis_password", "the Redis password is no longer harvested"),
        ("AUTH_SERVICE_URL" in os.environ, "AUTH_SERVICE_URL",
         "AB0T_AUTH_AUTH_URL" in os.environ, "AB0T_AUTH_AUTH_URL",
         "auth.url", "the auth service URL is no longer harvested"),
    )
    # A password embedded in the DECLARED Redis URL (redis://:pw@host/4 — the
    # conventional form) is a declaration of the password. Without this, a
    # correctly-configured consumer who also happens to have a generic
    # REDIS_PASSWORD in their environment gets an ERROR-level line saying "the
    # Redis password is no longer harvested" — true, but irrelevant to them,
    # because nothing is being lost. Sending someone to rename a variable that
    # is not being used is the same misleading-diagnosis class this resolver
    # exists to remove.
    def _declared_url_carries_password() -> bool:
        state, url = _walk(config, "storage.redis_url")
        if state != "declared" or not isinstance(url, str) or not url.strip():
            url = os.environ.get("QUOTA_REDIS_URL") or ""
        if not url:
            return False
        try:
            # bool(), not `is not None`: urlsplit("redis://:@host").password is
            # "" — an empty password is not a declared password. `is not None`
            # would let `redis://:@host` suppress the D-10 error while carrying
            # no credential.
            return bool(urlsplit(url).password)
        except Exception:
            return False

    for present, name, repl_present, replacement, config_key, consequence in removed:
        if not present or repl_present:
            continue
        if config_key and _walk(config, config_key)[0] == "declared":
            continue
        if name == "REDIS_PASSWORD" and _declared_url_carries_password():
            logger.debug(
                "generic REDIS_PASSWORD is set but the declared Redis URL already "
                "carries a password; nothing is harvested and nothing is lost. "
                "Consider removing the unused variable. (D-10)")
            continue
        log = logger.warning if suppress else logger.error
        log("DEPRECATED generic %s is set in this environment but its namespaced "
            "replacement %s is not (config %s undeclared). Since 0.7 the generic "
            "name is NEVER read — %s. Rename the variable to %s. (D-10; "
            "AB0T_QUOTA_SUPPRESS_DEPRECATION_ERRORS=true downgrades this error to "
            "a warning mid-migration.)",
            name, replacement, config_key or "(env-only)", consequence, replacement)
    sns_present = "SNS_LIFECYCLE_TOPIC_ARN" in os.environ
    sns_repl = "AB0T_MESH_SNS_LIFECYCLE_TOPIC_ARN" in os.environ
    if sns_present and not sns_repl \
            and _walk(config, "outbox.sns_topic_arn")[0] != "declared":
        logger.warning("DEPRECATED legacy SNS_LIFECYCLE_TOPIC_ARN is set; it still "
                       "resolves for this documented transition window but retires "
                       "in 0.8.0 — rename it to AB0T_MESH_SNS_LIFECYCLE_TOPIC_ARN. (D-10)")


def offline_mode(config: Optional[Mapping[str, Any]] = None) -> bool:
    """T-5's "contact nobody" mode: config `offline: true` or
    AB0T_QUOTA_OFFLINE=true|1|yes|on. Suppresses every startup reach-out
    (tier-catalog PUT, auth auto-subscribe, webhook alert dispatcher, paid-tier
    client wiring, and ALL THREE DDB self-provision paths: state store,
    activation ledger, outbox). Dev/CI mode — loudly logged."""
    if config and config.get("offline") is True:
        return True
    return os.environ.get("AB0T_QUOTA_OFFLINE", "").strip().lower() in ("1", "true", "yes", "on")
