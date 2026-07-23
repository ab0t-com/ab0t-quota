"""Gate C — the AST env-read census (replaces the provably-leaky line-grep;
VERIFICATION_STRATEGY.md §3 Gate C correction C1; hardened by T-22 against
the 11 evasion forms the Gate C verifier planted).

Three scanners, together covering the axis (D-14 rule 2):
  * `_census`  — literal-name reads: os.getenv / environ.get|setdefault|pop /
    environ[...] / environ.__getitem__|__contains__ / `"X" in os.environ`.
    Pinned to the justified ALLOWLIST.
  * `_dynamic_census` — the SAME forms with a NON-literal name. Every site is
    pinned in DYNAMIC_ALLOWLIST with its justification; a new dynamic read
    (f-string, concat, variable) must be enrolled here or it fails.
  * `_hygiene_violations` — EVASIVE FORMS are banned outright in ab0t_quota/
    (`from os import getenv/environ`, `import os as <alias>`, `__import__`,
    `getattr(os, …)`, aliasing/bare use of `os.environ` outside the read
    forms above). A name the scanner cannot resolve is not allowed to exist.

STATED LIMITS (D-14 rule 4): reading the environment via the FILESYSTEM
(`/proc/self/environ`) or a subprocess is invisible to any AST census — no
such read exists in the library and the hygiene ban cannot see one appear;
review is the backstop. `os.environb` is treated as `os.environ`. A name
propagated across function boundaries into
an enrolled dynamic site cannot be statically resolved — that is why every
dynamic site carries a justification and the resolver's `env=` tuples carry
their own namespaced-only pin. The hygiene ban closes the import-time and
aliasing channels; it does not analyse data flow.
"""
from __future__ import annotations

import ast
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "ab0t_quota"

_ENV_METHODS = ("get", "setdefault", "pop")

# name -> (justification, licensed-by)
ALLOWLIST = {
    "AWS_ENDPOINT_URL": ("AWS SDK's own documented contract (LocalStack/dev override); "
                         "deletion test: the SDK resolves it anyway", "ENV-04 scope ruling / T-4"),
    "AWS_REGION":       ("AWS SDK region chain (ECS/EKS/IRSA-injected); deferred, logged",
                         "ENV-04 scope ruling / T-4"),
    "REDIS_URL":        ("REPORT-ONLY read for the QUOTA-CFG-001 `previously:` line — value "
                         "never reaches a connection/client/config; redacted (D-9); plus the "
                         "D-10 presence-only startup call-out", "D-9 + D-10 (DECISIONS.md)"),
    # D-10 (SIGNED, option 1 generalised): presence-only checks — `name in
    # os.environ`, value never read — of the enumerated deprecated generic
    # names, for the loud startup call-out. Retire with the migration window.
    "STRIPE_WEBHOOK_SECRET":   ("presence-only startup call-out (silent-off trap, ENV-05); "
                                "value never read", "D-10 (DECISIONS.md)"),
    "REDIS_PASSWORD":          ("presence-only startup call-out; value never read",
                                "D-10 (DECISIONS.md)"),
    "AUTH_SERVICE_URL":        ("presence-only startup call-out; value never read",
                                "D-10 (DECISIONS.md)"),
    "SNS_LIFECYCLE_TOPIC_ARN": ("presence-only DEPRECATED warning (transition tier); the "
                                "VALUE read stays in the resolver's deprecated_env path",
                                "D-10 (DECISIONS.md)"),
}

# The resolver's documented-TRANSITION tier (read via _env_lookup, invisible to
# the literal-arg census below) — our OWN legacy names, warned on use, retiring 0.8.0.
TRANSITION_NAMES = {"DYNAMODB_ENDPOINT", "SNS_LIFECYCLE_TOPIC_ARN"}


def _is_environ_attr(node) -> bool:
    # environb included (Gate-D): a bytes-keyed read is the same read
    return (isinstance(node, ast.Attribute) and node.attr in ("environ", "environb")
            and getattr(node.value, "id", "") in ("os", "_os"))


def _env_read_name_node(node):
    """If `node` is one of the recognised env-read forms, return the AST node
    carrying the NAME (constant or not); else None."""
    if isinstance(node, ast.Subscript) and _is_environ_attr(node.value):
        return node.slice
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        f = node.func
        if f.attr in ("getenv", "getenvb") and getattr(f.value, "id", "") in ("os", "_os") and node.args:
            return node.args[0]
        if f.attr in _ENV_METHODS and _is_environ_attr(f.value) and node.args:
            return node.args[0]
        # environ.__getitem__("X") / environ.__contains__("X")
        if f.attr in ("__getitem__", "__contains__") and _is_environ_attr(f.value) \
                and node.args:
            return node.args[0]
    if isinstance(node, ast.Compare) and len(node.ops) == 1 \
            and isinstance(node.ops[0], (ast.In, ast.NotIn)) \
            and len(node.comparators) == 1 and _is_environ_attr(node.comparators[0]):
        return node.left
    return None


def _census(root: Path) -> dict[str, list[str]]:
    """Literal-name env reads (all recognised forms)."""
    hits: dict[str, list[str]] = {}
    for p in sorted(root.rglob("*.py")):
        tree = ast.parse(p.read_text())
        for node in ast.walk(tree):
            name_node = _env_read_name_node(node)
            if isinstance(name_node, ast.Constant):
                name = name_node.value
                if isinstance(name, bytes):  # environb keys are bytes
                    name = name.decode("utf-8", "replace")
                hits.setdefault(name, []).append(
                    f"{p.relative_to(root)}:{node.lineno}")
    return hits


def _dynamic_census(root: Path) -> list[tuple[str, str]]:
    """Env reads whose NAME is not a literal: (file, arg_source) pairs."""
    out = []
    for p in sorted(root.rglob("*.py")):
        src = p.read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            name_node = _env_read_name_node(node)
            if name_node is not None and not isinstance(name_node, ast.Constant):
                out.append((p.name, ast.get_source_segment(src, name_node)))
    return out


def _hygiene_violations(root: Path) -> list[str]:
    """Banned evasive forms — a read the census cannot attribute may not exist."""
    out = []
    for p in sorted(root.rglob("*.py")):
        src = p.read_text()
        tree = ast.parse(src)
        # collect every environ-Attribute node that is PART of a recognised
        # read form, so bare/aliased uses can be told apart
        sanctioned_environ = set()
        for node in ast.walk(tree):
            target = None
            if isinstance(node, ast.Subscript) and _is_environ_attr(node.value):
                target = node.value
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and _is_environ_attr(node.func.value):
                target = node.func.value
            elif isinstance(node, ast.Compare) and len(node.comparators) == 1 \
                    and _is_environ_attr(node.comparators[0]):
                target = node.comparators[0]
            if target is not None:
                sanctioned_environ.add(id(target))
        for node in ast.walk(tree):
            loc = f"{p.relative_to(root)}:{getattr(node, 'lineno', '?')}"
            if isinstance(node, ast.ImportFrom) and node.module == "os":
                for a in node.names:
                    if a.name in ("getenv", "getenvb", "environ", "environb", "putenv"):
                        out.append(f"{loc}: from os import {a.name}"
                                   f"{' as ' + a.asname if a.asname else ''} (banned form)")
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name == "os" and a.asname not in (None, "os", "_os"):
                        out.append(f"{loc}: import os as {a.asname} (banned alias)")
            if isinstance(node, ast.Call):
                if getattr(node.func, "id", "") == "__import__" and node.args \
                        and isinstance(node.args[0], ast.Constant) \
                        and node.args[0].value == "os":
                    out.append(f"{loc}: __import__('os') (banned form)")
                if getattr(node.func, "id", "") == "getattr" and node.args \
                        and getattr(node.args[0], "id", "") in ("os", "_os"):
                    out.append(f"{loc}: getattr(os, …) (banned form)")
            if _is_environ_attr(node) and id(node) not in sanctioned_environ:
                out.append(f"{loc}: bare/aliased os.environ use outside the "
                           f"recognised read forms (banned — closes aliasing/"
                           f"copying evasions)")
    return out


def test_no_unlicensed_generic_env_read():
    hits = _census(LIB)
    generic = {k: v for k, v in hits.items()
               if not (k.startswith("QUOTA_") or k.startswith("AB0T_"))}
    unlicensed = {k: v for k, v in generic.items() if k not in ALLOWLIST}
    assert unlicensed == {}, \
        f"UNLICENSED generic env read(s) — declared, not discovered: {unlicensed}"
    # Rule C: the census did real work, and the allowlist carries no dead rows.
    assert len(hits) >= 20, f"census implausibly small ({len(hits)}) — scanner broken?"
    dead = [k for k in ALLOWLIST if k not in generic]
    assert dead == [], f"allowlist rows with no surviving read (drop them): {dead}"


def test_transition_tier_is_exactly_the_documented_set():
    """The resolver's deprecated_env registrations (invisible to the literal
    census — they flow through _env_lookup) are pinned here by source scan."""
    names = set()
    for p in sorted(LIB.rglob("*.py")):
        tree = ast.parse(p.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "deprecated_env":
                for el in getattr(node.value, "elts", []):
                    if isinstance(el, ast.Constant):
                        names.add(el.value)
    assert names == TRANSITION_NAMES, \
        f"TRANSITION tier drifted: {names ^ TRANSITION_NAMES}"


def test_census_catches_the_line_masking_case(tmp_path):
    """NC-6: the exact defect Gate C's grep missed — a generic read sharing a
    line with a QUOTA_ read — must be reported by this scanner."""
    scratch = tmp_path / "mod.py"
    scratch.write_text(
        "import os\n"
        "x = os.getenv(\"QUOTA_REDIS_URL\") or os.getenv(\"REDIS_URL\")\n")
    hits = _census(tmp_path)
    assert "REDIS_URL" in hits and "QUOTA_REDIS_URL" in hits


# (file, name-expression source) -> justification. Every non-literal env read
# is enrolled HERE or the census fails (T-22; D-14 rule 3 — the list is frozen
# by exact equality, so a new dynamic site is a visible diff).
DYNAMIC_ALLOWLIST = {
    ("config.py", "var"):
        "${VAR} interpolation — namespaced to QUOTA_* by config.py's own rule",
    ("ddb_preflight.py", "PITR_CONFIRM_ENV"):
        "module constant AB0T_QUOTA_DDB_PITR_CONFIRMED",
    ("redis_preflight.py", "DURABILITY_CONFIRM_ENV"):
        "module constant AB0T_QUOTA_REDIS_DURABILITY_CONFIRMED",
    ("redis_preflight.py", "SCRIPTING_CONFIRM_ENV"):
        "module constant AB0T_QUOTA_REDIS_SCRIPTING_CONFIRMED (T-9)",
    ("resolve.py", "n"):
        "_env_lookup over resolver tuples — every env= tuple is pinned "
        "namespaced by test_resolver_primary_env_tuples_are_namespaced; "
        "deprecated_env pinned by the TRANSITION test",
    ("setup.py", 'f"AB0T_MESH_{service.upper()}_URL"'):
        "namespaced AB0T_MESH_* prefix visible in the literal f-string",
    ("topology.py", "CONFIRM_ENV"):
        "module constant AB0T_QUOTA_REDIS_CLUSTER_CONFIRMED_DISABLED",
}


def test_dynamic_name_reads_are_exactly_the_enrolled_set():
    got = set(_dynamic_census(LIB))
    assert got == set(DYNAMIC_ALLOWLIST), \
        (f"dynamic-name env reads drifted:\n  new: {got - set(DYNAMIC_ALLOWLIST)}\n"
         f"  gone: {set(DYNAMIC_ALLOWLIST) - got}\n"
         f"Enrol (with justification) or remove the dead row.")


def test_no_evasive_form_exists_in_the_library():
    assert _hygiene_violations(LIB) == []


# The Gate C verifier's 11 planted evasion forms — every one must now be
# caught by ONE of the three scanners (D-14 rules 1+2).
_EVASION_PLANTS = {
    "from-import":        "from os import getenv\nx = getenv('REDIS_URL')\n",
    "from-import-alias":  "from os import getenv as ge\nx = ge('REDIS_URL')\n",
    "getattr":            "import os\nx = getattr(os, 'getenv')('REDIS_URL')\n",
    "dunder-import":      "x = __import__('os').getenv('REDIS_URL')\n",
    "import-alias":       "import os as o\nx = o.getenv('REDIS_URL')\n",
    "environ-alias":      "import os\nenv = os.environ\nx = env.get('REDIS_URL')\n",
    "comprehension":      "import os\nd = {k: os.environ[k] for k in ('REDIS_URL',)}\n",
    "fstring-arg":        "import os\ns='URL'\nx = os.getenv(f'REDIS_{s}')\n",
    "concat-literal":     "import os\nx = os.getenv('REDIS' + '_URL')\n",
    "dunder-getitem":     "import os\nx = os.environ.__getitem__('REDIS_URL')\n",
    "setdefault":         "import os\nx = os.environ.setdefault('REDIS_URL', 'd')\n",
    "variable-arg":       "import os\nn = 'REDIS_URL'\nx = os.getenv(n)\n",
    "environb":           "import os\nx = os.environb[b'REDIS_URL']\n",
    "environb-alias":     "import os\nenv = os.environb\nx = env.get(b'REDIS_URL')\n",
    "getenvb":            "import os\nx = os.getenvb(b'REDIS_URL')\n",
    "getenvb-from":       "from os import getenvb\nx = getenvb(b'REDIS_URL')\n",
}

import pytest


@pytest.mark.parametrize("form", sorted(_EVASION_PLANTS))
def test_every_planted_evasion_form_is_caught(form, tmp_path):
    (tmp_path / "mod.py").write_text(_EVASION_PLANTS[form])
    literal = _census(tmp_path)
    dynamic = _dynamic_census(tmp_path)
    hygiene = _hygiene_violations(tmp_path)
    caught = ("REDIS_URL" in literal) or dynamic or hygiene
    assert caught, \
        (f"evasion form {form!r} is invisible to ALL THREE scanners — "
         f"the census is still porous (D-14)")


def test_census_catches_presence_only_membership(tmp_path):
    """NC (D-10): a presence check `\"X\" in os.environ` must be reported —
    the licensed D-10 form is visible, not a scanner blind spot."""
    scratch = tmp_path / "mod.py"
    scratch.write_text(
        "import os\n"
        "present = \"GENERIC_THING\" in os.environ\n")
    hits = _census(tmp_path)
    assert "GENERIC_THING" in hits


def test_resolver_primary_env_tuples_are_namespaced():
    """Gate-C verifier gap, closed cheaply: the resolver's PRIMARY `env=`
    tuples are pinned to namespaced names, the way `deprecated_env` already
    is. A generic name smuggled into an `env=` tuple fails here."""
    offenders = []
    for p in sorted(LIB.rglob("*.py")):
        tree = ast.parse(p.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "env":
                for el in getattr(node.value, "elts", []):
                    if isinstance(el, ast.Constant) and not str(el.value).startswith(
                            ("QUOTA_", "AB0T_")):
                        offenders.append(f"{p.name}:{node.value.lineno}:{el.value}")
    assert offenders == [], \
        f"non-namespaced name(s) in a resolver env= tuple: {offenders}"
