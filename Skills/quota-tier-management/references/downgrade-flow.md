# Downgrade & Grace Period Flow

## Trigger: Subscription Cancelled or Payment Failed

```
subscription.deleted → immediate tier change to "free"
                     → grace period starts (7 days)
                     → email org admin

invoice.payment_failed → 3-day retry window
                       → if still unpaid → tier change to "free"
                       → grace period starts (7 days)
```

## During Grace Period

- Existing resources **keep running** (no immediate termination)
- New resource creation **blocked** if usage exceeds new tier limits
- Dashboard shows "Your plan was downgraded" banner with days remaining
- Quota checks return the **new** tier limits but **allow** existing over-limit resources to continue

## After Grace Period

Background worker runs daily:
1. Query all orgs where `tier_changed_at + grace_period < now`
2. For each org, compare current usage vs tier limits
3. Stop idle resources that exceed new limits (least recently used first)
4. Email org admin: "These resources were stopped: ..."

## Implementation

Store grace period state in DynamoDB:

```
PK: ORG#{org_id}
SK: DOWNGRADE
Data:
  previous_tier: "pro"
  new_tier: "free"
  downgraded_at: "2026-03-27T00:00:00Z"
  grace_expires_at: "2026-04-03T00:00:00Z"
  notified: true
```

Quota engine check:
```python
# During grace period, allow existing resources but block new creation
if is_in_grace_period(org_id):
    if action == "create" and would_exceed_new_tier:
        return DENY with message "Your plan was downgraded. Stop existing resources to create new ones."
    else:
        return ALLOW  # existing resources continue
```
