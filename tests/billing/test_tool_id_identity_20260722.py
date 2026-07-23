"""Ticket 20260722_end_customer_experience_defects — TICKET_config_is_king.md
§5c "Other Python leaks", row 2 (board row CK-B2).

THE DEFECT
----------
`BillingServiceClient.record_resource_usage` carried

    tool_id: str = "sandbox-platform"

as a **default argument** — ONE consumer's name, baked into a shared library's
public metering helper. Every OTHER consumer that called it without passing
`tool_id` had its metering rows attributed to sandbox-platform, silently and
permanently. The one in-repo call site (sandbox-platform's own billing_helpers)
never passes it, which is exactly why nobody noticed.

THE CONTRACT (config is king — identity comes from the caller, never a literal)
------------------------------------------------------------------------------
`tool_id` resolves, in order:
  1. the explicit `tool_id=` argument;
  2. `BillingServiceClient(service_name=...)` — the consumer's own mesh
     identity, threaded in at construction;
  3. the `AB0T_SERVICE_NAME` env var — the same ambient identity knob
     `setup.py::_resolve_service_name` already consults;
  4. **loud refusal** — a ValueError naming all three ways to supply it,
     preceded by an ERROR log so a caller that swallows exceptions (the real
     one does) still leaves evidence.

There is no step that invents a name. A metering row with no identity is
dropped loudly; it is never filed under someone else's.
"""

import logging

import pytest

from ab0t_quota.billing import BillingServiceClient, RecordUsageRequest

ENV_KEY = "AB0T_SERVICE_NAME"


def _client(**kw):
    # base_url/api_key are unused — record_usage is captured, never sent.
    return BillingServiceClient(base_url="http://billing.invalid", api_key="x", **kw)


def _capture(client):
    captured = {}

    async def _fake_record_usage(payload):
        assert isinstance(payload, RecordUsageRequest), type(payload)
        captured["body"] = payload.model_dump(exclude_none=True)
        return {}

    client.record_usage = _fake_record_usage  # type: ignore[assignment]
    return captured


@pytest.fixture(autouse=True)
def _no_ambient_identity(monkeypatch):
    """Every test states its own identity source — no leakage from the box."""
    monkeypatch.delenv(ENV_KEY, raising=False)


# ---------------------------------------------------------------------------
# RED — the leak itself
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_RED_no_identity_anywhere_refuses_instead_of_naming_a_consumer():
    """A consumer that supplies no identity must NOT be filed as
    sandbox-platform. Pre-fix this silently sent tool_id="sandbox-platform"."""
    client = _client()
    cap = _capture(client)

    with pytest.raises(ValueError) as exc:
        await client.record_resource_usage(
            org_id="org-1", user_id="user-1", session_id="s-1",
        )

    assert "body" not in cap, (
        "a metering row was SENT with no caller identity — it carried "
        f"{cap.get('body', {}).get('tool_id')!r}, which is a name this caller "
        "never chose"
    )
    msg = str(exc.value)
    for expected in ("tool_id", "service_name", ENV_KEY):
        assert expected in msg, (
            f"the refusal must name every way to supply identity; {expected!r} "
            f"missing from: {msg}"
        )


@pytest.mark.asyncio
async def test_RED_no_consumer_name_survives_as_a_default_in_the_signature():
    """Own assertion, structural: the signature itself must not carry any
    consumer's name as a default value."""
    import inspect

    sig = inspect.signature(BillingServiceClient.record_resource_usage)
    tool_id = sig.parameters["tool_id"]
    assert tool_id.default is None, (
        "config is king: a shared library may not name one consumer in a "
        f"default argument. Got tool_id={tool_id.default!r}"
    )


@pytest.mark.asyncio
async def test_RED_refusal_is_logged_even_when_the_caller_swallows(caplog):
    """The real call site wraps this in `except Exception: logger.warning(...)`.
    The library must leave its OWN evidence before the exception is swallowed,
    or a misconfigured consumer loses all metering in silence."""
    client = _client()
    _capture(client)

    with caplog.at_level(logging.ERROR, logger="ab0t_quota.billing"):
        with pytest.raises(ValueError):
            await client.record_resource_usage(
                org_id="org-1", user_id="user-1", session_id="s-1",
            )

    assert any(r.levelno >= logging.ERROR for r in caplog.records), (
        "no ERROR logged — a caller that swallows the exception would see "
        "nothing at all"
    )


# ---------------------------------------------------------------------------
# The resolution ladder, one assertion per rung
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_explicit_tool_id_wins():
    client = _client(service_name="from-ctor")
    cap = _capture(client)
    await client.record_resource_usage(
        org_id="o", user_id="u", session_id="s", tool_id="explicit-arg",
    )
    assert cap["body"]["tool_id"] == "explicit-arg"


@pytest.mark.asyncio
async def test_constructor_service_name_is_used_when_no_argument(monkeypatch):
    monkeypatch.setenv(ENV_KEY, "from-env")
    client = _client(service_name="fintech-ledger")
    cap = _capture(client)
    await client.record_resource_usage(org_id="o", user_id="u", session_id="s")
    assert cap["body"]["tool_id"] == "fintech-ledger", (
        "the constructor identity is more specific than the ambient env var "
        "and must win"
    )


@pytest.mark.asyncio
async def test_env_service_name_is_the_last_resort(monkeypatch):
    monkeypatch.setenv(ENV_KEY, "fintech-ledger")
    client = _client()
    cap = _capture(client)
    await client.record_resource_usage(org_id="o", user_id="u", session_id="s")
    assert cap["body"]["tool_id"] == "fintech-ledger", (
        "AB0T_SERVICE_NAME is the same knob _resolve_service_name already "
        "reads; a consumer that set it has already declared its identity"
    )


@pytest.mark.asyncio
async def test_blank_identity_is_not_an_identity(monkeypatch):
    """Whitespace/empty must refuse, not send a blank tool_id."""
    monkeypatch.setenv(ENV_KEY, "   ")
    client = _client(service_name="  ")
    cap = _capture(client)
    with pytest.raises(ValueError):
        await client.record_resource_usage(org_id="o", user_id="u", session_id="s")
    assert "body" not in cap


# ---------------------------------------------------------------------------
# PERMANENT CONTROL — no consumer name anywhere in the client's defaults.
# ---------------------------------------------------------------------------

def test_CONTROL_no_consumer_name_in_any_client_default():
    """D-CK-5 made testable for this module: scan every default value of every
    public method on both mesh clients for a known consumer name."""
    import inspect
    from ab0t_quota.billing import clients as clients_mod

    forbidden = ("sandbox-platform", "sandbox_platform")
    offenders = []
    for cls_name in ("BillingServiceClient", "PaymentServiceClient"):
        cls = getattr(clients_mod, cls_name)
        for name, fn in vars(cls).items():
            if not callable(fn):
                continue
            try:
                sig = inspect.signature(fn)
            except (TypeError, ValueError):
                continue
            for pname, p in sig.parameters.items():
                if isinstance(p.default, str) and p.default.lower() in forbidden:
                    offenders.append(f"{cls_name}.{name}({pname}={p.default!r})")
    assert not offenders, (
        "a shared library must not name one consumer in a default argument — "
        "every other consumer inherits it silently. Offenders: " + ", ".join(offenders)
    )
