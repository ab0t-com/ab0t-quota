# Event-driven credit grant — verified design

**Date:** 2026-04-28
**Replaces:** the "lazy-on-first-balance-call" approach in earlier docs.
**Status:** verified plumbing exists, ready to implement

## Why event-driven (not lazy)

Lazy approach (lib's `/balance` handler runs grant on first hit) had two problems:
1. Frontend coupling — the dashboard had to call `/balance` for the credit to land
2. Race window — user can see $0 momentarily until the lazy grant completes

Event-driven fires the grant the moment auth confirms registration, server-to-server, before the user even loads a page.

## Verified: auth has a production webhook system

`GET https://auth.service.ab0t.com/openapi.json` exposes:

```
POST /events/subscriptions       - create a subscription
GET  /events/subscriptions       - list yours
GET  /events/subscriptions/{id}  - inspect one
POST /events/subscriptions/{id}/test
POST /events/subscriptions/{id}/toggle
GET  /events/subscriptions/{id}/stats
GET  /events/types               - list available event types
```

Confirmed event types include:
- `auth.user.registered` — fires on every new user registration
- `auth.user.login` — fires on every login
- `org.created`, `org.member.added`, etc.

Subscription body schema (from openapi):
```
{
  "name": "ab0t-quota-credit-grant",
  "event_types": ["auth.user.registered"],
  "endpoint": "https://<consumer>.ab0t.com/api/quotas/_webhooks/auth",
  "secret": "<HMAC signing secret>",
  "headers": {"X-Source": "auth"},
  "filters": [{"field": "org_id", "value": "<sandbox-platform-users-org-id>"}],
  "retry_policy": {...},
  "batch_size": 1,
  "max_events_per_minute": 1000
}
```

Delivery: HTTP POST to `endpoint` with HMAC signature in headers (signed with `secret`). Built-in retry, rate limit, filters. Production-grade.

## The flow end-to-end

```
1. User registers via auth's hosted login
   → auth fires `auth.user.registered` event internally
   → workspace_provisioning subscriber materializes workspace (existing behavior)
   → auth's webhook delivery system POSTs to subscriber endpoints

2. Auth POSTs to `https://sandbox.../api/quotas/_webhooks/auth` with:
   - body: { event_type: "auth.user.registered", user_id, org_id, email, ... }
   - header: X-Auth-Signature: <hmac-sha256(body, secret)>

3. ab0t-quota's webhook handler runs:
   a. Verify HMAC against configured secret. Reject 401 if invalid.
   b. Idempotency: check Redis flag `credit_granted:user:{user_id}:{tier_id}`.
      If set, return 200 immediately (already granted).
   c. Resolve billing org for user_id:
      - Call auth `GET /users/{user_id}/organizations` (with mesh creds)
      - Find org where role=owner — that's the billing target
      - Fall back to event's org_id if no owner-role org found
      - Pin to DDB for future reference
   d. Look up user's current tier via tier_provider.get_tier(billing_org)
   e. Read tier.initial_credit from quota-config.json
   f. POST /billing/{billing_org}/promotional-credit (idempotent via key)
   g. Set Redis flag with 30-day TTL
   h. Return 200

4. Auth marks event delivered. User has $10 in their workspace before
   they ever load /dashboard.
```

## What goes in each repo

### ab0t-quota lib

- `ab0t_quota/auth_events.py` (new ~150 lines)
  - `verify_hmac(body, signature, secret) -> bool`
  - `class AuthEventHandler` — dispatcher for event_type → handler fn
  - `async def on_user_registered(event)` — does the resolve + grant
  - `async def resolve_billing_org(user_id, fallback_org_id)` — internal lookup + DDB pin
- `ab0t_quota/setup.py` — `setup_quota` mounts `POST /api/quotas/_webhooks/auth` if `AB0T_AUTH_WEBHOOK_SECRET` env is set. Routes to handler. ~10 lines.
- `ab0t_quota/__main__.py` — new CLI command `subscribe-events`:
  - Reads consumer's auth credentials, computes the public webhook URL, POSTs to `/events/subscriptions`. Idempotent (checks GET first).
  - Operator runs once per environment.
- `tests/test_auth_events.py` — HMAC verify, dispatch, idempotency, fallback
- DDB schema: `USER#{user_id}` SK `BILLING_ORG` (the pinning data, internal to lib)

### Consumer (sandbox-platform)

- One env var: `AB0T_AUTH_WEBHOOK_SECRET=<some-secret>` (operator-domain)
- One operator command run once: `python -m ab0t_quota subscribe-events`
- Zero code changes. The lib mounts the webhook endpoint via `setup_quota`.

### Auth service

- Zero changes. The system already exists and works.

## What this displaces

- All the earlier "lazy on balance call" sketches — gone
- All the proposed `/api/quotas/billing-home/*` user-facing endpoints — gone
- `BillingHome` as a name — replaced with internal `BillingOrgPin` concept (no HTTP surface, just lib-internal DDB rows + a function)
- The need for any frontend coordination — gone

## Drawbacks (honest)

1. **Operator step required.** The subscription has to be created once per environment via the CLI. If skipped, no credits land. Mitigate: include in deploy runbook and add a startup health check that warns if no subscription exists for the configured endpoint.
2. **Webhook URL must be reachable.** Auth needs to POST to the consumer's URL. For local dev, requires ngrok or similar. For prod, the consumer's domain must be public (already true for sandbox.service.ab0t.com).
3. **HMAC secret has to be stored on both sides.** Auth knows it from subscription create; consumer needs it in env. Standard webhook pattern but one more secret to manage.
4. **First-touch race for users who pre-existed the subscription.** If users registered before the subscription was created, they never got credits. Need a one-shot backfill: scan auth for users without billing orgs, fire credits for them.
5. **Auth's webhook delivery has a retry policy but isn't infinite.** If consumer is down for a long outage, some events may be lost. Mitigate: lazy fallback in `/balance` handler (the OLD design) as a safety net — only fires if the event-driven path missed.
6. **Event subscription is per-org in auth.** Sandbox-platform's subscription only fires for events in its own end-users org. If the consumer has multiple end-users orgs, multiple subscriptions needed.
7. **DDB write on first event per user.** Same cost as the lazy approach, just shifted to the event handler.
8. **Library now mounts an HTTP endpoint.** Was internal-only before. New surface (auth-only, signed). Add to the consumer's API surface inventory.

## What stays from earlier sketches

- The `BillingOrgPin` (was BillingHome) DDB row — same schema, written by the event handler instead of the balance handler
- Anti-farming via `user_id` Redis dedup — unchanged
- `delete_user(user_id)` GDPR cleanup — unchanged
- Operator CLI override `python -m ab0t_quota pin-billing-org <user> <org>` — still useful for support cases

## Open questions

1. **Should the lazy `/balance` fallback be wired in too**, as a safety net for #5 above? Recommend YES — same code path, fires only if Redis flag is unset, so harmless when events work and saves us when they don't.
2. **Should the subscription include `auth.user.login` too** to handle the "user existed before subscription" backfill on their next login? Recommend YES — cheap and covers edge case 4.
3. **Where does `AB0T_AUTH_WEBHOOK_SECRET` live?** Env (operator-set). Document in `.env.example` and the deploy runbook.

## UJ tests need to be rewritten

The 3 UJs I wrote earlier hit the now-obsolete `/api/quotas/billing-home/*` endpoints. They need to be replaced with tests that:
- Trigger registration → wait briefly → assert balance has the credit
- Verify a second registration does NOT cross-pollinate
- Verify a registration retry does NOT double-grant

These are observable-behavior tests (call `/api/billing/balance`), no internal endpoints needed.
