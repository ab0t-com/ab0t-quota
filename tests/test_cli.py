"""Smoke tests for the ab0t_quota CLI.

Tests are sync (CLI uses asyncio.run internally; pytest-asyncio would conflict).
Seed the InMemoryLedgerStore by directly injecting LedgerRow into its dict.
"""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone

import pytest

from ab0t_quota import __main__ as cli
from ab0t_quota.handler_ledger import (
    InMemoryLedgerStore, LedgerStatus, LedgerRow,
)


def _seed(store: InMemoryLedgerStore, *, handler: str, event_id: str,
          status: LedgerStatus, user_id: str = None, payload: dict = None) -> None:
    """Inject a finished row directly into the store's dict (sync test helper)."""
    row = LedgerRow(
        handler_name=handler, event_id=event_id, event_type="x",
        status=status, user_id=user_id, attempts=1,
        attempted_at=datetime.now(timezone.utc).isoformat(),
        completed_at=datetime.now(timezone.utc).isoformat(),
        event_payload=payload or {},
    )
    store._rows[(handler, event_id)] = row


@pytest.fixture
def fake_store(monkeypatch):
    store = InMemoryLedgerStore()
    monkeypatch.setattr(cli, "_build_store_from_env", lambda: store)
    return store


class TestEvents:
    def test_events_by_user(self, fake_store):
        _seed(fake_store, handler="h", event_id="e1", status=LedgerStatus.SUCCESS, user_id="alice")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["events", "--user-id", "alice"])
        assert rc == 0
        out = buf.getvalue()
        assert "alice" in out and "success" in out and "e1" in out

    def test_events_requires_filter(self, fake_store):
        buf_err = io.StringIO()
        with redirect_stderr(buf_err):
            rc = cli.main(["events"])
        assert rc == 2

    def test_events_by_status(self, fake_store):
        _seed(fake_store, handler="h", event_id="ef", status=LedgerStatus.FAILED, user_id="alice")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["events", "--status", "failed"])
        assert rc == 0
        assert "failed" in buf.getvalue() and "ef" in buf.getvalue()

    def test_events_json_format(self, fake_store):
        _seed(fake_store, handler="h", event_id="ej", status=LedgerStatus.SUCCESS, user_id="alice")
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["events", "--user-id", "alice", "--format", "json"])
        parsed = json.loads(buf.getvalue())
        assert isinstance(parsed, list) and parsed[0]["status"] == "success"

    def test_events_empty_table(self, fake_store):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["events", "--user-id", "nobody"])
        assert rc == 0
        assert "no rows" in buf.getvalue()


class TestParseSince:
    def test_hours(self):
        import time
        assert abs((time.time() - cli._parse_since("2h")) - 7200) < 5

    def test_days(self):
        import time
        assert abs((time.time() - cli._parse_since("3d")) - 259200) < 5

    def test_minutes(self):
        import time
        assert abs((time.time() - cli._parse_since("30m")) - 1800) < 5

    def test_iso_passthrough(self):
        iso = "2026-01-01T00:00:00+00:00"
        t = cli._parse_since(iso)
        expected = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
        assert abs(t - expected) < 1


class TestDeleteUser:
    def test_requires_confirm(self, fake_store):
        buf_err = io.StringIO()
        with redirect_stderr(buf_err):
            rc = cli.main(["delete-user", "--user-id", "u1"])
        assert rc == 2

    def test_with_confirm_deletes(self, fake_store):
        _seed(fake_store, handler="h", event_id="dx", status=LedgerStatus.SUCCESS, user_id="gdpr_user")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["delete-user", "--user-id", "gdpr_user", "--confirm"])
        assert rc == 0
        assert "deleted 1" in buf.getvalue()


class TestReplayValidation:
    def test_requires_env_or_arg(self, fake_store, monkeypatch):
        monkeypatch.delenv("AB0T_AUTH_WEBHOOK_PUBLIC_URL", raising=False)
        monkeypatch.delenv("AB0T_AUTH_WEBHOOK_SECRET", raising=False)
        buf_err = io.StringIO()
        with redirect_stderr(buf_err):
            rc = cli.main(["replay", "--handler", "h", "--event-id", "e1"])
        assert rc == 2
        assert "AB0T_AUTH_WEBHOOK" in buf_err.getvalue()

    def test_missing_row_errors(self, fake_store, monkeypatch):
        monkeypatch.setenv("AB0T_AUTH_WEBHOOK_PUBLIC_URL", "https://app.test")
        monkeypatch.setenv("AB0T_AUTH_WEBHOOK_SECRET", "secret")
        buf_err = io.StringIO()
        with redirect_stderr(buf_err):
            rc = cli.main(["replay", "--handler", "nope", "--event-id", "no_such"])
        assert rc == 1
        assert "no ledger row" in buf_err.getvalue()


class TestBackfillValidation:
    def test_requires_env(self, monkeypatch):
        monkeypatch.delenv("AB0T_AUTH_WEBHOOK_PUBLIC_URL", raising=False)
        monkeypatch.delenv("AB0T_AUTH_WEBHOOK_SECRET", raising=False)
        buf_err = io.StringIO()
        with redirect_stderr(buf_err):
            rc = cli.main(["backfill", "--handler", "h", "--user-ids", "u1", "--org-id", "o1"])
        assert rc == 2

    def test_requires_org_id(self, monkeypatch):
        monkeypatch.setenv("AB0T_AUTH_WEBHOOK_PUBLIC_URL", "https://app.test")
        monkeypatch.setenv("AB0T_AUTH_WEBHOOK_SECRET", "secret")
        buf_err = io.StringIO()
        # argparse itself enforces --org-id is required
        with redirect_stderr(buf_err), pytest.raises(SystemExit):
            cli.main(["backfill", "--handler", "h", "--user-ids", "u1"])
