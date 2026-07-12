"""Adversarial proof of the raw-counter shim's residual, and that the identity
path (engine.release by activation_id) does NOT share it.

Ticket 20260709, DECISIONS D-10 / not_verified[] #3 (coordinator: "prove it, don't
argue it").

The QI-05.1 fix has two layers:
  * the RAW ``GaugeCounter.decrement_user`` shim, which scopes a caller-composed
    idempotency key by a library-minted CREATE generation. It greens the
    SEQUENTIAL reused-id bug (create→release→reuse→release), but…
  * …it CANNOT be made both reuse-safe and retry-safe, because a reused caller key
    is information-theoretically indistinguishable from a genuine retry: both
    present the SAME key with no activation identity. This is not a bug to patch at
    that layer — it is the reason the ticket introduces activation identity.

``test_raw_shim_residual_reproduces`` is the EXECUTABLE proof that the residual is
real (xfail(strict): it fails today and must keep failing until the raw shim is
retired — an unexpected pass means someone found an impossible fix and should say
so). ``test_release_by_id_has_no_retry_reuse_residual`` is the GREEN proof that the
FIX — engine.release(activation_id) — has no such hole.
"""
from __future__ import annotations

import fakeredis.aioredis
import pytest
import pytest_asyncio

from ab0t_quota.activations import InMemoryActivationStore
from ab0t_quota.counters.gauge import GaugeCounter
from ab0t_quota.engine import QuotaEngine
from ab0t_quota.models.core import CounterType, ResourceDef, TierConfig, TierLimits
from ab0t_quota.providers import StaticTierProvider
from ab0t_quota.registry import ResourceRegistry

USER = "user-1"
RK = "sandbox.desktop_sessions"
CID = "desktop-abc123"                       # reused id
END_IDEM = f"counter:lifecycle:end:{CID}:{RK}"


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.flushall()
    await r.aclose()


@pytest.mark.xfail(strict=True, reason=(
    "FUNDAMENTAL, not a patchable bug: the raw GaugeCounter.decrement_user shim "
    "scopes a caller key by the CURRENT create generation, so a teardown retry that "
    "lands AFTER a reused-id create scopes to the NEW generation and wrongly releases "
    "the new activation -> undercount / phantom headroom. A reused caller key is "
    "indistinguishable from a retry with no activation identity, so NO raw-counter fix "
    "exists (a per-key dedup that stops the retry re-introduces QI-05.1; a "
    "generation-scoped dedup admits the retry). The fix is engine.release(activation_id) "
    "— proven residual-free by test_release_by_id_has_no_retry_reuse_residual. This "
    "xfail must NOT be greened at the raw-counter layer; green it only by RETIRING the "
    "raw shim in favour of the identity path."))
class TestRawShimResidual:
    @pytest.mark.asyncio
    async def test_raw_shim_residual_reproduces(self, redis):
        """create#1 -> release#1 -> reuse create#2 -> release#1 RETRY (delayed) ->
        release#2. The delayed retry steals create#2's slot; release#2 is then
        absorbed as a no-op. The gauge reads 0 while a resource is still alive =
        undercount. Correct system: gauge reflects the 1 live resource."""
        g = GaugeCounter(redis, "org-1", RK)

        await g.increment_user(USER, 1.0, idempotency_key=None)             # create #1
        await g.decrement_user(USER, 1.0, idempotency_key=END_IDEM)         # release #1
        await g.increment_user(USER, 1.0, idempotency_key=None)             # reuse: create #2 (gauge=1)
        # Delayed retry of release #1 arrives NOW (after create #2 bumped the gen):
        await g.decrement_user(USER, 1.0, idempotency_key=END_IDEM)         # retry steals create #2's slot
        await g.decrement_user(USER, 1.0, idempotency_key=END_IDEM)         # release #2 -> absorbed

        final = await g.get_user(USER)
        # create #2 is still "alive" (its legitimate release was absorbed), so the
        # correct gauge is 1.0. The residual drives it to 0.0 (undercount).
        assert final == 1.0, (
            f"raw shim undercounted: gauge={final} but 1 resource is still live — "
            f"a delayed teardown retry stole the reused-id create's slot."
        )


class TestReleaseByIdHasNoResidual:
    def _engine(self, redis, store):
        reg = ResourceRegistry()
        reg.register(ResourceDef(service="t", resource_key=RK, display_name="D",
                                 counter_type=CounterType.GAUGE))
        return QuotaEngine(
            redis=redis, tier_provider=StaticTierProvider({"org-1": "free"}),
            registry=reg,
            tiers={"free": TierConfig(tier_id="free", display_name="F",
                                      limits={RK: TierLimits(limit=1000)})},
            resource_bundles={"box": [RK]}, activation_store=store,
        )

    @pytest.mark.asyncio
    async def test_release_by_id_has_no_retry_reuse_residual(self, redis):
        """The SAME adversarial sequence, but the lifecycle uses minted activation
        ids. The delayed retry of release(act1) is a no-op (act1 already RELEASED),
        so create#2 (act2) survives until ITS release. No undercount."""
        store = InMemoryActivationStore()
        engine = self._engine(redis, store)
        g = GaugeCounter(redis, "org-1", RK)

        a1 = await engine.acquire("org-1", "box", user_id=USER)            # create #1
        await engine.release(a1.activation_id)                              # release #1
        a2 = await engine.acquire("org-1", "box", user_id=USER)            # reuse: create #2
        assert await g.get_user(USER) == 1.0

        # Delayed retry of release #1 — identity distinguishes it from release #2:
        assert await engine.release(a1.activation_id) is False              # act1 already released -> no-op
        assert await g.get_user(USER) == 1.0, "retry must NOT touch act2's slot"

        assert await engine.release(a2.activation_id) is True               # release #2 (the real one)
        assert await g.get_user(USER) == 0.0
        assert await store.count_open("org-1") == 0
