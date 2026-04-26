#!/usr/bin/env bash
# pre-publish.sh — gate before pushing to the public ab0t-quota repo.
#
# Run before any push to main or any tag publish:
#   bash scripts/pre-publish.sh
#
# Exits non-zero if anything is unsafe to publish. Prints a clear
# summary of what to fix. Designed to be safe to run any time.
#
# Checks (in order, fail-fast):
#   1. gitleaks scan — staged changes + working tree
#   2. Real-looking live secrets the gitleaks rules might miss
#   3. Tests pass
#   4. Library version in pyproject.toml matches __init__.py
#   5. If a tag is requested, that tag matches the version
#   6. Internal-only patterns in NEW files only (since dev/ is grandfathered)
#   7. Internal customer / org names that shouldn't ship publicly
#
# Designed to ratchet — if a check finds something pre-existing in
# already-public files, it's reported as INFO not ERROR. Only NEW
# additions in this push gate it.

set -u
cd "$(dirname "$0")/.." || exit 1

readonly RED='\033[0;31m'
readonly YEL='\033[0;33m'
readonly GRN='\033[0;32m'
readonly BLU='\033[0;34m'
readonly NC='\033[0m'

errors=0
warnings=0

err()  { echo -e "${RED}✖ $*${NC}";  errors=$((errors + 1)); }
warn() { echo -e "${YEL}⚠ $*${NC}";  warnings=$((warnings + 1)); }
ok()   { echo -e "${GRN}✓ $*${NC}"; }
info() { echo -e "${BLU}ℹ $*${NC}"; }

section() { echo; echo -e "${BLU}── $* ──${NC}"; }

# Determine which files are NEW since the last published tag. These are
# the ones we hold to a strict bar — pre-existing files on main are
# grandfathered (we already shipped them).
last_tag="$(git describe --tags --abbrev=0 2>/dev/null || echo '')"
if [ -n "$last_tag" ]; then
  changed_files="$(git diff --name-only "$last_tag"..HEAD 2>/dev/null)"
  staged_files="$(git diff --cached --name-only 2>/dev/null)"
  new_files="$(printf '%s\n%s\n' "$changed_files" "$staged_files" | sort -u | grep -v '^$' || true)"
  info "Comparing against last tag: $last_tag"
else
  warn "No previous tag found — treating all tracked files as new"
  new_files="$(git ls-files)"
fi

# ---------------------------------------------------------------------------
# 1. Gitleaks scan
# ---------------------------------------------------------------------------

section "1. Gitleaks scan (full repo)"
if command -v gitleaks >/dev/null 2>&1; then
  if gitleaks detect --no-banner --redact --config .gitleaks.toml >/tmp/gitleaks.log 2>&1; then
    ok "No secrets detected by gitleaks"
  else
    err "Gitleaks found potential secrets — review /tmp/gitleaks.log"
    cat /tmp/gitleaks.log | head -40
  fi
else
  warn "gitleaks not installed — skipping (install: https://github.com/gitleaks/gitleaks)"
fi

# ---------------------------------------------------------------------------
# 2. High-precision live-secret patterns
# ---------------------------------------------------------------------------

section "2. Live-secret pattern scan (NEW files only)"

# Patterns that should NEVER appear in any source — values, not placeholders
declare -A patterns=(
  ["Stripe live secret key"]='sk_live_[A-Za-z0-9]{20,}'
  ["Stripe live publishable key"]='pk_live_[A-Za-z0-9]{20,}'
  ["Stripe webhook secret"]='whsec_[A-Za-z0-9]{20,}'
  ["AWS access key ID"]='AKIA[0-9A-Z]{16}'
  ["GitHub fine-grained token"]='github_pat_[A-Za-z0-9_]{60,}'
  ["GitHub OAuth token"]='gh[oprs]_[A-Za-z0-9]{30,}'
  ["Slack bot token"]='xox[baprs]-[A-Za-z0-9-]{20,}'
  ["Generic 32+ hex secret"]='(?i)(secret|token|password)["'"'"' :=]+[a-f0-9]{32,}'
  ["ab0t live mesh key"]='ab0t_sk_live_[A-Za-z0-9]{16,}'
)

if [ -z "$new_files" ]; then
  info "No new files to scan"
else
  scan_set="$(echo "$new_files" | tr '\n' ' ')"
  for label in "${!patterns[@]}"; do
    regex="${patterns[$label]}"
    # Only scan files that exist (some may be deleted)
    matches=""
    for f in $scan_set; do
      [ -f "$f" ] || continue
      hits=$(grep -EnH "$regex" "$f" 2>/dev/null | grep -v '__pycache__' || true)
      [ -n "$hits" ] && matches+="$hits"$'\n'
    done
    if [ -n "$matches" ]; then
      err "$label found in new files:"
      echo "$matches" | head -5 | sed 's/^/    /'
    fi
  done
  [ "$errors" -eq 0 ] && ok "No live secrets in new files"
fi

# ---------------------------------------------------------------------------
# 3. Test suite passes
# ---------------------------------------------------------------------------

section "3. Test suite"
if [ -x .venv/bin/python ]; then
  py=.venv/bin/python
elif command -v python3 >/dev/null 2>&1; then
  py=python3
else
  warn "No Python interpreter found — skipping tests"
  py=""
fi

if [ -n "$py" ]; then
  if "$py" -m pytest -q --tb=line >/tmp/pytest.log 2>&1; then
    test_count="$(grep -oE '[0-9]+ passed' /tmp/pytest.log | tail -1)"
    ok "Tests passing: ${test_count:-unknown count}"
  else
    err "Tests failing — see /tmp/pytest.log"
    tail -20 /tmp/pytest.log
  fi
fi

# ---------------------------------------------------------------------------
# 4. Version consistency
# ---------------------------------------------------------------------------

section "4. Version consistency"
pp_version=$(grep -E '^version\s*=' pyproject.toml | head -1 | grep -oE '"[^"]+"' | tr -d '"')
init_version=$(grep -E '^__version__\s*=' ab0t_quota/__init__.py | grep -oE '"[^"]+"' | tr -d '"')

if [ "$pp_version" = "$init_version" ]; then
  ok "pyproject.toml and __init__.py both report v$pp_version"
else
  err "Version mismatch: pyproject=$pp_version, __init__.py=$init_version"
fi

# ---------------------------------------------------------------------------
# 5. Tag readiness (if PRE_PUBLISH_TAG is set)
# ---------------------------------------------------------------------------

if [ -n "${PRE_PUBLISH_TAG:-}" ]; then
  section "5. Tag readiness ($PRE_PUBLISH_TAG)"
  expected="v$pp_version"
  if [ "$PRE_PUBLISH_TAG" = "$expected" ]; then
    ok "Requested tag matches version"
  else
    err "Tag '$PRE_PUBLISH_TAG' doesn't match version (expected '$expected')"
  fi
  if git rev-parse "$PRE_PUBLISH_TAG" >/dev/null 2>&1; then
    err "Tag $PRE_PUBLISH_TAG already exists — bump version or delete the tag"
  else
    ok "Tag $PRE_PUBLISH_TAG is available"
  fi
fi

# ---------------------------------------------------------------------------
# 6. Internal-only content in NEW files
# ---------------------------------------------------------------------------
# `dev/` was already public before we set this gate up — grandfathered.
# But anything new added to `dev/` in this push gets warned. Other
# patterns that should never appear in NEW content:

section "6. Internal-only content in NEW files"

if [ -n "$new_files" ]; then
  # Net-new files added under dev/ (warn, since dev/ is already public
  # but new additions are likely internal scratch)
  new_dev_files=$(echo "$new_files" | grep '^dev/' || true)
  if [ -n "$new_dev_files" ]; then
    warn "New files added under dev/ — confirm they're public-safe:"
    echo "$new_dev_files" | sed 's/^/    /'
  fi

  # Phrases that suggest confidential material added in NEW files
  forbidden_phrases=(
    "CONFIDENTIAL"
    "INTERNAL ONLY"
    "DO NOT DISTRIBUTE"
    "PROPRIETARY"
    "Customer:"
    "Revenue:"
    "MRR:"
    "ARR:"
  )

  for phrase in "${forbidden_phrases[@]}"; do
    matches=""
    for f in $(echo "$new_files" | tr '\n' ' '); do
      [ -f "$f" ] || continue
      # Don't scan the check script itself — it contains the literal
      # phrases as its needle list, which would always self-match.
      case "$f" in
        scripts/pre-publish.sh|scripts/push.sh) continue ;;
      esac
      hits=$(grep -nH -F "$phrase" "$f" 2>/dev/null || true)
      [ -n "$hits" ] && matches+="$hits"$'\n'
    done
    if [ -n "$matches" ]; then
      warn "Phrase '$phrase' found in new files (review before publish):"
      echo "$matches" | head -3 | sed 's/^/    /'
    fi
  done

  if [ -z "$new_dev_files" ]; then
    ok "No new internal-only content in new files"
  fi
fi

# ---------------------------------------------------------------------------
# 7. Internal customer / company names that should not ship
# ---------------------------------------------------------------------------

section "7. Customer / company name leakage in NEW files"

# Curate this list as we sign customers. Names that should NEVER appear
# in the public repo (customer logos, design partners under NDA, etc.)
forbidden_customers=(
  # Add real customer names here as you sign them, e.g.:
  # "AcmeCorp"
  # "FoobarInc"
)

if [ ${#forbidden_customers[@]} -eq 0 ]; then
  info "No customer-name allowlist configured (edit scripts/pre-publish.sh to add)"
else
  for name in "${forbidden_customers[@]}"; do
    matches=""
    for f in $(echo "$new_files" | tr '\n' ' '); do
      [ -f "$f" ] || continue
      hits=$(grep -inH -F "$name" "$f" 2>/dev/null || true)
      [ -n "$hits" ] && matches+="$hits"$'\n'
    done
    if [ -n "$matches" ]; then
      err "Customer name '$name' found in new files:"
      echo "$matches" | head -5 | sed 's/^/    /'
    fi
  done
fi

# ---------------------------------------------------------------------------
# 8. .gitignore covers the obvious
# ---------------------------------------------------------------------------

section "8. .gitignore sanity"

# Files that should never be tracked
must_ignore=(
  ".env"
  "*.pem"
  "*.key"
  "credentials.json"
  "secrets.json"
)
for pattern in "${must_ignore[@]}"; do
  if ! grep -q "^${pattern}$" .gitignore 2>/dev/null; then
    err ".gitignore missing pattern: $pattern"
  fi
done

# Check for accidentally-tracked sensitive files
tracked_sensitive=$(git ls-files | grep -E '^\.env$|\.pem$|\.key$|credentials\.json$|secrets\.json$' || true)
if [ -n "$tracked_sensitive" ]; then
  err "Sensitive files are TRACKED — these will be pushed publicly:"
  echo "$tracked_sensitive" | sed 's/^/    /'
fi

[ "$errors" -eq 0 ] && ok "No sensitive files tracked"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo
echo "─────────────────────────────────────────────"
if [ "$errors" -gt 0 ]; then
  echo -e "${RED}BLOCKED — $errors error(s), $warnings warning(s)${NC}"
  echo "Fix the errors above before publishing."
  exit 1
elif [ "$warnings" -gt 0 ]; then
  echo -e "${YEL}REVIEW — $warnings warning(s), 0 errors${NC}"
  echo "Review warnings above. Continue if you've confirmed they're safe."
  exit 0
else
  echo -e "${GRN}READY TO PUBLISH${NC}"
  echo "All checks passed. Safe to push."
  exit 0
fi
