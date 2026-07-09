#!/bin/bash
# Proprietary-gating PARITY GUARD (prevention-first).
#
# FAILS if any package.json-blocked `gateway/...` path is present in the
# committed public bundle. package.json's "files" "!"-negations keep these
# proprietary modules out of the npm TARBALL, but the tarball exclusion does
# nothing for the GIT repo — and delimit-ai/delimit-mcp-server is PUBLIC. A
# blocked path that is git-tracked is therefore an IP leak, even though npm
# never ships it.
#
# Single source of truth: package.json, via list-blocked-gateway-paths.js.
# This guard and scripts/sync-gateway.sh both derive their path list from that
# one extractor, so they cannot drift.
#
# Wired into scripts/security-check.sh (which runs in prepublishOnly + the
# publish.yml "Security check" step). Can also be run standalone in CI or by
# hand:  bash scripts/check-bundle-parity.sh
#
# Emergency override (use only for a genuine hotfix while a pre-existing leak
# is being remediated separately):  DELIMIT_ALLOW_COMMITTED_LEAK=1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NPM_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$NPM_ROOT"

# ── Derive the blocked-path list from package.json (single source of truth) ──
BLOCKED=()
while IFS= read -r line; do
    [ -n "$line" ] && BLOCKED+=("$line")
done < <(node "$SCRIPT_DIR/list-blocked-gateway-paths.js")

if [ ${#BLOCKED[@]} -eq 0 ]; then
    echo "⚠️  check-bundle-parity: no blocked gateway paths derived from package.json — nothing to check."
    exit 0
fi

# ── Exemptions ──────────────────────────────────────────────────────────────
# license_core.py is a transient compile input: sync-gateway keeps it, then
# build-license-core.sh compiles it to a native .so and strips the plaintext.
# It is legitimately never committed to git, so it is exempt from this check.
EXEMPT=("gateway/ai/license_core.py")
is_exempt() {
    local p="$1"
    for e in "${EXEMPT[@]}"; do [ "$p" = "$e" ] && return 0; done
    return 1
}

# ── Enumerate what is actually in the bundle ────────────────────────────────
# Prefer git (authoritative COMMITTED state — this is what the public repo
# exposes). Fall back to filesystem if not inside a git work tree.
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    PRESENT="$(git ls-files gateway/ 2>/dev/null || true)"
    MODE="git-tracked"
else
    PRESENT="$(find gateway -type f 2>/dev/null || true)"
    MODE="filesystem"
fi

# ── Compare ─────────────────────────────────────────────────────────────────
VIOLATIONS=()
for b in "${BLOCKED[@]}"; do
    is_exempt "$b" && continue
    if [[ "$b" == */ ]]; then
        # Directory negation — match any tracked file under that prefix.
        if printf '%s\n' "$PRESENT" | grep -q "^${b}"; then
            VIOLATIONS+=("$b")
        fi
    else
        # Exact file match.
        if printf '%s\n' "$PRESENT" | grep -qxF "$b"; then
            VIOLATIONS+=("$b")
        fi
    fi
done

if [ ${#VIOLATIONS[@]} -ne 0 ]; then
    echo "❌ BUNDLE PARITY VIOLATION ($MODE)"
    echo "   package.json blocks these proprietary paths from the npm tarball,"
    echo "   but they are present in the PUBLIC git bundle (= IP leak):"
    for v in "${VIOLATIONS[@]}"; do echo "      - $v"; done
    echo ""
    echo "   Remediation (founder decision — do NOT silently delete history):"
    echo "     1. Remove from HEAD:  git rm -r <path> && commit"
    echo "     2. Decide on history scrub (git filter-repo / BFG) for already-pushed commits."
    if [ "${DELIMIT_ALLOW_COMMITTED_LEAK:-}" = "1" ]; then
        echo ""
        echo "   ⚠️  DELIMIT_ALLOW_COMMITTED_LEAK=1 set — overriding (emergency hotfix only)."
        exit 0
    fi
    exit 1
fi

echo "✅ bundle parity ($MODE): no package.json-blocked path present in the bundle (${#BLOCKED[@]} paths checked)"
exit 0
