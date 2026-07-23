"""Permanent control: the packaged version and the importable version agree.

Found at the 0.6.3 release gate: `pyproject.toml` said 0.6.3 while
`ab0t_quota.__version__` still said 0.6.2. Nothing detected it, because the two
values live in different files and no test had ever compared them.

Why it matters to a consumer: `pip show ab0t-quota` and `ab0t_quota.__version__`
are the two ways anyone identifies which build they are running — in a bug
report, in a support thread, in their own telemetry. When they disagree, every
one of those answers is unreliable, and the disagreement is invisible until
someone is already debugging something else.

This is the D-13 shape (one value, one source, sync-checked) applied to the
release itself.
"""

import pathlib
import re

import ab0t_quota

_PYPROJECT = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"


def _declared_version() -> str:
    """The version pip will publish, read from the packaging source of truth."""
    for line in _PYPROJECT.read_text(encoding="utf-8").splitlines():
        m = re.match(r'^version\s*=\s*"([^"]+)"\s*$', line)
        if m:
            return m.group(1)
    raise AssertionError(f"no top-level `version = \"...\"` found in {_PYPROJECT}")


def test_importable_version_matches_packaged_version():
    assert ab0t_quota.__version__ == _declared_version(), (
        f"ab0t_quota.__version__ is {ab0t_quota.__version__!r} but pyproject.toml "
        f"declares {_declared_version()!r}. A consumer cannot tell which build they "
        f"are running. Bump BOTH."
    )


def test_changelog_documents_the_version_being_shipped():
    """A released version must have a CHANGELOG entry — release notes are the
    consumer's only account of what changed."""
    changelog = (_PYPROJECT.parent / "CHANGELOG.md").read_text(encoding="utf-8")
    version = _declared_version()
    assert f"[{version}]" in changelog, (
        f"CHANGELOG.md has no `[{version}]` section for the version about to ship."
    )
