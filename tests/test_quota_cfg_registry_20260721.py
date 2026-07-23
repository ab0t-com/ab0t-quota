"""T-25 (D-13, pack 20260721): QUOTA-CFG-nnn is ONE cross-runtime namespace,
defined in conformance/quota-cfg-registry.json and mirrored byte-identically
into the Go repo.

Checks: (a) registry shape — no duplicate or skipped numbers, append-only
contiguity; (b) every code LITERAL in the Python library exists in the
registry (a new refusal cannot ship an unregistered code); (c) the Go mirror
is byte-identical (skips with a loud reason if the sibling repo is absent —
D-14 rule 3: that skip is itself recorded, not silent). LIMIT (D-14 rule 4):
(b) scans string literals; a code assembled at runtime (f-string/concat)
would evade it — none exists today, and the doc-lint bans introducing one.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
REGISTRY = REPO / "conformance" / "quota-cfg-registry.json"
GO_MIRROR = REPO.parent / "ab0t-quota-go" / "conformance" / "quota-cfg-registry.json"
CODE_RE = re.compile(r"QUOTA-CFG-\d{3}")


def _registry() -> dict:
    return json.loads(REGISTRY.read_text())


def _codes_in_tree(root: Path, suffixes=(".py",)) -> set[str]:
    found: set[str] = set()
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix in suffixes:
            found |= set(CODE_RE.findall(p.read_text()))
    return found


def test_registry_is_contiguous_and_unduplicated():
    codes = sorted(_registry()["codes"])
    nums = [int(c.rsplit("-", 1)[1]) for c in codes]
    assert nums == list(range(nums[0], nums[-1] + 1)), \
        f"registry numbers skip or duplicate: {nums} (allocation is append-only)"
    assert len(set(codes)) == len(codes)


def test_every_python_code_literal_is_registered():
    registered = set(_registry()["codes"])
    used = _codes_in_tree(REPO / "ab0t_quota")
    unregistered = used - registered
    assert unregistered == set(), \
        (f"code(s) used in ab0t_quota/ but not in the registry: {unregistered} — "
         f"D-13: no code ships without a registry row in the same change")


def test_go_codes_are_registered_and_009_010_adopted():
    """Python adopts Go's 009/010 (assigned first, they stand — D-13)."""
    reg = _registry()["codes"]
    assert "QUOTA-CFG-009" in reg and "QUOTA-CFG-010" in reg
    assert reg["QUOTA-CFG-009"]["assigned_by"] == "go"
    go_root = REPO.parent / "ab0t-quota-go"
    if not go_root.exists():
        pytest.skip("sibling ab0t-quota-go repo not present on this checkout "
                    "(recorded skip — the Go-side scan did not run)")
    used = _codes_in_tree(go_root / "config", (".go",)) \
        | _codes_in_tree(go_root / "quota", (".go",))
    assert used - set(reg) == set(), \
        f"Go uses unregistered code(s): {used - set(reg)}"


def test_go_mirror_is_byte_identical():
    if not GO_MIRROR.parent.exists():
        pytest.skip("sibling ab0t-quota-go repo not present on this checkout "
                    "(recorded skip — mirror not verified)")
    assert GO_MIRROR.exists(), \
        "the Go mirror conformance/quota-cfg-registry.json does not exist (D-13)"
    assert GO_MIRROR.read_bytes() == REGISTRY.read_bytes(), \
        "the Go mirror drifted from the canonical registry — re-copy it (D-13)"


def test_scanner_flags_a_planted_unregistered_code(tmp_path):
    """D-14 rule 1: the code-literal scanner can go red — a planted
    unregistered code in a scratch tree must be found."""
    scratch = tmp_path / "mod.py"
    scratch.write_text('raise ValueError("QUOTA-CFG-099: planted offender")\n')
    found = _codes_in_tree(tmp_path)
    assert "QUOTA-CFG-099" in found
    assert "QUOTA-CFG-099" not in _registry()["codes"]
