# Stripe → Lib Proxy → Payment / Billing / Lib Handler — Tier + Credit Flow

## Overview (current, post-T0/T1/T2/T3/T8/T11)

```
Stripe Dashboard webhook
        │  (Stripe-Signature: t=<ts>,v1=<hex>)
        ▼
LIBRARY  POST /api/webhooks/stripe         <-- mounted by setup_quota in the consumer
        │  verify HMAC-SHA256 against AB0T_QUOTA_STRIPE_WEBHOOK_SECRET
        │  reject forged / missing signatures with 400 BEFORE forwarding
        │
        ▼
PAYMENT  POST /webhooks/stripe              <-- forwarded body, X-Forwarded-For preserved
        │  (also verifies signature server-side, multi-secret list supported)
        │
        ├──► subscription.created / .updated:
        │       resolve price_id -> tier_id (stable mapping, see T0e)
        │       PUT billing: /billing/{org_id}/tier  (T8: no hardcoded tier allowlist)
        │
        ├──► invoice.paid / invoice.payment_succeeded:
        │       dispatch into LIB handler `handle_subscription_invoice_paid`
        │       lib reads `tier.credit_grant` from quota-config and grants
        │       (idempotency_key: invoice:{invoice_id}:credit_grant — STABLE
        │        across both event types so accounts emitting both don't double-credit)
        │
        └──► subscription.deleted / .unpaid:
                lib's `reset_subscription_credit_on_tier_change` zeros
                subscription_credit; PUT billing tier=free
```

**Auth is not on the money path.** It IS involved for signup grants via the
auth-event webhook (`auth.user.registered`) — see `docs/auth-events.md` —
but those are independent of the Stripe pipeline above.

## What the consumer wires (one call)

```python
setup_quota(
    app,
    config_path="quota-config.json",
    enable_paid=True,     # mounts /api/webhooks/stripe + signature verifier
    redis=redis,
)
```

Then in `quota-config.json`:

<!-- doc-exec: fragment (shows ONLY the tier/billing_integration keys this flow adds — merge into your full config, which must declare storage; see docs/requirements.md) -->
```jsonc
{
  "tiers": [{
    "tier_id": "starter",
    "billing_model": "subscription_with_credits",
    "price": { "amount_per_period": "20.00" },
    "credit_grant": {
      "trigger": "subscription_invoice_paid",
      "amount_per_period": "25.00",
      "lifecycle": "use_it_or_lose_it",
      "destination": "subscription_credit"
    }
  }],
  "billing_integration": {
    "stripe_price_to_tier": { "price_starter_monthly": "starter" }
  }
}
```

Zero handler code. The library auto-registers the credit-grant handler on
startup and dispatches incoming invoice events through it. See T11 in
ticket `20260516_auto_credit_invoice_paid_wiring` for the drop-in delta.

## Required env vars on the consumer

- `AB0T_QUOTA_STRIPE_WEBHOOK_SECRET` — Stripe Dashboard webhook secret.
  When unset, the proxy route 503s (loud failure, not silent forward).
- `AB0T_AUTH_WEBHOOK_SECRET` — only needed if you want signup-credit
  grants from auth events; required for `auth.user.registered` dispatch.
- `AB0T_MESH_API_KEY` — mesh credential.

## Billing tier endpoint behaviour (post-T8)

`PUT /billing/{org_id}/tier`:
1. Accepts ANY `tier_id` string (no hardcoded allowlist).
2. Logs a WARN `quota_tier_set_outside_known_set` if the tier isn't in the
   billing-service's hint list — the consumer's lib catalog publish is the
   real source of truth.
3. Atomic DynamoDB `TransactWriteItems` for tier + history.
4. Invalidates `quota:tier:{org_id}` in Redis.
5. Idempotent — same tier twice is a no-op.

## Consumer-side tier read

Consumer services call `GET /billing/{org_id}/tier` via the bundled
`AuthServiceTierProvider` (so named for historical reasons — it targets
the billing service today, not auth). 5-minute Redis cache. After a tier
change, billing invalidates the cache key; the next request rehydrates.

## Failure modes

- **Forged/unsigned Stripe POST:** library proxy returns 400 before the
  request leaves your edge. T1 + T0d + T0f gates.
- **Missing `AB0T_QUOTA_STRIPE_WEBHOOK_SECRET`:** route 503s, Stripe
  retries until configured. Loud failure prevents silent bypass.
- **Payment forward fails (5xx):** lib returns the upstream status;
  Stripe retries with backoff.
- **Lib credit-grant handler throws:** payment-service still updated
  Stripe state, but lib didn't grant. Idempotency_key on the grant means
  the next dispatch (Stripe retry OR manual replay via reconciler) is
  safe to re-fire — it'll either succeed or no-op if already applied.
- **Billing returns 503:** payment retries on next Stripe webhook.
- **DynamoDB unreachable:** billing returns 503; same retry path.

## Backlinks

- Decision: `ticket 20260516_paid_plan_balance_model_gap/context_03_billing_model_decision.md`
- Implementation: `ticket 20260516_auto_credit_invoice_paid_wiring/TICKET.md` (T1–T17)
- API surface: `ab0t_quota/billing/router.py` (proxy), `ab0t_quota/billing/subscription_credit.py` (handler)
- Tests: `tests/test_billing_models.py`, `tests/test_auth_events.py`, UJ-310/311/312
