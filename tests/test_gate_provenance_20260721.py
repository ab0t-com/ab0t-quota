"""GATE family — T-8 (AccessDenied is not a missing table) and T-18 (required
GSIs are REQUIRED, not merely present-and-ACTIVE). RED-first, Rule A."""
from __future__ import annotations

import pytest


class _ClientError(Exception):
    """Stand-in shaped like botocore ClientError (type-name matched by code)."""


class AccessDeniedException(Exception):
    pass


class ResourceNotFoundException(Exception):
    pass


class _FakeDDB:
    """Minimal async DDB control-plane fake for verify_ddb_table."""

    def __init__(self, *, describe_exc=None, gsis=(), ttl_attr="ttl", pitr="ENABLED"):
        self.describe_exc = describe_exc
        self.gsis = list(gsis)
        self.ttl_attr = ttl_attr
        self.pitr = pitr

    async def describe_table(self, TableName):
        if self.describe_exc is not None:
            raise self.describe_exc
        return {"Table": {
            "TableStatus": "ACTIVE",
            "GlobalSecondaryIndexes": [
                {"IndexName": n, "IndexStatus": "ACTIVE"} for n in self.gsis],
        }}

    async def describe_time_to_live(self, TableName):
        return {"TimeToLiveDescription": {
            "TimeToLiveStatus": "ENABLED", "AttributeName": self.ttl_attr}}

    async def describe_continuous_backups(self, TableName):
        return {"ContinuousBackupsDescription": {
            "PointInTimeRecoveryDescription": {"PointInTimeRecoveryStatus": self.pitr}}}


# ---------------------------------------------------------------------------
# T-8 — GATE-01's AWS half (`ddb_preflight.py` describe_table fold)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_access_denied_describe_table_is_not_a_missing_table():
    """AccessDenied must stay a REFUSAL (fail-closed, MUST #6) but be reported
    as the permission problem it is — naming dynamodb:DescribeTable — never as
    'table not found or not describable'."""
    from ab0t_quota.ddb_preflight import verify_ddb_table
    cap, fatal, _ = await verify_ddb_table(
        _FakeDDB(describe_exc=AccessDeniedException("denied")), "t", ttl_attribute="ttl")
    assert fatal is not None, "AccessDenied must remain fail-CLOSED (a refusal)"
    assert "AccessDenied" in fatal or "dynamodb:DescribeTable" in fatal, \
        f"permission failure not classified: {fatal!r}"
    assert "not found" not in fatal, \
        f"AccessDenied is still misreported as a missing table: {fatal!r}"


@pytest.mark.asyncio
async def test_resource_not_found_still_reports_missing_table():
    """Negative control: a genuinely missing table keeps its verdict."""
    from ab0t_quota.ddb_preflight import verify_ddb_table
    cap, fatal, _ = await verify_ddb_table(
        _FakeDDB(describe_exc=ResourceNotFoundException("nope")), "t", ttl_attribute="ttl")
    assert fatal is not None and "not found" in fatal


# ---------------------------------------------------------------------------
# T-18 — ENV-16: required GSIs must be REQUIRED
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preflight_rejects_table_missing_required_gsi():
    """A ZERO-GSI table must FAIL when the caller requires gsi1/gsi2 — today
    the loop only checks that GSIs which EXIST are ACTIVE (vacuous pass)."""
    from ab0t_quota.ddb_preflight import verify_ddb_table
    try:
        cap, fatal, _ = await verify_ddb_table(
            _FakeDDB(gsis=()), "t", ttl_attribute="ttl",
            required_gsis=("gsi1", "gsi2"))
    except TypeError:
        pytest.fail("verify_ddb_table cannot express REQUIRED GSIs (ENV-16) — "
                    "a zero-GSI table passes trivially")
    assert fatal is not None and "gsi1" in fatal and "gsi2" in fatal

    # control: a table WITH the required GSIs passes
    cap, fatal, _ = await verify_ddb_table(
        _FakeDDB(gsis=("gsi1", "gsi2")), "t", ttl_attribute="ttl",
        required_gsis=("gsi1", "gsi2"))
    assert fatal is None, f"complete table wrongly refused: {fatal!r}"


class _FakeLedgerDDB:
    """Fake for DDBLedgerStore.ensure_table: first describe -> NotFound,
    create recorded, then ACTIVE."""

    def __init__(self):
        self.created_kwargs = None
        self._exists = False
        self.exceptions = None  # forces the type-name fallback path

    async def describe_table(self, TableName):
        if not self._exists:
            raise ResourceNotFoundException(TableName)
        return {"Table": {"TableStatus": "ACTIVE"}}

    async def create_table(self, **kwargs):
        self.created_kwargs = kwargs
        self._exists = True
        return {}

    async def update_time_to_live(self, **kwargs):
        return {}


@pytest.mark.asyncio
async def test_handler_ledger_table_has_required_gsis():
    """ENV-16(a): self-provisioning must create gsi1/gsi2 (with attribute
    definitions) — otherwise query_by_user/query_by_status ValidationException
    at runtime on every auto-created table."""
    from ab0t_quota.handler_ledger import DDBLedgerStore
    fake = _FakeLedgerDDB()
    store = DDBLedgerStore(fake, table_name="t")
    await store.ensure_table()
    assert fake.created_kwargs is not None, "table was not created"
    gsis = {g["IndexName"]: g for g in fake.created_kwargs.get("GlobalSecondaryIndexes", [])}
    assert set(gsis) >= {"gsi1", "gsi2"}, \
        f"create_table missing required GSIs: got {sorted(gsis)}"
    attrs = {a["AttributeName"] for a in fake.created_kwargs.get("AttributeDefinitions", [])}
    assert {"gsi1_pk", "gsi1_sk", "gsi2_pk", "gsi2_sk"} <= attrs, \
        f"GSI key attributes not defined: {sorted(attrs)}"
