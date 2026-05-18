# Webhook + Credit-Grant Architecture (Stripe → mesh → billing)

**Audience:** future engineers picking up the billing/payment/quota mesh. Don't touch this code path without reading this doc end-to-end. If you find drift between this doc and the code, update the doc in the same commit — the doc is the canonical contract.

**Status:** authoritative as of 2026-05-18. Originally drafted as part of `tickets/20260518_post_upgrade_credit_and_ux_propagation/` after a config-drift bug that took down the credit-grant pipeline.

---

## 0. TL;DR

When a customer pays a subscription invoice, three things must happen in our system:

1. **Tier flips** — billing-service stores the new plan tier; quota limits update (e.g. 1 sandbox → 25 sandboxes)
2. **Credit grants** — billing-service populates the org's `subscription_credit` bucket with the period's bundled-spend amount (e.g. $10)
3. **Subscription record** — payment-service stores the Stripe subscription row for invoice history + future renewals

The tier flip is driven by **two redundant paths** — the success-page `complete_checkout` call (primary) and `customer.subscription.*` webhooks (belt-and-braces backup). The credit grant is driven by **paid-invoice webhooks alone** (`invoice.paid` and `invoice.payment_succeeded` — the lib accepts both; no success-page path). The wiring is non-obvious. This doc maps it.

**Critical invariant:** the credit-grant path is **decoupled** from the tier-flip path. Tier changes happen on `customer.subscription.created/updated`. Credit grants happen on paid-invoice events (`invoice.paid` and/or `invoice.payment_succeeded`, depending on Stripe API version). They can land on different services on different cadences and that's intentional.

---

## 1. The cast of services

| Service | URL | Role | DB |
|---|---|---|---|
| **Stripe** | `api.stripe.com` | The external payment processor. The source of truth for whether money moved. | (theirs) |
| **payment-service** | `payment.service.ab0t.com` | Wraps Stripe SDK. Records invoices, subscriptions, payment methods. The financial system-of-record. | DynamoDB (`payment_service_*` tables) |
| **billing-service** | `billing.service.ab0t.com` | Holds account balances, quota state, tier records, accounting ledger. The customer-facing money state. | DynamoDB (`billing_service_*` tables) + ClickHouse (usage analytics) |
| **sandbox-platform** (any "consumer service") | `sandbox.service.ab0t.com` | The user-facing product. Hosts the ab0t-quota library which mediates billing/payment for that consumer's tiers. | DynamoDB (`resource_service_*`) |
| **auth-service** | `auth.service.ab0t.com` | Identity, multi-tenant orgs, OAuth, RBAC. | DynamoDB (`auth_service_data`) |
| **ab0t-quota (lib)** | (Python lib, mounted INTO each consumer service) | Consumer-side library that handles tier resolution, quota enforcement, credit-grant dispatch, webhook proxy. | (uses the consumer's DDB) |

**Key trust split:**
- Stripe secrets live on `payment-service` (legacy) AND on each consumer service (per-endpoint, owned by the lib proxy).
- Billing-service API keys live on each consumer (service-to-service auth between consumer-lib and billing).
- Payment-service API keys live on each consumer (service-to-service auth between consumer-lib and payment).
- Each org's data lives in its own multi-tenant partition across all DBs.

---

## 2. Current flow — what happens when a user clicks Upgrade

The happy path, with all current-day branches.

```
                              ┌───────────────────────────────────────────────┐
USER                          │   Browser (sandbox.service.ab0t.com)          │
                              │  static/auth/upgrade-modal.js                 │
                              └─────────────────┬─────────────────────────────┘
                                                │ 1. POST /api/payments/checkout/{plan_id}
                                                │    Authorization: Bearer <user JWT>
                                                ▼
┌──────────────────────────────────────────────────────────────────────────────────┐
│  sandbox-platform  (FastAPI; ab0t-quota lib mounted)                             │
│                                                                                  │
│  ab0t_quota/billing/router.py:603   create_checkout()                            │
│     ↓ resolves user.org_id, builds success_url + cancel_url                     │
│     ↓ calls PaymentServiceClient.create_checkout_session(...) [HTTP]            │
│           with X-API-Key=<sandbox's PAYMENT_SERVICE_API_KEY>                    │
│                                                                                  │
└────────────────────────┬─────────────────────────────────────────────────────────┘
                         │ 2. POST /checkout/{org_id}/plan/{plan_id}
                         │    X-API-Key: <sandbox service principal>
                         ▼
┌──────────────────────────────────────────────────────────────────────────────────┐
│  payment-service                                                                 │
│                                                                                  │
│  api/routes/checkout.py:862    create_plan_checkout()                            │
│     ↓ verifies service-principal cross_org permission (lines 899-907)            │
│     ↓ calls Stripe SDK to mint a Checkout Session                                │
│           subscription_data.metadata = {org_id, plan_id, ...}                   │
│     ↓ stashes verification_token in DDB                                          │
│     ↓ returns {checkout_url, session_id, verification_token}                    │
└────────────────────────┬─────────────────────────────────────────────────────────┘
                         │ 3. Returns CheckoutSessionResponse
                         ▼
┌──────────────────────────────────────────────────────────────────────────────────┐
│  sandbox-platform — back in ab0t_quota/billing/router.py:603+                    │
│     ↓ stashes verification_token in DDB at PK=CHECKOUT#{session_id}/INTENT      │
│     ↓ strips it from the response (server-only secret)                          │
│     ↓ returns to browser                                                         │
└────────────────────────┬─────────────────────────────────────────────────────────┘
                         │ 4. Returns redirect to Stripe checkout_url
                         ▼
                  ┌──────────────────┐
                  │      Stripe      │   ← user enters card; pays; redirects back
                  │   (test/live)    │
                  └────────┬─────────┘
                           │ 5. Stripe redirects to:
                           │    https://sandbox.service.ab0t.com/checkout/success?session_id=<id>
                           │
                           │ 6. In PARALLEL, Stripe fires webhooks (see §3 below):
                           │      checkout.session.completed
                           │      customer.subscription.created
                           │      customer.subscription.updated
                           │      invoice.paid / invoice.payment_succeeded
                           │
                           ▼ (the success page redirect — TIER FLIP TRACK)
                           │
           ┌───────────────┼───────────────┐
           │               │               │
           ▼               │               ▼
TIER FLIP TRACK       (asynchronous)    CREDIT GRANT TRACK (see §3)
           │
           │
┌─────────────────────────────────────────────────────────────────────────────────┐
│   Browser at /checkout/success page                                             │
│   JS: POST /api/payments/checkout/complete with session_id                      │
└────────────────────────┬────────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│  ab0t_quota/billing/router.py:499   complete_checkout()                         │
│     ↓ reads verification_token from DDB                                         │
│     ↓ calls payment.verify_checkout_session(session_id, verification_token)     │
│     ↓ payment-service verifies session is paid + metadata correct               │
│     ↓ returns {org_id, plan_id, status='complete'}                              │
│     ↓ resolves plan_id → tier_id via _resolve_plan_to_tier()                    │
│     ↓ calls billing.set_tier(org_id, tier_id, reason="checkout_complete")       │
│         ←─── TIER LIMITS NOW UPDATED in billing DDB                            │
│     ↓ returns to browser                                                        │
└─────────────────────────────────────────────────────────────────────────────────┘
                         │
                         ▼
                  ┌──────────────────┐
                  │   billing-service │  ← tier flipped; quota limits updated
                  └──────────────────┘
```

---

## 3. The webhook path — CREDIT GRANT TRACK (the part that broke)

Stripe sends async webhooks shortly after the user pays. These are what trigger the credit grant.

```
                  ┌──────────────────┐
                  │      Stripe      │
                  │   (test/live)    │
                  └────────┬─────────┘
                           │ Stripe POSTs the webhook event to the URL
                           │ configured in Stripe Dashboard for that
                           │ endpoint. Signed with that endpoint's secret.
                           │
                           │ Today: Dashboard points at sandbox lib proxy.
                           │ Legacy: Dashboard ALSO had payment-service direct.
                           ▼
┌────────────────────────────────────────────────────────────────────────────────────┐
│  sandbox-platform — ab0t_quota/billing/router.py:662   stripe_webhook_proxy()      │
│                                                                                    │
│  STEP A — verify Stripe signature with sandbox's lib proxy secret                  │
│           AB0T_QUOTA_STRIPE_WEBHOOK_SECRET   (set on sandbox-platform-prod)        │
│           ← this is set as the "endpoint secret" in Stripe Dashboard for the      │
│             sandbox lib proxy endpoint                                             │
│                                                                                    │
│  STEP B — branch by event type:                                                    │
│                                                                                    │
│     "invoice.paid" / "invoice.payment_succeeded" ──→ handle_subscription_invoice_paid(...) │
│            ↓                                                                       │
│            Reads invoice.metadata.{org_id, plan_id}                                │
│            ↓                                                                       │
│            Resolves plan_id → tier_id via quota-config.json                        │
│            ↓                                                                       │
│            Reads tier's billing_model + credit_grant config                        │
│            ↓                                                                       │
│            If billing_model="subscription_with_credits":                           │
│              billing.apply_credit_grant(org_id, amount, destination='subscription_credit')│
│              ←─── CORRECT BUCKET CREDITED in billing DDB                          │
│            Returns dispatch_result.status = "applied" / "skipped_*" / "deferred"   │
│                                                                                    │
│     "customer.subscription.updated/deleted" ──→ reset_subscription_credit_on_tier_change(...)│
│            ↓                                                                       │
│            Detects downgrade; calls billing.reset_subscription_credit()            │
│                                                                                    │
│  STEP C — forward to payment-service (regardless of dispatch outcome)              │
│           PaymentServiceClient.forward_webhook(body, signature)                    │
│           ← forwards the ORIGINAL body + ORIGINAL Stripe-Signature header         │
│           ← payment-service must accept the sandbox lib proxy's signing secret    │
│                                                                                    │
│  STEP D — webhook fallback tier-sync (router.py:923+)                              │
│           For checkout.session.completed: if INTENT row says "pending",            │
│           set_tier here too as a belt-and-braces backup                            │
└──────────────────────────┬─────────────────────────────────────────────────────────┘
                           │ POST /webhooks/stripe (forwarded)
                           │ Body: original Stripe payload
                           │ Header: original Stripe-Signature
                           ▼
┌────────────────────────────────────────────────────────────────────────────────────┐
│  payment-service — api/webhooks.py:97   stripe_webhook()                           │
│                                                                                    │
│  STEP E — verify signature against STRIPE_WEBHOOK_SECRETS (multi-secret)           │
│           Iterates the comma-separated list, tries each secret. Accepts if         │
│           ANY one verifies. Pre-T0d this was single-secret only.                   │
│                                                                                    │
│           MUST include BOTH:                                                       │
│             - legacy payment-service endpoint secret (whsec_Na...)                 │
│             - sandbox lib proxy endpoint secret (whsec_Ho...)                      │
│                                                                                    │
│           ↑ THIS IS WHAT BROKE 2026-05-18 — the lib proxy secret wasn't in the    │
│             list, so forwarded events failed signature check.                      │
│                                                                                    │
│  STEP F — route by event type:                                                     │
│                                                                                    │
│     "invoice.paid" / "invoice.payment_succeeded" ──→ handle_invoice_payment_succeeded() │
│            ↓                                                                       │
│            Records invoice as PAID in payment DDB                                  │
│            ↓                                                                       │
│            IF ENABLE_LEGACY_SUBSCRIPTION_INVOICE_CREDIT=true:                      │
│              billing.credit_account(org_id, full_invoice_amount, "balance")        │
│              ←─── BACKUP CREDIT (wrong bucket!) — only fires during cutover       │
│                   Should flip to false once lib proxy verified live                │
│                                                                                    │
│     "customer.subscription.created/updated/deleted" ──→ _sync_subscription_tier() │
│            ↓                                                                       │
│            Resolves new tier from Stripe subscription state                        │
│            ↓                                                                       │
│            Calls billing.PUT /{org_id}/tier {tier_id}                             │
│            ←─── REDUNDANT TIER FLIP (success-page complete_checkout already       │
│                 set it; webhook does it again for belt-and-braces)                │
│                                                                                    │
│     "checkout.session.completed" ──→ records the session, no further action       │
└────────────────────────────────────────────────────────────────────────────────────┘
                           │
                           ▼
                  ┌──────────────────┐
                  │   billing-service │  ← subscription_credit and/or balance credited
                  │                  │   ← tier verified/re-flipped via webhook path
                  └──────────────────┘
```

---

## 4. Trust + signature model (the part that's subtle)

Stripe signs webhooks per-endpoint. Each Stripe Dashboard endpoint has its OWN signing secret. The signature on an event reflects the endpoint URL Stripe sent it to.

| Endpoint (configured in Stripe Dashboard) | Signing secret | Who verifies | Env var |
|---|---|---|---|
| `https://sandbox.service.ab0t.com/api/webhooks/stripe` | `whsec_Ho...` (lib proxy endpoint secret) | sandbox-platform (lib proxy verifies) AND payment-service (after forward) | `AB0T_QUOTA_STRIPE_WEBHOOK_SECRET` (sandbox) + must also be in `STRIPE_WEBHOOK_SECRETS` (payment) |
| `https://payment.service.ab0t.com/webhooks/stripe` (legacy direct) | `whsec_Na...` (legacy endpoint secret) | payment-service only | `STRIPE_WEBHOOK_SECRET` (single) AND/OR first entry in `STRIPE_WEBHOOK_SECRETS` (payment) |

**Consequence**: when the lib proxy forwards an event to payment-service, the event still carries the LIB PROXY ENDPOINT's signature. Payment-service must accept that signature → its `STRIPE_WEBHOOK_SECRETS` list must include the lib proxy's secret.

**The 2026-05-18 bug**: `STRIPE_WEBHOOK_SECRETS` on payment-service-prod contained only `whsec_Na...` (legacy). Forwarded events with `whsec_Ho...` signatures failed → 400 → lib proxy returned 503 → Stripe Dashboard showed "Forward failed". Fix: add `whsec_Ho...` to the comma-separated list.

**The lib does NOT re-sign**. It forwards the original body + original Stripe-Signature header verbatim (see `shared/ab0t-quota/ab0t_quota/billing/clients.py:256-268`). This is correct passthrough behavior; the bug is in payment-service's accepted-list configuration.

---

## 5. Why this design (Alternative B) was picked — and what alternatives exist

The current design routes Stripe webhooks TO the consumer service first, which then forwards to payment-service. This couples the consumer to Stripe-secret handling.

**Three architectures considered:**

### Alternative A — Payment-service only + internal event bus

```
Stripe → payment-service → SQS/SNS/EventBridge → consumer subscribers
```

- Pros: clean trust boundary (payment-service is sole Stripe-secret holder); no per-consumer webhook endpoint sprawl; no signature-relay complexity; consumers don't need Stripe Dashboard config; no signature-mismatch failure mode like the 2026-05-18 bug
- Cons: requires an event-bus infrastructure that we don't run today; introduces an extra hop with its own retry/idempotency semantics; consumers react asynchronously not in-band

### Alternative B — Consumer lib proxy → forward to payment-service (CURRENT)

```
Stripe → consumer lib proxy → forwards to payment-service
```

- Pros: synchronous; no event bus infrastructure required; consumer's credit-grant logic runs before the Stripe `200 OK` returns; consumer can fail fast if its dispatch logic errors
- Cons: multi-secret config drift (the bug we fixed); every consumer is a Stripe webhook destination; cascade failure from forward issues
- This is what's deployed today

### Alternative C — Payment-service + per-consumer webhook callbacks

```
Stripe → payment-service → per-consumer-registered hook URLs (filtered by consumer_id + org_id)
```

- Pros: payment-service is sole Stripe-secret holder (clean trust boundary); payment-service filters events per consumer so no consumer sees another consumer's events; consumer-side dispatch logic still lives in ab0t-quota lib, just invoked from payment-service inbound rather than Stripe directly
- Cons: payment-service needs a consumer-URL registry; non-zero complexity to build but simpler than Alt A (no event bus required)

**Routing key — important**: do NOT route by `metadata.org_id` alone. In a multi-consumer mesh, an `org_id` is the customer/workspace identifier, not the consumer-service boundary. The same `org_id` could legitimately exist as a top-level org in multiple consumers' tenant graphs in some future topology, and even in today's topology the org-id-only filter conflates "which consumer owns this billing event" with "which workspace it credits". The fanout design ticket `tickets/20260507_multi_consumer_stripe_webhook_fanout/TICKET.md` correctly stipulates `metadata.consumer_id` (or equivalent service-audience identifier) for routing. Inside that boundary, `org_id` then selects the workspace target. **Routing key for Alt C: `metadata.consumer_id` (or `service_audience`) FIRST, `metadata.org_id` second.**

This means create-checkout calls today must already stamp BOTH `consumer_id` AND `org_id` into Stripe metadata if we want Alt C to be feasible without a data migration. Audit + amend create-checkout before scheduling Alt C.

**Honest assessment**: Alternative C is what we'd build today. It preserves the consumer-side dispatch (which has real value — consumer-specific quota-config drives credit grants) while consolidating the Stripe trust boundary. The current Alternative B was picked because:

1. We don't have an event bus running
2. Synchronous dispatch was easier to reason about during initial implementation
3. The May 16 cutover plan (`tickets/20260516_auto_credit_invoice_paid_wiring/§8`) was designed as a transition from "payment-service direct" to lib-proxy — both endpoints were active in parallel during cutover for safety

**Migration path**: Alt C could land as a future ticket without breaking existing code. Each consumer's ab0t-quota lib mount would expose an inbound `/api/internal/webhooks/stripe` endpoint authed via service-API-key (not Stripe signature). Payment-service would maintain a registry mapping `metadata.consumer_id` → consumer-callback URL, with `metadata.org_id` carried through to the consumer for workspace targeting. The consumer-side dispatch (T2/T3 handlers) stays the same; only the trust boundary moves.

**Until that migration**: this doc + the multi-secret config rule are the contract. Don't add new consumers to Alt B without registering their secret on payment-service's list as part of the rollout.

---

## 6. Why credit grant ≠ tier flip — the two-track design

This was a deliberate choice in `tickets/20260516_paid_plan_balance_model_gap/`. Reasoning preserved here so future engineers don't try to "simplify" it into one path.

| Question | Tier flip track | Credit grant track |
|---|---|---|
| What triggers it? | `customer.subscription.created/updated` Stripe webhook | Paid-invoice Stripe webhook (`invoice.paid` and/or `invoice.payment_succeeded`) |
| Why a different event? | Subscription state changes (upgrades, plan changes) need to take effect even between billing periods | Credit grants are bound to actual money movements (invoices), not subscription state |
| Cadence | On any subscription change (immediate) | Once per billing period (e.g. monthly) |
| Server responsibility | Updates `tier` field in billing | Updates `subscription_credit` bucket in billing |
| Failure mode | User has wrong limits | User has wrong balance |
| Auth-mesh side effects | Quota enforcement adjusts | Reservation system has different available budget |
| Backup path | Success-page `complete_checkout` calls `billing.set_tier` directly | Legacy `ENABLE_LEGACY_SUBSCRIPTION_INVOICE_CREDIT=true` credits `balance` bucket (wrong bucket, but covers the user during cutover) |

**Critical**: these two tracks can ship to billing independently with different timestamps. A user can have new tier limits BEFORE their period credit arrives (a few hundred ms typically), or never get the credit (today's bug) while having correct limits.

**Why this is not a bug**: tier change reflects subscription state; credit reflects payment. They're related but not the same event. Pretending they're one event papers over real edge cases (trial → paid conversion grants credit on day 1 but tier limits should already be paid-tier from invitation; downgrade should reset credit but keep limits until period-end; etc.).

---

## 7. The cutover plan (May 16 ticket §8) — where we are now

Original cutover plan from `tickets/20260516_auto_credit_invoice_paid_wiring/TICKET.md` §8:

```
Step 1. Deploy code: lib proxy webhook handler + multi-secret support on payment-service
Step 2. Configure Stripe Dashboard with NEW endpoint (lib proxy URL)
Step 3. Add new endpoint's secret to payment-service's STRIPE_WEBHOOK_SECRETS  ←─── MISSED
Step 4. Verify in dev with test webhook replays
Step 5. Verify in staging
Step 6. Live mode: switch Stripe Dashboard to new endpoint; legacy endpoint stays as fallback
Step 7 (T+30d). Verify zero failed webhooks; flip ENABLE_LEGACY_SUBSCRIPTION_INVOICE_CREDIT=false
Step 8 (T+30d). Remove legacy STRIPE_WEBHOOK_SECRETS entry; legacy endpoint can be deprecated
```

**Status as of 2026-05-18:**
- Steps 1-2: ✓ done
- **Step 3: ✗ MISSED** — this is the 2026-05-18 bug
- Steps 4-5: partially done; dev validation didn't catch the secret-config gap because dev STRIPE_WEBHOOK_SECRETS happened to be set correctly
- Step 6: ✓ done (Dashboard points at lib proxy)
- Step 7: NOT YET — needs Step 3 to land first AND 30-day soak. Today we're going to combine Step 3 + Step 7 in one deploy because we can't leave Step 3 ungated (would cause double-credit).
- Step 8: blocked on Step 7's 30-day soak

**Updated cutover plan (post-2026-05-18 fix):**

```
Step 3'. Add lib proxy secret to STRIPE_WEBHOOK_SECRETS  +
         flip ENABLE_LEGACY_SUBSCRIPTION_INVOICE_CREDIT=false  +
         deploy payment-service  (ALL ATOMICALLY)
Step 4'. Replay stuck Stripe events from Dashboard
Step 5'. Verify prod2's subscription_credit > 0
Step 6'. Live-mode end-to-end test with a fresh user signup
Step 7'. 30-day soak monitoring; no further config changes
Step 8'. Remove legacy STRIPE_WEBHOOK_SECRETS entry; deprecate legacy endpoint
```

---

## 8. Where things live (the structural map)

### 8.1 Code

```
shared/ab0t-quota/                                          ← THE consumer-side library
├── ab0t_quota/
│   ├── billing/
│   │   ├── router.py                                       ← THE webhook proxy entry point
│   │   │   :603-648  create_checkout                       ← Step 1 of tier flip track
│   │   │   :482-636  complete_checkout                     ← Step 4 of tier flip track (success page)
│   │   │   :662-921  stripe_webhook_proxy                  ← THE webhook handler
│   │   │   :736-798       T2 invoice.paid dispatch
│   │   │   :814-906       T3 subscription.updated/deleted dispatch
│   │   │   :910-921       forward to payment-service (← 503 source)
│   │   │   :923-957       webhook fallback tier sync
│   │   ├── clients.py                                      ← HTTP clients
│   │   │   :96-127    PaymentServiceClient
│   │   │   :256-268     forward_webhook  (← does NOT re-sign)
│   │   │   :275-...   BillingServiceClient
│   │   ├── subscription_credit.py                          ← The dispatch logic (T2/T3 helpers)
│   │   │   handle_subscription_invoice_paid
│   │   │   reset_subscription_credit_on_tier_change
│   │   └── models.py                                       ← Pydantic shapes
│   └── ...
├── BILLING_MODELS_GUIDE.md                                 ← THE cookbook for billing_model + credit_grant config
├── Skills/
│   ├── quota-paid-tier-onboarding/SKILL.md                 ← End-to-end onboarding guide
│   ├── billing-payment-integration/SKILL.md
│   └── quota-tier-management/SKILL.md
└── docs/
    └── WEBHOOK_AND_CREDIT_GRANT_ARCHITECTURE.md            ← THIS DOC

payment/output/                                             ← The payment-service
├── app/
│   ├── api/
│   │   ├── routes/
│   │   │   └── checkout.py                                 ← Stripe Checkout session creation
│   │   │       :308-516   _verify_checkout_session_impl    ← Verify endpoint (B5-T1 fixed cross_org here)
│   │   │       :862-...   create_plan_checkout             ← Create Stripe session
│   │   └── webhooks.py                                     ← THE webhook receiver (target of forward)
│   │       :97-163    stripe_webhook                       ← Signature verification (multi-secret)
│   │       :430-529   handle_invoice_payment_succeeded     ← Stripe webhook handler
│   │       :486-522     legacy fallback credit             ← Gated on ENABLE_LEGACY_*
│   │       :553-579   handle_subscription_created
│   │       :652-...   _sync_subscription_tier              ← Belt-and-braces tier flip
│   ├── config.py                                           ← Env declarations
│   │   :34       STRIPE_WEBHOOK_SECRET (single, fallback)
│   │   :43       STRIPE_WEBHOOK_SECRETS (plural, primary)
│   │   :59       ENABLE_LEGACY_SUBSCRIPTION_INVOICE_CREDIT
│   │   :250-...  stripe_webhook_secrets_list (parser)
│   └── ...
├── production/
│   └── .env.production                                     ← Prod env (gets synced to prod box via ops/)
│       :77   STRIPE_WEBHOOK_SECRETS=whsec_Na...,whsec_Ho...  ← Staged locally (multi-secret); pending ops sync + rebuild
│       :95   ENABLE_LEGACY_SUBSCRIPTION_INVOICE_CREDIT=false ← Staged locally (atomic pair with line 77); pending sync
│       :__   STRIPE_WEBHOOK_SECRET=whsec_Na...               ← Singular fallback; keep until §8 Step 8 of cutover
└── docs/  (no canonical webhook doc here — this doc is the canonical reference)

billing/output/                                             ← The billing-service
├── app/
│   ├── api/
│   │   └── billing.py
│   │       :__      PUT /tier endpoint                     ← Target of set_tier calls
│   │       :__      POST /apply-credit-grant endpoint      ← Target of credit-grant dispatches
│   ├── services/
│   │   └── usage_service.py
│   │       :30-145  record_usage                           ← Reserved vs direct-debit logic
│   └── ...
├── docs/
│   └── CLICKHOUSE_CONTRACT.md                              ← Analytics-side schema doc
└── ...

resource/output/sandbox-platform/                           ← The CONSUMER service (one of N)
├── app/
│   ├── main.py                                             ← FastAPI app entry point
│   ├── billing_helpers.py                                  ← post_launch_record, pre_launch_reserve
│   └── quota.py                                            ← ab0t-quota library mount
├── static/
│   ├── js/
│   │   └── env-config.js                                   ← __SANDBOX_AUDIENCE etc.
│   └── auth/
│       └── callback.html                                   ← OAuth callback (workspace switch)
└── production/
    └── docker-compose.production.yml                       ← Container config (env wiring)
```

### 8.2 Env vars (per service)

```
┌─────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ Service             │ Env var                                  │ Source of truth file                    │
├─────────────────────┼──────────────────────────────────────────┼─────────────────────────────────────────┤
│ payment-service     │ STRIPE_API_VERSION                       │ payment/output/production/.env.production│
│                     │ STRIPE_SECRET_KEY                        │ "                                       │
│                     │ STRIPE_PUBLISHABLE_KEY                   │ "                                       │
│                     │ STRIPE_WEBHOOK_SECRET                    │ "  (single, legacy/fallback)            │
│                     │ STRIPE_WEBHOOK_SECRETS                   │ "  (comma-separated; multi-endpoint)    │
│                     │ ENABLE_LEGACY_SUBSCRIPTION_INVOICE_CREDIT│ "  (bool; cutover flag)                 │
│                     │ STRIPE_MODE                              │ "  (test|live)                          │
├─────────────────────┼──────────────────────────────────────────┼─────────────────────────────────────────┤
│ sandbox-platform    │ AB0T_QUOTA_STRIPE_WEBHOOK_SECRET         │ sandbox-platform/production/...env*     │
│   (& other ab0t-quota│   (falls back to STRIPE_WEBHOOK_SECRET)  │                                         │
│    consumers)       │ AB0T_MESH_PAYMENT_URL                    │ "                                       │
│                     │ AB0T_MESH_PAYMENT_API_KEY                │ "                                       │
│                     │ AB0T_MESH_BILLING_URL                    │ "                                       │
│                     │ AB0T_MESH_BILLING_API_KEY                │ "                                       │
├─────────────────────┼──────────────────────────────────────────┼─────────────────────────────────────────┤
│ billing-service     │ (uses payment + auth keys via mesh API)  │ billing/output/.env*                    │
└─────────────────────┴──────────────────────────────────────────┴─────────────────────────────────────────┘
```

### 8.3 Stripe Dashboard

```
Dashboard endpoints  (Developers → Webhooks):
  - https://sandbox.service.ab0t.com/api/webhooks/stripe   (new, lib proxy; current primary)
       secret: whsec_Ho...
  - https://payment.service.ab0t.com/webhooks/stripe       (legacy direct; pre-cutover)
       secret: whsec_Na...

Test mode and Live mode are SEPARATE configurations. Both modes need both endpoints +
both secrets. Reconcile via the Dashboard before assuming a fix landed.

Event subscriptions (per endpoint, typically):
  invoice.paid                      ← drives credit grant via T2 dispatch
  invoice.payment_succeeded         ← also drives credit grant (older API; safe to also enable)
  customer.subscription.created     ← tier flip + record
  customer.subscription.updated     ← tier change + reset on downgrade
  customer.subscription.deleted     ← cancellation; reset to free + clear credit
  checkout.session.completed        ← record session; webhook-fallback tier sync
```

### 8.4 DDB tables (relevant to webhook flow)

```
auth_service_data            ← orgs, users, memberships, OAuth clients
payment_service_*            ← Stripe subscriptions, invoices, payment methods
billing_service_*            ← accounts (balance, subscription_credit, credit_balance), tier records, accounting_ledger, reservations
resource_service_*           ← sandboxes, containers, CHECKOUT#{session_id}/INTENT, /ACCOUNT (verification_token storage)

CHECKOUT#{session_id}/INTENT keys:
   - status: pending | completed | completed_by_webhook
   - verification_token: <plaintext, server-only>
   - org_id, plan_id, user_id
   - created_at, ttl_seconds (7 days)
```

---

## 9. Known issues + open questions

### 9.1 Today's bug (2026-05-18) — fix in progress

- Missing lib proxy secret in payment-service's `STRIPE_WEBHOOK_SECRETS`. All forwarded events 503. See `tickets/20260518_post_upgrade_credit_and_ux_propagation/tasklist_20260518_065140.md`.

### 9.2 Observability gap

- Log lines `payment_service_forward_failed`, `invoice_paid_dispatch_result`, `stripe_webhook_proxy` were not visible in the user's `docker logs --since 1h` grep during the 2026-05-18 investigation, even though events failed during that window. Either retention is short, level filtering is dropping them, or log shipping has a destination other than container stdout. Worth a follow-up to ensure these are landing somewhere queryable.

### 9.3 Architectural debt — Alternative C migration

- Move Stripe trust boundary entirely to payment-service. Consumers receive filtered events via internal callback URLs. See §5.

### 9.4 Auto-reconciliation worker

- There is NO worker that periodically polls Stripe for missed webhook events. If all webhooks fail for a window AND Stripe's retry budget is exhausted, events are lost. Manual backfill via `apply-credit-grant` with idempotency keys is the recovery path today.
- Worth adding a daily/hourly reconciliation job that lists Stripe events for the recent window and re-dispatches any that didn't land in our DDB. Tracked as candidate work in the analytics-business-value ticket.

### 9.5 Double-credit risk during cutover

- `ENABLE_LEGACY_SUBSCRIPTION_INVOICE_CREDIT=true` AND lib proxy dispatch BOTH active = same invoice credits two buckets. Mitigated by the cutover plan §7 (flip the flag false after lib proxy verified). Documented in §7 above.

### 9.6 Test-mode vs live-mode config drift

- Stripe Dashboard test mode and live mode are separate. The 2026-05-18 fix needs to land on BOTH modes' secret lists. Easy to miss the test-mode side and end up with prod working but staging-style testing broken.

### 9.7 No automated test of webhook flow against real Stripe

- We have UJ-static-contract tests but no end-to-end test that exercises Stripe (test mode) → lib proxy → payment-service → billing flow. Would catch config drift like the 2026-05-18 bug. Candidate for a separate ticket (a daily synthetic upgrade-flow test).

---

## 10. Glossary

| Term | Meaning |
|---|---|
| **Consumer service** | A user-facing product that uses ab0t-quota (e.g. sandbox-platform). Future: llm-gateway, ab0t-com, etc. |
| **Mesh** | The auth + billing + payment + quota + (consumer services) ecosystem |
| **Tier** | A named plan level (free, starter, pro, enterprise) with quota limits + billing semantics |
| **`billing_model`** | Discriminator on a tier: `capacity_only`, `consumption_only`, `subscription_with_credits`, `subscription_unlock_only`. See `BILLING_MODELS_GUIDE.md` |
| **`credit_grant`** | Per-tier config: `{trigger, amount_per_period, lifecycle, destination, reset_on_downgrade}`. See `BILLING_MODELS_GUIDE.md` |
| **`subscription_credit`** | Billing-side bucket for use-it-or-lose-it credit that resets per period |
| **`balance`** | Billing-side bucket for cash/topup credit; persistent; legacy fallback target |
| **`credit_balance`** | Billing-side bucket for one-time grants (e.g. signup credit); persistent |
| **Lib proxy** | ab0t-quota's webhook handler mounted into a consumer service. Verifies + dispatches + forwards |
| **Forward** | Lib proxy's call to `payment-service /webhooks/stripe` with the original Stripe event body+signature |
| **Cutover** | The transition from "payment-service direct" to "lib proxy" Stripe Dashboard endpoint |
| **Multi-secret** | `STRIPE_WEBHOOK_SECRETS` (plural) — comma-separated list of acceptable signing secrets |
| **Idempotency key** | Redis or DDB key that dedupes operations. For credit grants: `invoice:{id}:credit_grant` |

---

## 11. Cross-references

### Origin / parent tickets

- `tickets/20260518_post_upgrade_credit_and_ux_propagation/` — the bug that surfaced the architecture gaps and prompted this doc
- `tickets/20260516_auto_credit_invoice_paid_wiring/` — the wiring ticket; defines the cutover plan (§8)
- `tickets/20260516_paid_plan_balance_model_gap/` — schema design (the parent); D1-D5 design decisions live here
- `tickets/20260515_prod_checkout_verification_token_outage/` — sister 401 outage on the same handler family
- `tickets/20260518_ab0t_quota_subscription_followup/` — Bug 5 fix that unblocked the tier-flip path

### Design docs

- `shared/ab0t-quota/BILLING_MODELS_GUIDE.md` — the canonical `billing_model` + `credit_grant` config cookbook
- `shared/ab0t-quota/Skills/quota-paid-tier-onboarding/SKILL.md` — end-to-end pipeline diagram
- `shared/ab0t-quota/Skills/billing-payment-integration/SKILL.md` — service integration patterns
- `shared/ab0t-quota/Skills/quota-tier-management/SKILL.md` — tier lifecycle
- `shared/ab0t-quota/Skills/quota-service-integration/SKILL.md` — adding a new mesh consumer

### Code anchors

- `shared/ab0t-quota/ab0t_quota/billing/router.py:662-921` — the webhook proxy handler
- `shared/ab0t-quota/ab0t_quota/billing/clients.py:256-268` — `forward_webhook` (passthrough, NOT re-signing)
- `shared/ab0t-quota/ab0t_quota/billing/subscription_credit.py` — dispatch helpers
- `payment/output/app/api/webhooks.py:97-163` — multi-secret signature verification
- `payment/output/app/api/webhooks.py:486-522` — legacy backup credit path
- `payment/output/app/api/routes/checkout.py:308-516` — verify endpoint (`_verify_checkout_session_impl`)
- `payment/output/app/config.py:34-59` — env declarations
- `billing/output/app/services/usage_service.py:30-145` — usage record + balance debit logic
- `billing/output/docs/CLICKHOUSE_CONTRACT.md` — analytics-side complement

### Audit history

- `tickets/20260516_auto_credit_invoice_paid_wiring/codex_report_*.md` — codex audit reports during the wiring work
- `tickets/20260516_paid_plan_balance_model_gap/context_*.md` — design context docs (especially `context_03_billing_model_decision.md`)

---

## 12. How to update this doc

This doc is canonical. If you change:

- Any webhook handler signature: update §3 and §4
- Any env var name or default: update §8.2 and §11
- The cutover state: update §7
- The Stripe Dashboard endpoint config: update §8.3
- Add a new mesh consumer: update §1 and §8.2

Drift between doc and code is a bug. Catch it during code review.

If you find a path the doc doesn't cover, that's a doc gap — add the section. Future engineers should be able to read this doc top-to-bottom and have a complete mental model. Don't make them piece it together from source.
