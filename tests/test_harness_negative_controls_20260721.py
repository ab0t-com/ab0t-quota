"""Permanent harness self-tests (design_test_harness_20260721.md §1.3 Layer 1).

A guard never proven capable of failing has told you nothing. Each control
feeds the declared-not-discovered harness a DELIBERATE break and asserts the
harness catches it. If any of these fail, every green produced under the
harness is void.
"""
from __future__ import annotations

import socket

import pytest

from tests.dnd_harness_20260721 import (
    DECOYS,
    ContactAttempted,
    PollutedEnv,
    install_no_contact,
    install_pollution,
    install_seam_recorders,
)


def test_nc1_socket_guard_catches_connect(monkeypatch):
    install_no_contact(monkeypatch)
    s = socket.socket()
    try:
        with pytest.raises(ContactAttempted):
            s.connect(("127.0.0.1", 9))
    finally:
        s.close()


def test_nc2_socket_guard_catches_getaddrinfo(monkeypatch):
    install_no_contact(monkeypatch)
    with pytest.raises(ContactAttempted):
        socket.getaddrinfo("decoy-redis.invalid", 6399)


def test_nc2b_socket_guard_catches_create_connection(monkeypatch):
    install_no_contact(monkeypatch)
    with pytest.raises(ContactAttempted):
        socket.create_connection(("127.0.0.1", 9))


def test_nc3_leak_scan_catches_seeded_decoy(monkeypatch):
    env = install_pollution(monkeypatch)
    with pytest.raises(AssertionError) as ei:
        env.assert_no_decoy_leaked(
            recorded=[("redis", ("redis://x",), {"password": DECOYS["REDIS_PASSWORD"]})])
    assert "DECOY_PASSWORD_c4a1" in str(ei.value)
    with pytest.raises(AssertionError):
        env.assert_no_decoy_leaked(caplog_text=f"resolved url {DECOYS['REDIS_URL']}")


def test_nc4_each_seam_records_and_raises(monkeypatch):
    import aioboto3
    import boto3
    import httpx
    import redis.asyncio as ra

    rec = install_seam_recorders(monkeypatch)
    with pytest.raises(ContactAttempted):
        ra.Redis.from_url("redis://x")
    with pytest.raises(ContactAttempted):
        boto3.client("sns")
    with pytest.raises(ContactAttempted):
        aioboto3.Session()
    with pytest.raises(ContactAttempted):
        httpx.AsyncClient()
    assert {c[0] for c in rec.all_calls} == {"redis", "boto3", "aioboto3", "httpx"}


def test_nc7_pollution_count_floor():
    """Removing a decoy must fail the fixture itself — a five-decoy pollution
    pass would silently test less than it claims."""
    short = dict(DECOYS)
    short.pop("REDIS_URL")
    with pytest.raises(AssertionError):
        PollutedEnv(short)
