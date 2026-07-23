"""Ticket 20260722_end_customer_experience_defects — TICKET_config_is_king.md
§5c "Other Python leaks", row 3 (board row CK-B3).

THE DEFECT
----------
`BudgetChecker._build_cost_table` registered every product under its product_id
using the default variant, and then:

    # For sandbox-type products with instance_type variants
    if product_id == "sandbox":
        for itype, v in variants.items():
            costs[itype] = {...}

so a product's variants were addressable by variant name **only if the product
happened to be called "sandbox"**. `get_costs` / `estimate_reservation` /
`pre_launch_check` all take a parameter literally named `product_or_instance`,
so addressing a variant is the DECLARED api — the name check is an unfinished
generalisation, not a capability.

WHY THIS IS A FIX AND NOT A FRAMED QUESTION
-------------------------------------------
Checked against `billing/config.py::PRICING_SCHEMA`: `products` is
`additionalProperties` over `{display_name, variants}`, and `variants` is
`additionalProperties` over the price fields. **Nothing in the schema marks a
product as sandbox-shaped or grants it variant addressing**; no config surface
is missing. Every product with named variants has exactly the shape the branch
was checking for by name. So the honest fix is to make it data-driven.

THE CONTRACT
------------
  * Every product's variants are addressable, for every product.
  * `"<product>:<variant>"` ALWAYS resolves — unambiguous by construction.
  * A bare variant name resolves only when exactly ONE product declares it.
    Two products declaring `"small"` would otherwise silently serve one
    product's price for the other's variant — on the reservation path. Ambiguous
    names are left unregistered and logged; the qualified form still works.
  * A product_id is never shadowed by a variant of the same name.
"""

import ast
import inspect
import logging
import pathlib
from decimal import Decimal

import pytest

from ab0t_quota.billing.budget import (
    BudgetChecker, PricingAmbiguousError, PricingNotDeclaredError,
)


def _checker(products, **kw):
    return BudgetChecker(billing_client=None, pricing_config={"products": products}, **kw)


# Two products, each with named variants. NEITHER is called "sandbox".
NEUTRAL_PRICING = {
    "vm": {
        "display_name": "Virtual Machine",
        "variants": {
            "vm.standard": {"price_per_hour": 0.20, "allocation_price": 0.02, "default": True},
            "vm.gpu": {"price_per_hour": 2.50, "allocation_price": 0.25},
        },
    },
    "worker": {
        "display_name": "Worker",
        "variants": {
            "worker.small": {"price_per_hour": 0.05, "allocation_price": 0.005, "default": True},
        },
    },
}


# ---------------------------------------------------------------------------
# RED — variant addressing works for every product, not just one named product
# ---------------------------------------------------------------------------

def test_RED_non_sandbox_product_variants_are_addressable():
    """Pre-fix, `vm.gpu` fell through to the invented 0.10/0.01 default because
    the product was not called "sandbox" — a GPU variant priced as a
    commodity box, on the reservation path."""
    c = _checker(NEUTRAL_PRICING)
    costs = c.get_costs("vm.gpu")
    assert costs["hourly_rate"] == Decimal("2.5"), (
        "a declared variant must be priced from config for EVERY product, not "
        f"only for one named 'sandbox'. Got {costs['hourly_rate']} (the "
        "hardcoded fallback is 0.10)"
    )
    assert costs["allocation_fee"] == Decimal("0.25"), (
        f"allocation_price must come from the variant; got {costs['allocation_fee']}"
    )


def test_RED_estimate_reservation_uses_the_variant_price():
    """Own assertion, one layer up: the money-shaped call reserves the wrong
    amount whenever variant lookup silently misses."""
    c = _checker(NEUTRAL_PRICING)
    assert c.estimate_reservation("vm.gpu") == Decimal("2.75"), (
        "reservation = allocation_fee + 1h runtime = 0.25 + 2.50; got "
        f"{c.estimate_reservation('vm.gpu')}"
    )


def test_RED_no_product_name_literal_in_the_module():
    """Structural (D-CK-5): pricing behaviour may not branch on a consumer's
    product name. Catches the `== "sandbox"` comparison in any direction."""
    src = pathlib.Path(inspect.getfile(BudgetChecker)).read_text()
    offenders = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            for op in operands:
                if isinstance(op, ast.Constant) and isinstance(op.value, str):
                    if op.value.lower() in ("sandbox", "sandbox-platform"):
                        offenders.append(f"line {node.lineno}: comparison against {op.value!r}")
    assert not offenders, (
        "budget.py must be data-driven, not name-driven — one consumer's "
        "product name may not gate a pricing capability. " + "; ".join(offenders)
    )


# ---------------------------------------------------------------------------
# The disambiguation rules — one assertion each
# ---------------------------------------------------------------------------

def test_product_id_still_resolves_to_its_default_variant():
    """Unchanged behaviour: the bare product id prices from its default."""
    c = _checker(NEUTRAL_PRICING)
    assert c.get_costs("vm")["hourly_rate"] == Decimal("0.20")
    assert c.get_costs("worker")["hourly_rate"] == Decimal("0.05")


def test_qualified_product_colon_variant_always_resolves():
    c = _checker(NEUTRAL_PRICING)
    assert c.get_costs("vm:vm.gpu")["hourly_rate"] == Decimal("2.5")
    assert c.get_costs("worker:worker.small")["hourly_rate"] == Decimal("0.05")


def test_ambiguous_bare_variant_is_refused_not_silently_attributed(caplog):
    """Two products both declaring `small`: serving either price for a bare
    `small` would be a plausible wrong answer on the money path. The bare name
    is left unregistered (falls to the stated default) and the collision is
    logged; the qualified form still resolves both."""
    pricing = {
        "vm": {"display_name": "VM", "variants": {
            "small": {"price_per_hour": 1.00, "allocation_price": 0.10, "default": True}}},
        "worker": {"display_name": "Worker", "variants": {
            "small": {"price_per_hour": 0.01, "allocation_price": 0.001, "default": True}}},
    }
    with caplog.at_level(logging.WARNING, logger="ab0t_quota.billing.budget"):
        c = _checker(pricing)

    assert any("small" in r.getMessage() for r in caplog.records), (
        "an ambiguous variant name must be reported, not silently dropped"
    )
    assert c.get_costs("vm:small")["hourly_rate"] == Decimal("1.00")
    assert c.get_costs("worker:small")["hourly_rate"] == Decimal("0.01")

    with pytest.raises(PricingAmbiguousError) as exc:
        c.get_costs("small")
    msg = str(exc.value)
    assert "vm" in msg and "worker" in msg, (
        f"the refusal must name every claimant so it is actionable: {msg}")
    assert "vm:small" in msg or "<product>:small" in msg, (
        f"the refusal must state the qualified remedy: {msg}")


def test_variant_never_shadows_a_product_id():
    """A variant named after another product must not overwrite that product's
    own entry."""
    pricing = {
        "vm": {"display_name": "VM", "variants": {
            "worker": {"price_per_hour": 9.99, "allocation_price": 0.99, "default": True}}},
        "worker": {"display_name": "Worker", "variants": {
            "worker.small": {"price_per_hour": 0.05, "allocation_price": 0.005, "default": True}}},
    }
    c = _checker(pricing)
    assert c.get_costs("worker")["hourly_rate"] == Decimal("0.05"), (
        "the product id 'worker' must keep its own default-variant price; a "
        f"variant of 'vm' shadowed it. Got {c.get_costs('worker')['hourly_rate']}"
    )


def test_RED_unknown_key_refuses_instead_of_inventing_a_price():
    """§11.2 REPLACEMENT of `test_unknown_key_still_falls_back_to_the_stated_default`
    (D-CK-11). That test PINNED the leak: it certified that an undeclared key
    silently returns $0.10/h + $0.01. This is a config-driven client — a price
    the config never stated is invented money, and it flows
    get_costs → estimate_reservation → pre_launch_check → reserve_funds, i.e.
    it reserves real balance at a rate nobody agreed. Refusing is the same
    honest-empty rule as D-CK-4."""
    c = _checker(NEUTRAL_PRICING)
    with pytest.raises(PricingNotDeclaredError) as exc:
        c.get_costs("nothing-declared-here")
    msg = str(exc.value)
    assert "nothing-declared-here" in msg, msg
    assert "vm" in msg and "worker" in msg, (
        f"the refusal must say what IS declared so it is actionable: {msg}")


def test_RED_empty_pricing_config_refuses_rather_than_inventing():
    c = _checker({})
    with pytest.raises(PricingNotDeclaredError):
        c.get_costs("anything")


def test_estimate_reservation_propagates_the_refusal():
    """The money-shaped caller must not reserve against an invented rate."""
    c = _checker(NEUTRAL_PRICING)
    with pytest.raises(PricingNotDeclaredError):
        c.estimate_reservation("nothing-declared-here")


@pytest.mark.asyncio
async def test_enforcement_disabled_never_reaches_pricing():
    """Safety valve preserved: with enforcement off there is no reservation and
    therefore no pricing lookup to refuse."""
    c = _checker(NEUTRAL_PRICING, enforcement_enabled=False)
    assert await c.pre_launch_check("o", "u", "nothing-declared-here") is None


def test_a_product_literally_named_sandbox_gets_no_special_treatment():
    """The old branch's beneficiary must now be served by the SAME general
    rule as everyone else — no better, no worse."""
    pricing = {
        "sandbox": {"display_name": "Sandbox", "variants": {
            "t3.small": {"price_per_hour": 0.30, "allocation_price": 0.03, "default": True},
            "t3.large": {"price_per_hour": 0.90, "allocation_price": 0.09},
        }},
    }
    c = _checker(pricing)
    assert c.get_costs("sandbox")["hourly_rate"] == Decimal("0.30")
    assert c.get_costs("t3.large")["hourly_rate"] == Decimal("0.90"), (
        "the behaviour the name-check used to provide must survive as the "
        "general rule — this is a generalisation, not a removal"
    )
    assert c.get_costs("sandbox:t3.large")["hourly_rate"] == Decimal("0.90")


# ---------------------------------------------------------------------------
# REAL-CONFIG BINDING — the ambiguity rule is not hypothetical.
#
# Verified 2026-07-22 against sandbox-platform's live `quota-config.json` (THE
# reference consumer). Its `pricing.products` declares:
#     fargate    → browser, desktop, ephemeral   (3 products)
#     warm_pool  → browser, desktop, ephemeral   (3 products)
#     eks        → browser only
#     t3.* / g4dn.* → sandbox only
#
# So the collision case is LIVE, today, in the config that ships. Had the
# generalisation used first-wins, `get_costs("warm_pool")` would have returned
# browser's $0.10/h for a DESKTOP warm_pool priced at $0.20/h — a 2x
# under-reservation on the money path, silently, on real config. Refusing the
# ambiguous bare name keeps the pre-fix behaviour (falls to the stated default)
# and the qualified form resolves all three correctly.
#
# Net effect on the reference consumer: byte-identical everywhere EXCEPT `eks`,
# which is claimed by exactly one product and now resolves to its declared
# $0.08/h instead of the invented $0.10 default. Zero regression, one real fix.
# ---------------------------------------------------------------------------

SANDBOX_CONFIG = pathlib.Path(
    "/home/ubuntu/infra/infra/code/resource/output/sandbox-platform/quota-config.json"
)


def _real_pricing():
    import json
    if not SANDBOX_CONFIG.exists():
        pytest.skip(f"reference consumer config not present at {SANDBOX_CONFIG}")
    return json.loads(SANDBOX_CONFIG.read_text())["pricing"]


def test_REALCONFIG_ambiguous_variant_names_actually_occur():
    """If this ever stops being true the rule is untested by the real world —
    fail loudly rather than keep a rule nobody exercises."""
    products = _real_pricing()["products"]
    claims = {}
    for pid, p in products.items():
        for v in (p.get("variants") or {}):
            claims.setdefault(v, []).append(pid)
    ambiguous = {v: ps for v, ps in claims.items() if len(ps) > 1}
    assert ambiguous, (
        "the reference consumer no longer declares any variant name shared "
        "across products — re-check whether the ambiguity rule still earns its "
        "complexity"
    )
    assert "warm_pool" in ambiguous, f"expected warm_pool collision; got {ambiguous}"


def test_REALCONFIG_desktop_warm_pool_is_not_priced_as_a_browser():
    """The exact 2x under-reservation first-wins would have caused."""
    c = BudgetChecker(billing_client=None, pricing_config=_real_pricing())
    assert c.get_costs("desktop:warm_pool")["hourly_rate"] == Decimal("0.20")
    assert c.get_costs("browser:warm_pool")["hourly_rate"] == Decimal("0.10")
    with pytest.raises(PricingAmbiguousError) as exc:
        c.get_costs("warm_pool")
    assert {"browser", "desktop", "ephemeral"} <= set(str(exc.value).split()) or all(
        p in str(exc.value) for p in ("browser", "desktop", "ephemeral")), str(exc.value)


def test_REALCONFIG_no_regression_for_the_instance_types_that_worked_before():
    """Pre-fix, only `sandbox` got variant expansion. Every one of its instance
    types must still resolve to exactly its declared price."""
    pricing = _real_pricing()
    c = BudgetChecker(billing_client=None, pricing_config=pricing)
    for itype, v in pricing["products"]["sandbox"]["variants"].items():
        assert c.get_costs(itype)["hourly_rate"] == Decimal(str(v["price_per_hour"])), (
            f"{itype} regressed"
        )
        assert c.get_costs(itype)["allocation_fee"] == Decimal(str(v["allocation_price"]))


def test_REALCONFIG_uniquely_claimed_variant_is_newly_correct():
    """`eks` is declared by browser alone. Pre-fix it fell to the invented 0.10
    default; it now prices from config."""
    c = BudgetChecker(billing_client=None, pricing_config=_real_pricing())
    assert c.get_costs("eks")["hourly_rate"] == Decimal("0.08"), (
        "a uniquely-claimed variant must price from config"
    )
