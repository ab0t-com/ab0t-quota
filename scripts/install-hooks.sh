#!/usr/bin/env bash
# Install gitleaks git hooks for ab0t-quota
# Run this after cloning: bash scripts/install-hooks.sh

set -euo pipefail

HOOKS_DIR="$(git rev-parse --show-toplevel)/.git/hooks"

echo "Installing gitleaks git hooks..."

# Check for gitleaks
if ! command -v gitleaks &>/dev/null; then
    echo "WARNING: gitleaks is not installed."
    echo "Install: https://github.com/gitleaks/gitleaks#installing"
    echo "  brew install gitleaks      (macOS)"
    echo "  sudo apt install gitleaks  (Debian/Ubuntu)"
    echo "  go install github.com/zricethezav/gitleaks/v8@latest  (Go)"
    exit 1
fi

# pre-commit hook
cat > "${HOOKS_DIR}/pre-commit" << 'HOOK'
#!/usr/bin/env bash
set -euo pipefail
echo "[pre-commit] Running gitleaks on staged changes..."
if ! command -v gitleaks &>/dev/null; then
    echo "ERROR: gitleaks is not installed. Run: bash scripts/install-hooks.sh"
    exit 1
fi
gitleaks protect --staged --config .gitleaks.toml --verbose
echo "[pre-commit] gitleaks check passed."
HOOK
chmod +x "${HOOKS_DIR}/pre-commit"

# pre-push hook — runs the full pre-publish gate (gitleaks + secrets +
# tests + version + new-file audits). Bypass with: git push --no-verify
# (don't bypass unless you know what you're doing).
cat > "${HOOKS_DIR}/pre-push" << 'HOOK'
#!/usr/bin/env bash
set -e
REPO_ROOT="$(git rev-parse --show-toplevel)"
echo "[pre-push] Running scripts/pre-publish.sh (full publish-readiness check)..."
bash "$REPO_ROOT/scripts/pre-publish.sh"
HOOK
chmod +x "${HOOKS_DIR}/pre-push"

echo "Hooks installed successfully:"
echo "  - pre-commit: scans staged changes for secrets (gitleaks)"
echo "  - pre-push:   full publish-readiness check (scripts/pre-publish.sh)"
echo ""
echo "Bypass pre-push hook (NOT recommended): git push --no-verify"
echo "gitleaks version: $(gitleaks version)"
