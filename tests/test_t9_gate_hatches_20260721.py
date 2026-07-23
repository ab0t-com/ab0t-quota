"""T-9 (GATE-02, pack 20260721): every gate has an escape hatch or a documented
"none, because…" — an operator following the D-71→D-72 flag trail must never
dead-end at a gate with no door and no explanation.

D-73 gains `storage.redis_scripting_confirmed` for the UNVERIFIABLE case only
(SCRIPT LOAD could not run: command disabled/renamed/denied); an OBSERVED
rejection of the Lua itself (compile error) is a definitive negative no
assertion overrides (MUST #6 — the D-71/D-72 shape).
D-74's deliverable is the documented "none, because…": an observed below-floor
version is definitive; only unreadable versions degrade without refusing.
"""
from __future__ import annotations

import pytest


class _NoScriptRedis:
    """SCRIPT LOAD cannot RUN (managed Redis: command disabled/renamed)."""

    async def script_load(self, script):
        raise Exception("ERR unknown command 'script'")


class _CompileRejectRedis:
    """The server RAN the load and rejected the Lua — an observed negative."""

    async def script_load(self, script):
        raise Exception("ERR Error compiling script (new function): syntax error")


@pytest.mark.asyncio
class TestD73Hatch:
    async def test_unrunnable_probe_with_assertion_boots(self):
        """The hatch: SCRIPT LOAD unavailable + the on-the-record operator
        assertion => proceed (UNVERIFIED, loudly)."""
        from ab0t_quota.redis_preflight import check_redis_script_capability
        try:
            ok, detail = await check_redis_script_capability(
                _NoScriptRedis(), confirmed=True)
        except TypeError as e:
            pytest.fail(f"D-73 has no operator hatch (GATE-02 dead-end): {e}")
        assert ok is True, detail
        assert "redis_scripting_confirmed" in detail, \
            "the UNVERIFIED verdict must name the assertion it is riding on"

    async def test_unrunnable_probe_without_assertion_still_refuses(self):
        """Control: the hatch is a positive act, never a default."""
        from ab0t_quota.redis_preflight import check_redis_script_capability
        ok, detail = await check_redis_script_capability(_NoScriptRedis())
        assert ok is False

    async def test_observed_compile_reject_is_not_overridable(self):
        """MUST #6: the server ACCEPTED the probe and REJECTED our Lua — a
        definitive negative; the assertion must not override it."""
        from ab0t_quota.redis_preflight import check_redis_script_capability
        try:
            ok, detail = await check_redis_script_capability(
                _CompileRejectRedis(), confirmed=True)
        except TypeError as e:
            pytest.fail(f"D-73 hatch not implemented: {e}")
        assert ok is False, \
            "an observed compile rejection must refuse even under the assertion"

    async def test_scripting_error_names_the_hatch_and_its_limit(self):
        from ab0t_quota.redis_preflight import scripting_error
        msg = str(scripting_error("SCRIPT LOAD failed"))
        assert "redis_scripting_confirmed" in msg, \
            "the refusal must name the operator hatch (GATE-02)"


def test_version_floor_documents_it_has_no_hatch():
    """D-74's deliverable: the refusal STATES there is no override and why —
    the observed below-floor version is definitive (MUST #6)."""
    from ab0t_quota.redis_preflight import version_error
    msg = str(version_error("Redis 5.0.0 is below the floor")).lower()
    assert "no " in msg and ("assertion" in msg or "override" in msg), \
        "version_error must carry the documented 'none, because…' (T-9/D-74)"


def test_gate_family_hatch_audit():
    """GATE-02's audit: every refusing gate's error text names its hatch, or
    explicitly states no-override. (D-77 memory headroom never refuses — it
    only degrades — so it has no row.)"""
    from ab0t_quota.redis_preflight import (
        counter_eviction_error, scripting_error, version_error,
    )
    from ab0t_quota.topology import CLUSTER, UNKNOWN, topology_error

    rows = [
        # (gate, message, hatch named | no-override stated)
        ("D-71 unverifiable", str(topology_error(UNKNOWN, "d")),
         ["redis_cluster_confirmed_disabled"]),
        ("D-71 observed cluster", str(topology_error(CLUSTER, "d")),
         ["does NOT override"]),
        ("D-72 eviction/unverifiable", str(counter_eviction_error("d")),
         ["redis_durability_confirmed"]),
        ("D-73 scripting", str(scripting_error("d")),
         ["redis_scripting_confirmed"]),
        ("D-74 version floor", str(version_error("d")),
         ["no assertion", "no override"]),
    ]
    missing = [gate for gate, msg, markers in rows
               if not any(m.lower() in msg.lower() for m in markers)]
    assert missing == [], \
        f"gate(s) with neither a named hatch nor a documented none-because: {missing}"


def test_d34_refusal_names_every_working_remedy():
    """The D-34 paid-service refusal sits at the END of the T-6 cascade; its
    fix list must include the new auto_create_tables opt-in (design §8 row 6:
    the flag surfaces before/at the cascade's end, not never)."""
    import inspect
    import ab0t_quota.setup as setup_mod
    src = inspect.getsource(setup_mod._resolve_outbox_durability)
    assert "allow_ephemeral" in src
    assert "auto_create_tables" in src, \
        "the D-34 refusal's remedy list must name storage.auto_create_tables"
