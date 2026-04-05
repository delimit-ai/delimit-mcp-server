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

# ── Step 5: Commit, tag, and push ────────────────────────────────────
echo ""
echo "[5/5] Committing and tagging..."

# Stage synced gateway files too (sync-gateway may have updated them)
git add package.json package-lock.json gateway/

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
