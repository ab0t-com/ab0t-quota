"""Auth-event webhook receiver: grant initial credit on user.registered.

Auth's webhook system (POST /events/subscriptions on auth.service.ab0t.com)
delivers `auth.user.registered` events to the URL we register. This module
verifies the HMAC, looks up the user's billable org, and grants the
configured initial_credit — server-to-server, no frontend involved.

Pinning is sticky: the first event for a user_id resolves the org, writes
a BILLING_ORG row in QUOTA_STATE_TABLE, and uses it for every future
billing operation for that user.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Header, HTTPException, Request

logger = logging.getLogger(__name__)

WEBHOOK_PATH = "/_webhooks/auth"


def verify_hmac(body: bytes, signature: Optional[str], secret: str) -> bool:
    """Constant-time HMAC-SHA256 verify. Auth signs with the secret we set
    at subscription-create time."""
    if not signature or not secret:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    # Auth's header format: "sha256=<hex>" or just "<hex>" — accept both.
    sig = signature.split("=", 1)[1] if "=" in signature else signature
    return hmac.compare_digest(expected, sig)


async def resolve_billing_org(
    user_id: str,
    fallback_org_id: str,
    *,
    auth_url: str,
    mesh_api_key: str,
    pin_store: Any,  # PinStore protocol — get/set on DDB
) -> str:
    """Return the org to bill against. Sticky: first call writes a pin to
    DDB; subsequent calls return the pinned value.

    Resolution rule for first call: prefer the user's owner-role org
    (workspace if workspace-per-user is enabled; first org with role=owner
    otherwise). Fall back to the event's org_id if no owner-role org found.
    """
    pinned = await pin_store.get(user_id)
    if pinned:
        return pinned

    resolved = fallback_org_id
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{auth_url.rstrip('/')}/users/{user_id}/organizations",
                headers={"X-API-Key": mesh_api_key},
            )
            if r.status_code == 200:
                orgs = r.json() or []
                owner_orgs = [o for o in orgs if o.get("role") == "owner"]
                if owner_orgs:
                    resolved = owner_orgs[0]["id"]
    except Exception as e:
        logger.warning("resolve_billing_org auth lookup failed user=%s err=%s; using fallback", user_id, e)

    await pin_store.set(user_id, resolved, source="auto")
    return resolved


async def grant_initial_credit_for_user(
    user_id: str,
    org_id: str,
    *,
    initial_credits: dict,  # tier_id -> amount
    tier_provider: Any,
    redis: Any,
    billing_url: str,
    billing_api_key: str,
) -> None:
    """Idempotent. Resolves tier, grants once per (user_id, tier_id)."""
    tier_id = await tier_provider.get_tier(org_id)
    amount = initial_credits.get(tier_id)
    if not amount:
        return

    flag_key = f"credit_granted:user:{user_id}:{tier_id}"
    if await redis.get(flag_key):
        return

    idempotency_key = f"user:{user_id}:initial_credit:{tier_id}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{billing_url.rstrip('/')}/billing/{org_id}/promotional-credit",
                headers={"X-API-Key": billing_api_key, "Content-Type": "application/json"},
                json={"amount": amount, "reason": f"initial_credit_{tier_id}", "idempotency_key": idempotency_key},
            )
            if resp.status_code in (200, 400):
                # 400 is idempotency replay from billing — already granted, set the flag too
                await redis.set(flag_key, "1", ex=86400 * 30)
                logger.info("initial_credit_granted user=%s org=%s tier=%s amount=%s", user_id, org_id, tier_id, amount)
            else:
                logger.warning("initial_credit_unexpected_status status=%s user=%s body=%s", resp.status_code, user_id, resp.text[:200])
    except Exception as e:
        logger.warning("initial_credit_failed user=%s err=%s", user_id, e)


def make_router(
    *,
    webhook_secret: str,
    auth_url: str,
    mesh_api_key: str,
    billing_url: str,
    billing_api_key: str,
    initial_credits: dict,
    tier_provider: Any,
    redis: Any,
    pin_store: Any,
) -> APIRouter:
    """Build the webhook router. Mounted by setup_quota at /api/quotas."""
    router = APIRouter()

    @router.post(WEBHOOK_PATH, include_in_schema=False)
    async def on_auth_event(
        request: Request,
        # Auth's webhook delivery uses X-Event-Signature; X-Webhook-Signature is the
        # legacy publisher's name. Accept either for compat.
        x_event_signature: Optional[str] = Header(None),
        x_webhook_signature: Optional[str] = Header(None),
    ):
        body = await request.body()
        sig = x_event_signature or x_webhook_signature
        if not verify_hmac(body, sig, webhook_secret):
            raise HTTPException(status_code=401, detail="invalid signature")

        try:
            payload = json.loads(body)
        except Exception:
            raise HTTPException(status_code=400, detail="invalid json")

        event_type = payload.get("event_type") or payload.get("type")
        data = payload.get("data") or payload

        if event_type not in ("auth.user.registered", "auth.user.login"):
            return {"status": "ignored", "event_type": event_type}

        user_id = data.get("user_id")
        org_id = data.get("org_id")
        if not user_id or not org_id:
            return {"status": "skipped", "reason": "missing user_id or org_id"}

        billing_org = await resolve_billing_org(
            user_id, fallback_org_id=org_id,
            auth_url=auth_url, mesh_api_key=mesh_api_key, pin_store=pin_store,
        )
        await grant_initial_credit_for_user(
            user_id, billing_org,
            initial_credits=initial_credits,
            tier_provider=tier_provider,
            redis=redis,
            billing_url=billing_url,
            billing_api_key=billing_api_key,
        )
        return {"status": "ok", "billing_org": billing_org}

    return router


class PinStore:
    """DDB-backed user_id -> billing org_id pinning. Sticky on first set.

    Lives in the existing QUOTA_STATE_TABLE. Schema:
      PK: USER#{user_id}
      SK: BILLING_ORG
      attrs: org_id, set_at, source ("auto" | "operator")
    """

    def __init__(self, table_name: str, ddb_client: Any):
        self.table = table_name
        self.ddb = ddb_client

    async def get(self, user_id: str) -> Optional[str]:
        try:
            res = await self.ddb.get_item(
                TableName=self.table,
                Key={"PK": {"S": f"USER#{user_id}"}, "SK": {"S": "BILLING_ORG"}},
            )
            item = res.get("Item")
            return item["org_id"]["S"] if item else None
        except Exception as e:
            logger.warning("pin_store.get failed user=%s err=%s", user_id, e)
            return None

    async def set(self, user_id: str, org_id: str, *, source: str = "auto") -> None:
        from datetime import datetime, timezone
        try:
            # Conditional write: never overwrite an operator-set value with auto
            cond = "attribute_not_exists(org_id) OR #s = :auto" if source == "auto" else None
            kwargs = dict(
                TableName=self.table,
                Item={
                    "PK": {"S": f"USER#{user_id}"},
                    "SK": {"S": "BILLING_ORG"},
                    "org_id": {"S": org_id},
                    "set_at": {"S": datetime.now(timezone.utc).isoformat()},
                    "source": {"S": source},
                },
            )
            if cond:
                kwargs["ConditionExpression"] = cond
                kwargs["ExpressionAttributeNames"] = {"#s": "source"}
                kwargs["ExpressionAttributeValues"] = {":auto": {"S": "auto"}}
            await self.ddb.put_item(**kwargs)
        except Exception as e:
            # ConditionalCheckFailedException = operator value already set; that's fine
            if "ConditionalCheckFailed" not in str(e):
                logger.warning("pin_store.set failed user=%s err=%s", user_id, e)
