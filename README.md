# ab0t-quota — Product & Market Positioning

## What This Is

ab0t-quota is the usage control and monetization backbone of the ab0t platform. It decides who can use what, how much, and when to ask them to pay more. Every sandbox created, every API call made, every GPU spun up goes through this system.

Without it, every customer gets unlimited free compute. With it, the platform has a monetization engine.

## Customer Segments

### Evaluators (Free Tier)
**Who:** Individual developers testing the platform. No payment method.
**Volume:** 60-70% of signups. Most never convert.
**Behavior:** Create 1-2 sandboxes, explore for 30 minutes, leave.
**What they need from quota:**
- Zero friction to start. No credit card wall.
- Clear limits that feel generous enough to evaluate (1 sandbox, 2 browser sessions).
- When they hit a wall, a helpful message — not an error. "Upgrade to Starter for 5 sandboxes" is a conversion prompt, not a punishment.
- Feature discovery: they should see GPU/Desktop as greyed-out tier-locked features, not hidden. This plants the seed for "I'd pay for that."

**Critical UX moment:** The first 429 response. This is either a conversion event or a churn event. The `message` and `upgrade_url` in the response are the highest-ROI copy on the platform.

### Small Teams (Starter)
**Who:** 5-25 person teams. Have a payment method. Building with the platform daily.
**Volume:** 20-25% of orgs. Growing spend.
**Behavior:** 3-10 concurrent sandboxes. Mix of browser and compute. Cost-conscious.
**What they need from quota:**
- Predictable costs. The monthly spending cap (`sandbox.monthly_cost`) prevents bill shock.
- Usage visibility. The dashboard usage bars tell them where they stand before they hit a wall.
- Per-user fairness. Without per-user sub-quotas, one intern running 5 sandboxes blocks the whole team. The `per_user_limit` field in TierLimits solves this.
- Self-service upgrade path. When they outgrow Starter, the upgrade should be one click — not a sales call.

**Critical UX moment:** Month-end. If they hit their $100 spend limit on the 25th, they're blocked for 5 days. The warning at 80% ("You're using $82 of $100") gives them time to upgrade or clean up.

### Production Teams (Pro)
**Who:** Companies shipping product built on ab0t sandboxes. Sandboxes are customer-facing.
**Volume:** 5-10% of orgs. 60% of revenue.
**Behavior:** 10-50 concurrent sandboxes. GPU workloads. API-heavy. Latency-sensitive.
**What they need from quota:**
- Speed. Quota checks must be invisible (<5ms). Redis-only hot path delivers this.
- Reliability. No false denials. If the limit is 25, they must be able to reliably get 25. Atomic Redis counters with idempotency keys prevent double-counting.
- Burst headroom. During a live demo, hitting a hard wall is a deal-breaker. The `burst_allowance` soft cap lets them temporarily exceed the limit with a warning instead of a denial.
- API access to quota state. They build internal dashboards on top of `/api/quotas/usage`. The `QuotaUsageResponse` model is their contract.

**Critical UX moment:** The live demo. A Pro customer demonstrating their product to their customer, using ab0t sandboxes underneath. A 429 here loses them a deal. Burst allowance prevents this.

### Enterprise (Custom)
**Who:** Large companies. Negotiated contracts. Dedicated support.
**Volume:** 1-3% of orgs. 30% of revenue.
**Behavior:** Hundreds of sandboxes. Multiple teams. Compliance requirements.
**What they need from quota:**
- Custom limits that differ from any standard tier. The `QuotaOverride` system stores per-org overrides with audit trails (who set it, why, when it expires).
- Team-level sub-quotas. The `per_user_limit` field can be extended to per-team in future iterations.
- Audit trail. Every tier change, override creation, and quota denial is logged with structured events.
- Grace periods. If procurement delays a renewal, they get 7 days before anything changes. The downgrade flow grandfathers existing resources.

**Critical UX moment:** Contract renewal. If the enterprise customer's subscription lapses due to internal procurement delays, their 200 running sandboxes must not instantly die. The 7-day grace period with email notification gives their finance team time to pay.

## Market Context

### What Competitors Do

**Vercel:** Soft limits with email warnings before hard cutoff. Usage-based pricing with spend alerts. Self-service tier upgrades.

**AWS Service Quotas:** Dashboard showing all limits. "Request increase" button. Small increases auto-approved. Large increases require support ticket.

**Stripe Billing:** Real-time metering. Customers never surprised. Usage → invoice → charge is seamless.

**CodeSandbox/Gitpod:** Tier-gated features (e.g. GPU only on Pro). Usage bars in dashboard. Clear upgrade CTAs.

### Where ab0t-quota Positions

ab0t-quota combines:
- **Vercel's** warning-before-cutoff pattern (80%/95% thresholds with progressive messaging)
- **AWS's** self-service increase requests (QuotaIncreaseRequest model)
- **Stripe's** real-time metering (Redis counters, <5ms reads)
- **Gitpod's** tier-gated features (TierConfig.features set)

What's differentiated:
- **Burst allowance** — soft cap above the limit for Pro customers (competitors hard-deny)
- **Per-user sub-quotas** — prevent one team member exhausting the org (competitors don't do this)
- **Cross-service consistency** — same engine, same 429 body, same tier system across sandbox, resource, auth, API gateway (competitors have per-service quota systems that don't talk to each other)

## Monetization Flow

```
Free tier → user evaluates → hits limit → sees upgrade CTA
    │
    ▼
Stripe checkout → payment_intent.succeeded
    │
    ▼
Payment service → auth service: set org tier = "starter"
    │
    ▼
JWT carries org_tier="starter" → all services enforce new limits
    │
    ▼
User grows → hits Starter limits → sees Pro CTA
    │
    ▼
Upgrade → higher limits, GPU access, burst allowance
    │
    ▼
Enterprise needs → "Contact sales" → custom overrides
```

Every 429 response is a conversion touchpoint. The copy matters:

**Don't say:** `{"error": "quota_exceeded", "resource": "sandbox.concurrent"}`

**Do say:** `{"message": "You've reached the maximum of 5 sandboxes on the Starter plan. Stop an existing sandbox to free up a slot, or upgrade to Pro for up to 25.", "upgrade_url": "/billing/upgrade"}`

## Key Metrics to Track

| Metric | What It Tells You | Target |
|---|---|---|
| Denial rate (% of checks denied) | Are limits too tight? | <5% for paid tiers |
| Free→Starter conversion within 7 days of first denial | Is the 429 CTA working? | >3% |
| Time from denial to upgrade | How long do they deliberate? | <24 hours |
| Burst allowance utilization | Are Pro customers hitting limits often? | <10% of checks in burst zone |
| Counter drift incidents/month | Is the system reliable? | 0 after reconciliation worker ships |
| p99 latency of quota checks | Is it invisible to users? | <5ms |

## Pricing Page Content (Suggested)

| | Free | Starter ($29/mo) | Pro ($99/mo) | Enterprise |
|---|---|---|---|---|
| Concurrent sandboxes | 1 | 5 | 25 | Custom |
| Browser sessions | 2 | 10 | 50 | Unlimited |
| Desktop sessions | -- | 5 | 25 | Unlimited |
| GPU instances | -- | 1 | 5 | Custom |
| Monthly spend cap | $10 | $100 | $1,000 | Custom |
| API requests/hour | 1,000 | 10,000 | 50,000 | 100,000 |
| Team members | 5 | 25 | 100 | 10,000 |
| SSO | -- | -- | -- | Yes |
| Priority support | -- | -- | Yes | Dedicated |
| Burst allowance | -- | -- | Yes | Yes |

"--" means feature not available on that tier (rendered as greyed-out in UI via `TierConfig.features`).

## What's Not Solved Yet

1. **Overage billing** — today it's hard deny at the limit. Future: charge $X per unit above the limit instead of denying. Requires billing-service integration.
2. **Usage-based pricing** — today tiers are fixed monthly. Future: pay-per-sandbox-hour metered billing. Requires accumulator counter → billing invoice pipeline.
3. **Team-level quotas** — today per-user sub-quotas within an org. Future: per-team sub-quotas for enterprises with departments.
4. **Self-serve plan builder** — today tiers are fixed. Future: enterprise customers configure their own limits within a budget.
