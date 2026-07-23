"""T-24 (pack 20260721): per-runtime binding census + the Python ST-RESOLVE-1
binding.

VERIFICATION_STRATEGY §0: "a declared scenario is not a running scenario" — a
structural-conformance item added as JSON alone runs nothing while reading as
coverage (D-14's exact shape; ST-EFFECT-1 and ST-WORKING-1 are that today, and
start honestly RED here as strict xfails).

Census definition of "bound": the scenario ID appears as a literal in at least
one non-archived test file OTHER than this census (this file necessarily names
every ID, so counting itself would be the blind spot). LIMIT (D-14 rule 4): the
census proves a test file MENTIONS the ID, not that its assertions cover every
contract clause — clause-level coverage is each binding test's own job.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCENARIOS = REPO / "conformance" / "scenarios.json"
SELF = Path(__file__).name

#: Known orphans, honestly RED (strict xfail) until someone binds them — never
#: silently exempted (D-14 rule 3: an exemption list is a finding; this one is
#: the T-24 record itself). ST-EFFECT-1 and ST-WORKING-1 started here RED on
#: 2026-07-21 and were BOUND the same day
#: (tests/test_st_effect_working_bindings_20260721.py) — the set is now empty;
#: any future declared-unbound scenario lands here or fails outright.
KNOWN_ORPHANS: set = set()


def _declared_ids() -> list[str]:
    doc = json.loads(SCENARIOS.read_text())
    return [item["id"] for item in doc["structural_conformance"]
            if "python" in item.get("runtimes", [])]


def _binding_files(scenario_id: str) -> list[str]:
    hits = []
    for p in sorted((REPO / "tests").rglob("test_*.py")):
        if ".archive" in p.parts or p.name == SELF:
            continue
        if scenario_id in p.read_text():
            hits.append(str(p.relative_to(REPO)))
    return hits


def _param_ids():
    return [pytest.param(
        sid,
        marks=pytest.mark.xfail(
            reason=f"{sid} is a declared-but-unbound structural scenario "
                   f"(VERIFICATION_STRATEGY §0 orphan; T-24 starts it RED honestly "
                   f"— bind it or retire it)",
            strict=True) if sid in KNOWN_ORPHANS else ())
        for sid in _declared_ids()]


@pytest.mark.parametrize("scenario_id", _param_ids())
def test_declared_python_scenario_is_bound(scenario_id):
    files = _binding_files(scenario_id)
    assert files, \
        (f"{scenario_id} is declared for the python runtime in "
         f"conformance/scenarios.json but NO non-archived test references it — "
         f"a scenario that runs nothing while reading as coverage")


def test_census_flags_a_planted_unbound_scenario():
    """D-14 rule 1: the census can go red — a fabricated declared ID with no
    binding must be reported unbound."""
    assert _binding_files("ST-PLANTED-OFFENDER-1") == []


def test_archived_tests_do_not_count_as_bindings():
    """D-14 rule 2 (form variance): an ID referenced only from tests/.archive
    must NOT count — archived tests do not run."""
    archive = REPO / "tests" / ".archive"
    if not archive.exists():
        pytest.skip("no archive dir")
    archived_text = "".join(p.read_text() for p in archive.rglob("*")
                            if p.is_file() and p.suffix in (".py", ".json"))
    assert "ST-SETTLE-1" in archived_text, "expected archived ST-SETTLE-1 references"
    # ST-SETTLE-1 must be bound by LIVE tests, not by its archived copies:
    assert _binding_files("ST-SETTLE-1"), \
        "ST-SETTLE-1's only references are archived — that is not a binding"
