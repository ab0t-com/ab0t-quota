"""Release prep (T-29): the migration notice's self-audit must WORK.

`docs/migrating-from-ambient-resolution.md` ships grep commands a consumer
runs over THEIR repo to find the defect our old template compiled into their
code. A self-audit that misses the defect it exists to find would be the
program's failure shape shipped to the consumer — so the commands are
EXTRACTED FROM THE DOC (never re-typed) and executed against a
consumer-shaped scratch repo containing the OLD template's exact lines.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "migrating-from-ambient-resolution.md"

#: The OLD template's defective lines, verbatim (pre-2026-07-21 shape) — what
#: a consumer who followed our instructions has in their repo today.
OLD_TEMPLATE_QUOTA_PY = '''import os
from redis.asyncio import Redis

async def startup(redis_url=None):
    url = (redis_url
           or storage_config.get("redis_url")
           or os.getenv("QUOTA_REDIS_URL")
           or os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    _redis = Redis.from_url(url)
    tiers = load_tiers(config) or DEFAULT_TIERS
'''


def _doc_grep_commands() -> list[str]:
    """Extract the grep commands from the doc's bash fence — the doc is the
    source; a re-typed pattern here would be the constant written twice."""
    text = DOC.read_text()
    m = re.search(r"```bash\n(.*?)```", text, re.DOTALL)
    assert m, "the migration doc lost its self-audit bash block"
    cmds = [l.strip() for l in m.group(1).splitlines()
            if l.strip().startswith("grep")]
    assert len(cmds) >= 3, f"expected >=3 self-audit greps, found {cmds}"
    return cmds


@pytest.fixture
def consumer_repo(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "quota.py").write_text(OLD_TEMPLATE_QUOTA_PY)
    (tmp_path / "app" / "clean.py").write_text("x = 1\n")
    return tmp_path


def test_every_selfaudit_grep_finds_its_target(consumer_repo):
    for cmd in _doc_grep_commands():
        proc = subprocess.run(cmd, shell=True, cwd=consumer_repo,
                              capture_output=True, text=True)
        assert proc.returncode == 0 and proc.stdout.strip(), \
            (f"self-audit command found NOTHING in a repo carrying the old "
             f"template's defect:\n  {cmd}\n(stderr: {proc.stderr.strip()})")
    # and the union of hits covers the or-chain line itself
    all_out = "".join(
        subprocess.run(c, shell=True, cwd=consumer_repo,
                       capture_output=True, text=True).stdout
        for c in _doc_grep_commands())
    assert 'os.getenv("REDIS_URL"' in all_out, \
        "the or-chain's generic read was not surfaced by any self-audit grep"
    assert "DEFAULT_TIERS" in all_out


def test_selfaudit_is_quiet_on_a_clean_repo(tmp_path):
    """Control the other way: a repo WITHOUT the defect produces no hits —
    the audit does not cry wolf (a noisy audit trains consumers to ignore it)."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "quota.py").write_text(
        'url = storage_config.get("redis_url") or os.getenv("QUOTA_REDIS_URL")\n'
        "tiers = load_tiers(config)\n")
    for cmd in _doc_grep_commands():
        proc = subprocess.run(cmd, shell=True, cwd=tmp_path,
                              capture_output=True, text=True)
        assert not proc.stdout.strip(), \
            f"self-audit false positive on a clean repo: {cmd}\n{proc.stdout}"
