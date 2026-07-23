"""ST-RESOLVE-1 Clause 7 — the PYTHON binding of the D-2 retry contract (T-27).

The retry numbers (default 30 / 0 = immediate / 0.5s doubling capped at 5s /
unreachable-kind-only) are a CROSS-RUNTIME CONTRACT declared in
scenarios.json's ``retry_contract``. Go binds them as compiled constants
(quota/st_resolve_1_binding_20260721_test.go); Python's live in literals
inside ``_gate_redis_reachable`` (ab0t_quota/setup.py), so this binding
extracts them by AST — the house structural-audit pattern — and compares.
Planted drift on either side turns one of the two bindings RED; agreement is
no longer maintained by a human having diffed the other runtime (PAR-01's
shape, rebuilt inside its own fix).

Placed in conformance/ (the shared spine's home; lane G is authorised to
write here and nowhere else in this repo). Runs standalone:
    .venv/bin/pytest conformance/test_retry_contract_binding_20260721.py -q

D-14: ``test_planted_drift_is_detected`` proves the extractor can fail.
STATED LIMIT: the AST scan reads the literals at their known shapes
(.get("connect_retry_seconds", N) / delay = X / min(delay * 2, CAP) /
kind == "unreachable"); a rewrite of ``_gate_redis_reachable`` that moves
them into config or renames the function must update this binding — the
binding failing loudly on extraction is the desired outcome then, never a
silent pass.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _declared_retry_contract():
    doc = json.loads((REPO / "conformance" / "scenarios.json").read_text())
    item = next(i for i in doc["structural_conformance"] if i["id"] == "ST-RESOLVE-1")
    rc = item.get("retry_contract")
    assert rc, ("ST-RESOLVE-1 declares no retry_contract — the D-2 numbers are back to "
                "agreement-by-diffing (T-27's defect)")
    clauses = item["contract"]
    assert len(clauses) >= 7 and clauses[6].startswith("Clause 7"), "Clause 7 missing"
    return rc


class _GateVisitor(ast.NodeVisitor):
    """Extracts the D-2 literals from _gate_redis_reachable's AST."""

    def __init__(self):
        self.default = None        # .get("connect_retry_seconds", N)
        self.initial = None        # delay = X (first numeric assignment to `delay`)
        self.cap = None            # min(delay * 2, CAP)
        self.kind_guard = False    # kind == "unreachable" gating the retry

    def visit_Call(self, node):
        if (isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                and node.args and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "connect_retry_seconds"
                and len(node.args) > 1 and isinstance(node.args[1], ast.Constant)):
            self.default = node.args[1].value
        if (isinstance(node.func, ast.Name) and node.func.id == "min"
                and len(node.args) == 2 and isinstance(node.args[1], ast.Constant)):
            self.cap = node.args[1].value
        self.generic_visit(node)

    def visit_Assign(self, node):
        if (len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "delay" and isinstance(node.value, ast.Constant)
                and self.initial is None):
            self.initial = node.value.value
        self.generic_visit(node)

    def visit_Compare(self, node):
        if (isinstance(node.left, ast.Name) and node.left.id == "kind"
                and any(isinstance(c, ast.Constant) and c.value == "unreachable"
                        for c in node.comparators)):
            self.kind_guard = True
        self.generic_visit(node)


def _extract_from_source(source: str):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_gate_redis_reachable":
            v = _GateVisitor()
            v.visit(node)
            return v
    raise AssertionError(
        "_gate_redis_reachable not found — the D-2 gate moved; re-point this binding "
        "(a loud extraction failure is the desired outcome, never a silent pass)")


def test_python_retry_literals_match_declared_contract():
    rc = _declared_retry_contract()
    v = _extract_from_source((REPO / "ab0t_quota" / "setup.py").read_text())
    assert v.default == rc["default_seconds"], (
        f"Python default {v.default} != declared {rc['default_seconds']} — runtime drifted")
    assert v.initial == rc["backoff_initial_seconds"], (
        f"Python backoff initial {v.initial} != declared {rc['backoff_initial_seconds']}")
    assert v.cap == rc["backoff_cap_seconds"], (
        f"Python backoff cap {v.cap} != declared {rc['backoff_cap_seconds']}")
    assert v.kind_guard and rc["retries_kind"] == "unreachable", (
        "the retry loop is no longer gated on kind == 'unreachable' — auth failures "
        "would consume the budget (a slower wrong password)")
    assert rc["auth_never_consumes_budget"] is True


def test_planted_drift_is_detected():
    """D-14: the extractor must be able to fail — a planted default of 45
    (and a missing kind guard) must be seen as such."""
    planted = '''
async def _gate_redis_reachable(app, redis, config, plan):
    budget = float(storage_cfg.get("connect_retry_seconds", 45))
    delay = 0.7
    while True:
        delay = min(delay * 2, 9.0)
'''
    v = _extract_from_source(planted)
    assert v.default == 45 and v.initial == 0.7 and v.cap == 9.0, (
        "extractor failed to read planted literals — the binding cannot detect drift")
    assert not v.kind_guard, "planted source has no kind guard; extractor claims one"
    rc = _declared_retry_contract()
    assert v.default != rc["default_seconds"], "planted drift not distinguishable"
