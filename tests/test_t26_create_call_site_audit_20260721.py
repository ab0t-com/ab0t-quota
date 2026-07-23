"""T-26 (pack 20260721): STRUCTURAL audit of D-3's promise.

D-3 moved `auto_create_tables` enforcement out of `ensure_table` on the promise
that the audit makes bypass impossible. The Gate-C verifier proved the wired-up
fixture audit cannot see the handler-ledger call site (`setup.py`, D-82 block):
a bypass planted there passed 25 tests. This audit enumerates the call sites
from the SOURCE (AST over setup.py), so a bypass at ANY site — including one
added tomorrow — goes red the day it exists.

Rule: every `*.ensure_table(...)` / `*.initialize(...)` call in setup.py must
pass a `create=` keyword whose expression derives from the config policy
(references `auto_create` / `auto_create_tables`) — never omitted, never a
bare `True`.
"""
from __future__ import annotations

import ast
from pathlib import Path

SETUP = Path(__file__).resolve().parents[1] / "ab0t_quota" / "setup.py"

#: The four sites D-3/T-6 gated. A NEW site is allowed (the rule still binds
#: it); a DISAPPEARING site fails the count so the audit cannot rot silently.
EXPECTED_MIN_SITES = 4


def _provision_calls(src: str):
    """(lineno, method, create_expr_source | None) for every ensure_table /
    initialize call in setup.py."""
    tree = ast.parse(src)
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in ("ensure_table", "initialize"):
            continue
        create_kw = next((k for k in node.keywords if k.arg == "create"), None)
        seg = ast.get_source_segment(src, create_kw.value) if create_kw else None
        out.append((node.lineno, node.func.attr, seg))
    return out


def test_every_provision_call_site_passes_the_policy():
    src = SETUP.read_text()
    calls = _provision_calls(src)
    assert len(calls) >= EXPECTED_MIN_SITES, \
        (f"audit found only {len(calls)} ensure_table/initialize call site(s) in "
         f"setup.py — expected >= {EXPECTED_MIN_SITES}; if a site was removed "
         f"deliberately, update EXPECTED_MIN_SITES with a work-log note")
    offenders = []
    for lineno, method, seg in calls:
        if seg is None:
            offenders.append(f"setup.py:{lineno} {method}(...) omits create= "
                             f"(primitive defaults to CREATE — bypass)")
        elif "auto_create" not in seg:
            offenders.append(f"setup.py:{lineno} {method}(create={seg}) does not "
                             f"derive from storage.auto_create_tables — bypass")
    assert offenders == [], \
        "D-3 call-site policy bypassed:\n  " + "\n  ".join(offenders)


def test_audit_sees_the_ledger_site_specifically():
    """Pin the exact blind spot the verifier found: the D-82 handler-ledger
    call (`_ledger.ensure_table`) must be among the enumerated sites."""
    src = SETUP.read_text()
    ledger_lines = [i + 1 for i, l in enumerate(src.splitlines())
                    if "_ledger.ensure_table" in l]
    assert ledger_lines, "the D-82 ledger provision call disappeared from setup.py"
    audited = {lineno for lineno, _, _ in _provision_calls(src)}
    assert any(l in audited for l in ledger_lines), \
        f"the ledger call site(s) {ledger_lines} are invisible to the audit"


def test_scanner_flags_a_planted_bypass(tmp_path):
    """NC (Rule C): the scanner itself can go red — a scratch file with an
    ungated call and a create=True call must both be flagged."""
    scratch = ("async def f(store, auto_create):\n"
               "    await store.ensure_table()\n"
               "    await store.initialize(create=True)\n"
               "    await store.ensure_table(create=auto_create)\n")
    calls = _provision_calls(scratch)
    assert len(calls) == 3
    bad = [(m, seg) for _, m, seg in calls
           if seg is None or "auto_create" not in seg]
    assert bad == [("ensure_table", None), ("initialize", "True")]
