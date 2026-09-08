#!/bin/bash
# Pre-publish security check — blocks npm publish if secrets are found
# Run: bash scripts/security-check.sh

set -euo pipefail

echo "🔍 Delimit pre-publish security scan..."

FAIL=0

# Reconstruct the EXACT set of files npm would publish, then scan them.
#
# We deliberately avoid `npm pack` writing a tarball here: when this script
# runs inside `npm publish` (via prepublishOnly), a nested `npm pack` is
# unreliable on npm 10.x — it reports a tarball name but persists no file
# (not to --pack-destination, not to CWD), so the old `ls "$TMPDIR"/*.tgz`
# failed with exit 2 and blocked every publish. `npm pack --dry-run --json`
# only ENUMERATES the shipped files (honoring package.json "files" and the
# "!"-exclusions) and never writes a tarball, so it is re-entrancy-safe.
# We copy those exact files into TMPDIR/package/ and keep the proven scan
# blocks below byte-for-byte.
SCAN_TMP_DIR=$(mktemp -d)
cleanup_scan_tmp() {
    if [ -n "${SCAN_TMP_DIR:-}" ] && [ -d "$SCAN_TMP_DIR" ]; then
        rm -r "$SCAN_TMP_DIR"
    fi
}
trap cleanup_scan_tmp EXIT
mkdir -p "$SCAN_TMP_DIR/package"
if ! npm pack --dry-run --json 2>/dev/null \
    | node -e 'const d=JSON.parse(require("fs").readFileSync(0,"utf8"));for(const f of d[0].files||[])process.stdout.write(f.path+"\0")' \
    | while IFS= read -r -d '' f; do
        [ -n "$f" ] || continue
        mkdir -p "$SCAN_TMP_DIR/package/$(dirname "$f")"
        if ! cp "$f" "$SCAN_TMP_DIR/package/$f" 2>/dev/null; then
            echo "security-check: failed to copy npm-packed file: $f" >&2
            exit 1
        fi
      done; then
    echo "security-check: npm pack enumeration/copy pipeline failed" >&2
    exit 1
fi

if [ -z "$(find "$SCAN_TMP_DIR/package" -type f -print -quit)" ]; then
    echo "❌ security-check: could not enumerate shipped files (npm pack --dry-run --json returned nothing)"
    exit 1
fi

# 1. Credential patterns
echo -n "  Credentials... "
# Force text mode: source artifacts can contain literal NUL bytes, and grep's
# default binary-file shortcut would otherwise conceal the exact line and
# collapse it into an unauditable "binary file matches" result.
if grep -raEi '(password|passwd|secret|api_key|apikey)\s*[:=]\s*["\x27][^"\x27]{4,}' "$SCAN_TMP_DIR/package/" --include="*.py" --include="*.js" --include="*.json" 2>/dev/null | grep -av 'environ\|getenv\|process\.env\|os\.environ\|<configured\|example\|placeholder\|REDACTED\|\${credentials\|credentials\.\|security-scan-ignore'; then
    echo "❌ FOUND CREDENTIALS"
    FAIL=1
else
    echo "✅ clean"
fi

# 2. Blocklist terms
echo -n "  Blocklist... "
BLOCKLIST="jamsonsholdings|Bladabah|Domainvested26|Delimit26|home/jamsons|infracore|crypttrx|\.wr_env"  # delimit-security-allow: pattern definitions of the prepublish guard itself
if grep -raEi "$BLOCKLIST" "$SCAN_TMP_DIR/package/" --include="*.py" --include="*.js" --include="*.json" 2>/dev/null; then
    echo "❌ BLOCKED TERMS FOUND"
    FAIL=1
else
    echo "✅ clean"
fi

# 3. PII (email addresses that aren't examples)
echo -n "  PII... "
if grep -raEi '[a-z0-9._%+-]+@(gmail|yahoo|hotmail|outlook|proton|jamsons|wire\.report|domainvested)' "$SCAN_TMP_DIR/package/" --include="*.py" --include="*.js" --include="*.json" 2>/dev/null | grep -av "example\|placeholder\|<configured\|noreply\|e\.g\.\|docstring\|Args:\|Credential resolution"; then
    echo "❌ PII FOUND"
    FAIL=1
else
    echo "✅ clean"
fi

# 4. Proprietary files that shouldn't ship
echo -n "  Proprietary files... "
PROPRIETARY="social_target\.py|social\.py|founding_users\.py|inbox_daemon\.py|deliberation\.py"
if find "$SCAN_TMP_DIR/package/" -name "*.py" | grep -Ei "$PROPRIETARY" 2>/dev/null; then
    echo "❌ PROPRIETARY FILES IN PACKAGE"
    FAIL=1
else
    echo "✅ clean"
fi

# Cleanup
cleanup_scan_tmp
trap - EXIT

# 5. Bundle parity — no package.json-blocked path present in the committed
#    gateway/ bundle. Scans #1–#4 above only inspect the npm TARBALL file list
#    (which already honors package.json "!"-exclusions), so they can never see
#    a blocked file that is git-committed into this PUBLIC repo. This guard
#    closes that gap and catches sync-gateway/package.json drift.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "  Bundle parity..."
if ! bash "$SCRIPT_DIR/check-bundle-parity.sh"; then
    FAIL=1
fi

if [ $FAIL -ne 0 ]; then
    echo ""
    echo "❌ SECURITY CHECK FAILED — do not publish"
    exit 1
fi

echo ""
echo "✅ All security checks passed"
exit 0
