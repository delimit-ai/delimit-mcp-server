#!/bin/bash
# ============================================================================
# Guard A — CLASSIFICATION (fail-closed)
# ============================================================================
# Every source file under gateway/ai/** MUST be consciously classified as either
# PUBLIC (bundle-allowlist.txt) or INTERNAL (bundle-internal-exclude.txt). A file
# that is in NEITHER list is a NEW, UNCLASSIFIED file — this guard FAILS and
# names it, so a freshly-added internal module can never silently ship.
#
# This is the human gate that makes the allowlist fail-closed: the security
# boundary is only meaningful if someone must look at every new file and decide.
#
# Source of truth for enumeration: the GATEWAY SOURCE tree
# (GATEWAY_OVERRIDE or /home/delimit/delimit-gateway) when present — that is the
# authoritative set of files that COULD be synced. On a machine without the
# gateway source (e.g. a customer clone), we fall back to the committed bundle,
# which still validates that the shipped tree is fully classified.
#
# Wired into: prepublishOnly (before sync-gateway) + publish.yml + CI test flow.
# Run standalone:  bash scripts/check-bundle-classification.sh
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NPM_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$NPM_ROOT"

ALLOWLIST="$NPM_ROOT/bundle-allowlist.txt"
EXCLUDE="$NPM_ROOT/bundle-internal-exclude.txt"

for f in "$ALLOWLIST" "$EXCLUDE"; do
    if [ ! -f "$f" ]; then
        echo "❌ check-bundle-classification: missing $f — cannot enforce the fail-closed boundary."
        exit 1
    fi
done

# ── Build the set of classified paths (strip comments/blank lines) ───────────
# Paths in both files are bundle-relative (gateway/ai/...). We normalise to the
# ai-relative form (ai/...) for comparison with the enumerated source tree.
classified() {
    { grep -vE '^\s*(#|$)' "$1" || true; } | sed 's#^gateway/##'
}
CLASSIFIED_ALLOW="$(classified "$ALLOWLIST")"
CLASSIFIED_EXCL="$(classified "$EXCLUDE")"
# Combined lookup (one path per line).
CLASSIFIED_ALL="$(printf '%s\n%s\n' "$CLASSIFIED_ALLOW" "$CLASSIFIED_EXCL" | sort -u)"

# ── Locate the enumeration root ──────────────────────────────────────────────
GATEWAY_SRC="${GATEWAY_OVERRIDE:-/home/delimit/delimit-gateway}"
if [ -d "$GATEWAY_SRC/ai" ]; then
    ROOT="$GATEWAY_SRC"
    MODE="gateway-source ($GATEWAY_SRC)"
elif [ -d "$NPM_ROOT/gateway/ai" ]; then
    ROOT="$NPM_ROOT/gateway"
    MODE="committed-bundle"
else
    echo "⚠️  check-bundle-classification: no gateway/ai to enumerate (source or bundle) — skipping."
    exit 0
fi

# ── Enumerate every source file under ai/ ────────────────────────────────────
# Skip caches, compiled artifacts, and license_core.py (the transient compile
# input that build-license-core.sh strips — it is classified INTERNAL anyway).
UNCLASSIFIED=()
while IFS= read -r abs; do
    rel="ai/${abs#"$ROOT/ai/"}"
    case "$rel" in
        *__pycache__*|*.pyc|*.so) continue ;;
    esac
    if ! printf '%s\n' "$CLASSIFIED_ALL" | grep -qxF "$rel"; then
        UNCLASSIFIED+=("$rel")
    fi
done < <(find "$ROOT/ai" -type f 2>/dev/null | sort)

if [ ${#UNCLASSIFIED[@]} -ne 0 ]; then
    echo "❌ BUNDLE CLASSIFICATION FAILURE ($MODE)"
    echo "   The following gateway/ai file(s) are UNCLASSIFIED — in neither"
    echo "   bundle-allowlist.txt nor bundle-internal-exclude.txt:"
    for u in "${UNCLASSIFIED[@]}"; do echo "      - gateway/$u"; done
    echo ""
    echo "   Fix: classify EACH file in bundle-allowlist.txt (ships to public npm)"
    echo "        or bundle-internal-exclude.txt (stays internal). When uncertain,"
    echo "        classify INTERNAL-EXCLUDE. See bundle-classification.md."
    exit 1
fi

echo "✅ bundle classification ($MODE): every gateway/ai file is classified (allow or exclude)."
exit 0
