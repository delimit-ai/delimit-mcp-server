#!/bin/bash
# Publish Governance Gate — routes the existing owner entrypoint through the sole publisher
# Usage: bash scripts/publish-guard.sh
# LED-229: Ensures security scan and tests pass before npm publish.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

echo ""
echo "Publish Governance Gate"
echo "======================="
echo ""

FAIL=0

# 1. Git clean check
echo -n "  [1/4] Git clean... "
if [ -n "$(git status --porcelain)" ]; then
    echo "FAIL — working tree is dirty, commit first"
    FAIL=1
else
    echo "PASS"
fi

# 2. Security scan
echo -n "  [2/4] Security scan... "
if bash scripts/security-check.sh > /dev/null 2>&1; then
    echo "PASS"
else
    echo "FAIL — run: bash scripts/security-check.sh"
    FAIL=1
fi

# 3. Tests
echo -n "  [3/4] Tests... "
if npm test > /tmp/publish-guard-tests.log 2>&1; then
    echo "PASS"
else
    echo "FAIL — test suite failed (see /tmp/publish-guard-tests.log)"
    FAIL=1
fi

# 4. Dry-run pack check
echo -n "  [4/4] Pack dry-run... "
TMPDIR=$(mktemp -d)
if npm pack --pack-destination "$TMPDIR" --quiet > /dev/null 2>&1; then
    echo "PASS"
else
    echo "FAIL — npm pack failed"
    FAIL=1
fi
rm -rf "$TMPDIR"

echo ""

if [ $FAIL -ne 0 ]; then
    echo "PUBLISH BLOCKED — fix the issues above"
    exit 1
fi

echo "All checks passed — requesting the governed publisher..."
echo ""
# Keep existing standing owner release authority, while sharing the workflow's
# acceptance, credential boundary, package lock and rolling 24h limit with Fin.
# No caller-selected repository, workflow, ref or Fin order is accepted here.
test "$(git remote get-url origin)" = 'https://github.com/delimit-ai/delimit-mcp-server.git'
test "$(git symbolic-ref --short HEAD)" = 'main'
EXPECTED_SOURCE=$(git rev-parse HEAD)
LIVE_SOURCE=$(gh api repos/delimit-ai/delimit-mcp-server/git/ref/heads/main --jq .object.sha)
test "$EXPECTED_SOURCE" = "$LIVE_SOURCE"
gh workflow run publish.yml --repo delimit-ai/delimit-mcp-server --ref main -f dry_run=false -f expected_source="$EXPECTED_SOURCE"
echo 'Publication requested. Inspect the workflow and registry before claiming completion.'
