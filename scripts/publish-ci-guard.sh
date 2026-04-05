#!/bin/bash
# Publish CI Guard — warns when npm publish is run outside of CI
#
# In CI (GitHub Actions sets CI=true), this is a no-op.
# Locally, it prints a warning recommending the tag-based flow,
# but still allows the publish for emergency hotfixes.

set -euo pipefail

if [ "${CI:-}" = "true" ]; then
    # Running in CI — all good, proceed silently
    exit 0
fi

echo ""
echo "========================================================"
echo "  WARNING: You are running npm publish directly."
echo ""
echo "  The recommended flow is tag-based publishing:"
echo "    ./scripts/release.sh <version>"
echo ""
echo "  This bumps the version, creates a git tag, and pushes."
echo "  GitHub Actions then handles the npm publish with"
echo "  provenance, security checks, and a GitHub Release."
echo ""
echo "  Continuing in 5 seconds (Ctrl+C to abort)..."
echo "========================================================"
echo ""

sleep 5
