"""QI-09 — every key a Lua script touches MUST arrive via KEYS.

Ticket 20260709_ab0t_quota_systemic_integrity_redesign (W-T3, Lua/cross-runtime
hardening). Python port of the Go auditor (counters/qi09_keys_audit_test.go).

Background: `_DECR_USER` once computed a claim key INSIDE the script
(`KEYS[1] .. ':gen:' .. gen`) that was never declared in KEYS. Redis requires
every accessed key to be declared; violating that is undefined behaviour —
fakeredis (lupa) and miniredis (gopher-lua) tolerate it, a real Redis Cluster
rejects it outright. Two reviews missed it; the fix (HSETNX on a declared
`:idemgen:` key) landed in Phase 2. This suite makes the *class* unrepresentable
in Python the way Go already guards it:

1. A static parser walks every `redis.call` in every Lua script the library
   ships and asserts the key argument is a `KEYS[...]` reference — never a
   computed / concatenated string.
2. A COMPLETENESS NET: the audited-script inventory is reconciled against a
   source scan of the whole `ab0t_quota` package, so a new script (or a new
   `redis.call` added to an existing one) cannot dodge the audit silently.
3. A NEGATIVE CONTROL: the auditor is fed the exact historical defect shape
   (a computed key) plus a bare-variable key, and MUST flag both. A guard that
   has never caught anything has told you nothing.

Emulator caveat (stated per the ticket standard): this is a STATIC audit — it
does not need Redis at all, so it is one of the few suites here whose verdict
does NOT weaken under fakeredis. What only a real clustered Redis could add is
the runtime proof that an undeclared key errors; the auditor prevents the class
before that proof is needed.
"""

from __future__ import annotations

import pathlib
import re

import ab0t_quota
from ab0t_quota import engine as engine_mod
from ab0t_quota import activations as activations_mod
from ab0t_quota import handler_ledger as handler_ledger_mod
from ab0t_quota.counters import gauge as gauge_mod
from ab0t_quota.counters import accumulator as accumulator_mod

# ---------------------------------------------------------------------------
# The auditor (port of Go luaKeyViolations — counters/qi09_keys_audit_test.go)
# ---------------------------------------------------------------------------

# Redis commands whose FIRST argument is a key. Superset of what our scripts
# use today (mirrors + extends the Go keyCommands map).
KEY_COMMANDS = {
    "GET", "SET", "SETNX", "INCR", "INCRBYFLOAT", "DECR", "EXPIRE", "DEL",
    "SADD", "SREM", "SMEMBERS", "SCARD", "ZADD", "ZCARD", "ZREMRANGEBYSCORE",
    "PERSIST", "EXISTS", "HSETNX", "HGET", "HSET", "HDEL", "TTL", "PTTL",
    "GETSET", "TYPE",
}

_RE_REDIS_CALL = re.compile(r"redis\.call\(\s*'([A-Z]+)'\s*,\s*([^,\)]+)")


def lua_key_violations(src: str) -> list[str]:
    """Return the QI-09 violations in a Lua source string (empty => clean)."""
    violations: list[str] = []
    # The QI-09 smell in one line: a key built by concatenation.
    if "] .." in src or re.search(r"'[^']*:'\s*\.\.", src):
        violations.append("script concatenates to build a key (QI-09 smell)")
    for cmd, arg in _RE_REDIS_CALL.findall(src):
        arg = arg.strip()
        if cmd not in KEY_COMMANDS:
            continue
        if not arg.startswith("KEYS["):
            violations.append(
                f"redis.call('{cmd}', {arg} ...) accesses an undeclared key"
            )
    return violations


# ---------------------------------------------------------------------------
# The inventory — every Lua script the Python library ships, by name.
# (KEEP IN SYNC deliberately: test_inventory_is_complete below fails loudly if
# a redis.call exists anywhere in ab0t_quota that this table does not cover.)
# ---------------------------------------------------------------------------

AUDITED_SCRIPTS: dict[str, str] = {
    "gauge._INCR": gauge_mod._INCR,
    "gauge._DECR": gauge_mod._DECR,
    "gauge._INCR_USER": gauge_mod._INCR_USER,
    "gauge._DECR_USER": gauge_mod._DECR_USER,
    "gauge._TRY_INCR": gauge_mod._TRY_INCR,
    "gauge._TRY_INCR_USER": gauge_mod._TRY_INCR_USER,
    "accumulator._ACC_INCR": accumulator_mod._ACC_INCR,
    "engine._ACQUIRE": engine_mod._ACQUIRE,
    "activations._TRANSITION": activations_mod._TRANSITION,
    "handler_ledger.RedisLedgerStore._CAS_RECLAIM":
        handler_ledger_mod.RedisLedgerStore._CAS_RECLAIM,
}


class TestLuaKeysAllDeclaredQI09:
    def test_every_shipped_script_declares_every_key(self):
        """QI-09: no shipped Lua script may access a key not passed via KEYS."""
        failures: list[str] = []
        for name, src in AUDITED_SCRIPTS.items():
            for viol in lua_key_violations(src):
                failures.append(f"{name}: {viol}")
        assert not failures, (
            "QI-09 violation(s) — every key a script accesses must arrive via "
            "KEYS[] (undefined on standalone Redis, an outright error on "
            f"cluster):\n" + "\n".join(failures)
        )

    def test_inventory_is_complete(self):
        """COMPLETENESS NET: every `redis.call` in the ab0t_quota package source
        must be accounted for by the audited inventory above. If this fails, a
        Lua script was added (or grew a call) without being registered for the
        QI-09 audit — register it in AUDITED_SCRIPTS."""
        pkg_dir = pathlib.Path(ab0t_quota.__file__).parent
        source_calls = 0
        per_file: dict[str, int] = {}
        for py in sorted(pkg_dir.rglob("*.py")):
            n = py.read_text().count("redis.call")
            if n:
                per_file[str(py.relative_to(pkg_dir))] = n
                source_calls += n
        audited_calls = sum(s.count("redis.call") for s in AUDITED_SCRIPTS.values())
        assert audited_calls == source_calls, (
            f"redis.call count mismatch: audited scripts contain {audited_calls} "
            f"call sites but the package source contains {source_calls} "
            f"({per_file}). A Lua script is dodging the QI-09 audit — add it to "
            "AUDITED_SCRIPTS in this file."
        )


class TestQI09AuditorHasTeeth:
    """NEGATIVE CONTROLS — break the property, confirm the auditor goes red.
    A green auditor proves nothing until it has been shown it can flag."""

    def test_flags_the_historical_defect_shape(self):
        """The exact _DECR_USER defect: a claim key computed inside the script
        (`KEYS[1] .. ':gen:' .. gen`) and SET without declaration."""
        bad_computed = """
local gen = redis.call('GET', KEYS[4]); if not gen then gen = '0' end
if not redis.call('SET', KEYS[1] .. ':gen:' .. gen, '1', 'NX', 'EX', ARGV[2]) then
  return '0'
end
"""
        assert lua_key_violations(bad_computed), (
            "QI-09 auditor did not flag the historical _DECR_USER computed-key "
            "defect — the auditor is VACUOUS"
        )

    def test_flags_a_bare_variable_key(self):
        bad_undeclared = "redis.call('INCRBYFLOAT', somevar, 1)"
        assert lua_key_violations(bad_undeclared), (
            "QI-09 auditor did not flag a non-KEYS[] key argument — VACUOUS"
        )

    def test_flags_a_string_literal_key(self):
        bad_literal = "redis.call('SET', 'quota:hardcoded:key', '1')"
        assert lua_key_violations(bad_literal), (
            "QI-09 auditor did not flag a hardcoded string-literal key — VACUOUS"
        )

    def test_clean_script_not_false_flagged(self):
        clean = "redis.call('GET', KEYS[1]); redis.call('SET', KEYS[2], '0')"
        assert lua_key_violations(clean) == [], (
            "clean script wrongly flagged (false positive would train people "
            "to ignore the auditor)"
        )

    def test_hash_fields_need_no_declaration(self):
        """The QI-09 FIX shape must pass: HSETNX(KEYS[1], gen, 1) — the
        generation is a FIELD of a declared key, not a key."""
        fix_shape = "if redis.call('HSETNX', KEYS[1], gen, '1') == 0 then return '0' end"
        assert lua_key_violations(fix_shape) == [], (
            "the auditor wrongly flags the sanctioned HSETNX-on-declared-key fix"
        )
