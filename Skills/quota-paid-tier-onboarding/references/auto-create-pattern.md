# Billing Account Auto-Creation Pattern

## The Problem

Services in the mesh need billing accounts to exist before they can check
balances, reserve funds, or read tiers. But account creation shouldn't require
a separate signup step or auth hook — that couples billing to auth.

## The Solution: Lazy Initialization

`billing_repo.ensure_account(org_id)` creates an account on first contact:

```python
async def ensure_account(self, org_id: str) -> BillingAccount:
    existing = await self.get_account_by_org_id(org_id)
    if existing:
        return existing

    account = BillingAccount(
        id=f"acct_{uuid.uuid4().hex[:12]}",
        org_id=org_id,
        billing_type=BillingType.PREPAID,
        balance=Decimal("0"),
        # ... defaults
        metadata={"auto_created": True, "created_by": "ensure_account"},
    )

    try:
        return await self.create_account(account)
    except ConditionalCheckFailed:
        # Race condition: another request created it between check and create
        return await self.get_account_by_org_id(org_id)
```

## Where It's Called

Every billing service operation that touches an org calls `ensure_account`:
- `get_account_balance()` → ensures account before returning balance
- `validate_access()` → ensures account before checking limits
- `reserve_funds()` → ensures account before reserving
- Other entry points that receive an `org_id`

## Properties

- **Idempotent:** Safe to call many times. Returns existing if found.
- **Race-safe:** Handles concurrent creation with ConditionalCheckFailed fallback.
- **No coupling:** No auth hooks, no events, no manual steps.
- **Observable:** `metadata.auto_created=true` marks auto-created accounts for auditing.
- **Zero balance:** Starts at $0. User must pay (via Stripe) to get positive balance.

## What Triggers It

Any mesh consumer that calls the billing service. Common triggers:
1. Sandbox-platform user checks billing page (calls `GET /billing/{org}/balance`)
2. Sandbox creation triggers reserve (calls `POST /billing/{org}/reserve`)
3. Quota check reads tier (calls `GET /billing/{org}/tier`)
4. Stripe checkout success (webhook credits the account)

The account exists by the time the user needs it, with no explicit creation step.
