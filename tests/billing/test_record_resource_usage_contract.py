"""Contract test for ab0t-quota's typed metering helper (TASK 1).

Ticket 20260615_inter_service_contract_drift — WORKFLOW_FINDINGS sections 2, 3, 7.

Pins the metering rule: a create-time infra usage row MUST carry cost="0",
platform_fee="0" and reservation_id (else billing fabricates MINIMUM_USAGE_COST
or double-charges). resource_type is OPEN; action + descriptive extras live in
the metadata channel; session_id is the resource id, never a token; request_id
is present, unique, and matches sbx-<12 hex>. CHANGE 1: the metering helper must
emit cost=="0"/platform_fee=="0" EVEN when the caller omits them, so a metering
caller can never enter billing's priced branch.

We capture the wire body by monkeypatching record_usage so no network / billing
service is required.
"""
import re

import pytest

from ab0t_quota.billing import (
    BillingServiceClient,
    RecordUsageRequest,
    UsageMetadata,
)


def _client():
    # base_url/api_key are unused because we never hit the network here.
    return BillingServiceClient(base_url="http://billing.invalid", api_key="x")


async def _capture(client):
    """Replace record_usage with a capture that returns the model_dump body."""
    captured = {}

    async def _fake_record_usage(payload):
        # payload is a RecordUsageRequest (the helper always builds one)
        assert isinstance(payload, RecordUsageRequest), type(payload)
        body = payload.model_dump(exclude_none=True)
        captured["body"] = body
        return {}

    client.record_usage = _fake_record_usage  # type: ignore[assignment]
    return captured


# --- model existence / shape (additive, mirrors billing) --------------------

def test_record_usage_request_model_exists_and_open_resource_type():
    m = RecordUsageRequest(
        org_id="o1", user_id="u1", tool_id="sandbox-platform",
        session_id="sbx-abc", resource_type="fargate",  # OPEN string accepted
        cost="0", platform_fee="0",
    )
    assert m.resource_type == "fargate"
    assert m.metadata == {}


def test_usage_metadata_declares_no_secret_field():
    fields = set(UsageMetadata.model_fields.keys())
    for f in fields:
        low = f.lower()
        assert "token" not in low, f"secret-looking field on UsageMetadata: {f}"
        assert "secret" not in low, f"secret-looking field on UsageMetadata: {f}"


# --- metering helper behaviour ---------------------------------------------

@pytest.mark.asyncio
async def test_record_resource_usage_sends_metering_defaults():
    client = _client()
    cap = await _capture(client)

    await client.record_resource_usage(
        org_id="org-1",
        user_id="user-1",
        resource_type="sandbox",
        session_id="sbx-1234567890ab",
        reservation_id="resv-9",
        metadata={"action": "sandbox_created", "instance_type": "t3.small"},
    )

    body = cap["body"]
    # section 2: the non-negotiable trio
    assert body["cost"] == "0"
    assert body["platform_fee"] == "0"
    assert body["reservation_id"] == "resv-9"
    # section 3: identity fields
    assert body["tool_id"] == "sandbox-platform"
    assert body["org_id"] == "org-1"
    assert body["user_id"] == "user-1"
    assert body["session_id"] == "sbx-1234567890ab"
    assert body["resource_type"] == "sandbox"
    # request_id present + correct shape
    assert re.fullmatch(r"sbx-[0-9a-f]{12}", body["request_id"]), body["request_id"]
    # action + descriptive extras live under metadata, NOT top-level
    assert "action" not in body
    assert body["metadata"]["action"] == "sandbox_created"
    assert body["metadata"]["instance_type"] == "t3.small"


@pytest.mark.asyncio
async def test_record_resource_usage_forces_zero_when_cost_omitted():
    # CHANGE 1: a metering caller that passes NO cost/platform_fee must still
    # emit "0"/"0" (never None -> billing's MINIMUM_USAGE_COST/debit branch).
    client = _client()
    cap = await _capture(client)
    await client.record_resource_usage(
        org_id="o", user_id="u", session_id="sbx-x", reservation_id="resv-1",
        metadata={"action": "sandbox_created"},
    )
    body = cap["body"]
    assert body["cost"] == "0"
    assert body["platform_fee"] == "0"
    assert body["reservation_id"] == "resv-1"


@pytest.mark.asyncio
async def test_request_id_is_unique_across_calls():
    client = _client()
    seen = []

    async def _fake(payload):
        seen.append(payload.model_dump()["request_id"])
        return {}

    client.record_usage = _fake  # type: ignore[assignment]
    await client.record_resource_usage(org_id="o", user_id="u", session_id="s")
    await client.record_resource_usage(org_id="o", user_id="u", session_id="s")
    assert seen[0] != seen[1], "request_id must be unique per call"


@pytest.mark.asyncio
async def test_no_secret_key_in_metadata():
    client = _client()
    cap = await _capture(client)
    await client.record_resource_usage(
        org_id="o", user_id="u", session_id="sbx-x",
        metadata={"action": "sandbox_created", "instance_type": "t3.small"},
    )
    for k in cap["body"]["metadata"].keys():
        low = k.lower()
        assert "token" not in low and "secret" not in low, f"secret in metadata: {k}"


@pytest.mark.asyncio
async def test_explicit_request_id_is_preserved():
    client = _client()
    cap = await _capture(client)
    await client.record_resource_usage(
        org_id="o", user_id="u", session_id="s", request_id="sbx-deadbeef0000",
    )
    assert cap["body"]["request_id"] == "sbx-deadbeef0000"
