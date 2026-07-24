# information — VISION.md steps 4/5/6: state of the world before building

**Date:** 2026-07-22
**Author:** agent (read-only investigation; no library code touched)
**Scope:** `VISION.md:24-28, 46-52` — JS SDK, client commercial UI, white-label admin portal
**Verdict:** **the vision doc is materially stale in three places, and the brief that sent me
here is wrong in one.** Build on the corrected picture below, not on `VISION.md:46-52`.

Evidence grades: **FOUND** = read it at `file:line`. **INFERRED** = reasoned from FOUND facts.
**NOT-DETERMINED** = could not establish without something I was not allowed to touch.

---

## 1. Headline delta

| VISION.md claim | Reality | Grade |
|---|---|---|
| `VISION.md:46-52` — "The Web Components and admin portal code in `~/random/storyboard/` … **belong in** the payment service" (future tense) | A **partial extraction already happened.** `payment/output/www/` contains `components/{checkout-form,pricing-toggle,billing-dashboard,pricing-card}.html`, `css/payment-widgets.css`, `js/{payment-widgets,wizard}.js` | **FOUND** |
| implied: payment's copy == storyboard's | **Every shared file DIFFERS.** `checkout-form.html` is 8,009 B in payment vs 19,645 B in storyboard — payment's is a *reduced, diverged* fork, not a copy | **FOUND** |
| `VISION.md:31` — "JS SDK … **TICKET FILED**" | No ticket for the SDK exists in `shared/ab0t-quota/tickets/` (13 dirs, none SDK/UI). NOT-DETERMINED whether one exists in `payment/output/tickets/` — did not exhaustively enumerate | **FOUND / NOT-DETERMINED** |
| `VISION.md:15` — "`<script src="payment.service.ab0t.com/js/ab0t-billing.js">`" is the drop-in | **The payment service deliberately refuses to serve `www/`.** `payment/output/app/main.py:189-191`: *"The full `www/` tree is intentionally NOT mounted; only the two HTML files below are ever reachable."* Only `/static/privacy-policy.html` and `/static/terms-of-service.html` are routed (`main.py:208-216`) | **FOUND** |

**The single most important finding:** step 4 of the vision is not blocked on *writing* an SDK.
It is blocked on a **deliberate, documented decision in the payment service not to serve static
UI at all.** Someone hardened that surface on purpose. Shipping `ab0t-billing.js` from
`payment.service.ab0t.com/js/` requires reversing a security posture, and that is an **owner
decision**, not an implementation detail. See §4 for the alternative that does not require it.

---

## 2. What actually exists, by repo

### 2a. `~/random/storyboard/app/www2/www/` — FOUND, and it is big

The admin portal is **real and substantial**, ~10k lines of vanilla-JS components:

| File | Lines | Role |
|---|---|---|
| `js/components/invoice-manager.js` | 1673 | invoice list/detail/actions |
| `js/components/refund-manager.js` | 1442 | refund issue/track |
| `js/components/plan-list.js` | 1231 | plan catalog admin |
| `js/components/modal-system.js` | 1272 | shared modal primitives |
| `js/components/plan-creator.js` / `plan-editor.js` | 1060 / 1042 | plan CRUD |
| `js/components/subscription-manager.js` | 855 | subscription admin |
| `js/components/checkout-manager.js` | 750 | checkout orchestration |
| `js/components/plan-manager.js` | 673 | plan orchestration |
| `js/components/slide-panel.js` | 528 | shared panel primitive |
| `admin.html` | 829 | the portal shell |
| `pricing.html` | 568 | public pricing page |
| `js/lib/{payment,billing}-client{,-v3,-types}.js` | ~2900 | typed API clients for 8005 / 8002 |

Also present: `www2/payment-openapi.json`, `billing-openapi.json`, `auth-openapi.json` — the
contracts these were written against. **These are the highest-value artifacts in the whole
investigation**, because they date the fork.

**This is a working admin portal.** It should be *ported*, not rewritten. Rewriting 10k lines of
functioning plan/subscription/invoice/refund admin would be the single most wasteful thing this
program could do.

### 2b. `payment/output/` — FOUND

* API surface (`app/api/`): `health, invoices, payment_methods, payments, plans, refunds,
  reports, routes/, subscriptions, webhooks` — i.e. **the admin portal's backend already exists.**
* `app/templates/`: only `invoice_email.html`, `invoice_template.html` (server-rendered email/PDF,
  not UI).
* `app/static/`: only `legal/`.
* `www/`: the diverged widget fork (§1), **unserved** (`main.py:189-191`).
* No admin portal, no SDK bundle, no build tooling. **INFERRED:** the widgets in `www/` were
  copied in as reference material and then fenced off, never wired.

### 2c. `shared/ab0t-quota` — FOUND

* `ab0t_quota/billing/router.py` — **20 proxy routes**, verified by grep:
  `/billing/{balance,usage/summary,usage/records,transactions}`,
  `/payments/{subscriptions,invoices,methods,checkout/{plan_id},portal}`,
  DELETE `/payments/subscriptions/{id}`, PUT `/payments/methods/{id}/default`,
  DELETE `/payments/methods/{id}`, `POST /webhooks/stripe` (`router.py:220-667`).
* `ab0t_quota/billing/templates/` — exactly **one** file, `checkout_success.html`, served at
  `router.py:986` `GET /checkout/success`. **This is the only HTML the library ships today.**
* `setup.py:2343-2397` mounts `/api/quotas/{usage,tiers,check/{key},check-bundle/{name}}`.

---

## 3. The brief's own error — corrected

> The brief states: *"An 80%-of-limit warning is computed and currently discarded
> (`middleware.py:127-133`)."*

**That attribution is wrong.** `ab0t_quota/middleware.py` is **142 lines total** and contains
**no warning logic whatsoever** — grepping `warn|0.8|threshold` returns only
`fail_open_error_threshold` (`middleware.py:54,63,94-95`). Lines 127-133 are the response-header
block.

**What is actually true — and the conclusion survives:**

* The warning **is** computed, in `engine.py:1288-1307`: utilization is compared against
  `tier_limits.critical_threshold` then `tier_limits.warning_threshold`, returning
  `QuotaResult(decision=ALLOW_WARNING, severity=CRITICAL|WARNING, message=MessageBuilder.warning(...))`.
* It **is** fully surfaced on the model: `QuotaResult.warning` (`responses.py:99-101`),
  `.severity`, `.message`, `.utilization` (`responses.py:85-91`).
* **What is discarded is narrower and more specific:** `middleware.py:127-133` sets only
  `X-Quota-Limit` and `X-Quota-Remaining` on the success response. It never sets a warning
  header, never surfaces `result.message`, and never surfaces `result.severity`. So on the
  *middleware* path a warning is invisible to the browser.
* But `GET /api/quotas/usage` **does** expose per-resource `severity` and `utilization`
  (`engine.py:1155-1175`, via `QuotaState.severity`), so **a UI can render the warning today
  without any library change.**

**Net:** the brief's *conclusion* ("the pre-block upgrade prompt is unrendered") is correct and
is the right thing to build. Its *mechanism* is wrong, and a library change is **not** required —
which is fortunate, because I am barred from making one. **No library change is requested by
this work.** (A future `X-Quota-Warning` response header would improve the middleware path; that
is ticketed, not done — see the ticket pack, T-9.)

---

## 4. Where each artifact belongs, and why

The vision says SDK + admin portal both go to the payment service. **I agree on one and dissent
on the other.**

### Admin portal → **payment service.** Agree with `VISION.md:24-28`.
Its entire data contract is payment-service domain: plans, subscriptions, invoices, refunds,
payment methods. `payment/output/app/api/` already exposes exactly those. Co-locating portal and
API means one repo's tests catch a contract break. **No dissent.**

### JS SDK → **`shared/ab0t-quota`, served by payment.** Dissent from `VISION.md:31`.

Three arguments:

1. **Its data contract is 100% ab0t-quota's models, not payment's.** Every field the paywall and
   usage components render — `tier_display`, `upgrade_url`, `display_name`, `unit`, `action_hint`,
   `severity`, `utilization`, `limit` — is defined in `ab0t_quota/models/{core,responses}.py` and
   emitted by `engine.py` / `messages.py`. If the SDK lives in payment, a field rename in
   ab0t-quota breaks a different repo **silently**. If it lives here, the repo's 1417-test suite
   is the contract guard. Ownership should follow the schema, not the CDN.
2. **The SDK talks to the consumer's own origin, not to payment.** `/api/quotas/*` is mounted by
   `setup_quota()` **into the consumer's FastAPI app** (`setup.py:412`), and `create_billing_router()`
   mounts the 20 proxy routes there too (`router.py:220+`). So the SDK's requests are
   **same-origin against the consumer** — no CORS, no cross-origin token handoff, no third-party
   cookie exposure. The payment service is only ever the **file host**. Owning the file and
   serving the file are different jobs.
3. **It de-risks §1's blocker.** Because the SDK is same-origin at runtime, a consumer can vendor
   the file (`pip install ab0t-quota[billing]` ships it; one route serves it from their own app)
   and get the full drop-in **without payment ever un-hardening `www/`**. The
   `payment.service.ab0t.com/js/` CDN path becomes a convenience, not a prerequisite. That turns
   `main.py:189-191` from a blocker into a preference.

**INFERRED, flagged:** this is my call under the standing rule that the ticket system is the
product owner. It is recorded as **D-V456-1** in the ticket pack and is reversible.

---

## 5. What is genuinely missing (the real backlog)

| # | Item | Status |
|---|---|---|
| 1 | Paywall / upgrade-moment component (429 → rendered) | **MISSING** — nothing renders `to_api_error()` |
| 2 | Usage meters (`/api/quotas/usage` → rendered) | **MISSING** |
| 3 | Pre-block warning prompt (severity ≥ warning) | **MISSING** — data exists, no renderer |
| 4 | Pricing table from `/api/quotas/tiers` | **MISSING** — storyboard's `pricing.html` is hardcoded against payment plans, not quota tiers |
| 5 | Checkout / portal launch | **EXISTS backend** (`router.py:608,655`); **no client JS** |
| 6 | Invoice list (customer-facing) | **EXISTS backend** (`router.py:262`); **no client JS** |
| 7 | Admin portal | **EXISTS in storyboard, unported** |
| 8 | Payment serves static UI | **BLOCKED by design** (`main.py:189-191`) |
| 9 | SDK bundle + versioning + embed contract | **MISSING** |

Items 1-3 are the revenue surface and depend on **zero** unbuilt backend. That is what I built.

---

## 6. What I did NOT verify

* Whether `payment/output/tickets/` already holds SDK/admin tickets — **NOT-DETERMINED.**
* Which of payment-`www/` vs storyboard is newer — **NOT-DETERMINED** (no `git` allowed; mtimes
  are sync artifacts, not authorship).
* Any runtime behaviour. Nothing was executed against a live service. All findings are static.
* Whether the storyboard clients still match the current payment/billing OpenAPI — the captured
  `*-openapi.json` in `www2/` makes this a mechanical diff, but it is **unstarted**. It is the
  first task of the port ticket, because it sizes everything else.
