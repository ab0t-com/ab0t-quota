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
        self._product_costs = self._build_cost_table()

    def _build_cost_table(self) -> Dict[str, Dict[str, Decimal]]:
        """Build product → {hourly_rate, allocation_fee} from pricing config."""
        products = self.pricing.get("products", {})
        costs = {}
        for product_id, product in products.items():
            variants = product.get("variants", {})
            # Use default variant
            default = None
            for v in variants.values():
                if v.get("default") or default is None:
                    default = v
            if default:
                costs[product_id] = {
                    "hourly_rate": Decimal(str(default.get("price_per_hour", "0.10"))),
                    "allocation_fee": Decimal(str(default.get("allocation_price", "0.01"))),
                }
            # For sandbox-type products with instance_type variants
            if product_id == "sandbox":
                for itype, v in variants.items():
                    costs[itype] = {
                        "hourly_rate": Decimal(str(v.get("price_per_hour", "0.10"))),
                        "allocation_fee": Decimal(str(v.get("allocation_price", "0.01"))),
                    }
        return costs

    def get_costs(self, product_or_instance: str) -> Dict[str, Decimal]:
        """Get pricing for a product or instance type."""
        return self._product_costs.get(product_or_instance, {
            "hourly_rate": Decimal("0.10"),
            "allocation_fee": Decimal("0.01"),
        })

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
