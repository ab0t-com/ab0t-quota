# Counter types — the `counter_type` setting (how it works + how to choose)

Every resource you declare in `quota-config.json` carries a **`counter_type`** (`ab0t_quota/models/core.py` `CounterType`).
It is the single most important field: it tells the library HOW the resource is counted, what its SOURCE OF TRUTH is, how
it reconciles, and which way it fails on a truth-source outage. Pick the type that matches how the resource behaves in the
real world. There are exactly **three** values.

| `counter_type` | Means | Required extra field | Source of truth | How it reconciles | Fail direction on truth-source outage | Money-safe example |
|---|---|---|---|---|---|---|
| **`gauge`** | How many are **live right now** (concurrency / claim-bound). `+1` on acquire, `-1` on release. | — | the **live count** of the real things (your `observed_usage_provider` / real-state probe) | recompute = **count what's actually live now**; force-set the gauge to it | **fail-OPEN** — a stale-high gauge must never block a user; over-admission is bounded and healed | `sandbox.concurrent`, `browser_sessions`, `desktop_sessions`, `gpu_instances`, `home_storage_gb` |
| **`accumulator`** | **Total consumed this period** (metered / cumulative). Monotonically increases, then resets. | `reset_period` (e.g. `MONTHLY`) | the **durable, idempotent, append-only ledger**, summed over the current period (your `accumulator_usage_provider`) | recompute = **re-SUM the ledger for the period** (NOT a live count — past consumption is real) | **fail-CLOSED / keep-deny** — a ledger-backed meter must never let spend blow past the cap | `sandbox.monthly_cost` (USD), API-calls/month, tokens/month |
| **`rate`** | **Count within a sliding window** (throughput). | `window_seconds` (e.g. `3600`) | the **TTL'd window counter itself** | self-heals when the window rolls; no external recount needed | n/a (the window is the truth) | `requests_per_hour` |

`precision` (decimal places) applies to any type but matters for money accumulators (`precision=2` → dollars.cents).

## The core distinction: does the resource RELEASE, ACCRUE, or RATE-limit?
- **It goes up AND comes back down** as things start and stop → **`gauge`.** (5 sandboxes running now; stop one → 4.) Truth
  is "how many exist right now", so it is *derived by counting live things* and is *fail-open* (never block on a stale count).
- **It only goes up until the period resets** → **`accumulator`.** ($10.68 spent this month; stopping a sandbox does NOT
  un-spend last week's cost.) Truth is *the sum of an immutable ledger*, so you **do NOT recount from live state** — you
  re-sum the ledger — and it is *fail-closed* (never overspend on a source outage).
- **It limits how often, per time window** → **`rate`.** (100 requests/hour.) Truth is the window counter, which expires.

Getting this wrong is the classic drift bug: a **gauge** reconciled from a ledger, or a **meter** recounted from live
state, produces wrong numbers. **The type is a correctness contract — never mix strategies on one resource.**

## How to USE it
1. **Declare** the type per resource in `quota-config.json`; the library validates that `rate` has `window_seconds` and
   `accumulator` has `reset_period`.
2. **Wire the truth source that matches the type** into `setup_quota`:
   - `gauge` → `observed_usage_provider(org)` returning the **live count** per gauge resource (for infra-backed resources
     this is a real-state count — see the sandbox real-state reconciler).
   - `accumulator` → `accumulator_usage_provider(org)` returning the **period-ledger sum** per accumulator resource.
   - `rate` → nothing to wire (the window counter is truth).
3. The library then dispatches on the type automatically: **recount-before-deny** (a gauge recounts live / a meter re-sums
   the ledger before ever denying), **read-repair**, and **`reconcile_org`** all use the type's source of truth and the
   type's fail direction. You do not special-case this in your handlers — declare the type + wire the provider.

## Not a counter_type
**Credits / prepaid balance** (grants − debits) is **billing's** model, not an ab0t-quota `counter_type`. Do not model a
credit balance as an accumulator; use billing's reserve/commit/refund. ab0t-quota answers "is this allowed?" (gauge/rate/
accumulator vs a tier limit); billing answers "can this be afforded?" (balance).

## One-line rule
**Choose the `counter_type` that matches how the resource behaves in reality — releases (`gauge`), accrues (`accumulator`),
or rate-limits (`rate`) — and wire the matching truth source. The type decides truth, reconciliation, and fail-direction.**
