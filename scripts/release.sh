#!/bin/bash
# Tag-based release script for delimit-cli
# Usage: ./scripts/release.sh 4.2.0
#
# This script:
#   1. Validates the version argument
#   2. Syncs gateway files locally
#   3. Runs tests
#   4. Bumps package.json version
#   5. Commits the version bump
#   6. Creates and pushes the git tag
#
# The GitHub Actions workflow (.github/workflows/publish.yml) handles
# the actual npm publish when it sees the v* tag push.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# ── Argument validation ──────────────────────────────────────────────
VERSION="${1:-}"
if [ -z "$VERSION" ]; then
    echo "Usage: ./scripts/release.sh <version>"
    echo "  e.g. ./scripts/release.sh 4.2.0"
    exit 1
fi

# Strip leading v if provided (we add it to the tag ourselves)
VERSION="${VERSION#v}"

# Validate semver format
if ! echo "$VERSION" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+(-[a-zA-Z0-9.]+)?$'; then
    echo "Error: '$VERSION' is not a valid semver version"
    exit 1
fi

CURRENT=$(node -p "require('./package.json').version")
TAG="v$VERSION"

echo ""
echo "Delimit CLI Release"
echo "==================="
echo "  Current version: $CURRENT"
echo "  New version:     $VERSION"
echo "  Tag:             $TAG"
echo ""

# ── Pre-flight checks ────────────────────────────────────────────────

# Check for uncommitted changes
if [ -n "$(git status --porcelain)" ]; then
    echo "Error: working tree is dirty. Commit or stash changes first."
    exit 1
fi

# Check tag doesn't already exist
if git rev-parse "$TAG" >/dev/null 2>&1; then
    echo "Error: tag $TAG already exists"
    exit 1
fi

# Check we're on main branch
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$BRANCH" != "main" ] && [ "$BRANCH" != "master" ]; then
    echo "Warning: releasing from branch '$BRANCH' (not main)"
    read -p "Continue? [y/N] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# ── Step 1: Sync gateway ─────────────────────────────────────────────
echo "[1/5] Syncing gateway..."
npm run sync-gateway

# ── Step 2: Run tests ────────────────────────────────────────────────
echo ""
echo "[2/5] Running tests..."
npm test

# ── Step 3: Run security check ───────────────────────────────────────
echo ""
echo "[3/5] Running security check..."
bash scripts/security-check.sh

# ── Step 4: Bump version ─────────────────────────────────────────────
echo ""
echo "[4/5] Bumping version to $VERSION..."
npm version "$VERSION" --no-git-tag-version

# Keep server.json (MCP registry manifest) in lockstep with package.json.
# Without this, server.json.version drifts behind the published npm version
# (it lagged at 4.7.3 while package.json was 4.15.0 — LED-3717), which hurts
# MCP-directory freshness/completeness scoring. Updates both the top-level
# version and the npm package entry.
if [ -f server.json ]; then
    echo "  Syncing server.json version -> $VERSION"
    node -e '
        const fs = require("fs");
        const v = require("./package.json").version;
        const p = "server.json";
        const s = JSON.parse(fs.readFileSync(p, "utf8"));
        s.version = v;
        if (Array.isArray(s.packages)) {
            for (const pkg of s.packages) {
                if (pkg && typeof pkg === "object") pkg.version = v;
            }
        }
        fs.writeFileSync(p, JSON.stringify(s, null, 2) + "\n");
    '
fi

# ── Step 5: Commit, tag, and push ────────────────────────────────────
echo ""
echo "[5/5] Committing and tagging..."

# Stage synced gateway files too (sync-gateway may have updated them)
git add package.json package-lock.json server.json gateway/

# LED-1900: sync-gateway copies proprietary/internal SOURCE (e.g.
# license_core.py, which ships only as a compiled .so) into the tree. The
# bundle guards gate the npm PACK, not the git COMMIT — so `git add gateway/`
# would commit that source to the PUBLIC repo. Prune every bundle-internal
# path from the index + working tree, then HARD-ASSERT none remain staged.
if [ -f bundle-internal-exclude.txt ]; then
    while IFS= read -r _excl; do
        case "$_excl" in ''|\#*) continue ;; esac
        git reset -q -- "$_excl" 2>/dev/null || true
        rm -f "$_excl" 2>/dev/null || true
    done < bundle-internal-exclude.txt
    _leaked=$(git diff --cached --name-only | grep -Ff <(grep -vE '^\s*#|^\s*$' bundle-internal-exclude.txt) || true)
    if [ -n "$_leaked" ]; then
        echo "ERROR: internal/proprietary file(s) staged for the public release commit:" >&2
        echo "$_leaked" >&2
        echo "Refusing to commit. Fix the prune step before releasing." >&2
        exit 1
    fi
fi

# Use a release branch to avoid main branch protection
RELEASE_BRANCH="release/v$VERSION"
git checkout -b "$RELEASE_BRANCH"
git commit -m "release: v$VERSION"
git push -u origin "$RELEASE_BRANCH" --no-verify

# Create PR and merge
echo "Creating release PR..."
PR_URL=$(gh pr create --title "release: v$VERSION" --body "Automated release v$VERSION" 2>&1)
echo "  PR: $PR_URL"
gh pr merge --squash --admin "$RELEASE_BRANCH" 2>/dev/null || {
    echo "  Merge manually or with: gh pr merge --squash --admin $RELEASE_BRANCH"
}

# Switch back to main and pull the merge
git checkout main
git pull origin main

# Tag the merged commit
git tag -a "$TAG" -m "Release $VERSION"
git push origin "$TAG"

echo ""
echo "Done. GitHub Actions will handle npm publish."
echo "  Monitor: https://github.com/delimit-ai/delimit-mcp-server/actions"
echo "  Release: https://github.com/delimit-ai/delimit-mcp-server/releases/tag/$TAG"
