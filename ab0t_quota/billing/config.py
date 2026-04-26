"""Pricing config loader for quota-config.json.

Usage:
    from ab0t_quota.billing.config import load_pricing

    pricing = load_pricing("quota-config.json")
    # pricing = {"currency": "USD", "products": {...}, ...}
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("ab0t_quota.billing.config")


def load_pricing(config_path: str = "quota-config.json") -> Dict[str, Any]:
    """Load the pricing section from quota-config.json.

    Returns the pricing dict ready for BudgetChecker.
    Returns empty dict if file not found or no pricing section.
    """
    path = Path(config_path)
    if not path.exists():
        logger.warning("pricing_config_not_found: %s", config_path)
        return {}

    try:
        with open(path) as f:
            config = json.load(f)
        pricing = config.get("pricing", {})
        if not pricing:
            logger.warning("no_pricing_section: %s", config_path)
        return pricing
    except Exception as e:
        logger.error("pricing_config_load_error: %s %s", config_path, e)
        return {}


# JSON Schema for the pricing section of quota-config.json
PRICING_SCHEMA = {
    "type": "object",
    "properties": {
        "currency": {"type": "string", "default": "USD"},
        "billing_model": {"type": "string", "enum": ["per_minute", "per_hour", "per_second"]},
        "min_billing_seconds": {"type": "integer", "default": 60},
        "refund_on_stop": {"type": "boolean", "default": True},
        "surge": {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "multiplier": {"type": "number"},
                "description": {"type": "string"},
            },
        },
        "products": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "required": ["display_name", "variants"],
                "properties": {
                    "display_name": {"type": "string"},
                    "description": {"type": "string"},
                    "variants": {
                        "type": "object",
                        "additionalProperties": {
                            "type": "object",
                            "required": ["price_per_hour", "allocation_price"],
                            "properties": {
                                "compute": {
                                    "type": "string",
                                    "description": "Compute backend: fargate, fargate_pool, ec2, ec2_gpu, eks",
                                },
                                "cpu": {"type": "integer", "description": "CPU units (1024 = 1 vCPU)"},
                                "memory": {"type": "integer", "description": "Memory in MB"},
                                "cost_per_hour": {
                                    "type": "number",
                                    "description": "Internal: what we pay AWS per hour",
                                },
                                "price_per_hour": {
                                    "type": "number",
                                    "description": "Customer-facing: what we charge per hour",
                                },
                                "allocation_cost": {
                                    "type": "number",
                                    "description": "Internal: provisioning cost to us",
                                },
                                "allocation_price": {
                                    "type": "number",
                                    "description": "Customer-facing: one-time provisioning fee",
                                },
                                "default": {
                                    "type": "boolean",
                                    "description": "Whether this is the default variant for the product",
                                },
                                "note": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
    },
}

# Lifecycle event schema
LIFECYCLE_EVENT_SCHEMA = {
    "type": "object",
    "required": ["event_type", "org_id", "resource_id", "resource_type"],
    "properties": {
        "event_type": {
            "type": "string",
            "enum": ["resource.started", "resource.stopped", "resource.deleted", "resource.heartbeat"],
        },
        "org_id": {"type": "string"},
        "user_id": {"type": "string"},
        "resource_id": {"type": "string"},
        "resource_type": {"type": "string", "description": "Product ID (browser, desktop, sandbox, etc.)"},
        "reservation_id": {"type": ["string", "null"]},
        "instance_type": {"type": ["string", "null"]},
        "hourly_rate": {"type": ["string", "null"], "description": "Customer price per hour"},
        "allocation_fee": {"type": ["string", "null"], "description": "Customer allocation price"},
        "started_at": {"type": ["string", "null"], "format": "date-time"},
        "stopped_at": {"type": ["string", "null"], "format": "date-time"},
        "reason": {
            "type": "string",
            "description": "Why this event occurred",
            "examples": [
                "provisioned", "user_stopped", "user_deleted",
                "idle_timeout", "max_runtime_exceeded", "heartbeat_timeout",
                "released_to_pool", "launch_failed",
            ],
        },
        "metadata": {"type": "object"},
        "emitted_at": {"type": "string", "format": "date-time"},
    },
}
