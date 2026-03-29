---
name: quota-troubleshooting
description: Diagnose and fix ab0t-quota issues in production. Use when debugging false denials (user blocked but shouldn't be), counter drift (gauge shows wrong count), tier not updating after payment, Redis connection issues, DynamoDB persistence failures, reconciliation problems, alert spam, or customers reporting incorrect quota limits. Also use for operational tasks like manual counter reset, forced tier change, emergency kill switch, and quota system health checks.
---

# Quota Troubleshooting

## Quick Diagnosis

**"User is getting 429 but shouldn't be"**
1. Check counter: `redis-cli GET quota:{org_id}:{resource_key}:gauge`
2. Check actual count: query DynamoDB for real resource count
3. If counter > actual: drift occurred → [Counter Drift](#counter-drift)
4. If counter correct but limit wrong: check tier → [Tier Issues](#tier-issues)
5. If tier correct but override expected: check override → [Override Issues](#override-issues)

**"User paid but still on free tier"**
→ [Tier Not Updating](#tier-not-updating)
Check chain: Stripe webhook → payment-service → billing PUT tier → DynamoDB → Redis cache

**"Billing service returning 503 on tier endpoints"**
→ [Startup Failures](#startup-failures)
The quota module may have failed to initialize (DynamoDB unreachable).

**"Quota engine not initialized"**
→ [Startup Failures](#startup-failures)

## Counter Drift

Symptoms: Gauge counter shows higher value than actual resources. User appears at limit but has fewer sandboxes than the counter says.

Cause: A resource was terminated but the decrement failed (crash, network error, bug in termination path).

### Diagnose

```bash
# Check Redis counter
redis-cli GET quota:org-123:sandbox.concurrent:gauge
# → "5"

# Check actual sandboxes in DynamoDB
# (run from sandbox-platform container)
python -c "
import asyncio
from app.database import SandboxDatabase
db = SandboxDatabase()
asyncio.run(db.initialize_tables())
sandboxes = asyncio.run(db.list_user_sandboxes('user-id', status='running'))
print(f'Actual running: {len(sandboxes)}')
"
# → "Actual running: 3"
```

### Fix

```bash
# Manual counter reset via admin endpoint
curl -X POST http://localhost:8020/api/admin/quota/reset \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -d '{"org_id": "org-123", "resource_key": "sandbox.concurrent", "new_value": 3, "reason": "drift fix"}'
```

Or directly in Redis:
```bash
redis-cli SET quota:org-123:sandbox.concurrent:gauge 3
```

### Prevent

- Ensure ALL termination paths (stop, delete, crash cleanup, idle timeout) call `quota.on_sandbox_terminated()`
- Reconciliation worker runs every 15 minutes (task 6.1.1)
- User-facing "Recalculate usage" button (task 6.1.3)

## Tier Issues

### Tier Not Updating

Symptoms: User paid, Stripe shows active subscription, but quota still enforces free tier limits.

Check chain:
1. **Stripe webhook received?** Check payment-service logs for webhook event
2. **Billing tier set called?** Check payment-service logs for `billing_service.set_org_tier()`
3. **Billing PUT succeeded?** Check billing-service logs for `quota_tier_changed` event
4. **DynamoDB updated?** `aws dynamodb get-item --table ab0t_quota_state --key '{"PK":{"S":"ORG#org-123"},"SK":{"S":"TIER"}}'`
5. **Redis cache stale?** `redis-cli GET quota:tier:org-123` — if wrong, delete it: `redis-cli DEL quota:tier:org-123`
6. **Consumer service caching?** Each consumer caches tier for 5min. Wait or restart service.

### Force Tier Change

```bash
# Via billing-service API (preferred — billing owns tiers)
curl -X PUT https://billing.service.ab0t.com/billing/org-123/tier \
  -H "X-API-Key: $BILLING_ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"tier_id": "pro", "reason": "manual fix"}'

# Direct DynamoDB (emergency — bypasses audit trail in billing)
aws dynamodb put-item --table ab0t_quota_state --item \
  '{"PK":{"S":"ORG#org-123"},"SK":{"S":"TIER"},"tier_id":{"S":"pro"},"changed_by":{"S":"manual-fix"},"changed_at":{"S":"2026-03-27T00:00:00Z"}}'

# Clear Redis cache on all consumer services
redis-cli DEL quota:tier:org-123
```

## Override Issues

Symptoms: Enterprise customer should have custom limits but getting default tier limits.

```bash
# Check if override exists in DynamoDB
aws dynamodb get-item --table ab0t_quota_state \
  --key '{"PK":{"S":"ORG#org-123"},"SK":{"S":"OVERRIDE#sandbox.concurrent"}}'

# Check if override is expired
# Look at expires_at field — if past, it's being ignored
```

## Startup Failures

Symptoms: Service logs "Quota engine not initialized" or quota endpoints return errors.

Common causes:
- **Redis unreachable**: Check `QUOTA_REDIS_URL` or `REDIS_URL` env var, verify Redis is running
- **DynamoDB permission denied**: Check IAM policy has access to `ab0t_quota_state` table
- **Config file parse error**: Check `QUOTA_CONFIG_PATH` or `quota-config.json` for JSON syntax errors

The engine is designed to be non-fatal: if DynamoDB persistence fails, Redis-only mode continues. If Redis fails, the engine doesn't start and enforcement is skipped (fail-open).

## Emergency Kill Switch

Disable all quota enforcement immediately:

```bash
# Option 1: Environment variable (requires restart)
QUOTA_ENFORCEMENT_ENABLED=false

# Option 2: Config file (requires restart)
# Set enforcement.global_kill_switch = true in quota-config.json

# Option 3: Redis flag (no restart, checked on every request if implemented)
redis-cli SET quota:global:kill_switch 1
```

Counter tracking continues even when enforcement is off (for dashboards and reporting).

## Health Check

See [references/health-checks.md](references/health-checks.md) for a comprehensive health check script.

Quick checks:
```bash
# Redis connectivity
redis-cli PING  # → PONG

# DynamoDB table exists
aws dynamodb describe-table --table ab0t_quota_state --query 'Table.TableStatus'  # → "ACTIVE"

# Engine responding
curl http://localhost:8020/api/quotas/usage -H "Authorization: Bearer $TOKEN" | jq '.tier_id'

# Counter sanity
redis-cli KEYS "quota:*:gauge" | head -20
```
