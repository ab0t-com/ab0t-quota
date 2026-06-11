"""ab0t-quota CLI.

Subcommands:
  subscribe-events  - register a webhook subscription against auth so credit
                      grants fire on auth.user.registered. Idempotent.

Usage:
  python -m ab0t_quota subscribe-events \\
      --auth-url https://auth.service.ab0t.com \\
      --endpoint https://sandbox.service.ab0t.com/api/quotas/_webhooks/auth \\
      --org-id <end-users-org-id-to-watch> \\
      [--name ab0t-quota-credit-grant]

  Reads AB0T_AUTH_WEBHOOK_SECRET, AB0T_MESH_API_KEY (or AB0T_AUTH_ADMIN_TOKEN)
  from the environment.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import httpx


def _subscribe_events(args) -> int:
    auth_url = (args.auth_url or os.getenv("AB0T_AUTH_AUTH_URL") or "").rstrip("/")
    endpoint = args.endpoint
    org_id = args.org_id
    name = args.name or "ab0t-quota-credit-grant"
    secret = os.getenv("AB0T_AUTH_WEBHOOK_SECRET", "")
    # Subscription writes typically require an admin user token, not a service
    # API key. Accept either; admin token wins.
    admin_token = os.getenv("AB0T_AUTH_ADMIN_TOKEN", "")
    api_key = os.getenv("AB0T_MESH_API_KEY", "")

    missing = [k for k, v in [("--auth-url", auth_url), ("--endpoint", endpoint),
                              ("AB0T_AUTH_WEBHOOK_SECRET", secret),
                              ("admin token or mesh key", admin_token or api_key)] if not v]
    if missing:
        print(f"missing required: {', '.join(missing)}", file=sys.stderr)
        return 2

    headers = {"Content-Type": "application/json"}
    if admin_token:
        headers["Authorization"] = f"Bearer {admin_token}"
    else:
        headers["X-API-Key"] = api_key

    body = {
        "name": name,
        "event_types": ["auth.user.registered", "auth.user.login"],
        "endpoint": endpoint,
        "secret": secret,
    }
    if org_id:
        body["filters"] = [{"field": "org_id", "value": org_id}]

    # Idempotency: GET first, look for an existing subscription with the same
    # endpoint URL — if found, just print its id and exit 0.
    with httpx.Client(timeout=20) as client:
        r = client.get(f"{auth_url}/events/subscriptions", headers=headers)
        if r.status_code == 200:
            existing = (r.json() or {}).get("items") or r.json() or []
            for sub in existing if isinstance(existing, list) else []:
                if sub.get("endpoint") == endpoint:
                    print(f"subscription already exists: {sub.get('subscription_id') or sub.get('id')}")
                    return 0

        r = client.post(f"{auth_url}/events/subscriptions", headers=headers, json=body)
        if r.status_code in (200, 201):
            sub = r.json()
            print(f"created subscription: {sub.get('subscription_id') or sub.get('id')}")
            print(f"  events: {body['event_types']}")
            print(f"  endpoint: {endpoint}")
            return 0
        print(f"failed: HTTP {r.status_code} {r.text[:300]}", file=sys.stderr)
        return 1


def _build_store_from_env():
    """Construct a LedgerStore from env vars for CLI use.

    Priority: AB0T_QUOTA_DDB_TABLE+aioboto3 > QUOTA_REDIS_URL+redis.asyncio > InMemory.
    The CLI is meant to be run on the same host/network as the lib, so this
    typically matches what the running app picked at setup_quota time.
    """
    from .handler_ledger import DDBLedgerStore, RedisLedgerStore, InMemoryLedgerStore

    ddb_table = os.getenv("AB0T_QUOTA_DDB_TABLE")
    if ddb_table:
        try:
            import aioboto3
            session = aioboto3.Session()
            client = session.client("dynamodb")
            return DDBLedgerStore(client, table_name=ddb_table)
        except Exception as e:
            print(f"warning: DDB requested but unavailable ({e}); falling back", file=sys.stderr)

    redis_url = os.getenv("QUOTA_REDIS_URL") or os.getenv("REDIS_URL")
    if redis_url:
        try:
            from redis.asyncio import from_url
            r = from_url(redis_url, decode_responses=False)
            return RedisLedgerStore(r)
        except Exception as e:
            print(f"warning: Redis requested but unavailable ({e}); falling back", file=sys.stderr)

    print("warning: no persistent store (set AB0T_QUOTA_DDB_TABLE or QUOTA_REDIS_URL); using in-memory",
          file=sys.stderr)
    return InMemoryLedgerStore()


def _print_rows(rows, *, format="table"):
    """Pretty-print ledger rows. Default: a compact table."""
    if not rows:
        print("(no rows)")
        return
    if format == "json":
        print(json.dumps([r.to_dict() for r in rows], indent=2, default=str))
        return
    # Compact table
    print(f"{'STATUS':<18} {'HANDLER':<28} {'EVENT_ID':<24} {'USER':<14} {'ATTEMPTED':<25}")
    print("-" * 110)
    for r in rows:
        print(f"{r.status.value:<18} {r.handler_name[:27]:<28} {r.event_id[:23]:<24} "
              f"{(r.user_id or '-')[:13]:<14} {(r.attempted_at or '-')[:25]}")


def _events(args) -> int:
    import asyncio
    from .handler_ledger import LedgerStatus
    store = _build_store_from_env()
    since = None
    if args.since:
        since = _parse_since(args.since)

    async def _run():
        if args.user_id:
            return await store.query_by_user(args.user_id, limit=args.limit, since_epoch=since)
        if args.status:
            return await store.query_by_status(LedgerStatus(args.status), limit=args.limit, since_epoch=since)
        if args.event_id and args.handler:
            row = await store.get_row(handler_name=args.handler, event_id=args.event_id)
            return [row] if row else []
        print("error: provide --user-id, --status, or --event-id+--handler", file=sys.stderr)
        return None

    rows = asyncio.run(_run())
    if rows is None:
        return 2
    _print_rows(rows, format=args.format)
    return 0


def _parse_since(s: str) -> float:
    """Parse '1h' '24h' '7d' or ISO timestamp → epoch seconds."""
    import time
    if s.endswith("h"):
        return time.time() - int(s[:-1]) * 3600
    if s.endswith("d"):
        return time.time() - int(s[:-1]) * 86400
    if s.endswith("m"):
        return time.time() - int(s[:-1]) * 60
    from datetime import datetime
    return datetime.fromisoformat(s).timestamp()


def _replay(args) -> int:
    """Re-fire a handler for a specific event_id from the stored snapshot.

    Looks up the row in the ledger, posts the saved event_payload back to
    the configured webhook URL (signed with the same secret), letting the
    receiver dispatch normally. The handler's @idempotent ledger row is
    reset to allow re-processing.
    """
    import asyncio
    import hashlib as _hashlib
    import hmac as _hmac

    store = _build_store_from_env()
    webhook_url = args.webhook_url or os.getenv("AB0T_AUTH_WEBHOOK_PUBLIC_URL", "")
    if webhook_url and "/_webhooks/auth" not in webhook_url:
        webhook_url = webhook_url.rstrip("/") + "/api/quotas/_webhooks/auth"
    secret = os.getenv("AB0T_AUTH_WEBHOOK_SECRET", "")
    if not (webhook_url and secret):
        print("error: need AB0T_AUTH_WEBHOOK_PUBLIC_URL + AB0T_AUTH_WEBHOOK_SECRET (or --webhook-url)",
              file=sys.stderr)
        return 2

    async def _do():
        row = await store.get_row(handler_name=args.handler, event_id=args.event_id)
        if row is None:
            print(f"error: no ledger row for handler={args.handler} event_id={args.event_id}",
                  file=sys.stderr)
            return 1
        if row.event_payload is None:
            print(f"error: row has no stored payload (older format?) — cannot replay", file=sys.stderr)
            return 1
        # Reset the row so dispatcher proceeds. For simplicity: re-record_attempt
        # will see existing terminal state and short-circuit, so we manually
        # set status to FAILED to make it retryable.
        from .handler_ledger import LedgerStatus
        await store.record_outcome(handler_name=args.handler, event_id=args.event_id,
                                    status=LedgerStatus.FAILED, reason="reset_for_replay")
        body = json.dumps(row.event_payload).encode()
        sig = _hmac.new(secret.encode(), body, _hashlib.sha256).hexdigest()
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(webhook_url, content=body,
                                  headers={"X-Event-Signature": sig,
                                            "Content-Type": "application/json"})
            print(f"replay: HTTP {r.status_code} body={r.text[:200]}")
            return 0 if r.status_code == 200 else 1

    return asyncio.run(_do())


def _backfill(args) -> int:
    """Synthesize events for the given user_ids and post them. Useful for
    pre-existing users who signed up before the handler was wired up."""
    import asyncio
    import hashlib as _hashlib
    import hmac as _hmac

    webhook_url = args.webhook_url or os.getenv("AB0T_AUTH_WEBHOOK_PUBLIC_URL", "")
    if webhook_url and "/_webhooks/auth" not in webhook_url:
        webhook_url = webhook_url.rstrip("/") + "/api/quotas/_webhooks/auth"
    secret = os.getenv("AB0T_AUTH_WEBHOOK_SECRET", "")
    if not (webhook_url and secret):
        print("error: need AB0T_AUTH_WEBHOOK_PUBLIC_URL + AB0T_AUTH_WEBHOOK_SECRET", file=sys.stderr)
        return 2

    user_ids = [u.strip() for u in args.user_ids.split(",") if u.strip()]
    if args.org_id is None:
        print("error: --org-id required for backfill (events carry org_id)", file=sys.stderr)
        return 2

    async def _do():
        sent, failed = 0, 0
        async with httpx.AsyncClient(timeout=30) as client:
            for uid in user_ids:
                event = {
                    "event_type": args.event_type,
                    "event_id": f"backfill_{uid}_{int(__import__('time').time())}",
                    "data": {"user_id": uid, "org_id": args.org_id, "_synthetic": True},
                }
                body = json.dumps(event).encode()
                sig = _hmac.new(secret.encode(), body, _hashlib.sha256).hexdigest()
                r = await client.post(webhook_url, content=body,
                                      headers={"X-Event-Signature": sig,
                                                "Content-Type": "application/json"})
                if r.status_code == 200:
                    sent += 1
                    print(f"  {uid}: OK")
                else:
                    failed += 1
                    print(f"  {uid}: FAILED HTTP {r.status_code}")
        print(f"backfill: sent={sent} failed={failed}")
        return 0 if failed == 0 else 1

    return asyncio.run(_do())


def _delete_user(args) -> int:
    """GDPR cascade — delete all ledger rows for a user_id."""
    import asyncio
    store = _build_store_from_env()
    if not args.confirm:
        print(f"error: --confirm required (irreversible delete of all ledger rows for {args.user_id})",
              file=sys.stderr)
        return 2

    async def _do():
        n = await store.delete_user(args.user_id)
        print(f"deleted {n} ledger row(s) for user {args.user_id}")
        return 0

    return asyncio.run(_do())


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ab0t_quota", description="ab0t-quota CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    # TODO(public-mesh-ga): Add a provider-neutral sync-plans command that
    # reads quota-config.json plans/prices and publishes plan metadata/tier
    # mappings, replacing consumer-specific shell seed scripts. Backlink:
    # audit: 2026-05-16 public-mesh-ga readiness pass
    s = sub.add_parser("subscribe-events", help="register webhook subscription against auth")
    s.add_argument("--auth-url", help="defaults to $AB0T_AUTH_AUTH_URL")
    s.add_argument("--endpoint", required=True, help="public webhook URL on the consumer service")
    s.add_argument("--org-id", help="filter to this end-users org_id (recommended)")
    s.add_argument("--name", help="subscription name (default: ab0t-quota-credit-grant)")
    s.set_defaults(func=_subscribe_events)

    # events — query the ledger
    e = sub.add_parser("events", help="query the handler ledger")
    e.add_argument("--user-id", help="filter to a specific user_id")
    e.add_argument("--status", choices=["in_progress", "success", "skipped", "failed", "failed_permanent"])
    e.add_argument("--handler", help="(with --event-id) get one row by handler+event_id")
    e.add_argument("--event-id", help="(with --handler) get one row")
    e.add_argument("--since", help="e.g. '1h', '24h', '7d', or ISO timestamp")
    e.add_argument("--limit", type=int, default=50)
    e.add_argument("--format", choices=["table", "json"], default="table")
    e.set_defaults(func=_events)

    # replay — re-fire an event from the ledger snapshot
    r = sub.add_parser("replay", help="re-fire a handler for a specific event from the stored snapshot")
    r.add_argument("--handler", required=True, help="handler name (from @idempotent(handler=...))")
    r.add_argument("--event-id", required=True)
    r.add_argument("--webhook-url", help="defaults to $AB0T_AUTH_WEBHOOK_PUBLIC_URL/api/quotas/_webhooks/auth")
    r.set_defaults(func=_replay)

    # backfill — synthesize events for users who pre-existed the handler
    b = sub.add_parser("backfill", help="fire synthetic events for given user_ids")
    b.add_argument("--handler", required=True, help="handler name (for logging only)")
    b.add_argument("--user-ids", required=True, help="comma-separated list")
    b.add_argument("--org-id", required=True, help="org_id all events will carry")
    b.add_argument("--event-type", default="auth.user.registered")
    b.add_argument("--webhook-url", help="defaults to $AB0T_AUTH_WEBHOOK_PUBLIC_URL")
    b.set_defaults(func=_backfill)

    # delete-user — GDPR cascade
    d = sub.add_parser("delete-user", help="delete all ledger rows for a user (GDPR)")
    d.add_argument("--user-id", required=True)
    d.add_argument("--confirm", action="store_true",
                   help="required to actually delete (no-op without it)")
    d.set_defaults(func=_delete_user)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
