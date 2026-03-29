# quota-config.json Schema

## Top-Level Structure

```json
{
  "storage": { ... },
  "tier_provider": { ... },
  "alerts": { ... },
  "enforcement": { ... },
  "reconciliation": { ... },
  "tiers": [ ... ],
  "billing_integration": { ... }
}
```

## storage

| Field | Type | Default | Description |
|---|---|---|---|
| `redis_url` | string | `redis://localhost:6379/0` | Redis connection for hot-path counters |
| `redis_key_prefix` | string | `quota` | Prefix for all Redis keys |
| `dynamodb_table` | string | `ab0t_quota_state` | DynamoDB table for durable state |
| `dynamodb_region` | string | `us-east-1` | AWS region for DynamoDB |
| `persistence_enabled` | bool | `true` | Enable DynamoDB persistence |
| `persistence_sync_interval_seconds` | int | `300` | How often to snapshot Redis → DynamoDB |

## tier_provider

| Field | Type | Default | Description |
|---|---|---|---|
| `type` | string | `jwt` | Provider type: `jwt`, `auth_service`, `static` |
| `jwt_claim_key` | string | `org_tier` | JWT claim that carries tier ID |
| `default_tier` | string | `free` | Fallback when claim is missing |
| `cache_ttl_seconds` | int | `300` | Cache TTL for auth_service provider |

## tiers[] (array)

Each tier object:

| Field | Type | Required | Description |
|---|---|---|---|
| `tier_id` | string | yes | Machine name: `free`, `starter`, `pro`, `enterprise` |
| `display_name` | string | yes | Human-readable: "Starter Plan" |
| `description` | string | no | One-line description for pricing page |
| `sort_order` | int | no | 0=lowest tier, used for UI ordering |
| `features` | string[] | no | Feature flags: `gpu_access`, `sso`, etc. |
| `upgrade_url` | string | no | URL shown in 429 responses |
| `limits` | object | yes | `resource_key` → limit value or limit object |

### Limit values

Simple form (just a number):
```json
"sandbox.concurrent": 5
```

Object form (with thresholds):
```json
"sandbox.concurrent": {
  "limit": 5,
  "warning_threshold": 0.8,
  "critical_threshold": 0.95,
  "burst_allowance": 2,
  "per_user_limit": 3
}
```

- `null` = unlimited
- `0` = feature not available on this tier

## billing_integration

| Field | Type | Description |
|---|---|---|
| `stripe_price_to_tier` | object | Maps Stripe price IDs to tier IDs |
| `downgrade_grace_period_days` | int | Days before over-limit resources are stopped |
| `payment_failure_grace_period_days` | int | Days before tier downgrade on payment failure |

## enforcement

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Master switch — false means log-only (shadow mode) |
| `shadow_mode` | bool | `false` | Log denials but don't block (for rollout) |
| `global_kill_switch` | bool | `false` | Emergency: disable all quota checks |
