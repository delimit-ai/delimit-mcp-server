#!/bin/bash
# ============================================================================
# Guard B — TARBALL MANIFEST PARITY (fail-closed)
# ============================================================================
# Asserts that the set of gateway/ files `npm pack` would actually ship is
# EXACTLY equal to bundle-allowlist.txt. Any EXTRA shipped gateway path (a leak)
# fails; any allowlisted path MISSING from the pack (a mis-exclusion) fails.
#
# This replaces the old "no package.json-blocked path present" check (which was
# fail-OPEN: it only caught enumerated blocklist paths). The allowlist model is
# fail-CLOSED: the tarball must match the allowlist and nothing else.
#
# One glob is permitted in the allowlist — the compiled license_core .so
# (gateway/ai/license_core.cpython-*-*.so), whose exact name varies by ABI.
#
# Wired into: security-check.sh + prepublishOnly (after build) + publish.yml + CI.
# Run standalone (after sync + build):  bash scripts/check-bundle-parity.sh
#
# Emergency override (hotfix while a leak is remediated separately):
#   DELIMIT_ALLOW_COMMITTED_LEAK=1
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NPM_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$NPM_ROOT"

ALLOWLIST="$NPM_ROOT/bundle-allowlist.txt"
if [ ! -f "$ALLOWLIST" ]; then
    echo "❌ check-bundle-parity: missing bundle-allowlist.txt — cannot enforce parity."
    exit 1
fi

# ── Allowlisted gateway paths (strip comments/blank lines) ───────────────────
ALLOW_GATEWAY="$(grep -vE '^\s*(#|$)' "$ALLOWLIST" | grep '^gateway/' || true)"
# Split into the single .so glob and the exact paths.
ALLOW_GLOB="$(printf '%s\n' "$ALLOW_GATEWAY" | grep '\*' || true)"
ALLOW_EXACT="$(printf '%s\n' "$ALLOW_GATEWAY" | grep -v '\*' | sort -u)"

# ── What would npm actually ship? (enumerate, never writes a tarball) ────────
PACK_JSON="$(npm pack --dry-run --json 2>/dev/null || true)"
if [ -z "$PACK_JSON" ]; then
    echo "❌ check-bundle-parity: 'npm pack --dry-run --json' returned nothing."
    exit 1
fi
SHIPPED_GATEWAY="$(printf '%s' "$PACK_JSON" \
  | node -e 'const d=JSON.parse(require("fs").readFileSync(0,"utf8"));process.stdout.write((d[0].files||[]).map(f=>f.path).join("\n"))' \
  | grep '^gateway/' | sort -u || true)"

# ── A .so glob match in the shipped set is folded to its allowlist glob form ─
# so exact-set comparison ignores the ABI-specific filename.
matches_glob() {
    local path="$1"
    [ -n "$ALLOW_GLOB" ] || return 1
    while IFS= read -r g; do
        [ -n "$g" ] || continue
        case "$path" in $g) return 0 ;; esac
    done <<< "$ALLOW_GLOB"
    return 1
}

# ── EXTRA: shipped gateway path not permitted by the allowlist (a LEAK) ──────
EXTRA=()
SHIPPED_SO=0
while IFS= read -r p; do
    [ -n "$p" ] || continue
    if printf '%s\n' "$ALLOW_EXACT" | grep -qxF "$p"; then continue; fi
    if matches_glob "$p"; then SHIPPED_SO=1; continue; fi
    EXTRA+=("$p")
done <<< "$SHIPPED_GATEWAY"

# ── MISSING: allowlisted exact path not present in the pack (mis-exclusion) ──
MISSING=()
while IFS= read -r p; do
    [ -n "$p" ] || continue
    if ! printf '%s\n' "$SHIPPED_GATEWAY" | grep -qxF "$p"; then
        MISSING+=("$p")
    fi
done <<< "$ALLOW_EXACT"

FAIL=0
if [ ${#EXTRA[@]} -ne 0 ]; then
    FAIL=1
    echo "❌ BUNDLE PARITY VIOLATION — npm pack ships path(s) NOT in bundle-allowlist.txt:"
    for e in "${EXTRA[@]}"; do echo "      + $e   (LEAK — internal or unclassified file)"; done
    echo "   Fix: exclude it (bundle-internal-exclude.txt + keep it out of package.json"
    echo "        \"files\") or, if it is genuinely public, add it to bundle-allowlist.txt."
fi
if [ ${#MISSING[@]} -ne 0 ]; then
    FAIL=1
    echo "❌ BUNDLE PARITY VIOLATION — allowlisted path(s) MISSING from npm pack:"
    for m in "${MISSING[@]}"; do echo "      - $m   (mis-excluded — public tool may break)"; done
    echo "   Fix: ensure sync-gateway copies it and package.json \"files\" includes it."
fi

if [ -n "$ALLOW_GLOB" ] && [ "$SHIPPED_SO" -ne 0 ]; then
    echo "   (compiled license_core .so present and matched the allowlist glob)"
fi

if [ $FAIL -ne 0 ]; then
    if [ "${DELIMIT_ALLOW_COMMITTED_LEAK:-}" = "1" ]; then
        echo ""
        echo "   ⚠️  DELIMIT_ALLOW_COMMITTED_LEAK=1 set — overriding (emergency hotfix only)."
        exit 0
    fi
    exit 1
fi

COUNT="$(printf '%s\n' "$SHIPPED_GATEWAY" | grep -c '^gateway/' || true)"
echo "✅ bundle parity: npm pack ships exactly the allowlist ($COUNT gateway path(s), matches bundle-allowlist.txt)."
exit 0
