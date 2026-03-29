# Extending the Quota Module

## Adding a New Endpoint

1. Add the route to `router.py` with appropriate auth dependency
2. Add request/response models to `models.py`
3. Add business logic to `service.py`
4. If DynamoDB needed: add store method with `_serialize_item`/`_deserialize_item`
5. Use `TransactWriteItems` if writing data + history

Auth aliases available from `...auth`:
- `BillingReader` — any org member with `billing.read`
- `BillingAdmin` — org admin with `billing.admin`
- `BillingPlatformAdmin` — cross-tenant with `billing.cross_tenant`

Always call `verify_org_access(org_id, user)` in the route handler.

## Adding a New DynamoDB Record Type

1. Define SK pattern: `{TYPE}#{unique_suffix}`
2. Add corresponding `{TYPE}_HISTORY#{ts}#{uuid}` for audit
3. Use `TransactWriteItems` to write both atomically
4. Add query method with `begins_with(SK, :prefix)` filter
5. Filter out `{TYPE}_HISTORY#` records in list operations

## Adding New Config

1. Add to `config.py` with `os.getenv()` and sensible default
2. Add to billing `.env` and `.env.production`
3. Add to `quota-config.json` if operator-facing
4. Document in `quota-tier-management` skill's config-schema reference

## Testing

UJ tests live at `billing/output/scripts/curl_tests/user_journeys/UJ-03*.sh`.
Source `quota_test_helpers.sh` for auth bootstrap.
Each test must be self-contained and idempotent.
