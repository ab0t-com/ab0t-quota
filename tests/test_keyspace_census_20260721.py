"""K-6 — key-construction census + post-flip v1-write straggler alert.

Census law (spec §9-V1): every counter-key string in ab0t_quota is produced by
the ONE builder seam (ab0t_quota/keyspace.py). An AST walk flags any f-string
or folded concatenation that composes a counter-shape fragment elsewhere.
Planted offenders span FORMS (D-14 rule 2): raw f-string, split literal,
suffix-concat. Exemptions are justified per spec §7 rows, in place, and the
list can only shrink.

Stated limit (D-14 rule 4): a key assembled via %-format, .format(), or
runtime string ops on variables is invisible to this census; those forms are
covered by the behavioural suites (any such key would miss the dual/parse
tests) — not by this static instrument.
"""
import ast
import pathlib

import pytest
import pytest_asyncio
import fakeredis.aioredis

import ab0t_quota

# Fragments that mark a COUNTER-shape construction (spec §2.1 family).
_FRAGMENTS = ("quota:", ":gauge", ":idem", ":acc:", ":rate")

# file → justification (spec §7 row). Shrink-only; every entry is a finding.
EXEMPT = {
    "keyspace.py": "the ONE home of key shape (K-1)",
    "providers.py": "quota:tier:{org} — org-scoped tier cache, deliberately unversioned (spec §7 row 10)",
    "topology.py": "fixed synthetic probe keys, already hash-tagged (spec §7 row 15)",
    "alerts.py": "alert/drift cooldowns — class C cutover at flip, FUTURE svc scope (spec §7 row 9)",
}


def _fold(node):
    """Fold constant-string concatenations so 'quo' + 'ta:' is visible."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _fold(node.left), _fold(node.right)
        if left is not None or right is not None:
            return (left or "") + (right or "")
    return None


def census_violations(source: str, filename: str = "<mem>") -> list:
    """Counter-shape string constructions outside the builder seam."""
    out = []
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)):
                docstrings.add(id(node.body[0].value))
    for node in ast.walk(tree):
        text = None
        if isinstance(node, ast.JoinedStr):  # f-string: constant fragments
            text = "".join(v.value for v in node.values
                           if isinstance(v, ast.Constant) and isinstance(v.value, str))
            if not any(isinstance(v, ast.FormattedValue) for v in node.values):
                text = None  # a pure-literal f-string is handled below
        elif isinstance(node, ast.BinOp):
            folded = _fold(node)
            if folded is not None and (
                    not isinstance(node.left, ast.Constant)
                    or not isinstance(node.right, ast.Constant)):
                pass  # partial folds handled via full-node fold next line
            text = folded
        if text is None and isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings or "*" in node.value:  # docstring / SCAN pattern
                continue
            # a bare "quota:"/"quota:v2:" prefix literal is a PARSE guard
            # (startswith), not a construction — composition needs a suffix.
            if any(f in node.value for f in _FRAGMENTS[1:]):
                text = node.value
        if text and any(f in text for f in _FRAGMENTS):
            # keys never contain spaces — prose (error messages, log lines)
            # mentioning shapes is not a construction.
            if " " in text:
                continue
            out.append(f"{filename}:{getattr(node, 'lineno', '?')}: builds {text!r}")
    return out


def test_census_no_counter_keys_outside_the_seam():
    pkg = pathlib.Path(ab0t_quota.__file__).parent
    failures = []
    for py in sorted(pkg.rglob("*.py")):
        if py.name in EXEMPT:
            continue
        for v in census_violations(py.read_text(), str(py.relative_to(pkg))):
            failures.append(v)
    assert not failures, (
        "counter-key construction outside ab0t_quota/keyspace.py (K-6 census; "
        "route through the Keyspace builder):\n" + "\n".join(failures))


def test_census_exemptions_shrink_only():
    assert set(EXEMPT) <= {"keyspace.py", "providers.py", "topology.py", "alerts.py"}


# --------------------------- planted offenders (D-14: span forms, not instances)

@pytest.mark.parametrize("plant", [
    'key = f"quota:{org}:{rk}:gauge"',                    # raw f-string
    'key = "quo" + "ta:" + "v1:x"',                       # split literal
    'key = prefix + ":idem:" + k',                        # suffix concat
    'key = f":gauge:seq:user:{uid}"',                     # tail-only f-string
])
def test_census_catches_planted_offenders(plant):
    assert census_violations(plant), f"census blind to plant: {plant}"


def test_census_clean_source_not_flagged():
    clean = (
        "def gauge(ks, org, rk):\n"
        "    return ks.gauge_key(org, rk)\n"
    )
    assert census_violations(clean) == []


def test_census_docstring_mentions_not_flagged():
    doc = '''def f():\n    """Redis key: quota:{org_id}:{resource_key}:gauge."""\n    return 1\n'''
    assert census_violations(doc) == []


# ------------------------------------------- post-flip v1-write straggler alert

@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.flushall()
    await r.aclose()


@pytest.mark.asyncio
async def test_straggler_alert_fires_post_flip(redis):
    from ab0t_quota.keyspace_migration import KeyspaceMigrator, check_v1_stragglers
    from ab0t_quota.keyspace import IDEM_TTL_SECONDS
    t = [1_000_000.0]
    mig = KeyspaceMigrator(redis, "svc-a", now_fn=lambda: t[0])
    await mig.dual_on()
    t[0] += IDEM_TTL_SECONDS + 1
    await mig.verify()
    await mig.flip()
    # plant: a pre-mechanism replica writes a raw v1 counter key after the flip
    await redis.set("quota:org-1:sandbox.concurrent:gauge", "2")
    fired = []
    out = await check_v1_stragglers(redis, "svc-a", alert_fn=fired.append)
    assert out["post_flip"] and out["v1_stragglers"] == 1
    assert fired and fired[0]["sample"] == ["quota:org-1:sandbox.concurrent:gauge"]


@pytest.mark.asyncio
async def test_straggler_alert_quiet_pre_flip_and_when_clean(redis):
    """Negative controls: pre-flip v1 keys are normal (no alert); post-flip
    with a clean keyspace stays quiet."""
    from ab0t_quota.keyspace_migration import KeyspaceMigrator, check_v1_stragglers
    from ab0t_quota.keyspace import IDEM_TTL_SECONDS
    await redis.set("quota:org-1:sandbox.concurrent:gauge", "2")
    fired = []
    out = await check_v1_stragglers(redis, "svc-a", alert_fn=fired.append)
    assert not out["post_flip"] and not fired
    await redis.delete("quota:org-1:sandbox.concurrent:gauge")
    t = [1_000_000.0]
    mig = KeyspaceMigrator(redis, "svc-a", now_fn=lambda: t[0])
    await mig.dual_on()
    t[0] += IDEM_TTL_SECONDS + 1
    await mig.verify()
    await mig.flip()
    out2 = await check_v1_stragglers(redis, "svc-a", alert_fn=fired.append)
    assert out2["post_flip"] and out2["v1_stragglers"] == 0 and not fired
