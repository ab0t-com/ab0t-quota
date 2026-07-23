"""T-11 doc-lint controls (D-14): the lint is green over the repaired surface,
each rule is proven able to go RED by planted offenders SPANNING FORMS (prose,
fenced block, json value — the axis the instrument keys on), and the exemption
list is FROZEN so widening it is a visible diff, never a quiet coverage loss.

Also the Gate E class-1 twin, by execution not lint: shipped
`quota-config*.json` files must be STANDALONE-bootable configs — accepted by
preflight with the namespaced env CLEARED (a copy-paste file that only works
because the harness env declares a store would be the runbook defect shape).
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("doc_lint", REPO / "scripts" / "doc_lint.py")
doc_lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_spec and doc_lint)


def test_doc_lint_green_over_repaired_surface():
    findings = doc_lint.run()
    assert findings == [], "doc-lint findings:\n" + "\n".join(findings)


@pytest.mark.parametrize("name,text", [
    # R1 across forms: bare prose, markdown bullet, json env-block value
    ("r1-prose", "Set the REDIS_URL environment variable to point at Redis.\n"),
    ("r1-bullet", "- `STRIPE_WEBHOOK_SECRET` — webhook signing secret\n"),
    ("r1-json", '{\n  "env": "AUTH_SERVICE_URL=https://auth"\n}\n'),
    # R2 across forms: template default, fenced json value
    ("r2-shell", 'URL="${QUOTA_X:-redis://localhost:6379/0}"\n'),
    ("r2-json", '```json\n{"storage": {"x": "redis://localhost:6379/0"}}\n```\n'),
    # R3: bare fail-open advice
    ("r3-advice", "If the engine is unavailable, fall back gracefully (fail-open).\n"),
])
def test_planted_offender_turns_each_rule_red(name, text):
    findings = doc_lint.lint_text(f"planted-{name}.md", text)
    assert findings, f"planted offender {name!r} was NOT flagged: {text!r}"


def test_negation_context_is_honoured_not_blanket():
    """Control the other way: a truthful negated mention passes (the lint must
    not force docs into silence about the removed names)."""
    ok = doc_lint.lint_text(
        "ok.md", "The generic `REDIS_URL` is never read since 0.7.\n")
    assert ok == []


def test_exemption_list_frozen():
    """D-14 rule 3: widening the exemptions is a two-file diff, not a quiet
    loss. Each entry's justification lives in scripts/doc_lint.py's docstring."""
    assert tuple(doc_lint.EXEMPT_PATHS) == (
        "CHANGELOG.md",
        "docs/migrating-from-ambient-resolution.md",
    ), "exemption list changed — justify the new entry in doc_lint.py AND here"


def test_fragment_exemption_list_frozen():
    """The execution control's `doc-exec: fragment` markers, frozen (D-14
    rule 3). Five exist, added at the Gate-D remediation — each is a genuine
    SHAPE ILLUSTRATION (ellipsis key-map or single-feature snippet), never a
    copy-paste path; every full-config claim executes. Justifications live in
    each marker in place; widening this list is a two-file diff."""
    # T-29 R-1: ONE field of view — reuse the execution control's DOC_ROOTS,
    # never a private copy (a freeze test with its own narrower scan surface
    # is the R-1 defect inside the instrument's own control).
    from tests.test_doc_config_execution_20260721 import DOC_ROOTS
    hits = []
    for root in DOC_ROOTS:
        p = REPO / root
        files = [p] if p.is_file() else sorted(p.rglob("*.md"))
        for f in files:
            for i, line in enumerate(f.read_text().splitlines(), 1):
                if "doc-exec: fragment" in line:
                    hits.append(str(f.relative_to(REPO)))
    assert sorted(hits) == [
        "BILLING_MODELS_GUIDE.md",
        "README.md",
        "Skills/quota-paid-tier-onboarding/references/billing-models-guide.md",
        "Skills/quota-tier-management/references/config-schema.md",
        "Skills/quota-tier-management/references/payment-tier-flow.md",
        "docs/quickstart.md",
    ], f"fragment exemptions changed — justify here AND in the marker: {hits}"


# --- Gate E class 1, by execution: shipped configs stand alone --------------

def _shipped_config_files():
    hits = [REPO / "quota-config.example.json"]
    for d in ("docs", "Skills", "examples"):
        p = REPO / d
        if p.is_dir():
            hits += sorted(p.rglob("quota-config*.json"))
    return [h for h in hits if h.exists()]


@pytest.mark.parametrize("path", _shipped_config_files(),
                         ids=lambda p: str(p.relative_to(REPO)))
def test_shipped_config_file_is_standalone_bootable(path, tmp_path, monkeypatch):
    """A shipped quota-config*.json must be accepted by preflight with the
    namespaced env CLEARED — it must declare its own store, not borrow one
    from the environment (the Go runbook defect, class 1)."""
    from tests.dnd_harness_20260721 import install_no_contact
    from ab0t_quota.preflight import run_preflight
    install_no_contact(monkeypatch)
    for name in ("QUOTA_REDIS_URL", "QUOTA_REDIS_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("QUOTA_CONFIG_PATH", str(path))
    report = asyncio.run(run_preflight(offline=True, emit=lambda s: None))
    assert report.exit_code == 0, \
        (f"{path.relative_to(REPO)} is NOT standalone-bootable — a consumer "
         f"copying it gets a refused boot:\n" + "\n".join(report.config_errors))


def test_standalone_check_flags_a_planted_storeless_config(tmp_path, monkeypatch):
    """D-14 rule 1 for the class-1 check: a config with no declared store must
    be refused once the env is cleared."""
    from tests.dnd_harness_20260721 import install_no_contact
    from ab0t_quota.preflight import run_preflight
    install_no_contact(monkeypatch)
    monkeypatch.delenv("QUOTA_REDIS_URL", raising=False)
    p = tmp_path / "quota-config.json"
    p.write_text(json.dumps({"tiers": [], "storage": {"persistence_enabled": False}}))
    monkeypatch.setenv("QUOTA_CONFIG_PATH", str(p))
    report = asyncio.run(run_preflight(offline=True, emit=lambda s: None))
    assert report.exit_code == 2, "the planted store-less config must be refused"
