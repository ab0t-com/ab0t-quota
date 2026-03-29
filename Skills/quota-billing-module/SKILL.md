---
name: quota-billing-module
description: Work with the billing service's self-contained quota tier module at billing/output/app/modules/quota/. Use when modifying tier CRUD endpoints, adding new tier features, changing the DynamoDB store (ab0t_quota_state table), updating override logic, fixing tier history or audit trail issues, wiring payment webhook tier sync, debugging tier read/write failures, or extending the module for new commercial features. The module is designed to be extractable to its own service.
---

# Billing Quota Module

Self-contained module at `billing/output/app/modules/quota/`. Manages org tiers, overrides, and history. Billing owns tiers because it's the commercial state service. Auth stays lean (identity only).

## Module Structure

```
modules/quota/
├── __init__.py       # Public API: quota_router, QuotaTierService, models
├── config.py         # KNOWN_TIERS, plan→tier mapping, env-driven settings
├── models.py         # SetTierRequest, TierResponse, OverrideDetail, etc.
├── service.py        # QuotaTierService — business logic, cache invalidation
├── store.py          # DynamoDB CRUD with TransactWriteItems (atomic writes)
├── router.py         # 7 FastAPI endpoints, auth via BillingReader/Admin/PlatformAdmin
└── dependencies.py   # init_quota_module(), get_quota_tier_service()
```

Only import from `__init__.py` outside the module. Everything else is internal.

## Endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/{org_id}/tier` | BillingReader | Read org tier (consumer services call this) |
| PUT | `/{org_id}/tier` | BillingAdmin | Set tier (payment webhook calls this) |
| GET | `/{org_id}/tier/limits` | BillingReader | Tier limits merged with overrides |
| GET | `/{org_id}/tier/history` | BillingAdmin | Audit trail (newest first) |
| GET | `/{org_id}/tier/overrides` | BillingAdmin | List per-org overrides |
| PUT | `/{org_id}/tier/overrides/{key}` | PlatformAdmin | Set override |
| DELETE | `/{org_id}/tier/overrides/{key}` | PlatformAdmin | Remove override |

## DynamoDB Patterns

Table: `ab0t_quota_state`. All operations are single-partition on `PK=ORG#{org_id}`.

See [references/dynamodb-patterns.md](references/dynamodb-patterns.md) for SK patterns, TransactWriteItems usage, and the serialize/deserialize approach using boto3 TypeSerializer.

## Key Design Decisions

- **TransactWriteItems** for tier+history and override+history — both succeed or both fail
- **Idempotent tier set** — same tier_id skips write (no duplicate history on webhook retries)
- **boto3 TypeSerializer/TypeDeserializer** — correct type handling for all DynamoDB types
- **Exceptions propagate from store** — service returns 503, not silent fallback to "free"
- **resource_key validated** — `#` rejected to prevent SK collision
- **limit validated** — `ge=0` in model, service rejects negative values

## Modifying This Module

- Add new endpoints: edit `router.py`, use existing auth aliases from `...auth`
- Add new DynamoDB operations: edit `store.py`, use `_serialize_item`/`_deserialize_item`
- Add new config: edit `config.py`, add env var with `os.getenv`
- Wire into main.py: already done — `init_quota_module()` in lifespan, `quota_router` in include_router

Read [references/extending.md](references/extending.md) for patterns when adding features.
