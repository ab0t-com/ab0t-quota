#!/usr/bin/env python3
"""T-11 doc-lint (pack 20260721, D-14-compliant): prose rules for what
EXECUTION cannot reach.

The primary control for config examples is tests/test_doc_config_execution_
20260721.py, which runs every documented config through `preflight` — a
content check no lint can match (the Gate E lesson: Go's lint was green over
a runbook config the library refuses at boot). THIS lint covers claims prose
makes:

  R1  no un-namespaced env var presented as a supported source
      (REDIS_URL / REDIS_PASSWORD / STRIPE_WEBHOOK_SECRET /
      SNS_LIFECYCLE_TOPIC_ARN / AUTH_SERVICE_URL). A line mentioning one must
      carry a negation/deprecation context or an explicit
      `doc-lint:allow R1` pragma.
  R2  no invented endpoint: `localhost:6379` may not appear as a value in a
      consumer-facing file unless the line (or an adjacent comment) carries a
      local-dev marker or `doc-lint:allow R2`.
  R3  fail-direction honesty: a line saying "fail-open" must name the opt-in
      lever or a negation — bare fail-open advice is banned (DOC-08's shape).

STATED LIMITS (D-14 rule 4): R1/R3's negation-context detection is a token
HEURISTIC — a crafted sentence can evade it; the execution control and review
are the backstop. R2 keys on the literal host:port. The lint reads
.md/.json/.yaml/.yml (the Gate E widening — shipped example configs are
consumer-facing files too).

COMPLETENESS (C-rules, lane DOCS 2026-07-21): the R-rules prove the docs
don't teach the WRONG thing; they cannot prove the docs teach the RIGHT
thing — a doc silently omitting a new required config key is R-clean and
wrong (the instrument blind spot behind today's gap: doctor/provision and
five new storage keys shipped and ZERO reached the Skills). So:

  C1  every storage key the config schema accepts (AST-read from
      ab0t_quota/config_schema.py::_STORAGE_KEYS — the enforcing table, so
      schema and control cannot drift) + the top-level config keys must
      appear in BOTH consumer surfaces: docs (root *.md + docs/ + the
      example config) AND Skills/ (clients read the Skills as the contract).
  C2  every CLI verb + alias (AST-read from ab0t_quota/__main__.py
      add_parser calls) must appear in the docs surface; the infra triad
      (preflight/doctor/provision) must ALSO appear in Skills/. Every long
      flag (add_argument "--…") must appear in the docs surface.
  C3  every QUOTA-CFG-nnn in conformance/quota-cfg-registry.json must have
      a row in docs/error-codes.md with a NON-EMPTY remedy cell; and no
      QUOTA-CFG code may appear anywhere in either surface that is NOT in
      the registry (an invented/stale code is the same defect inverted).

STATED LIMITS of the C-rules (D-14 rule 4 — judgements that genuinely
cannot be automated, stated rather than implied covered):
  * C1/C2 prove token PRESENCE, not explanation quality — a key merely
    named in a table passes. Presence is the invariant this control owns;
    whether the sentence around the token is true is the execution
    control's and review's job.
  * C3 checks that a remedy CELL is non-empty, not that the remedy works.
  * This instrument scans THIS repo only. ab0t-quota-go's consumer surface
    (CONSUMING.md, its Skills/) is outside its field of view — Go's
    internal/doclint owns that repo; the registry sync test guards code
    parity, not Go doc coverage. A green here says nothing about Go docs.

EXEMPT PATHS (D-14 rule 3 — each justified here, frozen by
tests/test_doc_lint_20260721.py::test_exemption_list_frozen):
  * CHANGELOG.md — history may truthfully describe removed behaviour.
  * docs/migrating-from-ambient-resolution.md — the migration notice must
    quote the deprecated names to migrate consumers off them.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# T-29 R-1: every root-level .md (CHANGELOG excluded via EXEMPT_PATHS below) —
# the scan surface, not the rules, is where this program's instruments failed.
SCAN_ROOTS = sorted(p.name for p in REPO.glob("*.md")) + [
    "docs", "Skills", "quota-config.example.json"]
SUFFIXES = {".md", ".json", ".yaml", ".yml"}
EXEMPT_PATHS = ("CHANGELOG.md", "docs/migrating-from-ambient-resolution.md")

GENERIC_NAMES = ("REDIS_URL", "REDIS_PASSWORD", "STRIPE_WEBHOOK_SECRET",
                 "SNS_LIFECYCLE_TOPIC_ARN", "AUTH_SERVICE_URL")
# standalone token: not preceded by A-Z/_/$ (QUOTA_REDIS_URL etc. don't count)
_R1_RES = {n: re.compile(rf"(?<![A-Z_$]){n}\b") for n in GENERIC_NAMES}
_R1_NEGATION = re.compile(
    r"never read|no longer|not read|deprecated|legacy|retires?|generic|"
    r"payment-service'?s (own )?variable|service'?s own|own variable|"
    r"was consulted|removed|never reads|"
    # quoted-as-defect context (migration banners quote the OLD pattern):
    r"ambient-fallback|caused a production outage|"
    r"doc-lint:allow R1", re.IGNORECASE)

_R2_RE = re.compile(r"localhost:6379")
_R2_ALLOW = re.compile(r"local[- ]dev|doc-lint:allow R2|never invents|never read|"
                       r"ambient-fallback|caused a production outage",
                       re.IGNORECASE)

_R3_RE = re.compile(r"fail[- ]open", re.IGNORECASE)
_R3_CONTEXT = re.compile(
    r"opt[- ]?in|AB0T_QUOTA_BRIDGE_FAIL_OPEN|fail_open=|never|closed|"
    r"do(es)? not|don't|banned|instead of|doc-lint:allow R3", re.IGNORECASE)


def _files():
    for root in SCAN_ROOTS:
        p = REPO / root
        if p.is_file():
            yield p
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and f.suffix in SUFFIXES:
                    yield f


def lint_text(rel: str, text: str) -> list[str]:
    """Returns findings as 'file:line rule offending-text' strings."""
    findings = []
    lines = text.splitlines()
    for i, line in enumerate(lines, 1):
        # line + up to 6 neighbours above: annotations legitimately sit above
        # a fenced block, possibly with a doc-exec marker line between.
        context = " ".join(lines[max(0, i - 7):i + 1])
        for name, rx in _R1_RES.items():
            if rx.search(line) and not _R1_NEGATION.search(context):
                findings.append(f"{rel}:{i} R1 generic {name} presented without "
                                f"negation context: {line.strip()[:120]}")
        if _R2_RE.search(line) and not _R2_ALLOW.search(context):
            findings.append(f"{rel}:{i} R2 invented endpoint localhost:6379 "
                            f"without a local-dev marker: {line.strip()[:120]}")
        if _R3_RE.search(line) and not _R3_CONTEXT.search(line):
            findings.append(f"{rel}:{i} R3 bare fail-open claim (name the "
                            f"opt-in lever or negate): {line.strip()[:120]}")
    return findings


def run() -> list[str]:
    findings = []
    for f in _files():
        rel = str(f.relative_to(REPO))
        if rel in EXEMPT_PATHS:
            continue
        findings.extend(lint_text(rel, f.read_text()))
    return findings


# ---------------------------------------------------------------------------
# C-rules: completeness (see module docstring for rules + stated limits)
# ---------------------------------------------------------------------------

# Top-level config keys generate_schema() publishes (storage keys come from
# the AST read; these few are stable and enumerated here).
TOP_LEVEL_CONFIG_KEYS = ("service_name", "engine_mode", "offline", "storage",
                         "tiers")
INFRA_VERBS = ("preflight", "doctor", "provision")  # must ALSO reach Skills/
ERROR_CODES_DOC = "docs/error-codes.md"
_CODE_RE = re.compile(r"QUOTA-CFG-\d{3}")


def schema_storage_keys(source: str | None = None) -> list[str]:
    """Keys of _STORAGE_KEYS, read from the enforcing module's AST (no import
    — the doc lint must not need the library's runtime deps)."""
    source = source if source is not None else (
        REPO / "ab0t_quota" / "config_schema.py").read_text()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.AnnAssign) or isinstance(node, ast.Assign):
            targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if "_STORAGE_KEYS" in names and isinstance(node.value, ast.Dict):
                return [k.value for k in node.value.keys
                        if isinstance(k, ast.Constant)]
    raise RuntimeError("_STORAGE_KEYS dict not found in config_schema.py")


def cli_verbs_and_flags(source: str | None = None) -> tuple[list[str], list[str]]:
    """(verbs+aliases, long flags) from add_parser/add_argument calls in the
    CLI module's AST."""
    source = source if source is not None else (
        REPO / "ab0t_quota" / "__main__.py").read_text()
    verbs, flags = [], []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr == "add_parser" and node.args:
            a0 = node.args[0]
            if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                verbs.append(a0.value)
            for kw in node.keywords:
                if kw.arg == "aliases" and isinstance(kw.value, (ast.List, ast.Tuple)):
                    verbs.extend(e.value for e in kw.value.elts
                                 if isinstance(e, ast.Constant))
        elif node.func.attr == "add_argument" and node.args:
            a0 = node.args[0]
            if (isinstance(a0, ast.Constant) and isinstance(a0.value, str)
                    and a0.value.startswith("--")):
                flags.append(a0.value)
    return sorted(set(verbs)), sorted(set(flags))


def registry_codes(path: Path | None = None) -> list[str]:
    data = json.loads((path or REPO / "conformance" /
                       "quota-cfg-registry.json").read_text())
    return sorted(data["codes"])


def _surface_text(root: Path, docs: bool) -> str:
    """Concatenated consumer-surface text. docs=True: root *.md (CHANGELOG
    excluded — history), docs/, example config. docs=False: Skills/."""
    parts = []
    if docs:
        for f in sorted(root.glob("*.md")):
            if f.name != "CHANGELOG.md":
                parts.append(f.read_text())
        for f in sorted((root / "docs").rglob("*.md")):
            parts.append(f.read_text())
        ex = root / "quota-config.example.json"
        if ex.exists():
            parts.append(ex.read_text())
    else:
        for f in sorted((root / "Skills").rglob("*.md")):
            parts.append(f.read_text())
    return "\n".join(parts)


def _has_token(text: str, token: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(token)}(?![\w-])", text) is not None


def check_completeness(*, storage_keys=None, top_level_keys=None, verbs=None,
                       flags=None, codes=None, repo: Path | None = None) -> list[str]:
    """Returns findings. Parameters exist so the planted-offender control can
    inject a key/verb/flag/code and prove this goes RED (D-14 rule 1)."""
    repo = repo or REPO
    storage_keys = storage_keys if storage_keys is not None else schema_storage_keys()
    top_level_keys = top_level_keys if top_level_keys is not None else TOP_LEVEL_CONFIG_KEYS
    if verbs is None or flags is None:
        v, f = cli_verbs_and_flags()
        verbs = verbs if verbs is not None else v
        flags = flags if flags is not None else f
    codes = codes if codes is not None else registry_codes()

    docs_text = _surface_text(repo, docs=True)
    skills_text = _surface_text(repo, docs=False)
    findings = []

    # C1 — config keys in BOTH surfaces
    for key in list(storage_keys) + list(top_level_keys):
        if not _has_token(docs_text, key):
            findings.append(f"C1 config key `{key}` accepted by the schema but "
                            f"absent from the docs surface")
        if not _has_token(skills_text, key):
            findings.append(f"C1 config key `{key}` accepted by the schema but "
                            f"absent from Skills/ (the Skills are the contract)")

    # C2 — verbs + flags
    for verb in verbs:
        if not _has_token(docs_text, verb):
            findings.append(f"C2 CLI verb `{verb}` absent from the docs surface")
        if verb in INFRA_VERBS and not _has_token(skills_text, verb):
            findings.append(f"C2 CLI verb `{verb}` absent from Skills/")
    for flag in flags:
        if not _has_token(docs_text, flag):
            findings.append(f"C2 CLI flag `{flag}` absent from the docs surface")

    # C3 — error-code registry: every code documented WITH a remedy…
    ec_path = repo / ERROR_CODES_DOC
    ec_text = ec_path.read_text() if ec_path.exists() else ""
    if not ec_path.exists():
        findings.append(f"C3 {ERROR_CODES_DOC} missing — the registry has no "
                        f"consumer-facing home")
    for code in codes:
        rows = [ln for ln in ec_text.splitlines() if code in ln and "|" in ln]
        remedied = any(len([c for c in ln.split("|") if c.strip()]) >= 3
                       for ln in rows)
        if not remedied:
            findings.append(f"C3 {code} is in the registry but has no row with "
                            f"a remedy in {ERROR_CODES_DOC}")
    # …and no code in either surface that the registry does not define
    known = set(codes)
    for surface, text in (("docs", docs_text), ("Skills", skills_text)):
        for found in sorted(set(_CODE_RE.findall(text)) - known):
            findings.append(f"C3 {found} appears in the {surface} surface but "
                            f"is NOT in the registry (invented or stale code)")
    return findings


if __name__ == "__main__":
    problems = run() + check_completeness()
    for p in problems:
        print(p)
    sys.exit(1 if problems else 0)
