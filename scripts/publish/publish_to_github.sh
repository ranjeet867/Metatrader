#!/usr/bin/env bash
# publish_to_github.sh — one-shot script that:
#   1. Removes any stale git lock
#   2. Cleans personal data from git tracking (kept on disk)
#   3. Commits the open-source-ready snapshot
#   4. Creates a public GitHub repo via `gh` and pushes
#
# Run from the repo root on your Mac:
#   ./scripts/publish/publish_to_github.sh
#
# Prereqs:
#   - GitHub CLI installed:  brew install gh
#   - GitHub auth done:      gh auth login
set -euo pipefail

REPO_NAME="${REPO_NAME:-mt5_quant_trader_v2}"
DEFAULT_BRANCH="${DEFAULT_BRANCH:-main}"

cd "$(dirname "$0")/../.." || exit 1
echo "→ Working dir: $(pwd)"

if [[ -f .git/index.lock ]]; then
  echo "→ removing stale .git/index.lock"
  rm -f .git/index.lock
fi

# Untrack personal data (already in .gitignore but historically committed).
echo "→ untracking personal data from git index (files stay on disk)"
git rm --cached -r --ignore-unmatch \
    data/*.parquet \
    data/v2.db data/v2.db-wal data/v2.db-shm \
    data/EMERGENCY_STOP \
    data/risk_config.json \
    data/symbol_info.json \
    data/accounts.json \
    data/accounts/ \
    data/mt5_tester_reports/ \
    docs/grid_results.md \
    docs/optimization_*.md \
    docs/parity_*.md \
    docs/*_screening.md \
    2>/dev/null || true

# Stage everything that should be public.
echo "→ staging open-source files"
git add -A

# Sanity: confirm no obviously-personal files are staged.
if git diff --cached --name-only | grep -E "(accounts/|v2\.db|EMERGENCY_STOP|\.parquet$|optimization_)" >/dev/null; then
  echo "⛔ ABORT: personal data is still staged. Inspect:"
  git diff --cached --name-only | grep -E "(accounts/|v2\.db|EMERGENCY_STOP|\.parquet$|optimization_)"
  exit 1
fi

echo "→ committing"
git commit -m "Open-source release: README + LICENSE + CI + scrubbed personal data" || true

# Default branch
git branch -M "$DEFAULT_BRANCH"

# Check gh CLI
if ! command -v gh >/dev/null 2>&1; then
  echo
  echo "⛔ GitHub CLI not installed. Install with:  brew install gh"
  echo "   Then re-run this script. Alternatively, push manually:"
  echo "   gh repo create $REPO_NAME --public --source=. --remote=origin --push"
  echo "   — or —"
  echo "   git remote add origin git@github.com:<YOUR_USER>/$REPO_NAME.git"
  echo "   git push -u origin $DEFAULT_BRANCH"
  exit 1
fi

# Verify gh is authed
if ! gh auth status >/dev/null 2>&1; then
  echo
  echo "⛔ GitHub CLI not authenticated. Run: gh auth login"
  exit 1
fi

# Create the public repo if it doesn't exist
if ! gh repo view "$REPO_NAME" >/dev/null 2>&1; then
  echo "→ creating public repo $REPO_NAME on GitHub"
  gh repo create "$REPO_NAME" --public \
      --description "Production-grade MT5 quant trading platform with FTMO compliance, autonomous portfolio optimizer, and 13 hard invariants." \
      --homepage "" \
      --source=. --remote=origin --push
else
  echo "→ repo $REPO_NAME exists; pushing"
  if ! git remote get-url origin >/dev/null 2>&1; then
    USER="$(gh api user -q .login)"
    git remote add origin "https://github.com/$USER/$REPO_NAME.git"
  fi
  git push -u origin "$DEFAULT_BRANCH"
fi

# Replace the README placeholder with the actual owner
USER="$(gh api user -q .login)"
sed -i.bak "s|REPLACE_OWNER|$USER|g" README.md
git add README.md && git commit -m "docs: fix README badges to point at $USER" 2>/dev/null || true
git push origin "$DEFAULT_BRANCH" 2>/dev/null || true

# Add common topics so the repo is discoverable
gh repo edit "$REPO_NAME" \
    --add-topic mt5 \
    --add-topic metatrader5 \
    --add-topic quant-trading \
    --add-topic ftmo \
    --add-topic prop-firm \
    --add-topic algorithmic-trading \
    --add-topic backtest \
    --add-topic streamlit \
    --add-topic python 2>/dev/null || true

echo
echo "✓ Published to https://github.com/$USER/$REPO_NAME"
echo "→ Watch the CI run at https://github.com/$USER/$REPO_NAME/actions"
