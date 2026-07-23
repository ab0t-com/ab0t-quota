# Design — the ab0t commercial UI layer (VISION.md steps 4/5/6)

**Date:** 2026-07-22 · **Status:** design accepted, v1 slice built (see `ab0t-quota-ui.js`)
**Precondition:** read `../information_vision456_state_20260722.md` first — it corrects
`VISION.md:46-52` and this document builds on the corrected picture.

---

## 0. The one rule

> **The config is king, and examples never leak.**
> (`tickets/20260722_end_customer_experience_defects/TICKET_config_is_king.md` §5d, D-CK-1…D-CK-8)

Every noun a component renders — plan name, resource name, unit, CTA copy, the upgrade target,
the ladder order — comes from the consumer's own `quota-config.json`, transported over
`/api/quotas/*`. **The SDK ships zero tier names, zero plan ladder, zero resource copy, zero
currency.** A grep of the shipped bundle for `Free|Starter|Pro|Enterprise|\$|USD` must return
nothing. That grep is a test (T-4), not a convention.

> **Amended 2026-07-22 by D-V456-7.** The money half of this rule was narrowed once the invoice
> and balance widgets landed (T-6/T-7): the SDK may render an amount, but **only** from a field
> that carries both the number and its ISO code, via the single `money()` helper. The gate got
> *stronger*, not weaker — `test_sdk.js` X2 now extracts every string literal from the artifact
> and fails on a currency symbol, an ISO code, or a `currency: '…'` default, while permitting
> `currency: code` read from a response. It bans the **invention**, not the word.

This is not stylistic. The library carried `UPGRADE_TIER_MAP` and `ACTION_HINTS` until 0.6.3 and
told consumers with a `free`/`pro` catalog to "upgrade to Starter" — a plan that did not exist in
their product (`messages.py` module docstring). **A UI that hardcodes a ladder reproduces that
exact defect one layer up, where it is more visible and harder to unship.**

---

## 1. Component inventory and data contract

Every field below was read at `file:line`. Nothing here is aspirational.

| Component | Source endpoint | Fields consumed | Defined at |
|---|---|---|---|
| **Paywall** (429 moment) | any 429 body | `error, resource, current, limit, tier, tier_display, message, upgrade_url, retry_after` | `models/responses.py:103-117` |
| **Usage meters** | `GET /api/quotas/usage` | `org_id, tier_id, tier_display, resources[].{resource_key, display_name, unit, current, limit, utilization, severity, has_override, counter_type}` | `engine.py:1155-1182` |
| **Warning banner** | same as meters | `resources[].severity` ∈ `info\|warning\|critical`, `.utilization` | `engine.py:1162-1163`, `QuotaState.severity` |
| **Plan comparison** | `GET /api/quotas/tiers` | `tiers[].{tier_id, display_name, description, features[], limits{key:{limit, limit_display}}, upgrade_url}`, **pre-sorted by `sort_order`** | `setup.py:2361-2380` |
| **Checkout launch** | `POST /api/payments/checkout/{plan_id}` | session url | `billing/router.py:608` |
| **Customer portal** | `POST /api/payments/portal` | portal url | `billing/router.py:655` |
| **Invoice list** | `GET /api/payments/invoices` | `InvoicesResponse` | `billing/router.py:262` |
| **Balance / spend** | `GET /api/billing/balance`, `/usage/summary` | `BillingBalanceResponse` | `billing/router.py:220,227` |
| **Admin: plans** | payment `app/api/plans.py` | — | port from storyboard |
| **Admin: subscriptions** | payment `app/api/subscriptions.py` | — | port from storyboard |
| **Admin: refunds** | payment `app/api/refunds.py` | — | port from storyboard |

**Two contract notes that shape the design:**

1. **`/tiers` is already sorted server-side** by `sort_order` (`setup.py:2363`). The client must
   **not** re-sort, and must **not** infer order from tier ids. "The next tier" is *the next
   element of the returned array after the one matching `tier_id`* — nothing more. That is the
   whole ladder algorithm, and it is why a two-tier and a single-tier catalog both work with no
   special case.
2. **`/tiers` is not mounted in bridge mode** (`setup.py:989`: *"`/tiers` is intentionally NOT
   mounted in bridge mode"*). So **every tier-dependent component must degrade to a usable state
   with no catalog at all.** This is a first-class state, not an error. The paywall's own 429
   body carries `tier_display` and `upgrade_url`, so the paywall stays fully functional without
   `/tiers`; only the *comparison* view degrades.

---

## 2. White-labelling — brand flows from config, not constants

Three channels, in decreasing order of preference:

1. **Content** — already solved. Plan names, resource names, units, CTA targets and the
   `action_hint` remediation sentence are consumer-authored and arrive over the wire. The SDK
   never authors a noun.
2. **Voice** — the sentence itself is library-owned by decision **D-CK-2** (*"NO `messages`
   section in either schema, this release"*), overridable in one object via
   `messages.Templates` server-side. **The SDK must not re-implement the sentence.** It renders
   `result.message` verbatim. A client wanting different wording changes `Templates` in Python;
   there is deliberately no second copy of the copy.
3. **Skin** — CSS custom properties only, namespaced `--ab0t-*`. The SDK ships neutral defaults
   and a `data-ab0t-theme` hook; the host page overrides tokens. No inline colours, no `!important`,
   no CSS reset, no font loading (an SDK that loads a font has picked the client's brand for them).

**Non-goal, stated:** no logo slot, no template engine, no theme marketplace in v1. Custom
properties cover the realistic 95% and cost nothing to support.

---

## 3. Integration story

The embed the vision promises (`VISION.md:15`) is one script plus one attribute:

```html
<script src="/js/ab0t-quota-ui.js" defer></script>
<div data-ab0t="usage"></div>
```

`data-ab0t="usage" | "paywall" | "warning"` auto-mounts on `DOMContentLoaded`. Imperative API
(`AB0TQuotaUI.usage(el, opts)`, `.paywall(el, payload, opts)`) exists for SPAs.

**Auth/session.** The SDK makes **same-origin** requests with `credentials: 'same-origin'` and no
token handling of its own. This is the load-bearing choice: `/api/quotas/*` is mounted into the
*consumer's* app (`setup.py:412`) and the billing proxy routes likewise (`router.py:220+`), so the
consumer's existing session cookie or `Authorization` header already authorises them. A `fetchImpl`
option lets a bearer-token app inject its own fetch. **The SDK never stores, reads, or refreshes a
credential** — see `information_…:§4` for why this also removes the payment-service CORS problem.

**Error and empty states** — all four are designed, not incidental:
* *unauthenticated* (401): render nothing, emit `ab0t:auth-required`. Never a login form — that is
  the host app's job and its own brand.
* *unreachable / 5xx*: a quiet inline retry. **Never** a scary error; a billing widget that shouts
  during an outage costs more trust than it saves.
* *empty*: zero resources registered → render nothing at all (not "no data").
* *unlimited* (`limit === null`, `engine.py:1152` allows it): render the resource with an
  explicit unlimited affordance and **no meter bar** — a full or empty bar would both lie.

---

## 4. Accessibility, responsive, i18n

* **A11y is not deferred.** Meters use `role="meter"` with `aria-valuenow/min/max/valuetext`,
  where `valuetext` is the human sentence, not the number. The paywall is `role="alertdialog"`,
  focus-trapped, `Esc`-dismissible, with the CTA first in tab order. Severity is **never**
  colour-only — it carries a text label and a shape. Target contrast ≥ 4.5:1 in both schemes.
  Honours `prefers-reduced-motion` and `prefers-color-scheme`.
* **Responsive:** single fluid column, no breakpoints needed below 640px; meters and tier
  comparison scroll inside their own container so the host page never scrolls horizontally.
* **i18n / currency posture — stated position:** **v1 is single-locale-neutral, not
  single-locale.** The SDK renders no currency at all (§5), and formats every number through
  `Intl.NumberFormat` with the *document's* locale (`document.documentElement.lang`), falling back
  to the browser's. Pluralisation of units is server-side already (`messages._unit`), so the
  client never pluralises. Result: nothing needs re-engineering when a second locale arrives —
  the only missing piece will be translated `Templates`, which is a Python-side change. **We do
  not ship a string table, because we do not own any strings.**

---

## 5. What v1 deliberately excludes, and why

| Excluded | Why |
|---|---|
| **Pricing table with prices** | Prices live in payment's plan catalog, not quota config. Rendering money means currency, tax, proration, and locale — a materially larger contract with a real correctness risk. The **plan comparison** (limits + features, no money) is in scope and delivers most of the value. |
| **Checkout form / card fields** | PCI scope. `VISION.md:16` promises *"no PCI scope"*. The SDK will only ever *redirect* to the hosted session from `router.py:608`. Non-negotiable. |
| **Admin portal** | Different repo (payment), different auth (staff), different data (payment domain). Porting 10k lines of working storyboard code is its own ticket — see `../tickets/20260722_commercial_ui_layer/`. |
| **Invoice list / balance UI** | Backend exists (`router.py:227,262`); no quota-config dependency, so it carries none of the config-is-king risk and can be built later without redesign. Sequencing, not doubt. |
| **Framework packages (React/Vue)** | A framework wrapper before a single consumer has asked is speculative. Web-standard custom elements work everywhere; a wrapper is ~50 lines whenever asked for. |
| **A build step** | The SDK is one dependency-free ES file. Adding bundling to ship one file is cost without benefit, and a zero-build artifact is far easier for a consumer to vendor (§4 of the information doc). |

---

## 6. Why the paywall + usage slice was chosen for v1

**Chosen:** paywall (429), warning banner, usage meters, plan comparison.
**Chosen against:** invoice list, balance widget, admin portal port, pricing-with-prices.

Reasons: (a) these four are the **only** components whose data is already fully produced and
completely unrendered — `to_api_error()` and `/usage` exist and nothing on earth displays them;
(b) they are the **revenue surface** — the block and the pre-block warning are the two moments a
user decides to pay; (c) they carry **all** the config-is-king risk, so getting them right sets
the pattern the remaining components inherit; (d) they need **no** unbuilt backend and **no**
library change (§3 of the information doc), so nothing blocks. The excluded items are all either
backend-complete-but-lower-value (invoices, balance) or a different repo (admin).

---

## 7. Proof obligation

Any component must render correctly from a **single-tier** catalog and a **two-tier** (`free`/`pro`)
catalog — the exact shapes that broke the library's old hardcoding. Concretely:

* **single-tier** → no upgrade clause, no CTA, no "next plan" affordance, and *nothing dangling*.
* **two-tier, on the top tier** → same as single-tier.
* **two-tier, on the bottom tier** → CTA names the *consumer's* second tier by its `display_name`.
* **no `upgrade_url`** → message renders, link is omitted (never a dead `#`).
* **no `/tiers`** (bridge mode) → paywall fully functional from the 429 body alone.
* **`limit === null`** → unlimited affordance, no bar.

`demo.html` exercises all six live, side by side. `PROOF.md` records the results.
