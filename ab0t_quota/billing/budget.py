"""Pre-launch budget check for mesh services.

Drop-in budget enforcement: reserve funds before provisioning a resource,
refund on failure. Any mesh service using ab0t-quota[billing] gets this
pattern for free.

Usage:
    from ab0t_quota.billing.budget import BudgetChecker

    checker = BudgetChecker(billing_client, pricing_config)

    # Before provisioning:
    reservation_id = await checker.pre_launch_check(
        org_id, user_id, product_id="browser",
    )

    # On failure:
    await checker.on_failure(org_id, reservation_id)
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, Optional

from .clients import BillingServiceClient, BillingServiceError

logger = logging.getLogger("ab0t_quota.billing.budget")


class PricingNotDeclaredError(ValueError):
    """Asked for a price the pricing config does not declare.

    The config is the only source of prices. Inventing a rate here would
    reserve real balance against a number the operator never agreed
    (get_costs -> estimate_reservation -> pre_launch_check -> reserve_funds),
    so this refuses instead. Subclasses ValueError for back-compat.
    """


class PricingAmbiguousError(PricingNotDeclaredError):
    """A bare variant name is declared by more than one product, so it does not
    identify a price. Use the qualified `<product>:<variant>` form."""


class BudgetChecker:
    """Reusable budget check pattern for mesh services.

    Reads product pricing from the service's quota-config.json pricing section.
    Reserves funds via the billing service before resource provisioning.
    """

    def __init__(
        self,
        billing_client: BillingServiceClient,
        pricing_config: Dict[str, Any],
        enforcement_enabled: bool = True,
    ):
        self.billing = billing_client
        self.pricing = pricing_config
        self.enforcement = enforcement_enabled
        self._ambiguous: Dict[str, list] = {}
        self._product_costs = self._build_cost_table()

    @staticmethod
    def _variant_costs(v: Dict[str, Any]) -> Dict[str, Decimal]:
        return {
            "hourly_rate": Decimal(str(v.get("price_per_hour", "0.10"))),
            "allocation_fee": Decimal(str(v.get("allocation_price", "0.01"))),
        }

    def _build_cost_table(self) -> Dict[str, Dict[str, Decimal]]:
        """Build lookup key → {hourly_rate, allocation_fee} from pricing config.

        Three kinds of key, all derived from the config's own shape — no
        product is special-cased by name (ticket 20260722, §5c row 3):
          * `<product_id>`            → that product's default variant
          * `<product_id>:<variant>`  → always, unambiguous by construction
          * `<variant>` (bare)        → only when exactly ONE product declares
            that variant name and it does not collide with a product_id.
            Two products declaring `small` would otherwise silently serve one
            product's price for the other's variant, on the reservation path.
        """
        products = self.pricing.get("products", {})
        costs: Dict[str, Dict[str, Decimal]] = {}
        bare_claims: Dict[str, list] = {}

        for product_id, product in products.items():
            variants = product.get("variants", {})
            # Use default variant
            default = None
            for v in variants.values():
                if v.get("default") or default is None:
                    default = v
            if default:
                costs[product_id] = self._variant_costs(default)

            for variant_id, v in variants.items():
                costs[f"{product_id}:{variant_id}"] = self._variant_costs(v)
                bare_claims.setdefault(variant_id, []).append((product_id, v))

        for variant_id, claims in bare_claims.items():
            if variant_id in products:
                # A product id always outranks a variant of the same name.
                continue
            if len(claims) > 1:
                # Recorded, not registered: the lookup refuses by NAME so the
                # caller is told it is ambiguous rather than "not declared".
                self._ambiguous[variant_id] = [p for p, _ in claims]
                logger.warning(
                    "variant name %r is declared by %s — it does not identify a "
                    "price. Callers must use the qualified form '<product>:%s'.",
                    variant_id, self._ambiguous[variant_id], variant_id,
                )
                continue
            costs[variant_id] = self._variant_costs(claims[0][1])

        return costs

    def get_costs(self, product_or_instance: str) -> Dict[str, Decimal]:
        """Get pricing for a product id, a variant name, or `product:variant`.

        REFUSES on anything the pricing config does not declare (D-CK-11).
        This used to return a hardcoded 0.10/0.01 for unknown keys — an
        invented rate that reserves real customer balance through
        estimate_reservation -> pre_launch_check -> reserve_funds. The config
        is the only source of prices; not knowing one is reportable, not
        fillable.

        Raises:
            PricingAmbiguousError: bare variant name claimed by 2+ products.
            PricingNotDeclaredError: key not declared at all.
        """
        costs = self._product_costs.get(product_or_instance)
        if costs is not None:
            return costs

        if product_or_instance in self._ambiguous:
            claimants = self._ambiguous[product_or_instance]
            raise PricingAmbiguousError(
                f"pricing key {product_or_instance!r} is ambiguous: declared as a "
                f"variant by {claimants}. It does not identify a price — use the "
                f"qualified form, e.g. '{claimants[0]}:{product_or_instance}'."
            )

        raise PricingNotDeclaredError(
            f"pricing key {product_or_instance!r} is not declared in the pricing "
            f"config. Declared keys: {sorted(self._product_costs)}. Add it to "
            f"pricing.products in quota-config.json — a price that is not "
            f"configured cannot be charged."
        )

    def estimate_reservation(self, product_or_instance: str, count: int = 1) -> Decimal:
        """Estimate total reservation amount (allocation_fee + 1 hour of runtime)."""
        costs = self.get_costs(product_or_instance)
        return (costs["allocation_fee"] + costs["hourly_rate"]) * count

    async def pre_launch_check(
        self,
        org_id: str,
        user_id: str,
        product_or_instance: str,
        resource_type: str = "compute",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Reserve funds before provisioning. Returns reservation_id or None.

        If enforcement is disabled, returns None (no reservation created).
        If balance is insufficient, raises HTTPException 402.
        """
        if not self.enforcement:
            return None

        estimated = self.estimate_reservation(product_or_instance)
        reservation_id = await self.billing.reserve_funds(
            org_id=org_id,
            user_id=user_id,
            estimated_cost=str(estimated),
            operation_type=resource_type,
            metadata=metadata or {},
        )

        if reservation_id is None:
            # 402 — insufficient funds
            balance = await self.billing.get_balance(org_id)
            available = "0"
            if balance:
                available = str(getattr(balance, "available_balance", "0"))
            from fastapi import HTTPException
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "budget_exceeded",
                    "message": f"Insufficient budget (estimated ${estimated}/hr)",
                    "available_balance": available,
                    "estimated_cost": str(estimated),
                }
            )

        return reservation_id

    async def on_failure(self, org_id: str, reservation_id: Optional[str]) -> None:
        """Refund reservation on provisioning failure."""
        if reservation_id:
            await self.billing.refund_reservation(org_id, reservation_id, reason="launch_failed")
