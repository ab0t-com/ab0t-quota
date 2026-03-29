# Quota Isolation Model

## Data Isolation

| Layer | Mechanism | Scope |
|-------|-----------|-------|
| DynamoDB | PK = `ORG#{org_id}` | All quota state partitioned by org |
| Redis counters | Key = `quota:{org_id}:{resource_key}:*` | Per-org counter keys |
| Redis tier cache | Key = `quota:tier:{org_id}` | Per-org tier cache |
| API auth | `verify_org_access()` on every endpoint | User must belong to org |
| Override writes | `BillingPlatformAdmin` required | Only cross_tenant users can set overrides |

## Attack Surface

**Cross-org read:** Prevented by `verify_org_access()` in router + DynamoDB PK scoping.
An org A user calling `GET /billing/{org_b_id}/tier` gets 403.

**Cross-org write:** Prevented by `verify_org_access()` + `BillingAdmin` auth dependency.
An org A admin calling `PUT /billing/{org_b_id}/tier` gets 403.

**Override escalation:** Only `BillingPlatformAdmin` (requires `billing.cross_tenant` permission) can create overrides. Regular `BillingAdmin` cannot.

**Cache poisoning:** Redis keys are org-scoped. Even if an attacker poisons a key, it only affects their own org's cache. Cache TTL is 5 minutes — stale data self-heals.

**DynamoDB injection:** `org_id` is used as-is in PK construction. If it contains special chars, the PK is just a different string — no injection possible in DynamoDB.

## Multi-Org Test Coverage

UJ-032: Cross-tenant tier read/write denied
UJ-038: Override auth escalation prevented
UJ-039: 3-org isolation (modify A doesn't affect B/C)
