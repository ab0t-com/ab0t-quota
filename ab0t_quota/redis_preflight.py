"""D-72 / D-73 / D-74 — the Redis preflight. Machine-check every infrastructure
assumption this library makes, at boot, and refuse loudly.

D-32 checked Redis DURABILITY *for the outbox*. D-71 checked TOPOLOGY. This module
is the generalisation, and it owns the ONE durability implementation (the outbox's
check now delegates here — a second copy of the same judgement is D-35's mistake in
a new costume).

**D-72 — the counter may not live on an evicting Redis (the urgent one).**
`maxmemory-policy=allkeys-*` lets Redis evict ANY key under memory pressure — including
a **live gauge**. The counter then reads ZERO for a resource that is still running:
**under-count → phantom headroom → over-admission**, D-31's forbidden direction. The
counter is not a cache of convenience; it *is* the admission gate. And unlike D-71,
this does not announce itself: D-71 fails **loudly at boot**, D-72 fails **silently at
runtime, as free quota, behind a green health check**. A loud refusal is a support
ticket; a silently-evicted gauge is unbilled revenue and an over-admitted customer.

Note the asymmetry with the outbox check: the counter's fatal property is **eviction**,
not persistence. A restart-lost counter *heals* (the reconciler converges it to
Σ open activations, D-28); an evicted counter under load silently under-counts while the
process keeps happily serving. So `appendonly=no` alone does not block startup — over-
refusing trains operators to ignore the guard (D-49's false-503 lesson). The outbox is
the opposite: it holds money events nothing can reconstruct, so it needs BOTH.

**D-73 — scripting capability.** Every counter op is `EVAL`. A Redis with scripting
disabled/renamed fails at the FIRST acquire, not at boot. `SCRIPT LOAD` of the REAL
`_ACQUIRE` source at startup is definitive (a probe that loads `return 1` proves nothing
about our scripts) — and it warms the script cache.

**D-74 — a version floor.** Asserted at boot and named in the client docs.

The law, in all three: a **definitive negative** is a hard, unoverridable refusal; an
**absent signal** (`CONFIG` unavailable — ElastiCache disables it) needs an explicit
operator assertion **on the record** (`storage.redis_durability_confirmed`); absence is
never health (D-49/D-51).
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

#: An `allkeys-*` policy evicts ANY key — a live gauge included.
EVICTING_POLICIES = ("allkeys-lru", "allkeys-lfu", "allkeys-random")

#: The oldest Redis this library is tested against. Bump deliberately (D-74).
REDIS_VERSION_FLOOR = (6, 0, 0)

#: Env equivalent of `storage.redis_durability_confirmed` (config wins).
DURABILITY_CONFIRM_ENV = "AB0T_QUOTA_REDIS_DURABILITY_CONFIRMED"


class CounterEvictionError(RuntimeError):
    """Startup refusal: the COUNTER is on an evicting (or unverifiable) Redis (D-72)."""


class ScriptingUnsupportedError(RuntimeError):
    """Startup refusal: this Redis cannot run the counter's Lua scripts (D-73)."""


class RedisVersionError(RuntimeError):
    """Startup refusal: this Redis is below the supported version floor (D-74)."""


def durability_confirmed_from(config: dict) -> bool:
    """The operator's on-the-record assertion, from config (canonical) or env.
    An assertion is a positive act, never a default."""
    storage = (config or {}).get("storage", {}) or {}
    if "redis_durability_confirmed" in storage:
        return bool(storage["redis_durability_confirmed"])
    outbox = (config or {}).get("outbox", {}) or {}
    if "redis_durability_confirmed" in outbox:  # the D-32 knob, still honoured
        return bool(outbox["redis_durability_confirmed"])
    return os.getenv(DURABILITY_CONFIRM_ENV, "").strip().lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# the shared CONFIG read + the pure evaluators (ONE implementation)
# ---------------------------------------------------------------------------

async def read_redis_policy(redis) -> Tuple[str, str, str, bool, str]:
    """`CONFIG GET` maxmemory-policy / appendonly / save.
    Returns (policy, appendonly, save, unavailable, error_name)."""
    def _val(d, k):
        if isinstance(d, dict):
            return str(d.get(k, "") or "")
        return ""
    try:
        pol = await redis.config_get("maxmemory-policy")
        ao = await redis.config_get("appendonly")
        save = await redis.config_get("save")
    except Exception as e:
        return "", "", "", True, type(e).__name__
    return (_val(pol, "maxmemory-policy").lower(),
            _val(ao, "appendonly").lower(),
            _val(save, "save").strip(), False, "")


def evaluate_eviction(policy: str, *, unavailable: bool, confirmed: bool) -> Tuple[bool, str]:
    """D-72's pure decision: may the COUNTER live on this Redis?

    An `allkeys-*` policy READ from the server is a DEFINITIVE negative and is NOT
    overridable by the operator assertion — asserting that an evicting Redis does not
    evict does not stop it evicting (D-32's law: `redis_durability_confirmed` never
    overrode an `allkeys-*` policy either).
    """
    policy = (policy or "").strip().lower()
    if unavailable:
        if confirmed:
            return True, ("CONFIG unavailable (e.g. ElastiCache); a non-evicting policy is "
                          "asserted by the operator (storage.redis_durability_confirmed=true)")
        return False, ("Redis CONFIG is unavailable and storage.redis_durability_confirmed is "
                       "not set — the counter's eviction policy cannot be verified, and an "
                       "unverified counter store is not a safe one")
    if policy in EVICTING_POLICIES:
        return False, (f"maxmemory-policy={policy} can EVICT a live gauge key — the counter would "
                       f"read zero for a resource that is still running (under-count → phantom "
                       f"headroom → over-admission). Use noeviction (or a volatile-* policy: the "
                       f"counter keys carry no TTL)")
    return True, f"{policy or 'unset'}"


def evaluate_durability(policy: str, appendonly: str, save: str, *,
                        unavailable: bool, confirmed: bool) -> Tuple[bool, str]:
    """D-32's pure decision (the OUTBOX): eviction AND persistence. Money events cannot
    be reconstructed, so the outbox needs both; the counter needs only the first."""
    ok, reason = evaluate_eviction(policy, unavailable=unavailable, confirmed=confirmed)
    if not ok:
        if unavailable:
            return False, ("Redis CONFIG unavailable and outbox.redis_durability_confirmed is not "
                           "set — cannot verify persistence/eviction; treated as NON-durable")
        return False, (f"maxmemory-policy={(policy or '').lower()} can silently evict pending money "
                       f"events; use noeviction (or a volatile-* policy with no TTL on outbox keys)")
    if unavailable:  # confirmed
        return True, ("Redis CONFIG unavailable (e.g. ElastiCache); durability asserted by "
                      "operator (outbox.redis_durability_confirmed=true)")
    persisted = (appendonly or "").lower() == "yes" or bool((save or "").strip()) or confirmed
    if not persisted:
        return False, ("no Redis persistence (appendonly=no and no save points) — a restart/failover "
                       "loses pending events; enable appendonly or RDB save")
    return True, f"maxmemory-policy={policy or 'unset'}, appendonly={appendonly or 'unset'}"


async def check_redis_durability(redis, *, confirmed: bool = False) -> Tuple[bool, str]:
    """D-32 (the outbox): persistence + a non-evicting policy — AND (D-81) whether the
    persistence is actually WORKING. THE durability check; `billing.outbox.check_redis_outbox_durability`
    is an alias of this, not a copy (D-35).

    D-81: a Redis with `appendonly yes` whose `aof_last_write_status` is `err` is NOT durable,
    however green its configuration reads. Asking only the config is asking only the intent —
    and the existing D-34 boot gate (a paid service that cannot durably bill must not start)
    then refuses this Redis for free."""
    policy, appendonly, save, unavailable, _err = await read_redis_policy(redis)
    durable, reason = evaluate_durability(policy, appendonly, save,
                                          unavailable=unavailable, confirmed=confirmed)
    if not durable:
        return durable, reason

    facts = await check_persist_facts(redis)
    status, detail = evaluate_persist_facts(
        aof_enabled=facts.get("aof_enabled"),
        aof_write=facts.get("aof_last_write_status"),
        rdb_bgsave=facts.get("rdb_last_bgsave_status"),
        aof_rewrite=facts.get("aof_last_bgrewrite_status"))
    if status == "persist_failing":
        return False, detail
    return True, reason


async def check_redis_counter_eviction(redis, *, confirmed: bool = False) -> Tuple[bool, str]:
    """D-72 (the counter): a non-evicting policy. Same CONFIG read, same law, different
    (correct) severity — the counter tolerates a restart but never an eviction."""
    policy, _ao, _save, unavailable, _err = await read_redis_policy(redis)
    return evaluate_eviction(policy, unavailable=unavailable, confirmed=confirmed)


# ---------------------------------------------------------------------------
# D-73 — scripting capability
# ---------------------------------------------------------------------------

async def check_redis_script_capability(redis) -> Tuple[bool, str]:
    """`SCRIPT LOAD` the REAL `_ACQUIRE` source. If this Redis cannot run our Lua, we
    learn it at BOOT — not at the first admission decision. Bonus: the script cache is
    warm, so the first acquire does not pay the load."""
    from .engine import _ACQUIRE
    try:
        sha = await redis.script_load(_ACQUIRE)
    except Exception as e:
        return False, (f"SCRIPT LOAD of the counter's _ACQUIRE script failed ({type(e).__name__}: {e}) "
                       f"— this Redis cannot run the atomic counter")
    if isinstance(sha, (bytes, bytearray)):
        sha = sha.decode("utf-8", "replace")
    return True, f"on (EVAL verified, _ACQUIRE sha={str(sha)[:12]}…)"


# ---------------------------------------------------------------------------
# D-74 — version floor
# ---------------------------------------------------------------------------

_VER = re.compile(r"(\d+)\.(\d+)\.?(\d+)?")


def evaluate_version(version: Optional[str], *,
                     floor: tuple = REDIS_VERSION_FLOOR) -> Tuple[str, str]:
    """Returns (status, detail): "ok" | "below_floor" | "unknown"."""
    if not version:
        return "unknown", ("Redis version could not be read (INFO unavailable) — the supported "
                           "floor could not be verified")
    m = _VER.search(str(version))
    if not m:
        return "unknown", f"unparseable Redis version {version!r}"
    parts = tuple(int(x) for x in (m.group(1), m.group(2), m.group(3) or 0))
    floor_s = ".".join(str(x) for x in floor)
    if parts < tuple(floor):
        return "below_floor", (f"Redis {version} is below ab0t-quota's supported floor {floor_s}")
    return "ok", str(version)


async def check_redis_version(redis, *, floor: tuple = REDIS_VERSION_FLOOR) -> Tuple[str, str]:
    try:
        info = await redis.info("server")
    except Exception:
        return evaluate_version(None, floor=floor)
    version = None
    if isinstance(info, dict):
        version = info.get("redis_version") or info.get(b"redis_version")
        if isinstance(version, (bytes, bytearray)):
            version = version.decode("utf-8", "replace")
    return evaluate_version(version, floor=floor)


# ---------------------------------------------------------------------------
# the loud, typed refusals — name the CAUSE and the REMEDY
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# D-77 — memory headroom (the cliff we never surfaced)
# ---------------------------------------------------------------------------

#: Fraction of `maxmemory` at which we start degrading. `noeviction` fails CLOSED when
#: Redis runs out (writes OOM → acquire raises → admission denies), which is the SAFE
#: direction — but the service DIES. "Dies at 3am with no warning" is not zero-caveats.
MEMORY_WARN_RATIO = 0.90


def evaluate_memory_headroom(maxmemory, used_memory) -> Tuple[str, str]:
    """Returns (status, detail): "ok" | "low_headroom" | "unbounded" | "unknown"."""
    try:
        mm = int(maxmemory) if maxmemory is not None else None
        used = int(used_memory) if used_memory is not None else None
    except (TypeError, ValueError):
        return "unknown", "unparseable INFO memory"
    if mm is None or used is None:
        return "unknown", "Redis INFO memory unavailable — headroom cannot be computed"
    if mm == 0:
        return "unbounded", "maxmemory=0 (no eviction/OOM cliff configured)"
    ratio = used / mm
    pct = round(ratio * 100)
    if ratio >= MEMORY_WARN_RATIO:
        return "low_headroom", (f"{pct}% of maxmemory used ({used}/{mm}) — with a non-evicting "
                                f"policy Redis will start REFUSING WRITES at the cliff, and the "
                                f"counter's admission path fails closed (safe, but the service is "
                                f"down). Raise maxmemory or reduce load.")
    return "ok", f"{pct}% of maxmemory used ({used}/{mm})"


async def check_memory_headroom(redis) -> Tuple[str, str]:
    try:
        info = await redis.info("memory")
    except Exception:
        return evaluate_memory_headroom(None, None)
    if not isinstance(info, dict):
        return evaluate_memory_headroom(None, None)
    return evaluate_memory_headroom(info.get("maxmemory"), info.get("used_memory"))


# ---------------------------------------------------------------------------
# D-80 — the EFFECT, not the policy. "Did it already happen?"
# ---------------------------------------------------------------------------
#
# Every guard we own asks the server what its configuration IS. None asked what it DID.
# A Redis whose `maxmemory-policy` was corrected to `noeviction` at 09:00 — *after* it evicted
# a live gauge at 03:00 — passes EVERY check in this module, while the damage sits in the
# counter: an evicted gauge reads LOW, so the counter under-counts a resource that still
# exists → phantom headroom → over-admission (D-31's forbidden direction).
#
# `INFO stats.evicted_keys` is the FACT. The policy is only the forecast.
#
# The generalisation (worth carrying to every future check): for each assumption we verify,
# ask **"is there an observable FACT proving it was ALREADY violated?"** — and check that too.


def evaluate_eviction_facts(evicted_keys: Optional[int]) -> Tuple[str, str]:
    """Returns (status, detail): "ok" | "evictions_observed" | "unknown".

    ANY eviction is a money incident: we cannot know the evicted key was not a live gauge,
    and a gauge that was evicted reads LOW. The counter is no longer trustworthy — it must be
    reconciled to Σ open activations (D-28/D-33)."""
    if evicted_keys is None:
        return "unknown", ("Redis INFO stats unavailable — cannot tell whether this server has "
                           "ALREADY evicted keys")
    if evicted_keys > 0:
        return "evictions_observed", (
            f"this Redis has EVICTED {evicted_keys} key(s) (INFO stats.evicted_keys). Any one of "
            f"them may have been a live gauge: an evicted gauge reads LOW, so the counter now "
            f"UNDER-counts resources that still exist (phantom headroom → over-admission). The "
            f"eviction policy may since have been corrected — the damage is already in the "
            f"counter. The counter must be reconciled, and the Redis must never evict again "
            f"(maxmemory-policy noeviction).")
    return "ok", "0 (no keys evicted on this server)"


async def check_evicted_keys(redis) -> Tuple[Optional[int], str]:
    """Read `INFO stats.evicted_keys` — the fact that a policy check cannot see."""
    try:
        info = await redis.info("stats")
    except Exception as e:
        return None, f"INFO stats unavailable ({type(e).__name__})"
    if not isinstance(info, dict):
        return None, "INFO stats unparseable"
    raw = info.get("evicted_keys", info.get(b"evicted_keys"))
    try:
        return int(raw), "ok"
    except (TypeError, ValueError):
        return None, "evicted_keys not reported"


def eviction_facts_ok(value) -> bool:
    """Health predicate. Degrades on an OBSERVED eviction. `unknown` does not degrade (an
    unreadable statistic is not itself a hazard — the ratified D-74 deviation), but it is
    recorded, and the POLICY check already fails closed on an unverifiable server."""
    s = str(value or "").strip().lower()
    return not s.startswith("evictions_observed")


# ---------------------------------------------------------------------------
# D-81 — persistence is CONFIGURED. Is it WORKING?
# ---------------------------------------------------------------------------
#
# D-32 asked Redis `appendonly`. It never asked whether the writes were SUCCEEDING. A full
# disk, a permissions error, a failing volume → AOF configured, AOF failing, the config check
# GREEN, and the OUTBOX silently losing money events.
#
# This is WORSE than D-80's eviction. The counter only needs non-eviction and can HEAL (the
# reconciler converges it to Σ open activations, D-28). The OUTBOX REQUIRES persistence
# (D-30/D-32): **a lost outbox row is money nobody can reconstruct.**
#
# `aof_last_write_status` / `rdb_last_bgsave_status` / `aof_last_bgrewrite_status` are the
# FACTS. `appendonly yes` is only the intent.
#
# Observed on a real server (not a stubbed status string): with `appendfsync always` Redis
# EXITS on an AOF write error (loud); with the DEFAULT `everysec` it STAYS UP with
# `aof_last_write_status:err` — the quiet case, and the one that costs money.


async def check_persist_facts(redis) -> dict:
    """Read `INFO persistence` — the facts a config check cannot see."""
    try:
        info = await redis.info("persistence")
    except Exception:
        return {}
    if not isinstance(info, dict):
        return {}

    def _s(k):
        v = info.get(k, info.get(k.encode() if isinstance(k, str) else k))
        if isinstance(v, (bytes, bytearray)):
            v = v.decode("utf-8", "replace")
        return None if v is None else str(v).strip().lower()

    return {
        "aof_enabled": _s("aof_enabled"),
        "aof_last_write_status": _s("aof_last_write_status"),
        "aof_last_bgrewrite_status": _s("aof_last_bgrewrite_status"),
        "rdb_last_bgsave_status": _s("rdb_last_bgsave_status"),
    }


def evaluate_persist_facts(*, aof_enabled, aof_write, rdb_bgsave, aof_rewrite) -> Tuple[str, str]:
    """Returns (status, detail): "ok" | "persist_failing" | "unknown".

    ANY failed persist status is a money incident when the OUTBOX lives on this Redis: the
    library is being told to durably record billing events onto a server that cannot durably
    record anything."""
    vals = {"aof_last_write_status": aof_write,
            "rdb_last_bgsave_status": rdb_bgsave,
            "aof_last_bgrewrite_status": aof_rewrite}
    if all(v is None for v in vals.values()):
        return "unknown", ("Redis INFO persistence unavailable — cannot tell whether this "
                           "server's persistence is actually WORKING")
    failing = [f"{k}={v}" for k, v in vals.items()
               if v is not None and str(v).lower() not in ("ok", "")]
    if failing:
        return "persist_failing", (
            f"Redis reports FAILED persistence: {', '.join(sorted(failing))}. The configuration "
            f"says it persists (appendonly={aof_enabled}); the SERVER says the writes are not "
            f"landing — a full disk, a permissions error, a failing volume. The outbox holds "
            f"MONEY events that nobody can reconstruct: a pending settlement written here is "
            f"lost on the next restart/failover. Free the disk / fix the volume, then verify "
            f"aof_last_write_status=ok.")
    return "ok", ("persistence verified working (aof_last_write_status=%s, rdb_last_bgsave_status=%s)"
                  % (aof_write or "n/a", rdb_bgsave or "n/a"))


def persist_facts_ok(value) -> bool:
    """Health predicate. Degrades on an OBSERVED persist failure. `unknown` does not degrade —
    the CONFIG check (D-32) already fails closed on a server it cannot interrogate."""
    return not str(value or "").strip().upper().startswith("FAILING")


# ---------------------------------------------------------------------------
# D-75 — the invariants, re-verifiable at ANY time
# ---------------------------------------------------------------------------

async def verify_redis_invariants(redis, config: dict, *,
                                  outbox_on_redis: bool = False) -> Tuple[dict, list]:
    """D-75 — **"An assumption machine-checked once is an assumption trusted thereafter."**

    Every guard we own (D-32 durability, D-71 topology, D-72 eviction, D-73 scripting)
    verified the world at BOOT and then trusted it forever. A `CONFIG SET maxmemory-policy
    allkeys-lru` at 3am — or a managed-Redis failover onto a replica with a different
    config, or a clustered endpoint — is invisible to all of them. The counter silently
    becomes evictable → a live gauge is evicted → under-count → phantom headroom →
    over-admission (D-31), behind a green health check.

    This function is the SAME judgement as the boot gates, callable at ANY time. It never
    raises: it returns

        (capability_updates, unsafe) where unsafe = [(capability_key, detail), …]

    The caller decides the consequence — at BOOT that is a refusal (`setup_quota`); at
    RUNTIME it is **loud, not fatal**: degrade `/quota/health`, alert, update Capabilities.
    A running service that suddenly refuses is its own outage; the operator decides whether
    to drain.
    """
    from .topology import (
        SINGLE_NODE, capability_value as topo_capability_value,
        check_redis_cluster_topology, confirmed_disabled_from,
    )
    caps: dict = {}
    unsafe: list = []

    # D-71 topology
    topo, topo_detail = await check_redis_cluster_topology(
        redis, confirmed_disabled=confirmed_disabled_from(config))
    caps["redis_topology"] = topo_capability_value(topo, topo_detail)
    if topo != SINGLE_NODE:
        unsafe.append(("redis_topology", topo_detail))

    # D-72 counter eviction (the one a runtime flip actually changes)
    confirmed = durability_confirmed_from(config)
    ok, detail = await check_redis_counter_eviction(redis, confirmed=confirmed)
    caps["counter_eviction_policy"] = detail if ok else f"EVICTING/UNVERIFIED ({detail})"
    if not ok:
        unsafe.append(("counter_eviction_policy", detail))

    # D-73 scripting (a managed Redis can rename EVAL away under a version upgrade)
    script_ok, script_detail = await check_redis_script_capability(redis)
    caps["redis_scripting"] = script_detail if script_ok else f"OFF ({script_detail})"
    if not script_ok:
        unsafe.append(("redis_scripting", script_detail))

    # D-74 version (a failover can land you on an older node)
    status, ver_detail = await check_redis_version(redis)
    caps["redis_version"] = ver_detail if status == "ok" else f"{status} ({ver_detail})"
    if status == "below_floor":
        unsafe.append(("redis_version", ver_detail))

    # D-80 the FACT: has this server ALREADY evicted? A corrected policy hides a counter that
    # is already wrong. This is the one check that can catch damage the config no longer admits.
    evicted, _why = await check_evicted_keys(redis)
    fact_status, fact_detail = evaluate_eviction_facts(evicted)
    caps["counter_evictions_observed"] = (
        f"evictions_observed ({fact_detail})" if fact_status == "evictions_observed"
        else fact_detail if fact_status == "ok" else "unknown")
    if fact_status == "evictions_observed":
        unsafe.append(("counter_evictions_observed", fact_detail))

    # D-81 the FACT: persistence is configured — is it WORKING? Severity BY CONSEQUENCE (the
    # D-76 lesson, not uniformity): a failing AOF on the Redis holding the OUTBOX is money
    # nobody can reconstruct — a money incident. The same failure on a Redis that only holds
    # the COUNTER is not: the counter HEALS (reconciler → Σ open activations, D-28). Reporting
    # both as "money loss" would be the D-49 false-503 mistake.
    facts = await check_persist_facts(redis)
    p_status, p_detail = evaluate_persist_facts(
        aof_enabled=facts.get("aof_enabled"),
        aof_write=facts.get("aof_last_write_status"),
        rdb_bgsave=facts.get("rdb_last_bgsave_status"),
        aof_rewrite=facts.get("aof_last_bgrewrite_status"))
    if p_status == "persist_failing" and outbox_on_redis:
        caps["redis_persist_status"] = f"FAILING ({p_detail})"
        unsafe.append(("redis_persist_status", p_detail))
    elif p_status == "persist_failing":
        caps["redis_persist_status"] = (
            f"persistence failing, but the OUTBOX is not on this Redis — the counter heals "
            f"(reconciler → Σ open activations). Fix it anyway: {p_detail}")
        logger.warning("redis persistence is FAILING (D-81), though the outbox is elsewhere: %s",
                       p_detail)
    else:
        caps["redis_persist_status"] = p_detail if p_status == "ok" else "unknown"

    # D-77 memory headroom — degrades on the way to the cliff, never refuses.
    mem_status, mem_detail = await check_memory_headroom(redis)
    caps["memory_headroom"] = (mem_detail if mem_status == "ok"
                               else f"{mem_status} ({mem_detail})" if mem_status == "low_headroom"
                               else mem_status)
    if mem_status == "low_headroom":
        unsafe.append(("memory_headroom", mem_detail))

    return caps, unsafe


def counter_eviction_error(detail: str) -> CounterEvictionError:
    return CounterEvictionError(
        "ab0t-quota cannot run its COUNTER on this Redis. The counter is not a cache of "
        "convenience — it IS the admission gate. Under an `allkeys-*` maxmemory-policy Redis may "
        "EVICT a live gauge key under memory pressure; the counter then reads zero for a resource "
        "that is still running, and the library silently ADMITS work it has already run out of "
        "room for (under-count → phantom headroom → over-admission). Remedy: set "
        "`maxmemory-policy noeviction` on the Redis backing storage.redis_url (a `volatile-*` "
        "policy is also safe — the counter keys carry no TTL), or, if this Redis cannot report "
        "its CONFIG (some managed Redis disable it) and you KNOW it does not evict, put that "
        "assertion on the record: storage.redis_durability_confirmed: true (env: "
        f"AB0T_QUOTA_REDIS_DURABILITY_CONFIRMED=true). [detail: {detail}]")


def scripting_error(detail: str) -> ScriptingUnsupportedError:
    return ScriptingUnsupportedError(
        "ab0t-quota cannot run its counter on this Redis: every counter operation is an EVAL of a "
        "Lua script (the atomic acquire/incr/decr family), and this server did not accept a "
        "SCRIPT LOAD of the real _ACQUIRE source. Some managed Redis disable or rename the SCRIPT/"
        "EVAL commands. Remedy: use a Redis with scripting enabled. Refusing here is deliberate: "
        "the alternative is a service that boots green and fails at its first admission decision. "
        f"[detail: {detail}]")


def version_error(detail: str) -> RedisVersionError:
    floor_s = ".".join(str(x) for x in REDIS_VERSION_FLOOR)
    return RedisVersionError(
        f"ab0t-quota requires Redis >= {floor_s}. Remedy: upgrade the Redis backing "
        f"storage.redis_url. [detail: {detail}]")
