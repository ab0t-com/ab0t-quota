"""Subscription-credit grant handler.

Receives paid-invoice events (`invoice.paid` and `invoice.payment_succeeded`)
from the consumer's lib proxy and applies the configured `credit_grant`
for the org's current tier to the billing-service ledger. Both event
types are accepted because Stripe emits one or both depending on API
version (X2 in ticket 20260518_post_upgrade_credit_and_ux_propagation).
Idempotency on `invoice:{id}:credit_grant` prevents double-credit when
Stripe emits both for the same invoice.

Architecture (current — Alt B, see
docs/WEBHOOK_AND_CREDIT_GRANT_ARCHITECTURE.md and ticket
20260516_auto_credit_invoice_paid_wiring §8 cutover plan):

    Stripe -> consumer's lib proxy (e.g. sandbox.service.ab0t.com/api/webhooks/stripe)
           -> verify_signature(AB0T_QUOTA_STRIPE_WEBHOOK_SECRET)
           -> handle_subscription_invoice_paid()  ← THIS HANDLER, in-band
              |
              +-- extract org_id, plan_id from invoice.metadata
              +-- tier_id = resolve_plan_to_tier(plan_id, consumer_config)
              +-- tier   = TierRegistry.get(tier_id)
              +-- grant  = tier.credit_grant  (may be None)
              +-- if trigger matches: call BillingServiceClient.apply_credit_grant
           -> forward_webhook(body, signature) -> payment-service
              -> payment-service updates invoice/subscription DDB rows
              -> payment-service legacy balance credit is skipped when
                 ENABLE_LEGACY_SUBSCRIPTION_INVOICE_CREDIT=false (post-cutover)

The handler is intentionally generic: it makes no assumption about the
consumer service. The wiring that actually invokes it is consumer-
specific (via the lib proxy router's dispatch table).

Idempotency: the source invoice ID is used as the idempotency key, so
Stripe webhook redelivery is naturally deduplicated by billing-service.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Awaitable, Callable, Optional

from ..models.core import (
    CreditGrant,
    CreditTrigger,
    TierConfig,
)
from .clients import BillingServiceClient, BillingServiceError

try:
    import structlog
    logger = structlog.get_logger(__name__)
except ImportError:  # pragma: no cover — fallback for environments without structlog
    import logging
    logger = logging.getLogger(__name__)


# Callable signature for plan_id → tier_id resolvers (consumer-supplied).
# Mirrors the existing `_resolve_plan_to_tier` in billing/router.py.
PlanToTierResolver = Callable[[str], Awaitable[Optional[str]]]


def _extract_invoice_metadata(invoice: dict) -> tuple[Optional[str], Optional[str]]:
    """Pull org_id + plan_id from a Stripe invoice payload.

    Stripe propagates `subscription_data.metadata` (set at checkout
    creation, see payment-service Phase 2.1) to:
      - subscription.metadata
      - each invoice's metadata

    We read invoice.metadata first; fall back to invoice.subscription_details.metadata
    if present (Stripe sometimes nests it). Pre-Phase-2.1 subscriptions
    have neither; we return (None, None) and the caller no-ops.
    """
    md = (invoice or {}).get("metadata") or {}
    sub_details = (invoice or {}).get("subscription_details") or {}
    sub_md = sub_details.get("metadata") or {}

    org_id = md.get("org_id") or sub_md.get("org_id")
    # TODO(public-mesh-ga): Add a provider-neutral fallback that can resolve
    # the invoice's line-item price/subscription ID when plan_id metadata is
    # absent; public consumers should not depend on one Stripe metadata shape.
    # Backlink:
    # audit: 2026-05-16 public-mesh-ga readiness pass
    plan_id = md.get("plan_id") or sub_md.get("plan_id")
    return org_id, plan_id


async def handle_subscription_invoice_paid(
    invoice: dict,
    *,
    tier_registry: dict[str, TierConfig],
    plan_to_tier: PlanToTierResolver,
    billing_client: BillingServiceClient,
) -> dict:
    """Apply the configured credit_grant when a subscription invoice is paid.

    Returns a dict describing what happened. The dict is logged + returned
    to the webhook receiver; consumers may surface it for monitoring.

    Status values:
      "skipped_no_metadata"  — invoice missing org_id (pre-2.1 sub or non-sub)
      "skipped_no_tier"      — plan_id couldn't be resolved to a tier_id
      "skipped_no_grant"     — tier has no credit_grant configured
      "skipped_wrong_trigger" — tier.credit_grant.trigger is not subscription_invoice_paid
      "applied"              — grant landed; includes billing-service response
      "deferred_transient"   — billing-service returned a transient error; caller may retry
      "failed_permanent"     — billing-service returned a permanent error
    """
    org_id, plan_id = _extract_invoice_metadata(invoice)
    invoice_id = (invoice or {}).get("id") or "unknown"

    if not org_id:
        logger.info(
            "subscription_invoice_paid_skip",
            invoice_id=invoice_id,
            reason="no org_id in invoice metadata (pre-2.1 subscription, or non-subscription invoice)",
        )
        return {"status": "skipped_no_metadata", "invoice_id": invoice_id}

    # Resolve plan_id -> tier_id via the consumer-supplied resolver.
    # If plan_id is missing, we can still try to look up by org's CURRENT
    # tier (the resolver may have access to that), but typical case is
    # plan_id is set and tier_map drives lookup.
    tier_id: Optional[str] = None
    if plan_id:
        try:
            tier_id = await plan_to_tier(plan_id)
        except Exception as e:
            logger.warning(
                "subscription_invoice_paid_plan_lookup_failed",
                invoice_id=invoice_id,
                plan_id=plan_id,
                org_id=org_id,
                err=str(e),
            )

    if not tier_id:
        logger.info(
            "subscription_invoice_paid_skip",
            invoice_id=invoice_id,
            org_id=org_id,
            plan_id=plan_id,
            reason="plan_id could not be resolved to a tier_id",
        )
        return {"status": "skipped_no_tier", "invoice_id": invoice_id, "org_id": org_id}

    tier = tier_registry.get(tier_id)
    if tier is None:
        logger.info(
            "subscription_invoice_paid_skip",
            invoice_id=invoice_id,
            org_id=org_id,
            tier_id=tier_id,
            reason="tier_id not in registry (consumer config drift)",
        )
        return {"status": "skipped_no_tier", "invoice_id": invoice_id, "org_id": org_id, "tier_id": tier_id}

    grant: Optional[CreditGrant] = tier.credit_grant
    if grant is None:
        logger.info(
            "subscription_invoice_paid_skip",
            invoice_id=invoice_id,
            org_id=org_id,
            tier_id=tier_id,
            reason="tier has no credit_grant configured (capacity_only or unlock_only)",
        )
        return {"status": "skipped_no_grant", "invoice_id": invoice_id, "org_id": org_id, "tier_id": tier_id}

    if grant.trigger != CreditTrigger.SUBSCRIPTION_INVOICE_PAID:
        logger.info(
            "subscription_invoice_paid_skip",
            invoice_id=invoice_id,
            org_id=org_id,
            tier_id=tier_id,
            trigger=grant.trigger.value,
            reason="tier.credit_grant.trigger is not subscription_invoice_paid",
        )
        return {
            "status": "skipped_wrong_trigger",
            "invoice_id": invoice_id, "org_id": org_id, "tier_id": tier_id,
            "trigger": grant.trigger.value,
        }

    # Apply the grant. Use the invoice ID as the idempotency key so
    # Stripe redelivery is naturally deduplicated by billing-service.
    # Pass source=invoice_id + source_tier=tier_id so billing-service
    # persists provenance on the account, enabling the safety-checked
    # downgrade reset (Option B per ticket 20260516).
    idempotency_key = f"invoice:{invoice_id}:credit_grant"
    try:
        resp = await billing_client.apply_credit_grant(
            org_id=org_id,
            amount=float(grant.amount_per_period),
            destination=grant.destination.value,
            lifecycle=grant.lifecycle.value,
            idempotency_key=idempotency_key,
            rollover_max=float(grant.rollover_max_periods * grant.amount_per_period)
                          if grant.rollover_max_periods is not None else None,
            reason=f"subscription_invoice_paid:{tier_id}",
            source=invoice_id,
            source_tier=tier_id,
        )
        logger.info(
            "subscription_invoice_paid_applied",
            invoice_id=invoice_id,
            org_id=org_id,
            tier_id=tier_id,
            amount=str(grant.amount_per_period),
            destination=grant.destination.value,
            lifecycle=grant.lifecycle.value,
        )
        return {
            "status": "applied",
            "invoice_id": invoice_id,
            "org_id": org_id,
            "tier_id": tier_id,
            "billing_response": resp,
        }
    except BillingServiceError as e:
        # Transient (5xx, 429) — caller may retry. Permanent (4xx other
        # than 409 idempotent-replay) — log loudly so it surfaces.
        is_transient = e.status_code in (429, 500, 502, 503, 504)
        if is_transient:
            logger.warning(
                "subscription_invoice_paid_transient",
                invoice_id=invoice_id,
                org_id=org_id,
                tier_id=tier_id,
                status_code=e.status_code,
                detail=str(e.detail)[:200],
            )
            return {
                "status": "deferred_transient",
                "invoice_id": invoice_id, "org_id": org_id,
                "tier_id": tier_id, "status_code": e.status_code,
            }
        logger.error(
            "subscription_invoice_paid_failed_permanent",
            invoice_id=invoice_id,
            org_id=org_id,
            tier_id=tier_id,
            status_code=e.status_code,
            detail=str(e.detail)[:200],
        )
        return {
            "status": "failed_permanent",
            "invoice_id": invoice_id, "org_id": org_id,
            "tier_id": tier_id, "status_code": e.status_code,
        }


async def reset_subscription_credit_on_tier_change(
    org_id: str,
    *,
    old_tier_id: str,
    new_tier_id: str,
    tier_registry: dict[str, TierConfig],
    billing_client: BillingServiceClient,
    tier_change_event_id: Optional[str] = None,
) -> dict:
    """Reset `subscription_credit` after a downgrade IF the old tier's
    `credit_grant.reset_on_downgrade` flag is True.

    Decision matrix:

      no old tier in registry             → skip (status="skipped_unknown_old")
      old tier has no credit_grant        → skip (status="skipped_no_grant")
      reset_on_downgrade is False         → skip (status="skipped_policy")
      new tier rank >= old tier rank      → skip (status="skipped_not_downgrade")
      reset endpoint 409 (tier mismatch)  → skipped (status="skipped_safety_check")
                                            — Option B safety: stored credit
                                            came from a different subscription
                                            (relevant in multi-sub orgs);
                                            don't wipe it
      reset succeeds                      → done (status="reset")
      reset 5xx                           → deferred_transient

    Rank is computed by `TierConfig.sort_order` — consumer-declared. If
    either tier lacks sort_order or they're equal, downgrade detection
    falls back to "not a downgrade" (safe default — don't reset).

    Idempotency key: tier_change_event_id when provided, else a
    composite of org+old+new. Caller should pass an event-specific key
    so repeated tier changes each get their own reset attempt.
    """
    if old_tier_id == new_tier_id:
        return {"status": "skipped_not_downgrade", "reason": "same tier"}

    old_tier = tier_registry.get(old_tier_id)
    new_tier = tier_registry.get(new_tier_id)
    if old_tier is None:
        return {"status": "skipped_unknown_old", "old_tier_id": old_tier_id}

    grant = getattr(old_tier, "credit_grant", None)
    if grant is None:
        return {"status": "skipped_no_grant", "old_tier_id": old_tier_id}

    if not getattr(grant, "reset_on_downgrade", True):
        return {
            "status": "skipped_policy",
            "old_tier_id": old_tier_id,
            "reset_on_downgrade": False,
        }

    # Detect downgrade via sort_order. If new tier is missing or ranks
    # >= old, treat as NOT a downgrade (don't reset). Tier rank
    # ordering is a consumer concern (declared in quota-config.json);
    # the library never invents it.
    old_rank = getattr(old_tier, "sort_order", None)
    new_rank = getattr(new_tier, "sort_order", None) if new_tier else None
    if old_rank is None or new_rank is None:
        return {
            "status": "skipped_not_downgrade",
            "reason": "missing sort_order on one or both tiers",
        }
    if new_rank >= old_rank:
        return {
            "status": "skipped_not_downgrade",
            "old_rank": old_rank, "new_rank": new_rank,
        }

    idempotency_key = (
        tier_change_event_id
        or f"tier_change:{org_id}:{old_tier_id}->{new_tier_id}"
    )

    try:
        resp = await billing_client.reset_subscription_credit(
            org_id=org_id,
            expected_source_tier=old_tier_id,
            idempotency_key=idempotency_key,
            reason=f"downgrade {old_tier_id}->{new_tier_id}",
        )
        logger.info(
            "downgrade_reset_applied",
            org_id=org_id, old_tier_id=old_tier_id, new_tier_id=new_tier_id,
            forfeit_amount=str(resp.get("amount", "?")),
        )
        return {"status": "reset", "billing_response": resp}
    except BillingServiceError as e:
        if e.status_code == 409:
            # Safety check rejected — recorded credit belongs to a
            # different subscription (or there's no credit). Not an
            # error; expected behavior under multi-sub. Log + skip.
            logger.info(
                "downgrade_reset_skipped_safety_check",
                org_id=org_id, old_tier_id=old_tier_id, new_tier_id=new_tier_id,
                detail=str(e.detail)[:200],
            )
            return {"status": "skipped_safety_check"}
        if e.status_code in (429, 500, 502, 503, 504):
            logger.warning(
                "downgrade_reset_transient",
                org_id=org_id, status_code=e.status_code,
            )
            return {"status": "deferred_transient", "status_code": e.status_code}
        logger.error(
            "downgrade_reset_failed",
            org_id=org_id, status_code=e.status_code,
        )
        return {"status": "failed", "status_code": e.status_code}
