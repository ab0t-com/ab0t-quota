# quota.py Module Template

Full template for `app/quota.py` in any service integrating ab0t-quota.

```python
"""Quota integration for {service-name}."""

import os
import logging
from typing import Optional
from redis.asyncio import Redis
from fastapi import HTTPException

from ab0t_quota import (
    QuotaEngine, QuotaCheckRequest, QuotaIncrementRequest,
    QuotaDecrementRequest, QuotaBatchCheckRequest, QuotaResult,
)
from ab0t_quota.config import load_config, load_tiers, load_resources
from ab0t_quota.models.requests import QuotaCheckItem
from ab0t_quota.providers import JWTTierProvider
from ab0t_quota.registry import ResourceRegistry
from ab0t_quota.tiers import DEFAULT_TIERS
from ab0t_quota.alerts import AlertManager, LogAlertDispatcher
from ab0t_quota.persistence import QuotaStore

logger = logging.getLogger(__name__)

_engine: Optional[QuotaEngine] = None
_redis: Optional[Redis] = None
_store: Optional[QuotaStore] = None


async def startup(redis_url: Optional[str] = None) -> QuotaEngine:
    global _engine, _redis, _store

    config = load_config()
    storage_config = config.get("storage", {})

    url = (redis_url
           or storage_config.get("redis_url")
           or os.getenv("QUOTA_REDIS_URL")
           or os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    _redis = Redis.from_url(url, decode_responses=False)

    registry = ResourceRegistry()
    config_resources = load_resources(config)
    if config_resources:
        registry.register(*config_resources)
    # Register service-specific resources here:
    # from ab0t_quota.registry import SANDBOX_RESOURCES
    # registry.register(*SANDBOX_RESOURCES)

    tiers = load_tiers(config) or DEFAULT_TIERS
    provider = JWTTierProvider(
        claim_key=config.get("tier_provider", {}).get("jwt_claim_key", "org_tier"),
        default_tier=config.get("tier_provider", {}).get("default_tier", "free"),
    )

    _store = QuotaStore(
        table_name=storage_config.get("dynamodb_table", "ab0t_quota_state"),
        region=storage_config.get("dynamodb_region", os.getenv("AWS_REGION", "us-east-1")),
        endpoint_url=os.getenv("DYNAMODB_ENDPOINT") or None,
    )
    try:
        await _store.initialize()
    except Exception as e:
        logger.warning("Quota persistence failed (non-fatal): %s", e)
        _store = None

    async def load_override(org_id, resource_key):
        if _store:
            return await _store.get_override(org_id, resource_key)
        return None

    _engine = QuotaEngine(
        redis=_redis, tier_provider=provider, registry=registry,
        tiers=tiers, override_loader=load_override,
    )
    _engine.set_alert_manager(AlertManager(redis=_redis, dispatchers=[LogAlertDispatcher()]))

    if _store and storage_config.get("persistence_enabled", True):
        try:
            await _store.seed_redis(_redis, registry)
        except Exception as e:
            logger.warning("Counter seeding failed (non-fatal): %s", e)

    logger.info("Quota engine initialized")
    return _engine


async def shutdown():
    global _engine, _redis, _store
    if _store:
        await _store.close()
        _store = None
    if _redis:
        await _redis.aclose()
        _redis = None
    _engine = None


def get_engine() -> QuotaEngine:
    if _engine is None:
        raise RuntimeError("Quota engine not initialized")
    return _engine


async def check_quota(org_id, resource_key, user_id=None, increment=1.0, **kw) -> QuotaResult:
    result = await get_engine().check(
        QuotaCheckRequest(org_id=org_id, resource_key=resource_key,
                          user_id=user_id, increment=increment), **kw)
    if result.denied:
        raise HTTPException(status_code=429, detail=result.to_api_error())
    return result
```
