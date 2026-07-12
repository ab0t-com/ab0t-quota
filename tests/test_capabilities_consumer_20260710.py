"""D-40 — the Capabilities snapshot must be CONSUMED, not merely emitted.

Eight times in this library a mechanism was mistaken for the guarantee it owed.
Twice, the mechanism was an event that reached nobody. The Capabilities snapshot
was designated (D-38) as "the artifact that ends this pattern" — and the release
gate found it was logged, stashed on `app.state`, and read by NOTHING.

These tests assert at the CONSUMER (the route / the health verdict), never at the
emitter. A test that inspects `app.state.quota_capabilities` directly would prove
the snapshot is built and say nothing about whether anyone can see it — which is
exactly the mistake the shipped code made.

Boundary crossed: the **human**. See DECISIONS.md D-40.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ab0t_quota.setup import (
    _register_capability_routes,
    _MONEY_CRITICAL_CAPS,
    quota_health,
)


def _app_with(caps: dict) -> FastAPI:
    app = FastAPI()
    app.state.quota_capabilities = dict(caps)
    _register_capability_routes(app)
    return app


HEALTHY = {
    "billing": "on (outbox=ddb)",
    "reconciler": "on(provider)",
    "activations": "on",
    "activation_store": "DDB (durable)",
}


def test_capabilities_endpoint_serves_the_snapshot():
    """The snapshot is reachable by a human, not just by a log grep."""
    client = TestClient(_app_with(HEALTHY))
    r = client.get("/quota/capabilities")
    assert r.status_code == 200
    assert r.json()["activation_store"] == "DDB (durable)"
    # The vestigial `unknown(owned:…)` placeholder must never ship.
    assert not any("unknown(owned:" in str(v) for v in r.json().values())


def test_health_is_ok_when_money_critical_caps_are_on():
    client = TestClient(_app_with(HEALTHY))
    r = client.get("/quota/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "degraded": []}


@pytest.mark.parametrize("cap", _MONEY_CRITICAL_CAPS)
def test_health_DEGRADES_and_503s_when_a_money_capability_is_off(cap):
    """A health check that is always green is an event with no sink and a 200
    attached. Billing OFF means usage is silently un-billed (QB-01). Reconciler
    OFF means an orphaned over-count never heals (D-28). Both must fail a probe.
    """
    caps = dict(HEALTHY)
    caps[cap] = "OFF — no durable outbox" if cap == "billing" else "OFF — activation store not durable"

    client = TestClient(_app_with(caps))
    r = client.get("/quota/health")

    assert r.status_code == 503, f"{cap} OFF must fail the probe, not report 200"
    body = r.json()
    assert body["status"] == "degraded"
    assert body["degraded"] == [cap]


def test_health_reports_only_capability_KEYS_not_values():
    """House rule: no dynamic strings leak to a client. Diagnostics live on
    /quota/capabilities; the probe body carries a fixed vocabulary."""
    caps = dict(HEALTHY, billing="OFF — no durable outbox (secret-ish detail)")
    body = TestClient(_app_with(caps)).get("/quota/health").json()
    assert body["degraded"] == ["billing"]
    assert "secret-ish detail" not in str(body)


def test_reconciler_off_by_config_also_degrades():
    """`off(config)` is not a benign state: D-28's orphaned over-count heals never."""
    assert quota_health(_app_with(dict(HEALTHY, reconciler="off(config)")))["status"] == "degraded"


def test_negative_control_the_probe_can_actually_fail():
    """Prove the guard can go red before trusting that it goes green.

    If `_MONEY_CRITICAL_CAPS` were ever emptied, every test above would still pass
    while the probe became permanently cheerful. Assert the vocabulary is non-empty
    and that an unknown capability does NOT degrade (so the list is load-bearing).
    """
    assert _MONEY_CRITICAL_CAPS, "an empty money-critical set makes /quota/health a no-op"
    assert quota_health(_app_with(dict(HEALTHY, ledger_store="off-but-not-money-critical")))["status"] == "ok"


# ---------------------------------------------------------------------------
# W-T2 extension (ticket 20260709) — the snapshot must fail CLOSED when it does
# not clearly say a money capability is ON. D-40 row #8 (the human): a health
# check that is only red on an explicit "off" is still cheerfully green when the
# snapshot is MISSING, PARTIAL, or `unknown` — the exact states a service that
# never finished wiring integrity would present. Fail-direction law (D-31): the
# absence of a positive signal is not health.
# ---------------------------------------------------------------------------

def test_missing_snapshot_fails_closed_not_open():
    """A service that never ran setup_quota has NO capabilities snapshot. A
    money-aware probe must NOT report a cheerful 200 over it — every money-critical
    capability is absent, therefore not-on, therefore degraded."""
    app = FastAPI()                      # deliberately NO app.state.quota_capabilities
    _register_capability_routes(app)

    h = quota_health(app)
    assert h["status"] == "degraded", "a missing snapshot must fail CLOSED (D-31)"
    assert set(h["degraded"]) == set(_MONEY_CRITICAL_CAPS)

    r = TestClient(app).get("/quota/health")
    assert r.status_code == 503


def test_partial_snapshot_degrades_the_absent_money_cap():
    """billing is present and ON, but the reconciler capability is ABSENT (a
    partial snapshot). The absent money cap must degrade — not be treated as ok
    just because its key is missing."""
    caps = {"billing": "ON (outbox=ddb)"}       # reconciler intentionally omitted
    h = quota_health(_app_with(caps))
    assert h["status"] == "degraded"
    assert h["degraded"] == ["reconciler"]


@pytest.mark.parametrize("cap", _MONEY_CRITICAL_CAPS)
def test_unknown_value_degrades(cap):
    """`unknown` is the value the snapshot uses for a seam it could not resolve. It
    must degrade — reporting OK over `billing=unknown` is exactly the false-green
    D-40 names. (The shipped predicate only caught `off`.)"""
    caps = dict(HEALTHY)
    caps[cap] = "unknown"
    h = quota_health(_app_with(caps))
    assert h["status"] == "degraded"
    assert h["degraded"] == [cap]


def test_only_an_explicit_on_is_healthy_others_still_ok_control():
    """[control] The tightened predicate must not over-reach: a money cap that IS
    on ("on(ledger)") stays healthy even alongside a non-money cap set to junk."""
    caps = dict(HEALTHY, reconciler="on(ledger)", ledger_store="whatever")
    assert quota_health(_app_with(caps))["status"] == "ok"


# D-49 — "the absence of a positive signal is not health." The predicate must
# degrade unless the value AFFIRMATIVELY asserts on — not merely "isn't off". An
# unparseable/transitional value (`starting`, `degraded`, a typo) is not-proven-on
# and must fail closed. This is the case a "degrade only on off/unknown" predicate
# still lets through — the reason D-49 inverts it rather than just widening it.

@pytest.mark.parametrize("cap", _MONEY_CRITICAL_CAPS)
@pytest.mark.parametrize("value", ["starting", "degraded", "true", "1", "???"])
def test_unparseable_value_degrades(cap, value):
    caps = dict(HEALTHY)
    caps[cap] = value
    h = quota_health(_app_with(caps))
    assert h["status"] == "degraded", f"{cap}={value!r} is not 'affirmatively on'"
    assert h["degraded"] == [cap]


@pytest.mark.parametrize("healthy_value", [
    "ON (outbox=ddb)",   # billing, shipped (setup.py:1219) — uppercase, space+paren
    "on(provider)",      # reconciler, shipped (setup.py:506)
    "on(ledger)",        # reconciler, shipped (setup.py:507)
    "on",                # reconciler, shipped (setup.py:1023)
])
def test_every_real_shipped_on_value_reads_healthy(healthy_value):
    """A false 503 is not free — it trains operators to ignore the probe. Every
    ACTUAL on-value the code ships (varied case/shape) must read healthy under the
    normalized-prefix match. Sourced from setup.py; see the artifact."""
    for cap in _MONEY_CRITICAL_CAPS:
        caps = dict(HEALTHY)
        caps[cap] = healthy_value
        assert quota_health(_app_with(caps))["status"] == "ok", (
            f"a real shipped on-value {healthy_value!r} was falsely degraded")
