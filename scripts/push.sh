#!/usr/bin/env bash
# push.sh — single command to publish ab0t-quota safely.
#
# Usage:
#   bash scripts/push.sh                    # push current branch (after gate)
#   bash scripts/push.sh v0.2.0             # push branch + create & push tag
#   DRY_RUN=1 bash scripts/push.sh v0.2.0   # run all checks, don't push
#   AUTO_CONFIRM=1 bash scripts/push.sh ... # skip y/N prompt (CI)
#   SKIP_CHECK=1 bash scripts/push.sh ...   # bypass pre-publish (NOT recommended)
#
# Idempotency rules (every state is a fixed point of "run me again"):
#   - Tag already exists locally AT HEAD          -> reuse it, push it
#   - Tag already exists locally NOT at HEAD      -> abort with a clear remedy
#   - Tag already exists on remote AT HEAD        -> success, nothing to do
#   - Tag already exists on remote NOT at HEAD    -> abort (version is burned)
#   - Branch already in sync, tag missing remote  -> push tag only
#   - Branch ahead, tag in sync remotely          -> push branch only
#   - Both in sync                                -> exit 0 with a friendly note

set -u
cd "$(dirname "$0")/.." || exit 1

readonly RED='\033[0;31m'
readonly YEL='\033[0;33m'
readonly GRN='\033[0;32m'
readonly BLU='\033[0;34m'
readonly BOLD='\033[1m'
readonly NC='\033[0m'

err()   { echo -e "${RED}x $*${NC}"  >&2; }
warn()  { echo -e "${YEL}! $*${NC}"  >&2; }
ok()    { echo -e "${GRN}- $*${NC}"; }
info()  { echo -e "${BLU}i $*${NC}"; }
step()  { echo; echo -e "${BOLD}-- $* --${NC}"; }
fatal() { err "$*"; exit 1; }

TAG="${1:-}"
DRY_RUN="${DRY_RUN:-0}"
AUTO_CONFIRM="${AUTO_CONFIRM:-0}"
SKIP_CHECK="${SKIP_CHECK:-0}"

# ---------------------------------------------------------------------------
# 0. Sanity
# ---------------------------------------------------------------------------

git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || fatal "Not inside a git repo"

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[ -n "$BRANCH" ] || fatal "Could not determine current branch"

REMOTE="$(git config --get branch."$BRANCH".remote || echo origin)"

HEAD_SHA="$(git rev-parse HEAD)"
TAG_LOCAL_SHA=""
TAG_REMOTE_SHA=""
TAG_LOCAL_AT_HEAD=0
TAG_REMOTE_AT_HEAD=0
TAG_NEEDS_CREATE=0
TAG_NEEDS_PUSH=0

# Validate tag format + reconcile local/remote state
if [ -n "$TAG" ]; then
  case "$TAG" in
    v[0-9]*.[0-9]*.[0-9]*|v[0-9]*.[0-9]*.[0-9]*-*) ;;
    *) fatal "Tag '$TAG' must look like v0.2.0 or v0.2.0-rc1" ;;
  esac

  # Tag must match pyproject.toml version
  pp_version="$(grep -E '^version\s*=' pyproject.toml | head -1 | grep -oE '"[^"]+"' | tr -d '"')"
  if [ "$TAG" != "v$pp_version" ]; then
    fatal "Tag '$TAG' doesn't match pyproject.toml version 'v$pp_version' — bump or rename"
  fi

  # Resolve local tag (if any)
  if git rev-parse "$TAG" >/dev/null 2>&1; then
    TAG_LOCAL_SHA="$(git rev-list -n 1 "$TAG")"
    if [ "$TAG_LOCAL_SHA" = "$HEAD_SHA" ]; then
      TAG_LOCAL_AT_HEAD=1
    fi
  fi

  # Resolve remote tag (if any). Note: ls-remote shows the tag object SHA for
  # annotated tags; use ^{} to dereference to the commit SHA so we compare
  # like-for-like with HEAD_SHA.
  remote_line="$(git ls-remote --tags "$REMOTE" "refs/tags/$TAG^{}" 2>/dev/null \
               || git ls-remote --tags "$REMOTE" "refs/tags/$TAG" 2>/dev/null)"
  if [ -n "$remote_line" ]; then
    TAG_REMOTE_SHA="$(echo "$remote_line" | head -1 | awk '{print $1}')"
    if [ "$TAG_REMOTE_SHA" = "$HEAD_SHA" ]; then
      TAG_REMOTE_AT_HEAD=1
    fi
  fi

  # Decide what (if anything) needs to happen for the tag.
  if [ -n "$TAG_REMOTE_SHA" ]; then
    if [ "$TAG_REMOTE_AT_HEAD" = "1" ]; then
      ok "Tag $TAG already on $REMOTE pointing at HEAD ($HEAD_SHA) — nothing to push for the tag"
    else
      err  "Tag $TAG exists on $REMOTE pointing at $TAG_REMOTE_SHA, but HEAD is $HEAD_SHA"
      err  "That version is already published. Bump pyproject.toml to a new version and re-run."
      exit 1
    fi
  elif [ -n "$TAG_LOCAL_SHA" ]; then
    if [ "$TAG_LOCAL_AT_HEAD" = "1" ]; then
      ok "Tag $TAG exists locally at HEAD — will reuse and push it"
      TAG_NEEDS_PUSH=1
    else
      err  "Tag $TAG exists locally pointing at $TAG_LOCAL_SHA, but HEAD is $HEAD_SHA"
      err  "Stale local tag from a previous attempt. Fix by either:"
      err  "  - moving the tag:  git tag -f $TAG  (then re-run)"
      err  "  - deleting:        git tag -d $TAG  (then re-run; we will recreate)"
      exit 1
    fi
  else
    TAG_NEEDS_CREATE=1
    TAG_NEEDS_PUSH=1
  fi
fi

# ---------------------------------------------------------------------------
# 1. Hook installed?
# ---------------------------------------------------------------------------

step "1. Verifying git pre-push hook"
HOOK="$(git rev-parse --show-toplevel)/.git/hooks/pre-push"
if [ ! -x "$HOOK" ]; then
  warn "pre-push hook not installed — installing now"
  bash scripts/install-hooks.sh >/dev/null
fi
if grep -q "pre-publish.sh" "$HOOK" 2>/dev/null; then
  ok "Hook is wired to scripts/pre-publish.sh"
else
  warn "pre-push hook exists but doesn't call pre-publish.sh — re-installing"
  bash scripts/install-hooks.sh >/dev/null
fi

# ---------------------------------------------------------------------------
# 2. Show what's about to ship + decide what's needed
# ---------------------------------------------------------------------------

step "2. Push preview"
echo "  Branch:    $BRANCH"
echo "  Remote:    $REMOTE"
echo "  HEAD:      $HEAD_SHA"
[ -n "$TAG" ] && echo "  Tag:       $TAG"

BRANCH_NEEDS_PUSH=0
upstream="$REMOTE/$BRANCH"
if git rev-parse --verify "$upstream" >/dev/null 2>&1; then
  ahead="$(git rev-list --count "$upstream"..HEAD)"
  behind="$(git rev-list --count HEAD.."$upstream")"
  echo "  Commits ahead:  $ahead"
  echo "  Commits behind: $behind"
  if [ "$behind" -gt 0 ]; then
    warn "$behind commit(s) on remote not present locally — consider pulling first"
  fi
  if [ "$ahead" -gt 0 ]; then
    BRANCH_NEEDS_PUSH=1
    echo
    echo "  Commits to push:"
    git log --oneline "$upstream"..HEAD | head -10 | sed 's/^/    /'
    if [ "$ahead" -gt 10 ]; then
      echo "    ... and $((ahead - 10)) more"
    fi
  fi
else
  info "Branch '$BRANCH' has no upstream on $REMOTE yet — first push"
  BRANCH_NEEDS_PUSH=1
fi

# Uncommitted changes warning
if [ -n "$(git status --porcelain)" ]; then
  warn "Uncommitted changes in working tree — they will NOT be pushed"
  git status --short | head -10 | sed 's/^/    /'
fi

# Idempotency exit: if there is genuinely nothing to do, say so and stop.
if [ "$BRANCH_NEEDS_PUSH" = "0" ] && [ "$TAG_NEEDS_CREATE" = "0" ] && [ "$TAG_NEEDS_PUSH" = "0" ]; then
  if [ -z "$TAG" ]; then
    warn "Nothing to push (branch up to date) and no tag given"
    info "Specify a tag (bash scripts/push.sh v0.2.0) to publish a release"
  else
    ok "Branch and tag $TAG are both already in sync with $REMOTE — nothing to do"
  fi
  exit 0
fi

# ---------------------------------------------------------------------------
# 3. Pre-publish gate
# ---------------------------------------------------------------------------

if [ "$SKIP_CHECK" = "1" ]; then
  step "3. Pre-publish gate — SKIPPED (SKIP_CHECK=1)"
  warn "You're skipping the safety net. The pre-push hook will still fire."
else
  step "3. Pre-publish gate"
  if [ -n "$TAG" ]; then
    PRE_PUBLISH_TAG="$TAG" bash scripts/pre-publish.sh
  else
    bash scripts/pre-publish.sh
  fi
  gate_status=$?
  if [ "$gate_status" -ne 0 ]; then
    fatal "Pre-publish gate failed — fix the errors above and retry"
  fi
fi

# ---------------------------------------------------------------------------
# 4. Confirm
# ---------------------------------------------------------------------------

if [ "$DRY_RUN" = "1" ]; then
  step "4. DRY RUN — would push but stopping here"
  ok "All gates passed. Re-run without DRY_RUN=1 to actually push."
  exit 0
fi

if [ "$AUTO_CONFIRM" != "1" ]; then
  step "4. Confirm"
  plan=""
  [ "$BRANCH_NEEDS_PUSH" = "1" ]   && plan="${plan}push branch $BRANCH; "
  [ "$TAG_NEEDS_CREATE" = "1" ]    && plan="${plan}create tag $TAG; "
  [ "$TAG_NEEDS_PUSH" = "1" ]      && plan="${plan}push tag $TAG; "
  echo "About to: ${plan%; }"
  printf "Proceed? [y/N] "
  read -r answer
  case "$answer" in
    y|Y|yes|YES) ;;
    *) info "Aborted by user"; exit 0 ;;
  esac
else
  step "4. AUTO_CONFIRM=1 — proceeding without prompt"
fi

# ---------------------------------------------------------------------------
# 5. Create tag (if needed)
# ---------------------------------------------------------------------------

if [ "$TAG_NEEDS_CREATE" = "1" ]; then
  step "5. Creating annotated tag $TAG"
  if git tag -a "$TAG" -m "Release $TAG"; then
    ok "Tag created locally"
  else
    fatal "git tag failed"
  fi
fi

# ---------------------------------------------------------------------------
# 6. Push branch (if needed)
# ---------------------------------------------------------------------------

if [ "$BRANCH_NEEDS_PUSH" = "1" ]; then
  step "6. Pushing branch $BRANCH to $REMOTE"
  if git push "$REMOTE" "$BRANCH"; then
    ok "Branch pushed"
  else
    err "Branch push failed"
    if [ "$TAG_NEEDS_CREATE" = "1" ]; then
      warn "Local tag $TAG remains — delete with: git tag -d $TAG"
    fi
    exit 1
  fi
else
  info "6. Branch already up to date — skipping branch push"
fi

# ---------------------------------------------------------------------------
# 7. Push tag (if needed)
# ---------------------------------------------------------------------------

if [ "$TAG_NEEDS_PUSH" = "1" ]; then
  step "7. Pushing tag $TAG to $REMOTE"
  if git push "$REMOTE" "$TAG"; then
    ok "Tag pushed"
  else
    err "Tag push failed — local tag still exists"
    err "Re-run this script — it will detect the local tag and finish the push."
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo
echo "---------------------------------------------"
echo -e "${GRN}${BOLD}PUBLISHED${NC}"
echo "  Branch: $BRANCH -> $REMOTE"
[ -n "$TAG" ] && echo "  Tag:    $TAG -> $REMOTE"
echo
if [ -n "$TAG" ]; then
  echo "Next:"
  echo "  - Verify the tag on GitHub: https://github.com/ab0t-com/ab0t-quota/releases/tag/$TAG"
  echo "  - Update consumer requirements.txt files to '@$TAG' if they don't already pin"
  echo "  - If this was a breaking release, post in #mesh-platform"
fi
