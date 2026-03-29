# DynamoDB Access Patterns

Table: `ab0t_quota_state`
Key schema: PK (HASH, String), SK (RANGE, String)
Billing mode: PAY_PER_REQUEST

## SK Patterns

| PK | SK | Data |
|----|-----|------|
| `ORG#{org_id}` | `TIER` | Current tier (tier_id, changed_by, changed_at, reason) |
| `ORG#{org_id}` | `TIER_HISTORY#{iso_ts}#{uuid8}` | Tier change record |
| `ORG#{org_id}` | `OVERRIDE#{resource_key}` | Per-org limit override |
| `ORG#{org_id}` | `OVERRIDE_HISTORY#{iso_ts}#{uuid8}` | Override change record |
| `ORG#{org_id}` | `COUNTER#{resource_key}` | Counter snapshot (for Redis recovery) |

History SKs include `uuid.uuid4().hex[:8]` suffix to prevent microsecond collisions.

## Atomic Writes

All mutations use `transact_write_items` to write the record + its history atomically:

```python
await self._client.transact_write_items(
    TransactItems=[
        {"Put": {"TableName": self._table_name, "Item": self._serialize_item(tier_item)}},
        {"Put": {"TableName": self._table_name, "Item": self._serialize_item(history_item)}},
    ]
)
```

If either write fails, both roll back. No orphaned tier records without audit trail.

## Serialization

Uses `boto3.dynamodb.types.TypeSerializer` and `TypeDeserializer` (not hand-rolled).
Numbers deserialize as `Decimal`. Service layer calls `float()` when needed.

```python
from boto3.dynamodb.types import TypeSerializer, TypeDeserializer
_serializer = TypeSerializer()
_deserializer = TypeDeserializer()

def _serialize_item(self, item):
    return {k: _serializer.serialize(v) for k, v in item.items() if v is not None}

def _deserialize_item(self, item):
    return {k: _deserializer.deserialize(v) for k, v in item.items()}
```

## Queries

All read operations are single-partition (`PK=ORG#{org_id}`).

- Get tier: `GetItem` with `SK=TIER`
- Get history: `Query` with `SK begins_with TIER_HISTORY#`, `ScanIndexForward=False`
- Get override: `GetItem` with `SK=OVERRIDE#{resource_key}`
- List overrides: `Query` with `SK begins_with OVERRIDE#`, filter out `OVERRIDE_HISTORY#`

No table scans. No GSIs needed for current access patterns.
