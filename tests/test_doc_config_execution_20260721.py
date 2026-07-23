"""T-11 (pack 20260721) — the EXECUTION control: every config example our docs
and Skills ship must be ACCEPTED by the library, proven by running it through
`preflight --offline` (schema + resolution — no network, no infra needed).

Why execution and not lint: Gate E found `ab0t-quota-go/docs/INTEGRATION_RUNBOOK.md`
shipping a copy-paste config the library refuses at boot — under a GREEN
doc-lint. Text-matching cannot catch a *content* defect; running the config
through the library's own front door can (D-14).

Fragment exemptions: a fenced json block that is deliberately partial carries
`<!-- doc-exec: fragment (reason) -->` on the line above its fence. Each marker
is a visible, justified exemption (D-14 rule 3 — an exemption list is a
finding; grep `doc-exec: fragment` to audit them).

Execution environment — PER DOCUMENT (Gate-D F-2): the harness sets ONLY the
env vars THAT DOCUMENT itself instructs (scanned from its text). A control
that supplies what the document fails to supply cannot detect that the
document fails to supply it — the quickstart shipped a refused 5-minute path
under exactly that blindness. LIMIT (D-14 rule 4): offline preflight proves
the CONFIG CONTRACT (schema + resolution + mode identity); it cannot prove
infrastructure claims (gate outcomes need a live/faked Redis — covered by the
T-12 suite, not per-doc-example). Fences: ```json AND ```jsonc (//-comments
stripped) — the Go re-gate's class, ported. An UNPARSEABLE block that
textually carries config trigger keys fails LOUDLY unless it carries an
explicit fragment marker (silent skip is the zero-work pass).
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from tests.dnd_harness_20260721 import install_no_contact

REPO = Path(__file__).resolve().parents[1]

# T-29 R-1: EVERY root-level .md is in scope (the scan surface was the hole —
# BILLING_MODELS_GUIDE.md's root original sat outside it while its Skills copy
# was scanned; four instrument failures in this program were scan-surface
# boundaries, not analysis errors). CHANGELOG.md is the one exclusion, same
# justification as the doc-lint's: history may truthfully show old shapes.
DOC_ROOTS = sorted(
    [p.name for p in Path(__file__).resolve().parents[1].glob("*.md")
     if p.name != "CHANGELOG.md"]) + ["docs", "Skills"]
FRAGMENT_MARK = "doc-exec: fragment"
# Trigger keys: present only in CONFIG documents. `resources`/`service_name`
# alone are NOT triggers — API *response* examples carry them too (two false
# positives found on the first run; the discriminator keys on config-only
# structure, an explicitly stated limit per D-14 rule 4: a config example
# containing none of these keys is invisible to this control).
_CONFIG_KEYS = {"engine_mode", "storage", "tiers", "tier_provider"}
_FENCE_RE = re.compile(r"^```json[c5]?\s*$")

#: The env vars a document can instruct its reader to set. The harness
#: provides a value ONLY when the document names the variable.
_INSTRUCTABLE_ENV = {
    "QUOTA_REDIS_URL": "redis://documented-declared:6379/0",
    "QUOTA_REDIS_PASSWORD": "documented-pw",
    "AB0T_MESH_API_KEY": "doc-exec-key",
    "AB0T_SERVICE_NAME": "doc-exec-svc",
    "AB0T_CONSUMER_ORG_ID": "doc-exec-org",
    "QUOTA_DYNAMODB_ENDPOINT": "http://localhost:8000",
}


def _instructs(doc_text: str, var: str) -> bool:
    """T-29 R-2: a document INSTRUCTS a var only in an instruction FORM — an
    assignment (`VAR=…`), an export, or an env-table/list row beginning with
    the var. A prose MENTION ("never uses VAR") does not count: the property
    is "the doc tells the reader to set it", not "the string appears".
    STATED LIMIT (D-14 #4): form-matching is a proxy for intent — prose that
    happens to open a list row with the var still counts, and an instruction
    phrased purely narratively ("go set VAR in your dashboard") does not;
    review of new docs is the backstop for both directions."""
    return re.search(
        rf"(^|\s)(export\s+`?{var}\b|{var}=)|^\s*[-|]\s*`?{var}`?\s*[|——:-]",
        doc_text, re.MULTILINE) is not None


def _doc_instructed_env(doc_text: str) -> dict:
    """Gate-D F-2 + T-29 R-2: exactly the vars THIS document instructs."""
    return {k: v for k, v in _INSTRUCTABLE_ENV.items() if _instructs(doc_text, k)}


def _strip_jsonc(block: str) -> str:
    return re.sub(r"^\s*//.*$", "", block, flags=re.MULTILINE)


def _doc_files():
    for root in DOC_ROOTS:
        p = REPO / root
        if p.is_file():
            yield p
        elif p.is_dir():
            yield from sorted(p.rglob("*.md"))


def extract_config_examples(path: Path):
    """(lineno, json_text, exempt) for every fenced json/jsonc block that
    parses to a dict sharing keys with the quota-config surface. An
    UNPARSEABLE block whose text still carries a trigger key is returned as
    (lineno, None, exempt) — the caller fails it loudly unless exempted
    (silent skip is the zero-work pass; Gate-D blind class)."""
    lines = path.read_text().splitlines()
    out = []
    i = 0
    while i < len(lines):
        if _FENCE_RE.match(lines[i].strip()):
            start = i + 1
            j = start
            while j < len(lines) and not lines[j].strip().startswith("```"):
                j += 1
            block = "\n".join(lines[start:j])
            exempt = any(FRAGMENT_MARK in lines[k]
                         for k in range(max(0, i - 4), i))
            stripped = _strip_jsonc(block)
            try:
                parsed = json.loads(stripped)
            except Exception:
                parsed = None
            if isinstance(parsed, dict) and _CONFIG_KEYS & set(parsed):
                # execute the comment-STRIPPED text: the jsonc fence declares
                # the //-comments presentational (a reader copies the JSON);
                # the strip never adds content, so a missing store stays missing
                out.append((start + 1, stripped, exempt))
            elif parsed is None and any(f'"{k}"' in block for k in _CONFIG_KEYS):
                out.append((start + 1, None, exempt))  # config-looking, unparseable
            i = j
        i += 1
    return out


def _collect_params():
    params = []
    for f in _doc_files():
        for lineno, block, exempt in extract_config_examples(f):
            if exempt:
                continue
            rel = str(f.relative_to(REPO))
            params.append(pytest.param(rel, lineno, block,
                                       id=f"{rel}:{lineno}"))
    # the shipped example config is a config example by definition
    params.append(pytest.param(
        "quota-config.example.json", 1,
        (REPO / "quota-config.example.json").read_text(),
        id="quota-config.example.json"))
    return params


def _run_offline_preflight(tmp_path, monkeypatch, config_text: str,
                           env: dict | None = None):
    """F-2: `env` is derived from the DOCUMENT under test — never a blanket."""
    from ab0t_quota.preflight import run_preflight
    p = tmp_path / "quota-config.json"
    p.write_text(config_text)
    for name in _INSTRUCTABLE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("QUOTA_CONFIG_PATH", str(p))
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    out: list[str] = []
    report = asyncio.run(run_preflight(offline=True, emit=out.append))
    return report, "\n".join(out)


@pytest.mark.parametrize("rel,lineno,block", _collect_params())
def test_documented_config_example_is_accepted(rel, lineno, block,
                                               tmp_path, monkeypatch):
    install_no_contact(monkeypatch)
    assert block is not None, \
        (f"{rel}:{lineno} is a config-LOOKING block the harness cannot parse "
         f"(ellipsis/pseudo-json). Either make it a real, runnable example or "
         f"mark it `<!-- {FRAGMENT_MARK} (reason) -->` — silent skip is the "
         f"zero-work pass (Gate-D blind class).")
    # the shipped example FILE must stand alone (no doc to instruct env)
    doc_text = "" if rel == "quota-config.example.json" else (REPO / rel).read_text()
    env = _doc_instructed_env(doc_text)
    report, out = _run_offline_preflight(tmp_path, monkeypatch, block, env=env)
    assert report.exit_code == 0, \
        (f"{rel}:{lineno} ships a config example the library REFUSES under "
         f"the env THAT DOCUMENT instructs ({sorted(env) or 'none'}) — the "
         f"Gate E defect shape, in our own docs:\n"
         + "\n".join(report.config_errors))


def test_extractor_finds_examples_at_all():
    """Rule C: an extractor that finds nothing would pass every check."""
    total = sum(len(extract_config_examples(f)) for f in _doc_files())
    assert total >= 2, f"implausibly few config examples found ({total}) — extractor broken?"


def test_planted_refused_config_turns_the_control_red(tmp_path, monkeypatch):
    """D-14 rule 1+2: plant the Gate E defect in BOTH content forms — an
    unknown storage key (the redis_key_prefix shape) and a null redis_url with
    the namespaced env cleared — and prove the control refuses each."""
    install_no_contact(monkeypatch)
    # Form 1: an unknown storage key (typo'd — the schema-strict class; note
    # Python legitimately KNOWS redis_key_prefix, the key Go refuses).
    doc = tmp_path / "planted.md"
    doc.write_text('# scratch\n```json\n{"storage": {"redis_urll": "redis://h:6379/0"'
                   '}, "tiers": []}\n```\n')
    examples = extract_config_examples(doc)
    assert len(examples) == 1 and not examples[0][2]
    report, _ = _run_offline_preflight(tmp_path, monkeypatch, examples[0][1])
    assert report.exit_code == 2, "the planted unknown-storage-key config must be refused"

    # Form 2: missing tiers — fatal since T-3 (the README's old zero-config shape).
    report2, _ = _run_offline_preflight(tmp_path, monkeypatch,
                                        '{"storage": {"redis_url": "redis://h:6379/0"}}')
    assert report2.exit_code == 2, "the planted tier-less config must be refused"

    # Form 3: env beats explicit null (clause 3) — a null redis_url WITH the
    # documented namespaced env present resolves; the control must NOT flag
    # the documented remedy as broken. (F-2: the env is passed explicitly,
    # standing for a document that DOES instruct QUOTA_REDIS_URL.)
    report3, _ = _run_offline_preflight(
        tmp_path, monkeypatch, '{"storage": {"redis_url": null}, "tiers": []}',
        env={"QUOTA_REDIS_URL": "redis://documented-declared:6379/0"})
    assert report3.exit_code == 0

    # Form 4 (F-2's own control): a STORELESS config in a doc that does NOT
    # instruct QUOTA_REDIS_URL must be refused — the quickstart defect shape.
    report4, _ = _run_offline_preflight(
        tmp_path, monkeypatch, '{"tiers": [], "storage": {"persistence_enabled": false}}',
        env={})
    assert report4.exit_code == 2, \
        "a doc-uninstructed storeless config must be refused (F-2)"

    # Form 5 (Gate-D blind class, Go's re-gate ported): the same storeless
    # config in a ```jsonc fence must be SEEN by the extractor.
    doc2 = tmp_path / "planted-jsonc.md"
    doc2.write_text('# scratch\n```jsonc\n{\n  // storeless\n  "tiers": []\n}\n```\n')
    found = extract_config_examples(doc2)
    assert found and found[0][1] is not None, \
        "a jsonc-fenced config example is invisible to the extractor"

    # Form 6 (Gate-D blind class): a config-LOOKING but unparseable block is
    # returned for LOUD failure, never silently skipped.
    doc3 = tmp_path / "planted-ellipsis.md"
    doc3.write_text('# scratch\n```json\n{ "storage": { ... }, "tiers": [ ... ] }\n```\n')
    found3 = extract_config_examples(doc3)
    assert found3 and found3[0][1] is None, \
        "an ellipsis config example was silently skipped (the zero-work pass)"


def test_mention_is_not_instruction():
    """T-29 R-2's plant: prose that merely NAMES the var (even negatively)
    must not put it in the execution env; instruction forms must."""
    assert _doc_instructed_env(
        "The library never reads QUOTA_REDIS_URL twice.\n") == {}, \
        "a prose mention counted as an instruction (R-2)"
    for form in ("export QUOTA_REDIS_URL=redis://h:6379/0\n",
                 "QUOTA_REDIS_URL=redis://h:6379/0\n",
                 "| `QUOTA_REDIS_URL` | yes | — | the declared Redis |\n",
                 "- `QUOTA_REDIS_URL` — Redis URL for the CLI\n"):
        assert "QUOTA_REDIS_URL" in _doc_instructed_env(form), \
            f"instruction form not recognised: {form!r}"


def test_fragment_marker_is_honoured_and_visible(tmp_path):
    doc = tmp_path / "frag.md"
    doc.write_text("<!-- doc-exec: fragment (shows one key only) -->\n"
                   "```json\n{\"storage\": {\"redis_url\": null}}\n```\n")
    examples = extract_config_examples(doc)
    assert examples and examples[0][2] is True
