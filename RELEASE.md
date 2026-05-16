# Releasing `ab0t-quota`

This is a public library — customers drop it into their services to get billing/quota/payments. Releases go through `scripts/push.sh` which gates on gitleaks, live-secret patterns, pytest, version consistency, internal-only phrases, and customer-name leakage.

## TL;DR

```bash
# 1. Bump version (both files MUST match)
#    pyproject.toml         version = "X.Y.Z"
#    ab0t_quota/__init__.py __version__ = "X.Y.Z"

# 2. Commit
git add -A && git commit -m "vX.Y.Z: <one-line change summary>"

# 3. Dry-run to validate gates (no push)
DRY_RUN=1 bash scripts/push.sh vX.Y.Z

# 4. Real push (creates annotated tag, pushes branch + tag)
bash scripts/push.sh vX.Y.Z
```

The script is idempotent — re-running after a partial failure picks up where it left off.

## Env vars / flags

| Var | Effect |
|---|---|
| `DRY_RUN=1` | Run every gate; do not push. Use this first. |
| `AUTO_CONFIRM=1` | Skip the `y/N` prompt. CI only. |
| `SKIP_CHECK=1` | Bypass `pre-publish.sh`. **Don't.** The git pre-push hook fires regardless and it's the safety net for a public repo. |

Tag format is mandatory `vMAJOR.MINOR.PATCH` (or `vX.Y.Z-rcN`). The script aborts if the tag doesn't match `pyproject.toml`.

## What the pre-publish gate checks

1. **Gitleaks scan** — full repo. Install `gitleaks` first (`brew install gitleaks` or see [releases](https://github.com/gitleaks/gitleaks/releases)). Failures = real secrets in tracked files.
2. **Live-secret pattern scan** — only files changed since the last tag. Catches `sk_live_…`, `pk_live_…`, `whsec_…`, `AKIA…`, `gh[oprs]_…`, `xox[baprs]-…`, `ab0t_sk_live_…`.
3. **Pytest** — `pytest -q --tb=line` against your venv (or system `python3`). All tests must pass.
4. **Version consistency** — `pyproject.toml` `version =` and `ab0t_quota/__init__.py` `__version__ =` must match, and the tag must be `v<that version>`.
5. **Tag readiness** — tag doesn't exist remotely (or exists at HEAD = idempotent retry).
6. **Internal-only content** — warns on new files under `dev/` or containing `CONFIDENTIAL`, `INTERNAL ONLY`, `DO NOT DISTRIBUTE`, `PROPRIETARY`, `Customer:`, `Revenue:`, `MRR:`, `ARR:`.
7. **Customer name leakage** — fails on names from the allowlist in `scripts/pre-publish.sh:243`. Add real customer names as you sign them.
8. **.gitignore sanity** — `.env`, `*.pem`, `*.key`, `credentials.json`, `secrets.json` are all ignored AND not tracked.

A failure exits non-zero with the offending file/line — fix and re-run.

## After the push lands

1. Check the tag landed: `https://github.com/ab0t-com/ab0t-quota/releases/tag/vX.Y.Z`
2. Bump consumer `requirements.txt` pins from the previous `@vA.B.C` to `@vX.Y.Z` (or leave `@main` if the consumer wants to ride latest).
3. If breaking: post in `#mesh-platform` and tag the change with `BREAKING:` in the commit summary.

## Recovery from a half-pushed state

The script's idempotency rules (see top of `push.sh`):
- Tag exists locally at HEAD, missing on remote → re-run, it'll push the tag.
- Tag exists remotely at HEAD → re-run, it'll say "nothing to do".
- Tag exists somewhere OTHER than HEAD → that version is burned. Bump to next patch and re-run.
- Stale local tag (different SHA) → `git tag -d vX.Y.Z` then re-run.

## When NOT to release

- Tests are skipping silently because `pytest` couldn't find a venv. Make sure `pytest -q` actually runs N tests.
- You're adding a required parameter to a function or endpoint. That's a breaking change — bump the MINOR, not just the PATCH, and post `BREAKING:` in `#mesh-platform`. (See 0.2.7 → 0.2.8 retrospective: payment-service added `verification_token` as required on `/verify` without coordinating a lib release — caused a prod outage.)
- Your changes touch files in a customer's directory that shouldn't be in this repo. The lib is public; private content goes in the consumer's repo.

## Files involved

| File | Purpose |
|---|---|
| `scripts/push.sh` | The single command users run |
| `scripts/pre-publish.sh` | The gate — runs the 8 checks above |
| `scripts/install-hooks.sh` | Installs `.git/hooks/pre-push` so the gate also fires on plain `git push` |
| `pyproject.toml` | Authoritative version |
| `ab0t_quota/__init__.py` | Mirror version (must match pyproject) |
| `.gitleaksignore` | Allowlist for the gitleaks scan (use sparingly) |
