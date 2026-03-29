# Quota System Health Checks

## Full Health Check Script

Run from a machine with Redis and AWS access:

```bash
#!/bin/bash
set -e

echo "=== Quota System Health Check ==="

# 1. Redis
echo ""
echo "--- Redis ---"
REDIS_URL="${QUOTA_REDIS_URL:-redis://localhost:6379/0}"
if redis-cli -u "$REDIS_URL" PING | grep -q PONG; then
    echo "OK: Redis reachable"
    COUNTER_COUNT=$(redis-cli -u "$REDIS_URL" KEYS "quota:*:gauge" 2>/dev/null | wc -l)
    RATE_COUNT=$(redis-cli -u "$REDIS_URL" KEYS "quota:*:rate" 2>/dev/null | wc -l)
    ACC_COUNT=$(redis-cli -u "$REDIS_URL" KEYS "quota:*:acc:*" 2>/dev/null | wc -l)
    echo "  Gauge counters: $COUNTER_COUNT"
    echo "  Rate counters: $RATE_COUNT"
    echo "  Accumulator counters: $ACC_COUNT"
else
    echo "FAIL: Redis unreachable at $REDIS_URL"
fi

# 2. DynamoDB
echo ""
echo "--- DynamoDB ---"
TABLE="${QUOTA_DYNAMODB_TABLE:-ab0t_quota_state}"
STATUS=$(aws dynamodb describe-table --table-name "$TABLE" --query 'Table.TableStatus' --output text 2>/dev/null || echo "NOT_FOUND")
if [ "$STATUS" = "ACTIVE" ]; then
    echo "OK: Table $TABLE is ACTIVE"
    ITEM_COUNT=$(aws dynamodb describe-table --table-name "$TABLE" --query 'Table.ItemCount' --output text)
    echo "  Items: $ITEM_COUNT"
else
    echo "FAIL: Table $TABLE status: $STATUS"
fi

# 3. Tier assignments
echo ""
echo "--- Tier Assignments ---"
TIER_COUNT=$(aws dynamodb scan --table-name "$TABLE" \
  --filter-expression "SK = :sk" \
  --expression-attribute-values '{":sk":{"S":"TIER"}}' \
  --select COUNT --query 'Count' --output text 2>/dev/null || echo "0")
echo "  Orgs with tier set: $TIER_COUNT"

# 4. Overrides
echo ""
echo "--- Active Overrides ---"
OVERRIDE_COUNT=$(aws dynamodb scan --table-name "$TABLE" \
  --filter-expression "begins_with(SK, :prefix)" \
  --expression-attribute-values '{":prefix":{"S":"OVERRIDE#"}}' \
  --select COUNT --query 'Count' --output text 2>/dev/null || echo "0")
echo "  Active overrides: $OVERRIDE_COUNT"

# 5. Service endpoint
echo ""
echo "--- Service Endpoint ---"
QUOTA_URL="${QUOTA_ENDPOINT:-http://localhost:8020/api/quotas/tiers}"
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$QUOTA_URL" 2>/dev/null || echo "000")
if [ "$HTTP_CODE" = "200" ]; then
    echo "OK: Quota tiers endpoint responding ($QUOTA_URL)"
else
    echo "FAIL: Quota tiers endpoint returned $HTTP_CODE ($QUOTA_URL)"
fi

echo ""
echo "=== Health Check Complete ==="
```

## Drift Detection

Compare Redis gauge counters vs actual DynamoDB resource counts:

```python
async def check_drift(db, redis, registry):
    """Compare Redis counters vs actual resource counts."""
    drift_report = []
    # For each org with gauge counters
    keys = await redis.keys("quota:*:sandbox.concurrent:gauge")
    for key in keys:
        org_id = key.decode().split(":")[1]
        redis_count = float(await redis.get(key) or 0)
        actual = len(await db.list_user_sandboxes_by_org(org_id, status="running"))
        if abs(redis_count - actual) > 0:
            drift_report.append({
                "org_id": org_id,
                "resource": "sandbox.concurrent",
                "redis": redis_count,
                "actual": actual,
                "drift": redis_count - actual,
            })
    return drift_report
```
