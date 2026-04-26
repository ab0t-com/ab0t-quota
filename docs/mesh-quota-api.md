# Public Mesh Quota API — Bridge Mode

> Public surface served by the ab0t mesh quota service. Third-party
> consumers use this when they don't want to (or can't) run the
> ab0t-quota library against shared mesh infrastructure.

## Audience

- Third-party companies in different cloud accounts / regions, who
  cannot reach `shared-redis` or `shared-dynamodb` directly.
- Integration partners who want HTTPS-only access to mesh quotas.
- Consumers prototyping who don't want to provision Redis up front.

Internal mesh services with co-located infrastructure should use the
library's engine-local mode instead — sub-5ms enforcement, zero network
hop on the hot path.

## Base URL

```
https://billing.service.ab0t.com/billing/quota/{service_name}/{org_id}/...
```

`service_name` identifies which mesh consumer's catalog to enforce
against. `org_id` is the consumer's customer organization.

## Authentication

Single mesh credential, passed as `X-API-Key` header on every request.

```
X-API-Key: ab0t_sk_live_<your_mesh_key>
```

The same key authenticates catalog publish (PUT `/tier-catalog/...`)
and quota enforcement (POST `/quota/...`). Get one by registering as
a mesh consumer per the onboarding flow (separate doc).

## Lifecycle

1. **Publish your catalog** — the ab0t-quota library does this for you
   on `setup_quota()` startup. If you're integrating without the
   library, PUT your tiers/resources/bundles to
   `/billing/tier-catalog/{service_name}` once at startup.
2. **Enforce** — call the bridge endpoints below from your service code.

## Endpoints

### Pre-flight check (single resource)

```
POST /billing/quota/{service}/{org_id}/check/{resource_key}
       ?user_id=<optional>
       &increment=<float, default 1.0>
```

Body: empty.

Response 200:
```json
{
  "decision": "allow" | "allow_warning" | "deny" | "unlimited",
  "resource_key": "thing.concurrent",
  "current": 3.0,
  "requested": 1.0,
  "limit": 5.0,
  "remaining": 1.0,
  "utilization": 0.6,
  "tier_id": "starter",
  "tier_display": "Starter",
  "severity": "info" | "warning" | "critical" | "exceeded",
  "message": "<human-readable>",
  "upgrade_url": "/billing/upgrade",
  "retry_after": null,
  "denied_level": null
}
```

Recommended: treat `decision in ("allow", "allow_warning", "unlimited")`
as proceed; on `deny`, return 429 to your end user using the response
body as the error payload.

### Pre-flight check (resource bundle)

```
POST /billing/quota/{service}/{org_id}/check-bundle/{bundle_name}
       ?user_id=<optional>
```

Returns a `QuotaBatchResult` with per-resource results. `allowed=false`
if any resource is denied.

### Increment counter (after successful create)

```
POST /billing/quota/{service}/{org_id}/increment/{resource_key}
       ?user_id=<optional>
       &delta=<float, default 1.0>
       &idempotency_key=<optional, dedupes for 24h>
```

Response 200:
```json
{ "resource_key": "thing.concurrent", "new_value": 4.0 }
```

Use a stable `idempotency_key` (e.g. your resource ID) so retries don't
double-count.

### Decrement counter (on resource teardown)

```
POST /billing/quota/{service}/{org_id}/decrement/{resource_key}
       ?user_id=<optional>
       &delta=<float, default 1.0>
       &idempotency_key=<optional>
```

Only valid for GAUGE counters. Floors at zero. Rate counters and
accumulators reject decrement.

### Full usage report

```
GET /billing/quota/{service}/{org_id}/usage
```

Response 200:
```json
{
  "org_id": "org-1",
  "tier_id": "starter",
  "tier_display": "Starter",
  "resources": [
    {
      "resource_key": "thing.concurrent",
      "display_name": "Concurrent Things",
      "unit": "things",
      "current": 3,
      "limit": 5,
      "utilization": 0.6,
      "severity": "info",
      "has_override": false,
      "counter_type": "gauge"
    }
  ],
  "warnings_count": 0,
  "exceeded_count": 0,
  "timestamp": "2026-04-25T12:34:56Z"
}
```

## Errors

| Status | Reason | Body |
|---|---|---|
| 400 | Unknown resource_key for this service | `{"detail": "Unknown resource: ..."}` |
| 400 | Decrement called on non-gauge counter | `{"detail": "Cannot decrement..."}` |
| 401 | Missing or invalid X-API-Key | `{"detail": "Unauthenticated"}` |
| 403 | Key valid but not authorized for this org | `{"detail": "Forbidden"}` |
| 404 | No catalog published for service | `{"detail": "No bridge-mode catalog published for service '...'"}` |
| 429 | Rate-limited (this API has its own quotas) | Standard quota response body |
| 503 | Bridge engine cannot be built (catalog corrupt, Redis down) | `{"detail": "..."}` |

The 429 case is interesting: the quota service has its own quotas. To
prevent a single misbehaving consumer from melting the bridge, requests
to `/quota/*` are rate-limited per `service_name` (1000 req/min default;
higher tiers get more). Hit your own quota and you'll get a 429 — at
which point you should back off, batch, or migrate to BYO-Redis mode.

## Performance

Expected latency p50 / p99:

| Region match | p50 | p99 |
|---|---|---|
| Same AWS region | ~5ms | ~25ms |
| Cross-region (US ↔ EU) | ~80ms | ~200ms |
| Cross-cloud (AWS ↔ GCP) | ~100ms | ~300ms |

For high-frequency operations (rate-limit middleware, hot-path checks),
this latency is unacceptable. Use the library's engine-local mode or
BYO-Redis mode instead. Bridge mode is for low-volume per-org checks
and prototyping.

## Idempotency

All counter writes (`increment`, `decrement`) accept an
`idempotency_key`. Same key within 24h is a no-op (returns the existing
value). Use a stable identifier from your resource lifecycle (e.g. the
resource UUID) so SDK retries and replays don't double-count.

## Catalog publish (separate, infrequent)

```
PUT /billing/tier-catalog/{service_name}
X-API-Key: ab0t_sk_live_...

{
  "tiers": [...],
  "resources": [...],
  "resource_bundles": {...}
}
```

See `quota-config.example.json` in the library for the full schema.
The ab0t-quota library does this for you on startup; if you're not
using the library, publish on every restart (idempotent — fully
replaces the previous catalog).

## Rate-limit and overage policy

Bridge-mode requests count against your service's own
`api.requests_per_hour` quota (defined in your tier definitions). At
90% utilization billing returns `decision: "allow_warning"` with a
warning banner; at 100% returns 429.

Cap recovery: rate counters auto-expire on a sliding window (default
1 hour). No human action needed to lift a 429 — wait it out.

## Cache and consistency

- Tier reads inside billing are direct DynamoDB GetItem (no cache).
  Tier changes via `PUT /billing/{org_id}/tier` are visible to the
  next quota check immediately.
- Override reads are direct DynamoDB; same immediate visibility.
- Counter writes are atomic (Redis INCRBYFLOAT or sorted-set ZADD).
  No cross-bridge consistency issue: the counter IS the source of truth.

## Versioning

Endpoints under `/billing/quota/...` are mesh-internal; expect
breaking changes during v2 development. The `/v1/quota/...` namespace
will be added when the API is frozen for third-party consumption.

## Comparison to library modes

| Concern | Library engine-local | Library BYO-Redis | Bridge mode (this API) |
|---|---|---|---|
| Consumer needs Redis | Yes (shared) | Yes (own) | No |
| Consumer needs DynamoDB | Yes (shared) | No | No |
| Consumer needs library install | Yes | Yes | Optional (HTTPS works without it) |
| Hot-path latency | <5ms | <5ms | 5-300ms (region-dependent) |
| Right for | Internal mesh services | Third parties willing to provision Redis | Prototypes, low-volume third parties |

## See also

- `docs/onboarding.md` — registering as a mesh consumer (TODO)
- `quota-config.example.json` — full config schema
- `dev/ARCHITECTURE_LEARNINGS_20260425.md` — design rationale for the three modes
