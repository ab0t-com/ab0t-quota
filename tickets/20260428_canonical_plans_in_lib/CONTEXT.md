# Context — everything an implementer should know up front

Reference doc. Written 2026-04-28 against ab0t-quota v0.2.4 + sandbox-platform commit `905c2da`.

## Repo + version state

| Repo | Path | Current version |
|---|---|---|
| ab0t-quota lib | `/home/ubuntu/infra/infra/code/shared/ab0t-quota` | 0.2.4 |
| sandbox-platform (primary consumer) | `/home/ubuntu/infra/infra/code/resource/output/sandbox-platform` | n/a (service) |
| payment service | `/home/ubuntu/infra/infra/code/payment/output` | n/a (service) |

ab0t-quota recent versions:
- v0.2.0 — `setup_quota` drop-in API + bridge mode + sandbox migration
- v0.2.1 — per-upstream API key support
- v0.2.2 — fix BillingServiceClient + PaymentServiceClient URL mismatches
- v0.2.3 — fix billing/payment response model schema mismatches
- v0.2.4 — typed responses + descriptions on checkout/topup routes (current)

User publishes via `push.sh` themselves — never run git commit/tag/push for ab0t-quota.

## How tiers are loaded today (lib-side)

`ab0t_quota/config.py:55-83` — `load_tiers(config) -> dict[str, TierConfig]`:

```python
for tier_data in config["tiers"]:
    limits = {...}  # parsed from tier_data["limits"]
    tiers[tier_data["tier_id"]] = TierConfig(
        tier_id=tier_data["tier_id"],
        display_name=tier_data.get("display_name", ...),
        description=tier_data.get("description"),
        sort_order=tier_data.get("sort_order", 0),
        limits=limits,
        features=set(tier_data.get("features", [])),
        upgrade_url=tier_data.get("upgrade_url"),
        default_per_user_fraction=tier_data.get("default_per_user_fraction"),
    )
```

Explicit field-by-field copy. Unknown JSON keys are silently dropped before Pydantic sees them. So adding `pricing` or `plans[]` to JSON today does not break the loader, just gets ignored.

## TierConfig schema (Pydantic BaseModel)

`ab0t_quota/models/core.py:192-...`:
- `tier_id: str` (regex `^[a-z][a-z0-9_-]*$`)
- `display_name: str`
- `description: Optional[str]`
- `sort_order: int = 0`
- `limits: dict[str, TierLimits]`
- `features: set[str]`
- (plus `upgrade_url`, `default_per_user_fraction` per the loader)
- `model_config` not explicitly set — defaults to Pydantic strict (extra="ignore" in v2). Adding fields via `**tier_data` would NOT raise, but loader doesn't do that today.

## DEFAULT_TIERS

`ab0t_quota/tiers.py:9` — hardcoded fallback when no config exists. Used by:
- `ab0t_quota/engine.py:32` — engine init
- `sandbox-platform/app/main.py:2848` — fallback in `/api/quotas/tiers` endpoint

## sandbox-platform's quota-config.json shape

Top-level keys:
```
$comment_resource_bundles, $comment_resources, alerts, billing_integration,
enforcement, pricing, resource_bundles, resources, service_name, storage,
tier_provider, tiers
```

Sample tier (free):
```json
{
  "tier_id": "free",
  "display_name": "Free",
  "description": "For experimentation and evaluation",
  "sort_order": 0,
  "initial_credit": 10.00,
  "features": ["basic_sandboxes", "browser_sessions"],
  "upgrade_url": "/billing/upgrade",
  "limits": {
    "sandbox.concurrent": 1,
    "sandbox.monthly_cost": 10.00,
    "sandbox.gpu_instances": 0,
    "sandbox.browser_sessions": 2,
    "sandbox.desktop_sessions": 0
  }
}
```

Note: `initial_credit` is already a non-standard field the loader silently drops. It's read directly via `jq` in `ab0t_quota/quota.py:299-312` (sandbox-platform's local quota module — different file). This is precedent for the "lib loader ignores it but consumer reads it via jq" pattern. Don't repeat that mistake — make the new field a first-class field the loader returns.

## Current seed_plans.sh — what it does, what to replicate

Path: `sandbox-platform/scripts/seed_plans.sh` (203 lines)

Inputs:
- `quota-config.json` — for tier name, description, sort_order, features, limits
- Hardcoded bash assoc-arrays: `MONTHLY_PRICES=( starter=29 pro=99 )`, `ANNUAL_PRICES=( starter=290 pro=990 )`
- `setup/scripts/service-client-setup/credentials/payment-client.json` — API key + identity for payment-service

Payment-service endpoints called:
- `GET /checkout/{org}/plans?include_prices=true` — list existing plans (idempotency check by name)
- `POST /plans/{org}/` — create plan (body has name, description, visibility, sort_order, features, metadata.tier_id, sync_to_stripe)
- `POST /plans/{org}/{plan_id}/prices` — create price (body has plan_id, amount, currency, type, interval, metadata.label)

Idempotency is by **plan name match** today (case-sensitive). When porting to lib, suggest a stable `plan_id` or external ID for cleaner matching.

Bug we know about: env var name mismatch — script reads `AUTH_SERVICE_URL` but docs/error messages reference `AUTH_URL`. User hit this 2026-04-28.

## Payment service org identity

For sandbox-platform as a payment-service consumer:
- slug `payment-customer-sandbox-platform`
- org_id in PROD auth: `1290d9d3-1e56-48f7-a6e8-36e8d5cc55ec`
- API key permissions (validated against prod auth 2026-04-28): `payment.read`, `payment.write`, `payment.create.plans`, `payment.create.payments`, `payment.create.subscriptions`, `payment.admin`, `payment.cross_org` and various read/write subsets

The library should accept the consumer's payment-customer org_id as a function arg; do NOT hardcode `1290d9d3-…`.

## Stripe integration

ab0t-quota does not call Stripe directly. Payment-service handles Stripe — consumer sets `sync_to_stripe: true` on plan create and payment-service mints the Stripe Product + Price. Stripe price IDs come back in the price-create response.

Today there is **no** map from Stripe price ID → tier ID anywhere in code. The lib's docs say `quota-config.json.billing_integration.stripe_price_to_tier` should hold this, but the field doesn't exist in any actual config file. This is a separate gap — address in the same ticket or split it out.

## Existing API surface that exposes tier info to clients

`sandbox-platform/app/main.py:2840-2866` — `GET /api/quotas/tiers`:

```python
async def get_quota_tiers():
    engine = quota_module.get_engine()
    tier_source = engine._tiers if engine else DEFAULT_TIERS
    tiers = []
    for tier in sorted(tier_source.values(), key=lambda t: t.sort_order):
        tiers.append({
            "tier_id": tier.tier_id,
            "display_name": tier.display_name,
            "description": tier.description,
            "features": list(tier.features),
            "limits": limits,  # built from tier.limits with limit_display strings
            "upgrade_url": tier.upgrade_url,
        })
    return {"tiers": tiers}
```

Hand-rolled dict build. Does NOT pass through new fields. If we add `pricing` or `plans[]` on the lib side and want the API to surface it, this builder needs updating too — OR the lib should expose a typed serialiser the consumer can use directly.

## Where plans are referenced in the runtime today

- `ab0t_quota/billing/router.py:130` — `tier_map = {t["display_name"].lower(): t["tier_id"] for t in config.get("tiers", [])}` — used during checkout to resolve a plan's display name back to a tier_id. Brittle (case-sensitive on names). New `plans[]` should make this a direct `plan_id → tier_id` lookup.
- `ab0t_quota/billing/router.py:298` — `checkout_org = new_org or consumer_org_id`
- `ab0t_quota/billing/router.py:389` — `tier_id = await _resolve_plan_to_tier(plan_id, tier_map, payment, consumer_org_id)` — looks up a plan_id (returned from checkout) and tries to find which tier it represents. New `plans[]` config makes this trivial.
- `ab0t_quota/setup.py:794` — warns if `AB0T_CONSUMER_ORG_ID` not set; "billing router not mounted" if absent

## Existing tests in ab0t-quota

- `tests/test_config.py` — likely covers `load_tiers()` parsing. New `load_plans()` tests go here.
- `tests/test_setup.py` — covers `setup_quota` lifecycle. Auto-sync (Phase 4) tests land here.
- `tests/test_tier_catalog_publish.py` — already publishes the tier catalog to billing on startup. Pattern for sync-on-startup is already established here; reuse it for plan sync.
- `tests/billing/` — billing router tests. `_resolve_plan_to_tier` tests probably here — update when the resolution becomes config-driven.

## env var conventions across the lib

Read by ab0t-quota at runtime:
- `AB0T_MESH_API_KEY` — universal mesh credential
- `AB0T_MESH_BILLING_API_KEY` — per-upstream override (preferred)
- `AB0T_MESH_PAYMENT_API_KEY` — per-upstream override (preferred)
- `AB0T_MESH_BILLING_URL` — per-upstream URL
- `AB0T_MESH_PAYMENT_URL` — per-upstream URL
- `AB0T_CONSUMER_ORG_ID` — single mesh-org identity (NOT per-upstream — known limitation, see comments in `setup.py:760`)

CLI for `python -m ab0t_quota sync-plans` should read these as defaults.

## Constraints / things not to break

1. **Consumers without `plans[]` keep working.** Engine is quota-only for many consumers; they don't sell anything.
2. **`setup_quota()` signature is stable.** Add new kwargs, never reorder or rename.
3. **`AB0T_CONSUMER_ORG_ID` semantics.** Currently single value used for both billing AND payment URL paths. Architecturally wrong (different sub-orgs per provider) but baked in. Don't make it worse. Eventual fix is `AB0T_BILLING_CONSUMER_ORG_ID` + `AB0T_PAYMENT_CONSUMER_ORG_ID` split.
4. **Idempotency.** sync_plans must be safe to re-run any number of times. Existing seed_plans.sh achieves this via `name` match; we should achieve it via `plan_id` (stable, operator-chosen) match.
5. **No deletes.** Removing a plan from config must NOT delete the plan in payment-service — paying customers stay on it.
6. **No `.env` writes.** The lib never writes to operator-owned config files. Even if it generates a Stripe price ID that "should" be cached somewhere, surface it in CLI output and let the operator decide.

## Related work (don't conflict)

- **SSH proxy mesh customer pattern** — `sandbox-platform/tickets/20260427_ssh_proxy_mesh_customer_pattern/` — touches the same setup scripts (`07-register-consumer.sh`). Read first if changing setup tooling.
- **Production audit** — `sandbox-platform/production/AUDIT-REPORT-20260427.md` — flagged `PAYMENT_CONSUMER_ORG_ID` empty in prod env (now resolved). Same area; coordinate.
- **Sandbox-platform main.py:2848 (`/api/quotas/tiers`)** — will need updating in Phase 3 if we want the dashboard to surface plan info. Doesn't have to land in the same PR.

## Helpful data points (verified live during the discussion that produced this ticket)

PROD auth (`auth.service.ab0t.com`):
- sandbox-platform parent org: `9782399a-fbfe-4981-bead-91b5764c0ded` (slug `sandbox-platform`)
- payment-customer sub-org: `1290d9d3-1e56-48f7-a6e8-36e8d5cc55ec` (slug `payment-customer-sandbox-platform`)
- billing-customer sub-org: `05b10ab7-09e1-4b61-8b6b-4c49495eaeb1` (slug `billing-customer-sandbox-platform`)
- end-users org: slug `sandbox-platform-users`

Probe technique that worked: `POST /organizations/{slug}/auth/login` with bogus-but-syntactically-valid creds → 401 = slug exists, 404 = slug doesn't. `GET /login/{slug}` returns HTML containing `window.__AUTH_CONFIG__ = {"orgId": "..."}` — useful for verifying slug→UUID mappings without auth.

## Out-of-band facts that often trip people up

- `quota-config.json` is searched in multiple paths — see `ab0t_quota/config.py:CONFIG_SEARCH_PATHS`. Local-dev consumers sometimes wonder why edits don't apply: wrong file got picked.
- `engine._tiers` is loaded once at startup; runtime config edits don't take effect until restart.
- `_tier_provider.get_tier(org_id)` resolves tier from DDB, NOT from config. Config defines the catalog; DDB stores assignment. A new tier in config doesn't apply to anyone until something `set_tier`s them to it.
- Free tier has `initial_credit: 10.00` in config — granted in `quota.py:319 maybe_apply_initial_credit_for_org`, only fires on first `enforce_user_cost_limit` call (not on signup). Open issue: dashboards show $0 until first sandbox creation.
