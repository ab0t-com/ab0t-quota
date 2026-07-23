# PROOF — ab0t-quota-ui v0.2.0

**Date:** 2026-07-22 · **Command:** `node sdk/test_sdk.js`
**Result: 27/27 groups, 109 assertions, exit 0**

Discharges the proof obligation in `DESIGN_commercial_ui_20260722.md` §7: every component must
render correctly from a **two-tier**, a **single-tier** and a **no-catalog** config — the exact
shapes that broke the library's old hardcoded ladder (`messages.py` docstring, D-CK-4).

There is no jsdom and no Chrome on this box, so `test_sdk.js` ships a ~110-line DOM shim and
drives the **real SDK** through it. `demo.html` renders the same fixtures visually with the same
assertions; both share `demo-fixtures.js`, so the browser page and the headless run cannot drift.

## Scenario groups

| # | Scenario | Proves |
|---|---|---|
| A | three-tier catalog (Hobby/Studio/Agency) | next tier read from the consumer's own array; no foreign plan names; override badge; warning banner |
| B | **two-tier**, on the bottom tier — paywall | CTA says "Upgrade to **Pro**", never "Starter"; the consumer's `action_hint` survives verbatim |
| C | **two-tier, on the TOP tier** — usage | 94% critical meter with **no CTA at all**, no dangling upgrade text |
| D | **single-tier** catalog — paywall | message only; no CTA, no link, no invented plan |
| E | **bridge mode**, `/tiers` → 404 (`setup.py:989`) | paywall fully functional from the 429 body alone; generic CTA rather than a guessed tier name; `retry_after` rendered |
| F | `limit === null` + no `upgrade_url` | ∞ affordance and **no bar**; unit omitted when config omits it, rendered when config supplies it |
| **G** | plan grid, three tiers | one column per tier; exactly one current marker; **exactly one CTA**, on the current column, naming the next array element; row labels from `/usage` |
| **H** | plan grid, **two-tier, on the top** | two columns, **no CTA anywhere** |
| **I** | plan grid, **single-tier** | one column, no CTA, no second plan invented |
| **J** | plan grid, **no catalog** | renders **nothing** — not an error, not an empty shell |
| **K** | plan grid with `TierConfig.price` | the priced tier renders in **its own** code; the tier whose price omits the code renders **no amount** |
| **L** | invoices, populated | per-invoice currency; the `currency: null` invoice shows no amount; "amount due" derived arithmetically; status verbatim |
| **M/M2/M3** | invoices — empty / 401 / outage | plain empty sentence; 401 renders nothing; outage is a **quiet** line with a working retry |
| **N/N2** | balance & spend | balance in the response's own code; spend shows `—` because its contract has no code; summary absent still renders the balance |
| **O/P** | subscription | `plan_id` printed **verbatim** (never title-cased); non-renewal stated with a date; `amount` withheld for want of a code; empty state |
| **Q** | the money contract | `money()` returns null for a missing, empty or malformed code, and for a non-numeric amount; zero is a real amount |
| **R** | meter a11y | every track is `role="progressbar"` with min/max/now/valuetext; one persistent polite live region naming the **state**; icons `aria-hidden` |
| **S** | catalog-independence | the invoice widget's output is **byte-identical** under two-tier, single-tier and no-catalog transports |

Deliberately no shared vocabulary between scenarios — media, logistics, seats, compute. A
hardcoded ladder would name a nonexistent plan in C–K.

## Structural gates

| # | Gate | Method |
|---|---|---|
| **X1** | no tier name or plan ladder in the shipped bundle | greps the artifact (comments stripped) for `Free\|Starter\|Pro\|Enterprise\|Premium\|Basic` |
| **X2** | no invented money | extracts every **string literal** and fails on a currency symbol, an ISO code, or any `currency: '…'` default. `currency: code` — a variable read from a response — passes |
| **X3** | no upstream status vocabulary | fails on `paid\|unpaid\|past_due\|trialing\|canceled\|void\|draft` in a string literal; the provider adds statuses without asking us |
| **X4** | **WCAG AA contrast, measured** | parses the shipped stylesheet, computes 39 pairs per theme, plus a theme-drift check |
| **X5** | **white-label stays AA** | merges each demo brand with the base theme and re-runs all pairs (156) |

**X1 and X2 are the important ones.** They fail the build the moment anyone types a plan name or
a currency into the SDK — precisely how the original defect entered the library. X2 is a
strengthening, not a relaxation, of the old `currency`-word ban: it permits reading a real field
and forbids inventing a value, which is the thing that was ever actually wrong (D-V456-7).

## Measured contrast

Computed by `contrast.js` from `ab0t-quota-ui.css` using WCAG 2.1 relative luminance. Run
`node sdk/contrast.js` for the full 78-row table. Thresholds: **4.5:1** body text, **3.0:1**
non-text UI and focus indicators.

| Theme | Pairs | Worst text | Worst non-text |
|---|---|---|---|
| light | 39 | **4.91:1** — OK icon on its tint (`--ab0t-info` on `--ab0t-info-bg`) | **3.23:1** — secondary-button boundary on a sunken tile |
| dark | 39 | **6.95:1** — limit-reached badge on a sunken tile | **3.55:1** — secondary-button boundary on a sunken tile |
| Atlas Press brand | 78 | pass | pass |
| Quanta Systems brand | 78 | pass | pass |

Everything clears its threshold; nothing is claimed that was not computed.

Severity is **never colour alone**: each level carries a distinct icon *shape* (circle / triangle
/ diamond / octagon), a text label, and — third — a colour.

## Not proven

* **No browser has rendered any of this.** There is no browser on this box. The shim asserts DOM
  *structure*; it cannot assert layout, paint, focus **order**, or that a screen reader actually
  announces the live region. Contrast is computed from the **stylesheet**, not from rendered
  pixels — a correct ratio in CSS can still be defeated by an overlay or an opacity nobody
  measured. Horizontal scroll at 320px is designed for (`overflow`-safe grids, `overflow-wrap`)
  and **unverified**. Ticketed as **T-8**.
* **No live service was called.** All transports are fixtures. Field names were read at
  `engine.py:1155-1182`, `setup.py:2361-2380`, `models/responses.py:103-117` and
  `billing/models.py:19-270`, but no response from a running consumer was observed.
  Ticketed as **T-3**.
* **Three contract gaps are worked around, not fixed** — the SDK renders honestly around them and
  they are filed rather than papered over: `/tiers` drops `TierConfig.price` and ships resource
  keys rather than names (**T-14**); no per-tier checkout URL exists (**T-15**); two billing
  models carry an amount with no currency (**T-16**). None was fixed here: `ab0t_quota/` is at a
  verified 1417-green.
* Screen-reader behaviour is designed to spec, unverified.

## Files

| File | What it is |
|---|---|
| `ab0t-quota-ui.js` | the SDK. Zero dependencies, no build step, no external fetch |
| `ab0t-quota-ui.css` | the design system: tokens, states, both themes |
| `contrast.js` | the contrast gate (also runnable alone) |
| `test_sdk.js` | the headless proof |
| `demo.html` | the proof harness — evidence, with per-scenario verdicts |
| `showcase.html` | the product showcase, incl. the live brand switcher |
| `demo-brands.css` | **the white-label proof** — two brands, token overrides only |
| `demo-fixtures.js` / `demo-page.css` | shared fake back-end and page chrome (not part of the SDK) |
| `page-pricing.html` · `page-upgrade.html` · `page-billing.html` | hosted-page compositions |
