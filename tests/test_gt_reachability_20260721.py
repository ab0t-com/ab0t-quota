"""GT lane (parent pack T1–T5) — THE ORIGINAL INCIDENT'S FIX, in Python.

A Redis auth failure must never produce a Redis Cluster topology verdict.
Go fixed this in T-G3; the canonical ST-TOPOLOGY-1 clauses forbid it; until
this lane, Python still shipped the original misdiagnosis. RED-1 below IS the
original traceback, reproduced before fixing.

T1 check_redis_reachable + typed RedisUnreachableError (classified) ·
T2 the reachability gate runs FIRST, capability written before refusal ·
T3 assertion flags cannot mask an unreachable Redis · T4 data-plane CROSSSLOT
probe primary (definitive-cluster with zero admin privileges; success is NOT
single-node proof) · T5 the D-72/73/74 probes re-raise auth/connection errors
and treat NOPERM as the genuine absent signal.
"""
from __future__ import annotations

import json
import logging

import pytest
import redis.exceptions as rex
from fastapi import FastAPI
from unittest.mock import patch

MINIMAL_CONFIG = {
    "service_name": "test-svc",
    "storage": {"redis_url": "redis://test/0", "persistence_enabled": False,
                "connect_retry_seconds": 0},
    "tier_provider": {"type": "static", "default_tier": "starter"},
    "alerts": {"enabled": False},
    "enforcement": {"enabled": True},
    "resources": [{"service": "t", "resource_key": "thing.concurrent",
                   "display_name": "T", "counter_type": "gauge", "unit": "t"}],
    "tiers": [{"tier_id": "starter", "display_name": "S", "sort_order": 1,
               "limits": {"thing.concurrent": 5}, "features": []}],
}


def _write_config(tmp_path, monkeypatch, cfg):
    p = tmp_path / "quota-config.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.setenv("QUOTA_CONFIG_PATH", str(p))


class AuthFailRedis:
    """The incident's Redis: every command fails authentication."""

    def __getattr__(self, name):
        async def _fail(*a, **kw):
            raise rex.AuthenticationError("invalid username-password pair")
        return _fail


class ConnFailRedis:
    def __getattr__(self, name):
        async def _fail(*a, **kw):
            raise rex.ConnectionError("Connection refused")
        return _fail


# ---------------------------------------------------------------------------
# RED-1 — the original traceback: auth failure => topology verdict (today)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auth_failure_is_never_a_topology_verdict(tmp_path, monkeypatch, caplog):
    """THE INCIDENT: a Redis that fails auth must refuse with a typed
    reachability/credentials error carrying the ST-TOPOLOGY-1 tokens — never
    a ClusterTopologyError blaming Redis Cluster."""
    from fastapi.testclient import TestClient
    from ab0t_quota import setup_quota
    from ab0t_quota.topology import ClusterTopologyError

    monkeypatch.delenv("AB0T_QUOTA_REDIS_CLUSTER_CONFIRMED_DISABLED", raising=False)
    _write_config(tmp_path, monkeypatch, MINIMAL_CONFIG)
    app = FastAPI()
    with patch("redis.asyncio.Redis.from_url",
               side_effect=lambda *a, **kw: AuthFailRedis()):
        setup_quota(app, enable_paid=False)
        with caplog.at_level(logging.ERROR):
            with pytest.raises(Exception) as ei:
                with TestClient(app):
                    pass
    err = ei.value
    # unwrap possible ExceptionGroup/starlette wrapping to the root cause
    while getattr(err, "__cause__", None) is not None or \
            (hasattr(err, "exceptions") and err.exceptions):
        err = err.__cause__ or err.exceptions[0]
    assert not isinstance(err, ClusterTopologyError), \
        f"THE ORIGINAL MISDIAGNOSIS: auth failure produced a topology verdict: {err}"
    msg = str(err)
    for tok in ("credential", "NOT a topology verdict", "never ran"):
        assert tok in msg, f"ST-TOPOLOGY-1 token {tok!r} missing from: {msg[:300]}"
    # T2: the misdiagnosis vocabulary must be ABSENT from the failure output
    for banned in ("CROSSSLOT", "redis_cluster_confirmed_disabled"):
        assert banned not in msg, \
            f"reachability refusal still walks the operator to {banned!r}: {msg[:300]}"


@pytest.mark.asyncio
async def test_capability_written_before_refusal_and_fails_health(tmp_path, monkeypatch):
    """T2: `redis_reachable` is recorded BEFORE the refusal, and quota_health
    degrades on it."""
    from fastapi.testclient import TestClient
    from ab0t_quota import setup_quota
    from ab0t_quota.setup import quota_health

    _write_config(tmp_path, monkeypatch, MINIMAL_CONFIG)
    app = FastAPI()
    with patch("redis.asyncio.Redis.from_url",
               side_effect=lambda *a, **kw: AuthFailRedis()):
        setup_quota(app, enable_paid=False)
        with pytest.raises(Exception):
            with TestClient(app):
                pass
    caps = dict(getattr(app.state, "quota_capabilities", {}) or {})
    assert "redis_reachable" in caps, "capability must be written before the refusal"
    assert caps["redis_reachable"].startswith("PROBE FAILED"), caps["redis_reachable"]
    h = quota_health(app)
    assert "redis_reachable" in h.get("degraded", []), h


# ---------------------------------------------------------------------------
# T1 — the classifier
# ---------------------------------------------------------------------------

class TestT1Classifier:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("exc,kind", [
        (rex.AuthenticationError("WRONGPASS invalid username-password pair"), "auth"),
        (rex.NoPermissionError("NOPERM this user has no permissions to run the 'ping' command"), "acl"),
        (rex.ConnectionError("Connection refused"), "unreachable"),
        (rex.TimeoutError("Timeout connecting to server"), "unreachable"),
        (OSError("No route to host"), "unreachable"),
        (RuntimeError("some other explosion"), "error"),
    ])
    async def test_each_exception_class_classifies(self, exc, kind):
        from ab0t_quota.redis_preflight import check_redis_reachable

        class R:
            async def ping(self):
                raise exc

        ok, got_kind, detail = await check_redis_reachable(R(), timeout=1)
        assert ok is False and got_kind == kind, (got_kind, detail)

    @pytest.mark.asyncio
    async def test_reachable_redis_is_ok(self):
        from ab0t_quota.redis_preflight import check_redis_reachable

        class R:
            async def ping(self):
                return True

        ok, kind, detail = await check_redis_reachable(R(), timeout=1)
        assert ok is True and kind == ""

    def test_error_names_source_and_the_never_ran_contract(self):
        from ab0t_quota.redis_preflight import reachability_error
        e = reachability_error("auth", "WRONGPASS", url_display="redis://h:6379/0",
                               source="config:storage.redis_url")
        msg = str(e)
        assert "config:storage.redis_url" in msg, "must say WHICH source declared it"
        for tok in ("credential", "NOT a topology verdict", "never ran"):
            assert tok in msg


# ---------------------------------------------------------------------------
# T3 — assertions cannot mask an unreachable Redis
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_all_assertion_flags_cannot_mask_unreachable(tmp_path, monkeypatch):
    """The 'safety rails off' cascade: every *_confirmed_* flag set + PING
    failing => STILL refuses, with the reachability error."""
    from fastapi.testclient import TestClient
    from ab0t_quota import setup_quota
    from ab0t_quota.redis_preflight import RedisUnreachableError

    cfg = json.loads(json.dumps(MINIMAL_CONFIG))
    cfg["storage"].update({
        "redis_cluster_confirmed_disabled": True,
        "redis_durability_confirmed": True,
        "redis_scripting_confirmed": True,
        "ddb_pitr_confirmed": True,
    })
    _write_config(tmp_path, monkeypatch, cfg)
    app = FastAPI()
    with patch("redis.asyncio.Redis.from_url",
               side_effect=lambda *a, **kw: ConnFailRedis()):
        setup_quota(app, enable_paid=False)
        with pytest.raises(Exception) as ei:
            with TestClient(app):
                pass
    err = ei.value
    while getattr(err, "__cause__", None) is not None or \
            (hasattr(err, "exceptions") and err.exceptions):
        err = err.__cause__ or err.exceptions[0]
    assert isinstance(err, RedisUnreachableError), \
        f"assertions masked an unreachable Redis: {type(err).__name__}: {err}"


# ---------------------------------------------------------------------------
# T4 — the data-plane CROSSSLOT probe
# ---------------------------------------------------------------------------

def _crc16_xmodem(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


class TestT4DataPlaneProbe:
    def test_probe_keys_hash_to_different_slots(self):
        """Asserted, not assumed: the two probe keys' hash TAGS land on
        different cluster slots, so a cluster MUST answer CROSSSLOT."""
        from ab0t_quota.topology import PROBE_KEY_A, PROBE_KEY_B
        tags = []
        for key in (PROBE_KEY_A, PROBE_KEY_B):
            assert "{" in key and "}" in key, f"probe key {key!r} carries no hash tag"
            tags.append(key[key.index("{") + 1:key.index("}")])
        slots = [_crc16_xmodem(t.encode()) % 16384 for t in tags]
        assert slots[0] != slots[1], f"probe keys hash to the SAME slot {slots}"
        assert all(k.startswith("quota:") for k in (PROBE_KEY_A, PROBE_KEY_B)), \
            "probe keys must stay inside the library's own keyspace (ACL ~quota:*)"

    @pytest.mark.asyncio
    async def test_crossslot_is_definitive_cluster_with_zero_privileges(self):
        """A CROSSSLOT answer to a data-plane EXISTS is a definitive cluster
        verdict — no INFO, no CLUSTER INFO, and no assertion overrides it."""
        from ab0t_quota.topology import CLUSTER, check_redis_cluster_topology

        class ClusterRedis:
            admin_called = False

            async def exists(self, *keys):
                # Gate-D F-1: the REAL post-parse exception — redis-py maps the
                # -CROSSSLOT prefix to ClusterCrossSlotError and STRIPS it, so
                # a hand-raised "CROSSSLOT ..." string here was a false green
                # (the shipped client never produces it).
                raise rex.ClusterCrossSlotError(
                    "Keys in request don't hash to the same slot")

            async def info(self, *a, **kw):
                self.admin_called = True
                raise rex.NoPermissionError("NOPERM")

            async def execute_command(self, *a, **kw):
                self.admin_called = True
                raise rex.NoPermissionError("NOPERM")

        r = ClusterRedis()
        topo, detail = await check_redis_cluster_topology(r, confirmed_disabled=True)
        assert topo == CLUSTER, (topo, detail)
        assert r.admin_called is False, \
            "the definitive cluster verdict must not need admin probes"

    @pytest.mark.asyncio
    async def test_dataplane_success_is_not_single_node_proof(self):
        """Go got this right — do not overclaim: EXISTS succeeding (a splitting
        proxy can mask a cluster) still defers to the control plane / the
        operator assertion. No assertion + no INFO => UNKNOWN."""
        from ab0t_quota.topology import UNKNOWN, check_redis_cluster_topology

        class TrimmedRedis:
            async def exists(self, *keys):
                return 0

            async def info(self, *a, **kw):
                raise rex.NoPermissionError("NOPERM")

            async def execute_command(self, *a, **kw):
                raise rex.NoPermissionError("NOPERM")

        topo, detail = await check_redis_cluster_topology(
            TrimmedRedis(), confirmed_disabled=False)
        assert topo == UNKNOWN, (topo, detail)

    @pytest.mark.asyncio
    async def test_least_privilege_acl_user_with_assertion_is_single_node(self):
        """The requirements.md promise made true: under the minimal ACL
        (+@read +@write +@scripting +ping — INFO and CLUSTER both NOPERM) the
        data-plane probe runs, and the on-the-record assertion completes a
        single-node verdict. No admin privilege is ever required."""
        from ab0t_quota.topology import SINGLE_NODE, check_redis_cluster_topology

        class LeastPrivRedis:
            async def exists(self, *keys):
                return 0

            async def info(self, *a, **kw):
                raise rex.NoPermissionError("NOPERM this user has no permissions")

            async def execute_command(self, *a, **kw):
                raise rex.NoPermissionError("NOPERM this user has no permissions")

        topo, detail = await check_redis_cluster_topology(
            LeastPrivRedis(), confirmed_disabled=True)
        assert topo.startswith(SINGLE_NODE), (topo, detail)

    @pytest.mark.asyncio
    async def test_auth_and_connection_probe_errors_are_probe_failed(self):
        from ab0t_quota.topology import PROBE_FAILED, check_redis_cluster_topology
        for fake in (AuthFailRedis(), ConnFailRedis()):
            topo, detail = await check_redis_cluster_topology(
                fake, confirmed_disabled=True)  # assertion must not mask it
            assert topo == PROBE_FAILED, (topo, detail)

    @pytest.mark.asyncio
    async def test_topology_error_type_impossible_from_probe_failure(self):
        """The old failure mode made IMPOSSIBLE: the topology refusal builder
        maps PROBE_FAILED to the typed reachability error — a
        ClusterTopologyError can no longer be constructed from an auth or
        connection failure path."""
        from ab0t_quota.redis_preflight import RedisUnreachableError
        from ab0t_quota.topology import (
            PROBE_FAILED, ClusterTopologyError, check_redis_cluster_topology,
            topology_error,
        )
        for fake in (AuthFailRedis(), ConnFailRedis()):
            topo, detail = await check_redis_cluster_topology(fake,
                                                              confirmed_disabled=False)
            assert topo == PROBE_FAILED
            err = topology_error(topo, detail)
            assert isinstance(err, RedisUnreachableError), type(err).__name__
            assert not isinstance(err, ClusterTopologyError), \
                "auth/connection failure still constructs a topology verdict"


# ---------------------------------------------------------------------------
# Gate-D F-1 — the probe classified through the REAL client, over a real socket
# ---------------------------------------------------------------------------

import asyncio as _asyncio


async def _resp_server(reply_for_exists: bytes):
    """Minimal loopback RESP server: answers EXISTS with `reply_for_exists`,
    +OK to everything else. Returns (host, port, server)."""

    async def _read_command(reader):
        head = await reader.readline()
        if not head or not head.startswith(b"*"):
            return None
        n = int(head[1:].strip())
        parts = []
        for _ in range(n):
            lenline = await reader.readline()          # $<len>
            size = int(lenline[1:].strip())
            data = await reader.readexactly(size + 2)  # payload + \r\n
            parts.append(data[:-2])
        return parts

    async def handle(reader, writer):
        try:
            while True:
                cmd = await _read_command(reader)
                if cmd is None:
                    break
                name = cmd[0].upper()
                writer.write(reply_for_exists if name == b"EXISTS" else b"+OK\r\n")
                await writer.drain()
        except (ConnectionError, _asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    server = await _asyncio.start_server(handle, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    return host, port, server


@pytest.mark.asyncio
async def test_crossslot_detected_through_the_real_client_over_a_real_socket():
    """F-1's bar: a loopback server answering `-CROSSSLOT …`, driven by the
    REAL redis.asyncio client (which strips the prefix into
    ClusterCrossSlotError), must yield the definitive CLUSTER verdict — with
    the assertion set, which must not mask it."""
    import redis.asyncio as aredis
    from ab0t_quota.topology import CLUSTER, check_redis_cluster_topology

    host, port, server = await _resp_server(
        b"-CROSSSLOT Keys in request don't hash to the same slot\r\n")
    try:
        client = aredis.Redis(host=host, port=port, decode_responses=False,
                              socket_connect_timeout=2, socket_timeout=2)
        topo, detail = await check_redis_cluster_topology(
            client, confirmed_disabled=True)
        await client.aclose()
    finally:
        server.close()
        await server.wait_closed()
    assert topo == CLUSTER, \
        (f"a REAL cluster's CROSSSLOT through the shipped client was verdicted "
         f"{topo!r} ({detail}) — the under-refusal defect")


@pytest.mark.asyncio
async def test_wrongpass_through_the_real_client_is_probe_failed():
    """Companion: a real `-WRONGPASS` through the shipped client classifies as
    a failed probe, never a topology verdict."""
    import redis.asyncio as aredis
    from ab0t_quota.topology import PROBE_FAILED, check_redis_cluster_topology

    host, port, server = await _resp_server(
        b"-WRONGPASS invalid username-password pair or user is disabled.\r\n")
    try:
        client = aredis.Redis(host=host, port=port, decode_responses=False,
                              socket_connect_timeout=2, socket_timeout=2)
        topo, detail = await check_redis_cluster_topology(
            client, confirmed_disabled=True)
        await client.aclose()
    finally:
        server.close()
        await server.wait_closed()
    assert topo == PROBE_FAILED, (topo, detail)


@pytest.mark.asyncio
async def test_compil_token_survives_the_real_clients_prefix_stripping():
    """F-1 audit: the D-73 'compil' discriminator keys on a MID-MESSAGE token
    — redis-py strips only the leading error-class word ('ERR'), so the token
    survives the real client's parse (unlike the CROSSSLOT prefix). Proven
    through the real client, not asserted from memory."""
    import redis.asyncio as aredis
    from ab0t_quota.redis_preflight import check_redis_script_capability

    host, port, server = await _resp_server_cmd(
        {b"SCRIPT": b"-ERR Error compiling script (new function): user_script:1\r\n"})
    try:
        client = aredis.Redis(host=host, port=port, decode_responses=False,
                              socket_connect_timeout=2, socket_timeout=2)
        ok, detail = await check_redis_script_capability(client, confirmed=True)
        await client.aclose()
    finally:
        server.close()
        await server.wait_closed()
    assert ok is False, \
        ("the observed compile rejection was masked — the 'compil' token did not "
         f"survive the client's parse: {detail}")


async def _resp_server_cmd(replies: dict):
    """RESP server with per-command replies; +OK otherwise."""

    async def _read_command(reader):
        head = await reader.readline()
        if not head or not head.startswith(b"*"):
            return None
        n = int(head[1:].strip())
        parts = []
        for _ in range(n):
            lenline = await reader.readline()
            size = int(lenline[1:].strip())
            data = await reader.readexactly(size + 2)
            parts.append(data[:-2])
        return parts

    async def handle(reader, writer):
        try:
            while True:
                cmd = await _read_command(reader)
                if cmd is None:
                    break
                writer.write(replies.get(cmd[0].upper(), b"+OK\r\n"))
                await writer.drain()
        except (ConnectionError, _asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    server = await _asyncio.start_server(handle, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    return host, port, server


# ---------------------------------------------------------------------------
# T5 — the D-72/73/74 probes classify
# ---------------------------------------------------------------------------

class TestT5ProbeClassification:
    @pytest.mark.asyncio
    async def test_read_redis_policy_auth_reraises_noperm_is_absent(self):
        from ab0t_quota.redis_preflight import (
            RedisUnreachableError, read_redis_policy,
        )
        with pytest.raises(RedisUnreachableError):
            await read_redis_policy(AuthFailRedis())

        class NoPermRedis:
            async def config_get(self, key):
                raise rex.NoPermissionError("NOPERM")

        policy, ao, save, unavailable, err = await read_redis_policy(NoPermRedis())
        assert unavailable is True, "NOPERM is the genuine absent signal (assertions apply)"

    @pytest.mark.asyncio
    async def test_script_probe_auth_reraises_and_hatch_cannot_mask(self):
        """An auth failure during SCRIPT LOAD must re-raise — and in
        particular redis_scripting_confirmed must NOT convert it into a pass
        (the T-9 hatch is for an unrunnable PROBE, never an unreachable
        server)."""
        from ab0t_quota.redis_preflight import (
            RedisUnreachableError, check_redis_script_capability,
        )
        with pytest.raises(RedisUnreachableError):
            await check_redis_script_capability(AuthFailRedis(), confirmed=True)

    @pytest.mark.asyncio
    async def test_version_probe_auth_reraises_noperm_stays_unknown(self):
        from ab0t_quota.redis_preflight import (
            RedisUnreachableError, check_redis_version,
        )
        with pytest.raises(RedisUnreachableError):
            await check_redis_version(ConnFailRedis())

        class NoPermRedis:
            async def info(self, *a, **kw):
                raise rex.NoPermissionError("NOPERM")

        status, detail = await check_redis_version(NoPermRedis())
        assert status == "unknown"

    @pytest.mark.asyncio
    async def test_fact_probes_auth_reraise(self):
        from ab0t_quota.redis_preflight import (
            RedisUnreachableError, check_evicted_keys, check_memory_headroom,
            check_persist_facts,
        )
        for probe in (check_memory_headroom, check_evicted_keys, check_persist_facts):
            with pytest.raises(RedisUnreachableError):
                await probe(AuthFailRedis())

    @pytest.mark.asyncio
    async def test_verify_redis_invariants_reports_probe_failed_never_verdicts(self):
        """D-75's runtime loop: an auth failure mid-run degrades LOUDLY as a
        reachability finding — never as topology/eviction/scripting verdicts,
        and never an unhandled crash."""
        from ab0t_quota.redis_preflight import verify_redis_invariants
        caps, unsafe = await verify_redis_invariants(AuthFailRedis(), {})
        assert any(k == "redis_reachable" for k, _ in unsafe), unsafe
        assert str(caps.get("redis_reachable", "")).startswith("PROBE FAILED")
        judged = {k for k, _ in unsafe} - {"redis_reachable"}
        assert judged == set(), \
            f"an unreachable Redis received infrastructure verdicts: {judged}"
