"""2.1.4 — Provider tests."""

import pytest
import pytest_asyncio
import fakeredis.aioredis

from ab0t_quota.providers import JWTTierProvider, AuthServiceTierProvider, StaticTierProvider


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.flushall()
    await r.aclose()


class TestJWTTierProvider:
    @pytest.mark.asyncio
    async def test_reads_claim(self):
        p = JWTTierProvider(claim_key="org_tier")
        tier = await p.get_tier("org-1", token_claims={"org_tier": "pro"})
        assert tier == "pro"

    @pytest.mark.asyncio
    async def test_falls_back_to_default(self):
        p = JWTTierProvider(default_tier="free")
        tier = await p.get_tier("org-1", token_claims={})
        assert tier == "free"

    @pytest.mark.asyncio
    async def test_no_claims_kwarg(self):
        p = JWTTierProvider(default_tier="free")
        tier = await p.get_tier("org-1")
        assert tier == "free"


class TestAuthServiceTierProvider:
    @pytest.mark.asyncio
    async def test_fetches_and_caches(self, redis):
        call_count = 0

        async def fetch(org_id):
            nonlocal call_count
            call_count += 1
            return "starter"

        p = AuthServiceTierProvider(fetch_fn=fetch, redis=redis, cache_ttl=300)
        assert await p.get_tier("org-1") == "starter"
        assert await p.get_tier("org-1") == "starter"  # from cache
        assert call_count == 1  # only one fetch

    @pytest.mark.asyncio
    async def test_handles_fetch_error(self, redis):
        async def fail(org_id):
            raise ConnectionError("auth service down")

        p = AuthServiceTierProvider(fetch_fn=fail, redis=redis, default_tier="free")
        assert await p.get_tier("org-1") == "free"

    @pytest.mark.asyncio
    async def test_invalidate(self, redis):
        call_count = 0

        async def fetch(org_id):
            nonlocal call_count
            call_count += 1
            return "pro" if call_count > 1 else "free"

        p = AuthServiceTierProvider(fetch_fn=fetch, redis=redis, cache_ttl=300)
        assert await p.get_tier("org-1") == "free"
        await p.invalidate("org-1")
        assert await p.get_tier("org-1") == "pro"
        assert call_count == 2


class TestStaticTierProvider:
    @pytest.mark.asyncio
    async def test_mapping(self):
        p = StaticTierProvider(mapping={"org-vip": "enterprise"}, default_tier="free")
        assert await p.get_tier("org-vip") == "enterprise"
        assert await p.get_tier("org-other") == "free"
