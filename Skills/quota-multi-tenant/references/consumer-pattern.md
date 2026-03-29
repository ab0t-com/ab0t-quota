# Consumer Service Integration Pattern

Every service that enforces quota follows this pattern. Sandbox-platform is the reference implementation.

## Files

```
your-service/
├── app/quota.py            # Engine init, lifecycle hooks, check helpers
├── quota-config.json       # Tier definitions, billing provider config
├── requirements.txt        # + redis>=5.0, git+https://github.com/ab0t-com/ab0t-quota.git
├── Dockerfile              # + COPY quota-config.json ./quota-config.json
├── docker-compose.yml      # + QUOTA_* env vars, mount quota-config.json
└── .env                    # + QUOTA_CONFIG_PATH, QUOTA_REDIS_URL, QUOTA_STATE_TABLE
```

## quota.py Tier Provider

```python
# Fetch tier from billing service (not auth, not JWT)
async def fetch_tier_from_billing(org_id: str) -> str:
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get(
            f"{billing_url}/billing/{org_id}/tier",
            headers={"X-API-Key": billing_api_key},
        )
        if resp.status_code == 200:
            return resp.json().get("tier_id", "free")
    return "free"

provider = AuthServiceTierProvider(
    fetch_fn=fetch_tier_from_billing,
    redis=redis,
    cache_ttl=300,
)
```

## Enforcement Pattern

```python
# Before provisioning:
await quota_module.check_quota(org_id, resource_key, user_id=user_id)

# After successful provisioning:
await quota_module.on_resource_created(org_id, user_id)

# On termination:
await quota_module.on_resource_terminated(org_id, user_id)
```

Wrap increment/decrement in try/except — quota tracking failures must not block operations.

## Config File

`quota-config.json` controls tiers without code deploys:
- `tier_provider.type: "billing"` — fetch from billing service
- `tiers[]` — tier definitions with limits per resource
- `enforcement.enabled` — master switch
- `enforcement.shadow_mode` — log denials without blocking (for rollout)
