"""2.1.4 — Provider tests."""

import pytest
import pytest_asyncio
import fakeredis.aioredis

from ab0t_quota.providers import (
    JWTTierProvider,
    AuthServiceTierProvider,
    StaticTierProvider,
    TierFetchError,
)


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


class TestAuthServiceTierProviderLastKnownGood:
    """Ticket 20260810_tier_failclosed_lastknowngood (CLASS-1 DSD-01/02).

    A transient billing/tier fetch failure must never downgrade a paying org
    to the cheapest tier, and must never poison the cache with an invented
    `free`. See AuthServiceTierProvider failure semantics."""

    @pytest.mark.asyncio
    async def test_fetch_fails_with_good_cache_serves_last_known_good(self, redis):
        """A PRO org, then billing blips: serve PRO (last-known-good), NOT free."""
        state = {"fail": False}

        async def fetch(org_id):
            if state["fail"]:
                raise TierFetchError("billing 503")
            return "pro"

        # cache_ttl short so the fast cache expires and we hit the fetch again.
        p = AuthServiceTierProvider(
            fetch_fn=fetch, redis=redis, cache_ttl=1, default_tier="free"
        )
        # 1) successful fetch records last-known-good = pro
        assert await p.get_tier("org-1") == "pro"
        # 2) force the fast cache to expire, then make billing fail
        await p.invalidate("org-1")
        state["fail"] = True
        # 3) transient failure ⇒ serve last-known-good pro, never downgrade
        assert await p.get_tier("org-1") == "pro"
        # 4) and it must NOT have cached the (absent) free fallback: the fast
        #    cache stays empty so recovery is instant.
        assert await redis.get("quota:tier:org-1") is None

    @pytest.mark.asyncio
    async def test_fetch_failure_does_not_cache_free(self, redis):
        """The invented `free` fallback is never written to the fast cache."""
        state = {"fail": False}

        async def fetch(org_id):
            if state["fail"]:
                raise TierFetchError("down")
            return "pro"

        p = AuthServiceTierProvider(
            fetch_fn=fetch, redis=redis, cache_ttl=300, default_tier="free"
        )
        await p.get_tier("org-1")            # good fetch → cache pro
        await p.invalidate("org-1")          # drop fast cache
        state["fail"] = True
        await p.get_tier("org-1")            # failure → serves pro, caches nothing
        # The fast cache must not hold an invented free.
        cached = await redis.get("quota:tier:org-1")
        assert cached is None

    @pytest.mark.asyncio
    async def test_unknown_org_fetch_fails_returns_default_uncached(self, redis):
        """A never-fetched org with a failing source ⇒ default (free), correct
        for a genuinely-unknown org, and NOT cached so the next call retries."""
        async def fail(org_id):
            raise TierFetchError("down")

        p = AuthServiceTierProvider(
            fetch_fn=fail, redis=redis, cache_ttl=300, default_tier="free"
        )
        assert await p.get_tier("new-org") == "free"
        # Not cached ⇒ next request re-attempts the source (no free poisoning).
        assert await redis.get("quota:tier:new-org") is None
        assert await redis.get("quota:tier:lkg:new-org") is None

    @pytest.mark.asyncio
    async def test_last_known_good_age_is_surfaced(self, redis, caplog):
        """The served last-known-good logs its age so staleness is observable."""
        import logging

        state = {"fail": False}

        async def fetch(org_id):
            if state["fail"]:
                raise TierFetchError("down")
            return "enterprise"

        p = AuthServiceTierProvider(
            fetch_fn=fetch, redis=redis, cache_ttl=1, default_tier="free"
        )
        await p.get_tier("org-1")
        await p.invalidate("org-1")
        state["fail"] = True
        with caplog.at_level(logging.WARNING, logger="ab0t_quota.providers"):
            assert await p.get_tier("org-1") == "enterprise"
        assert any(
            "serving_last_known_good" in r.message and "age_seconds" in r.message
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_explicit_downgrade_is_honored(self, redis):
        """A SUCCESSFUL fetch returning a lower tier is a real downgrade and
        must update both the fast cache and the last-known-good."""
        state = {"tier": "pro"}

        async def fetch(org_id):
            return state["tier"]

        p = AuthServiceTierProvider(
            fetch_fn=fetch, redis=redis, cache_ttl=1, default_tier="free"
        )
        assert await p.get_tier("org-1") == "pro"
        await p.invalidate("org-1")
        state["tier"] = "free"  # explicit cancellation confirmed by billing
        assert await p.get_tier("org-1") == "free"
        # last-known-good advanced to the confirmed free (not a stale pro).
        await p.invalidate("org-1")

        async def boom(org_id):
            raise TierFetchError("down")

        p2 = AuthServiceTierProvider(
            fetch_fn=boom, redis=redis, cache_ttl=1, default_tier="free"
        )
        # a later blip now serves the CONFIRMED free, not the old pro.
        assert await p2.get_tier("org-1") == "free"

    @pytest.mark.asyncio
    async def test_success_updates_good_cache(self, redis):
        """A successful fetch writes both the fast cache and the LKG record."""
        async def fetch(org_id):
            return "starter"

        p = AuthServiceTierProvider(
            fetch_fn=fetch, redis=redis, cache_ttl=300, default_tier="free"
        )
        assert await p.get_tier("org-1") == "starter"
        assert (await redis.get("quota:tier:org-1")).decode() == "starter"
        import json
        lkg = json.loads((await redis.get("quota:tier:lkg:org-1")).decode())
        assert lkg["tier"] == "starter"
        assert "ts" in lkg
