"""Lane DOCS (pack 20260721) — binding for the doc COMPLETENESS control
(scripts/doc_lint.py C-rules).

Why it exists: the R-rules prove the docs don't teach the WRONG thing; they
could not see that ZERO of today's new consumer surface (doctor/provision,
keyspace_version/dual_write, connect_retry_seconds, auto_create_tables,
redis_scripting_confirmed) reached the Skills — a doc that silently omits a
required key is lint-clean and wrong. RED-first evidence: the control's first
run on the real repo produced 44 findings (recorded in the lane report /
board row DOC-A) before the surfaces were updated.

D-14: every plant below was chosen on the axis the instrument keys on
(schema dict / parser AST / registry JSON / remedy cell / stale code), not
just repeated instances of one shape. Stated limits live in the control's
own docstring (presence-not-quality; this repo only — Go docs are outside
its field of view).
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import doc_lint  # noqa: E402


def test_repo_docs_are_complete():
    """The real repo, after the lane's surface updates: zero C-findings."""
    findings = doc_lint.check_completeness()
    assert findings == [], "doc completeness regressions:\n" + "\n".join(findings)


def test_prose_rules_still_green():
    """Never relax an existing test: the R-rules stay green alongside C."""
    findings = doc_lint.run()
    assert findings == [], "doc-lint R-rule regressions:\n" + "\n".join(findings)


# ---- planted offenders (D-14 rule 1+2: one per axis the control keys on) ---

def test_planted_undocumented_schema_key_is_red():
    """D-14's named scenario: add a key to the schema without documenting it
    => RED. The plant goes through the same AST reader the control uses."""
    planted_src = (REPO / "ab0t_quota" / "config_schema.py").read_text().replace(
        '"redis_url": (str,),',
        '"redis_url": (str,),\n    "planted_undocumented_key_xyz": (str,),')
    keys = doc_lint.schema_storage_keys(planted_src)
    assert "planted_undocumented_key_xyz" in keys, "AST reader missed the plant"
    findings = doc_lint.check_completeness(storage_keys=keys)
    assert any("planted_undocumented_key_xyz" in f and f.startswith("C1")
               for f in findings), "an undocumented schema key was NOT flagged"


def test_planted_undocumented_verb_and_flag_are_red():
    src = (REPO / "ab0t_quota" / "__main__.py").read_text() + (
        '\ndef _planted(sub):\n'
        '    q = sub.add_parser("planted-verb-xyz", aliases=["planted-alias-xyz"])\n'
        '    q.add_argument("--planted-flag-xyz")\n')
    verbs, flags = doc_lint.cli_verbs_and_flags(src)
    assert {"planted-verb-xyz", "planted-alias-xyz"} <= set(verbs)
    assert "--planted-flag-xyz" in flags
    findings = doc_lint.check_completeness(verbs=verbs, flags=flags)
    text = "\n".join(findings)
    assert "planted-verb-xyz" in text and "planted-alias-xyz" in text \
        and "--planted-flag-xyz" in text, \
        "an undocumented verb/alias/flag was NOT flagged"


def test_planted_unregistered_and_undocumented_codes_are_red():
    # Axis 1: a registry code with no documented remedy.
    findings = doc_lint.check_completeness(
        codes=doc_lint.registry_codes() + ["QUOTA-CFG-998"])
    assert any("QUOTA-CFG-998" in f and "no row with a remedy" in f
               for f in findings), "an undocumented registry code was NOT flagged"
    # Axis 2 (inverted defect): a code in the docs that the registry does not
    # define. Simulated by REMOVING a known-documented code from the code list.
    codes = [c for c in doc_lint.registry_codes() if c != "QUOTA-CFG-001"]
    findings2 = doc_lint.check_completeness(codes=codes)
    assert any("QUOTA-CFG-001" in f and "NOT in the registry" in f
               for f in findings2), "a stale/invented documented code was NOT flagged"


def test_planted_empty_remedy_cell_is_red(tmp_path, monkeypatch):
    """A row that NAMES the code but ships an empty remedy cell must not
    count as documented (the zero-work pass)."""
    fake = tmp_path
    (fake / "docs").mkdir()
    (fake / "Skills").mkdir()
    (fake / doc_lint.ERROR_CODES_DOC).write_text(
        "| Code | Meaning | Remedy |\n|---|---|---|\n"
        "| QUOTA-CFG-001 | Redis undeclared |  |\n")
    findings = doc_lint.check_completeness(
        storage_keys=[], top_level_keys=[], verbs=[], flags=[],
        codes=["QUOTA-CFG-001"], repo=fake)
    assert any("QUOTA-CFG-001" in f and "no row with a remedy" in f
               for f in findings), "an empty remedy cell passed as documented"


def test_inflected_word_is_not_the_verb():
    """`provisioning` prose must not satisfy the `provision` verb requirement
    — this is exactly how the gap hid from a naive grep."""
    assert not doc_lint._has_token("provisioning the mesh credentials", "provision")
    assert doc_lint._has_token("run `provision --emit acl`", "provision")
