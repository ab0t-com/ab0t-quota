"""ab0t_quota.billing — Drop-in billing & payment proxy for mesh services.

Mount all billing, payment, checkout, and webhook routes with one call:

    from ab0t_quota.billing import create_billing_router

    app.include_router(create_billing_router(
        payment_url="http://payment:8005",
        payment_api_key="ab0t_sk_live_...",
        billing_url="http://billing:8002",
        billing_api_key="ab0t_sk_live_...",
        consumer_org_id="...",
    ))

Provides 20 routes: plans, checkout (auth + anonymous), portal, top-up,
subscriptions, invoices, payment methods, billing balance/usage/transactions,
webhook forwarding, and post-checkout processing with defense-in-depth.

Requires: pip install ab0t-quota[billing]
"""

from __future__ import annotations

from .clients import PaymentServiceClient, PaymentServiceError, BillingServiceClient, BillingServiceError
from .lifecycle import LifecycleEmitter
from .budget import BudgetChecker
from .heartbeat import HeartbeatMonitor
from .config import load_pricing


def create_billing_router(**kwargs):
    """Create a FastAPI router with all billing & payment proxy routes.

    Lazy-imported to avoid requiring jinja2 at module level.
    See router.py for full parameter docs.
    """
    from .router import create_billing_router as _create
    return _create(**kwargs)


__all__ = [
    "create_billing_router",
    "PaymentServiceClient",
    "PaymentServiceError",
    "BillingServiceClient",
    "BillingServiceError",
    "LifecycleEmitter",
    "BudgetChecker",
    "HeartbeatMonitor",
    "load_pricing",
]
