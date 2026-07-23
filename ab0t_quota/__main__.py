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
    """Construct a LedgerStore from DECLARED env vars for CLI use (T-7/ENV-10).

    Priority: AB0T_QUOTA_DDB_TABLE+aioboto3 > QUOTA_REDIS_URL+redis.asyncio.
    DECLARED, NOT DISCOVERED: no generic REDIS_URL, and no silent in-memory
    fallback — replay/events against an undeclared store used to read and
    write NOTHING while exiting 0. A declared-but-unavailable store refuses
    (SystemExit) rather than silently running against the wrong one.
    """
    from .handler_ledger import DDBLedgerStore, RedisLedgerStore

    ddb_table = os.getenv("AB0T_QUOTA_DDB_TABLE")
    if ddb_table:
        try:
            import aioboto3
            session = aioboto3.Session()
            client = session.client("dynamodb")
            return DDBLedgerStore(client, table_name=ddb_table)
        except Exception as e:
            print(f"error: AB0T_QUOTA_DDB_TABLE={ddb_table!r} is declared but the DDB "
                  f"client cannot be built ({e}). Fix it or unset it — the CLI will "
                  f"not silently fall back to another store.", file=sys.stderr)
            raise SystemExit(2) from e

    redis_url = os.getenv("QUOTA_REDIS_URL")
    if redis_url:
        try:
            from redis.asyncio import from_url
            r = from_url(redis_url, decode_responses=False)
            return RedisLedgerStore(r)
        except Exception as e:
            print(f"error: QUOTA_REDIS_URL is declared but a client cannot be built "
                  f"({e}). Fix it or unset it.", file=sys.stderr)
            raise SystemExit(2) from e

    print("error: no ledger store declared — set AB0T_QUOTA_DDB_TABLE or "
          "QUOTA_REDIS_URL. (The generic REDIS_URL is no longer read, and there is "
          "no silent in-memory fallback: it read and wrote nothing.)", file=sys.stderr)
    raise SystemExit(2)


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


def _preflight(args) -> int:
    """T-12: pre-deploy verification — schema, resolved plan + provenance,
    then the boot gates read-only. See ab0t_quota/preflight.py."""
    import asyncio as _asyncio

    from .preflight import EXIT_INTERNAL, run_preflight

    emit = (lambda s: print(s, file=sys.stderr)) if args.json_out else print
    try:
        report = _asyncio.run(run_preflight(
            config_path=args.config,
            offline=args.offline,
            check_mesh=args.check_mesh,
            strict=args.strict,
            timeout=args.timeout,
            script_load=not args.no_script_load,
            emit=emit,
        ))
    except Exception as e:  # exit 4: a bug in US, never the consumer's env
        print(f"preflight internal error (a bug in ab0t-quota, not your "
              f"environment — please report it): {type(e).__name__}: {e}",
              file=sys.stderr)
        return EXIT_INTERNAL
    if args.json_out:
        print(json.dumps(report.to_json(), indent=2))
    return report.exit_code


def _doctor(args) -> int:
    """T-1 (tooling lane): posture grading over the SAME evaluator set as
    preflight/boot. `preflight` answers "will it boot"; `doctor` answers
    "is what boots production-grade" — and says what it could not check."""
    import asyncio as _asyncio

    from .preflight import EXIT_INTERNAL, run_doctor

    emit = (lambda s: print(s, file=sys.stderr)) if args.json_out else print
    try:
        doc = _asyncio.run(run_doctor(
            config_path=args.config,
            offline=args.offline,
            check_mesh=args.check_mesh,
            strict=args.strict,
            timeout=args.timeout,
            script_load=not args.no_script_load,
            fail_on_risk=args.fail_on_risk,
            emit=emit,
        ))
    except Exception as e:  # exit 4: a bug in US, never the consumer's env
        print(f"doctor internal error (a bug in ab0t-quota, not your "
              f"environment — please report it): {type(e).__name__}: {e}",
              file=sys.stderr)
        return EXIT_INTERNAL
    if args.json_out:
        print(json.dumps(doc.to_json(), indent=2))
    return doc.exit_code


def _provision(args) -> int:
    """T-2/T-3 (tooling lane): emit infra artifacts from the enforcing
    registry, or start a conforming local dev Redis. NEVER creates cloud
    resources — emit-and-let-them-apply (D-3's rule)."""
    from . import provision as prov

    if args.local:
        return prov.run_local(port=args.port, name=args.name,
                              dry_run=args.dry_run, timeout=args.timeout,
                              emit_line=lambda s: print(s, file=sys.stderr))
    if not args.emit:
        print("error: choose --emit {compose|terraform|acl|iam} or --local",
              file=sys.stderr)
        return 2
    config = None
    cfg_path = args.config or os.environ.get("QUOTA_CONFIG_PATH")
    if cfg_path:
        from .config import load_config
        from .errors import QuotaConfigError
        try:
            config = load_config(args.config)
        except QuotaConfigError as e:
            print(f"CONFIG ERROR: {e}", file=sys.stderr)
            return 2
    else:
        print("note: no --config/QUOTA_CONFIG_PATH — emitting the documented "
              "default table names", file=sys.stderr)
    try:
        kw = {"include_create": args.include_create} if args.emit == "iam" else {}
        text = prov.emit(args.emit, config, **kw)
    except AssertionError as e:  # our registry bug, never the consumer's env
        print(f"provision internal error (a bug in ab0t-quota — please report "
              f"it): {e}", file=sys.stderr)
        return 4
    print(text, end="")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ab0t_quota", description="ab0t-quota CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    # preflight — T-12 (D-4 names it; `check` kept as an alias so the parent
    # pack's provisional wording stays true)
    pf = sub.add_parser(
        "preflight", aliases=["check"],
        help="pre-deploy verification: config schema, resolved plan with "
             "provenance, boot gates read-only. Exit: 0 ok / 1 gate refusal / "
             "2 config / 3 unreachable / 4 internal")
    pf.add_argument("--config", help="quota-config.json (default: same search order as boot)")
    pf.add_argument("--json", dest="json_out", action="store_true",
                    help="machine-readable report on stdout (human report -> stderr)")
    pf.add_argument("--offline", action="store_true",
                    help="schema + plan + provenance only; contacts NOTHING")
    pf.add_argument("--check-mesh", action="store_true",
                    help="ALSO probe mesh /health endpoints (GET only; OFF by default per D-6)")
    pf.add_argument("--strict", action="store_true",
                    help="warnings also fail the exit code")
    pf.add_argument("--timeout", type=float, default=5.0, help="per-probe timeout seconds")
    pf.add_argument("--no-script-load", action="store_true",
                    help="skip D-73's SCRIPT LOAD (the one server-visible write); "
                         "D-73 reports SKIPPED and boot will still perform it")
    pf.set_defaults(func=_preflight)

    # doctor — T-1 (tooling lane): posture grading, one evaluator set.
    # `preflight` = "will it boot" (CI); `doctor` = "is it production-grade,
    # and what could I not check" (humans + auditors).
    dr = sub.add_parser(
        "doctor",
        help="grade production POSTURE (persistence, PITR, TTL, eviction "
             "facts, ACL breadth, encryption, retention) over the same "
             "evaluators preflight/boot use; honest about what it cannot "
             "check. Exit mirrors preflight (0/1/2/3/4); --fail-on-risk "
             "turns RISK posture findings into exit 1")
    dr.add_argument("--config", help="quota-config.json (default: same search order as boot)")
    dr.add_argument("--json", dest="json_out", action="store_true",
                    help="preflight-report/v1 EXTENDED with a posture section "
                         "(hand it to an auditor); human report -> stderr")
    dr.add_argument("--offline", action="store_true",
                    help="static posture only; contacts NOTHING")
    dr.add_argument("--check-mesh", action="store_true",
                    help="ALSO probe mesh /health endpoints (GET only; OFF by default per D-6)")
    dr.add_argument("--strict", action="store_true",
                    help="preflight warnings also fail the exit code")
    dr.add_argument("--fail-on-risk", action="store_true",
                    help="RISK posture findings fail the exit code (advice by default)")
    dr.add_argument("--timeout", type=float, default=5.0, help="per-probe timeout seconds")
    dr.add_argument("--no-script-load", action="store_true",
                    help="skip D-73's SCRIPT LOAD (doctor's one server-visible write)")
    dr.set_defaults(func=_doctor)

    # provision — T-2/T-3 (tooling lane): the vehicle. Emits artifacts from
    # the enforcing registry; NEVER creates cloud resources.
    pv = sub.add_parser(
        "provision",
        help="emit infrastructure artifacts (compose|terraform|acl|iam) "
             "generated from the enforcing gate registry, or start a "
             "conforming local dev Redis (--local). Never touches cloud "
             "resources: emit-and-let-them-apply")
    pv.add_argument("--emit", choices=["compose", "terraform", "acl", "iam"],
                    help="artifact to print on stdout")
    pv.add_argument("--config", help="quota-config.json for declared table names "
                                     "(optional; defaults are emitted otherwise)")
    pv.add_argument("--include-create", action="store_true",
                    help="(--emit iam) include the self-provisioning actions — "
                         "only with storage.auto_create_tables=true")
    pv.add_argument("--local", action="store_true",
                    help="start ONE local Docker Redis conforming to the gates, "
                         "then verify it with the boot evaluator")
    pv.add_argument("--port", type=int, default=6399,
                    help="host port for --local (default 6399 — deliberately "
                         "not 6379: declare, never discover)")
    pv.add_argument("--name", default="ab0t-quota-dev-redis",
                    help="container name for --local")
    pv.add_argument("--dry-run", action="store_true",
                    help="print the docker command --local would run, run nothing")
    pv.add_argument("--timeout", type=float, default=5.0, help="verification timeout seconds")
    pv.set_defaults(func=_provision)

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
