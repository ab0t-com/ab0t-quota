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


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ab0t_quota", description="ab0t-quota CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("subscribe-events", help="register webhook subscription against auth")
    s.add_argument("--auth-url", help="defaults to $AB0T_AUTH_AUTH_URL")
    s.add_argument("--endpoint", required=True, help="public webhook URL on the consumer service")
    s.add_argument("--org-id", help="filter to this end-users org_id (recommended)")
    s.add_argument("--name", help="subscription name (default: ab0t-quota-credit-grant)")
    s.set_defaults(func=_subscribe_events)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
