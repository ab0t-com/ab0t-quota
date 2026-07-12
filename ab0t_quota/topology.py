"""D-71 — machine-check the Redis TOPOLOGY at startup.

The atomic counter is implemented with **multi-key Lua scripts** (`_INCR`,
`_DECR`, `_INCR_USER`, `_DECR_USER`, `_ACQUIRE` — an idempotency key plus the
org/user counter keys). On a **Redis Cluster** those keys hash to different
slots and every one of them fails with **CROSSSLOT** (observed at the server —
`information_real_redis_conformance_20260711.md` §4, D-23).

Our own prod Redis is single-node, so *we* never hit it. **But this is a
library.** A mesh client on a clustered Redis installs it, writes one
`quota-config.json` (the drop-in promise), boots — and the counter primitive
fails outright at the first `acquire`, with no startup signal explaining why.
"Drop-in" must mean *"it tells you at startup that your setup will not work"*,
never *"it silently breaks."*

This module is the topology twin of `billing/outbox.py::check_redis_outbox_durability`
(D-32), and deliberately has the SAME shape:

  * a **definitive negative** (`cluster_enabled:1`, like an `allkeys-*` eviction
    policy) is a hard refusal that **no operator flag can override** — CROSSSLOT
    does not care what anyone asserted;
  * an **absent signal** (`CLUSTER INFO` unavailable — some managed Redis disable
    it, exactly as ElastiCache disables `CONFIG`) is refused *unless* the operator
    puts an explicit assertion on the record:
    `storage.redis_cluster_confirmed_disabled: true`
    (env: `AB0T_QUOTA_REDIS_CLUSTER_CONFIRMED_DISABLED=true`);
  * the verdict is surfaced in Capabilities (`redis_topology`) and **fails
    `/quota/health`** — an event with no sink is not observability (D-40), and the
    absence of a positive signal is not health (D-49/D-51).

Cluster **support** (the hash-tagged `quota:{org}:…` keyspace, `storage.keyspace_version`
+ dual-read/write) is a gated roadmap item (D-23). v1 ships an honest refusal:
refusing loudly is shippable; breaking silently is not.
"""
from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

#: Capability values for `redis_topology`. `single-node` may carry a suffix when
#: the verdict rests on an operator assertion — it is always on the record.
SINGLE_NODE = "single-node"
CLUSTER = "CLUSTER (unsupported)"
UNKNOWN = "unknown"

#: Env equivalent of `storage.redis_cluster_confirmed_disabled` (config wins).
CONFIRM_ENV = "AB0T_QUOTA_REDIS_CLUSTER_CONFIRMED_DISABLED"


class ClusterTopologyError(RuntimeError):
    """Startup refusal: the Redis topology is unsupported or unverifiable (D-71)."""


def confirmed_disabled_from(config: dict) -> bool:
    """Read the operator's on-the-record assertion from config, falling back to
    the env var. An assertion is a positive act, never a default."""
    storage = (config or {}).get("storage", {}) or {}
    if "redis_cluster_confirmed_disabled" in storage:
        return bool(storage["redis_cluster_confirmed_disabled"])
    return os.getenv(CONFIRM_ENV, "").strip().lower() in ("1", "true", "yes")


def parse_cluster_enabled(raw) -> Optional[bool]:
    """Parse `cluster_enabled:{0,1}` out of an INFO payload (text or the dict
    redis-py returns). None when the field is absent/unparseable — which is
    UNKNOWN, never "safe" (D-51: absence is not a value)."""
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, dict):
        val = raw.get("cluster_enabled", raw.get(b"cluster_enabled"))
        if val is None:
            return None
        if isinstance(val, (bytes, bytearray)):
            val = val.decode("utf-8", "replace")
        return str(val).strip() in ("1", "True", "true")
    for line in str(raw).replace("\r\n", "\n").split("\n"):
        if line.strip().startswith("cluster_enabled:"):
            return line.split(":", 1)[1].strip() == "1"
    return None


async def probe_cluster_enabled(redis) -> Tuple[Optional[bool], str]:
    """Ask the client's Redis whether it is clustered. Returns (enabled, probe).

    **`INFO cluster`, not `CLUSTER INFO`** — and that distinction is load-bearing,
    verified against real redis:7 servers (not an emulator, which agrees with
    whatever you assumed):

      * a NON-clustered redis:7 **errors** on `CLUSTER INFO`
        (`ERR This instance has cluster support disabled`);
      * a CLUSTER-enabled node **answers** `CLUSTER INFO` — but its payload contains
        **no `cluster_enabled` field at all** (it carries cluster_state, slots, …).

    So a `CLUSTER INFO`-only guard refuses every correct single-node deployment and
    cannot even parse the cluster. `INFO cluster` answers `cluster_enabled:0|1` on
    BOTH, and is the primary probe. `CLUSTER INFO` remains a fallback for a server
    with a trimmed INFO: it *answering at all* is itself the positive cluster signal,
    and its "cluster support disabled" error is a positive single-node signal.
    """
    # (1) INFO cluster — definitive on both topologies.
    try:
        raw = await redis.info("cluster")
        enabled = parse_cluster_enabled(raw)
        if enabled is not None:
            return enabled, "INFO cluster"
    except Exception as e:
        info_err = f"{type(e).__name__}"
    else:
        info_err = "no cluster_enabled field"

    # (2) CLUSTER INFO fallback (trimmed/proxied INFO).
    try:
        raw = await redis.execute_command("CLUSTER", "INFO")
    except Exception as e:
        if "cluster support disabled" in str(e).lower():
            return False, "CLUSTER INFO (server: cluster support disabled)"
        return None, f"INFO cluster [{info_err}]; CLUSTER INFO [{type(e).__name__}]"
    enabled = parse_cluster_enabled(raw)
    if enabled is not None:
        return enabled, "CLUSTER INFO"
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    if "cluster_state" in text:
        # It answered CLUSTER INFO at all ⇒ this server runs in cluster mode.
        return True, "CLUSTER INFO (answered ⇒ cluster mode)"
    return None, f"INFO cluster [{info_err}]; CLUSTER INFO [unparseable]"


def evaluate_topology(
    cluster_enabled: Optional[bool],
    *,
    confirmed_disabled: bool,
    probe: str = "topology probe",
) -> Tuple[str, str]:
    """The pure topology decision (D-71), separated from the Redis plumbing so it is
    directly testable — the same split as `EvaluateDurability` (D-32).

    `cluster_enabled is None` means the probes could not verify the topology (a
    managed Redis that trims INFO and disables CLUSTER, an emulator, a proxy).

    Returns ``(topology, human_reason)`` — SINGLE_NODE | CLUSTER | UNKNOWN.
    """
    if cluster_enabled is True:
        # A DEFINITIVE negative. NOT overridable by the operator assertion — that
        # assertion exists for an ABSENT signal, exactly as redis_durability_confirmed
        # cannot override an allkeys-* eviction policy (D-32). CROSSSLOT does not care
        # what anyone asserted.
        return CLUSTER, f"{probe} reports cluster_enabled:1"
    if cluster_enabled is False:
        return SINGLE_NODE, f"{probe} reports cluster_enabled:0"
    if confirmed_disabled:
        return SINGLE_NODE, (
            f"{SINGLE_NODE} (topology unverifiable — {probe}; non-clustered asserted by "
            f"the operator: storage.redis_cluster_confirmed_disabled=true)"
        )
    return UNKNOWN, (
        f"topology unverifiable ({probe}) and storage.redis_cluster_confirmed_disabled "
        f"is not set — an unverified topology is not a safe one"
    )


async def check_redis_cluster_topology(
    redis,
    *,
    confirmed_disabled: bool = False,
) -> Tuple[str, str]:
    """Ask the client's Redis what it is → (topology, reason).

    Never raises: the caller decides what to do with UNKNOWN/CLUSTER (setup_quota
    refuses to start; see `topology_error`)."""
    enabled, probe = await probe_cluster_enabled(redis)
    return evaluate_topology(enabled, confirmed_disabled=confirmed_disabled, probe=probe)


_CLUSTER_MSG = (
    "ab0t-quota requires a NON-CLUSTERED Redis. The atomic counter is implemented with "
    "multi-key Lua scripts (idempotency key + org/user counter keys); on a Redis Cluster "
    "those keys hash to different slots and EVERY counter script fails with CROSSSLOT — so "
    "the library would admit work it cannot count. Your Redis reports cluster_enabled:1. "
    "Remedy: point storage.redis_url at a single-node (non-clustered) Redis. Hash-tagged "
    "keyspace support for Redis Cluster is on the roadmap (D-23); until it ships, ab0t-quota "
    "refuses to start rather than break silently at the first acquire. "
    "(storage.redis_cluster_confirmed_disabled does NOT override a positive cluster_enabled:1 "
    "signal — it exists only for a Redis whose CLUSTER INFO is unavailable.)"
)

_UNKNOWN_MSG = (
    "ab0t-quota could not VERIFY the Redis topology: neither `INFO cluster` nor `CLUSTER INFO` "
    "gave a usable answer (some managed Redis trim INFO and disable CLUSTER). The atomic counter "
    "uses multi-key Lua scripts that fail with CROSSSLOT on a Redis Cluster, so an unverified "
    "topology cannot be assumed safe — unknown fails closed. Remedy: use a Redis whose `INFO "
    "cluster` is reachable, or — if you KNOW this Redis is not clustered — put that assertion on "
    "the record by setting storage.redis_cluster_confirmed_disabled: true in quota-config.json "
    "(env: AB0T_QUOTA_REDIS_CLUSTER_CONFIRMED_DISABLED=true)."
)


def topology_error(topology: str, detail: str) -> ClusterTopologyError:
    """Build the loud, typed startup refusal. Names the CAUSE and the REMEDY —
    a refusal a client cannot act on is just an outage."""
    head = _CLUSTER_MSG if topology == CLUSTER else _UNKNOWN_MSG
    return ClusterTopologyError(f"{head} [detail: {detail}]")


def capability_value(topology: str, detail: str) -> str:
    """The value that lands on `app.state.quota_capabilities['redis_topology']` and
    is read by `/quota/health` — so a bad topology FAILS the probe rather than
    terminating in a log line nobody reads (D-40). An operator assertion is carried
    into the value: the record shows *why* we believe the topology is safe."""
    if topology == SINGLE_NODE:
        return detail if detail.startswith(SINGLE_NODE) else SINGLE_NODE
    return topology


def topology_ok(value) -> bool:
    """The health predicate. Only an affirmative `single-node` is healthy; missing,
    empty, `unknown`, `CLUSTER (unsupported)`, or anything unparseable is NOT
    (D-49/D-51 — absence is not a value, and unknown fails closed). `n/a` is the one
    other affirmative: a deployment with no Redis counter store has no cluster to
    break on."""
    s = str(value or "").strip().lower()
    return s.startswith(SINGLE_NODE) or s.startswith("n/a")
