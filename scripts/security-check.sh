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
TMPDIR=$(mktemp -d)
mkdir -p "$TMPDIR/package"
npm pack --dry-run --json 2>/dev/null \
  | node -e 'const d=JSON.parse(require("fs").readFileSync(0,"utf8"));process.stdout.write((d[0].files||[]).map(f=>f.path).join("\n"))' \
  | while IFS= read -r f; do
      [ -n "$f" ] || continue
      mkdir -p "$TMPDIR/package/$(dirname "$f")"
      cp "$f" "$TMPDIR/package/$f" 2>/dev/null || true
    done

if [ -z "$(find "$TMPDIR/package" -type f -print -quit)" ]; then
    echo "❌ security-check: could not enumerate shipped files (npm pack --dry-run --json returned nothing)"
    rm -rf "$TMPDIR"
    exit 1
fi

# 1. Credential patterns
echo -n "  Credentials... "
if grep -rEi '(password|passwd|secret|api_key|apikey)\s*[:=]\s*["\x27][^"\x27]{4,}' "$TMPDIR/package/" --include="*.py" --include="*.js" --include="*.json" 2>/dev/null | grep -v 'environ\|getenv\|process\.env\|os\.environ\|<configured\|example\|placeholder\|REDACTED\|\${credentials\|credentials\.\|security-scan-ignore'; then
    echo "❌ FOUND CREDENTIALS"
    FAIL=1
else
    echo "✅ clean"
fi

# 2. Blocklist terms
echo -n "  Blocklist... "
BLOCKLIST="jamsonsholdings|Bladabah|Domainvested26|Delimit26|home/jamsons|infracore|crypttrx|\.wr_env"
if grep -rEi "$BLOCKLIST" "$TMPDIR/package/" --include="*.py" --include="*.js" --include="*.json" 2>/dev/null; then
    echo "❌ BLOCKED TERMS FOUND"
    FAIL=1
else
    echo "✅ clean"
fi

# 3. PII (email addresses that aren't examples)
echo -n "  PII... "
if grep -rEi '[a-z0-9._%+-]+@(gmail|yahoo|hotmail|outlook|proton|jamsons|wire\.report|domainvested)' "$TMPDIR/package/" --include="*.py" --include="*.js" --include="*.json" 2>/dev/null | grep -v "example\|placeholder\|<configured\|noreply\|e\.g\.\|docstring\|Args:\|Credential resolution"; then
    echo "❌ PII FOUND"
    FAIL=1
else
    echo "✅ clean"
fi

# 4. Proprietary files that shouldn't ship
echo -n "  Proprietary files... "
PROPRIETARY="social_target\.py|social\.py|founding_users\.py|inbox_daemon\.py|deliberation\.py"
if find "$TMPDIR/package/" -name "*.py" | grep -Ei "$PROPRIETARY" 2>/dev/null; then
    echo "❌ PROPRIETARY FILES IN PACKAGE"
    FAIL=1
else
    echo "✅ clean"
fi

# Cleanup
rm -rf "$TMPDIR"

if [ $FAIL -ne 0 ]; then
    echo ""
    echo "❌ SECURITY CHECK FAILED — do not publish"
    exit 1
fi

echo ""
echo "✅ All security checks passed"
exit 0
