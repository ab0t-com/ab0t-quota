# Quota API Endpoint Templates

## GET /api/quotas/usage

Returns usage for all registered resources in the caller's org. Frontend uses this for usage bars.

```python
@app.get("/api/quotas/usage", tags=["Quotas"])
async def get_quota_usage(request: Request, user: AuthenticatedUser):
    from . import quota as quota_module
    engine = quota_module.get_engine()
    usage = await engine.get_usage(user.org_id)
    return usage.model_dump()
```

Response shape:
```json
{
  "org_id": "org-123",
  "tier_id": "starter",
  "tier_display": "Starter",
  "resources": [
    {
      "resource_key": "sandbox.concurrent",
      "display_name": "Concurrent Sandboxes",
      "unit": "sandboxes",
      "current": 3.0,
      "limit": 5.0,
      "utilization": 0.6,
      "severity": "info",
      "counter_type": "gauge"
    }
  ],
  "warnings_count": 0,
  "exceeded_count": 0
}
```

## GET /api/quotas/tiers

Public endpoint (no auth). Returns all tiers for pricing page comparison.

```python
@app.get("/api/quotas/tiers", tags=["Quotas"])
async def get_quota_tiers():
    from ab0t_quota.tiers import DEFAULT_TIERS
    tiers = []
    for tier in sorted(DEFAULT_TIERS.values(), key=lambda t: t.sort_order):
        limits = {}
        for key, tl in tier.limits.items():
            limits[key] = {
                "limit": tl.limit,
                "limit_display": "Unlimited" if tl.limit is None else f"{tl.limit:g}",
            }
        tiers.append({
            "tier_id": tier.tier_id,
            "display_name": tier.display_name,
            "description": tier.description,
            "features": list(tier.features),
            "limits": limits,
        })
    return {"tiers": tiers}
```

## GET /api/quotas/check/{resource_key}

Pre-flight check: can the caller create one more of this resource?

```python
@app.get("/api/quotas/check/{resource_key}", tags=["Quotas"])
async def check_quota_preflight(resource_key: str, request: Request, user: AuthenticatedUser):
    from . import quota as quota_module
    result = await quota_module.check_quota(user.org_id, resource_key, user_id=user.user_id)
    return result.model_dump()
```

Frontend calls this before showing the "Create" button to determine if it should be enabled or greyed out with an upgrade prompt.
