#!/bin/bash
# LED-1259: Verify that the compiled license_core .so:
#   1. Compiles cleanly from gateway/ai/license_core.py
#   2. Imports successfully when the .py is absent
#   3. Exposes all public functions/constants the shim relies on
#   4. Returns correct validation verdicts for known-valid / known-expired
#      license dicts
#   5. Contains zero bypass identifiers in `strings` output
#
# Runs in an isolated tmp dir so it doesn't pollute the bundle layout.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NPM_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC="$NPM_ROOT/gateway/ai/license_core.py"

if [ "$(uname -s)" != "Linux" ]; then
    echo "⏭️  test-license-core-so: non-Linux host — skipping (Linux-only first ship)"
    exit 0
fi

if [ ! -f "$SRC" ]; then
    echo "ℹ️  license_core.py not in bundle (already compiled or pre-build) — copying from gateway src for test"
    GW_SRC="${GATEWAY_OVERRIDE:-/home/delimit/delimit-gateway}/ai/license_core.py"
    if [ ! -f "$GW_SRC" ]; then
        echo "❌ No license_core.py source found (bundle or gateway). Cannot test."
        exit 1
    fi
    SRC="$GW_SRC"
fi

PY="${PYTHON:-python3}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "🧪 test-license-core-so: building in $TMP"
mkdir -p "$TMP/ai"
touch "$TMP/ai/__init__.py"
cp "$SRC" "$TMP/license_core.py"

cd "$TMP"
"$PY" -m nuitka --module --quiet --remove-output --output-dir=. license_core.py 2>&1 | tail -3

SO_FILE="$(ls -1 license_core.cpython-*-*.so 2>/dev/null | head -1)"
if [ -z "$SO_FILE" ]; then
    echo "❌ Compile failed — no .so produced"
    exit 1
fi
echo "  ✅ compiled: $SO_FILE ($(stat -c%s "$SO_FILE") bytes)"

# Strings-grep for bypass identifiers
HITS="$(strings "$SO_FILE" | grep -iE 'DELIMIT_TEST_MODE|DELIMIT_INTERNAL_LICENSE_KEY|JAMSONS' || true)"
if [ -n "$HITS" ]; then
    echo "❌ Bypass identifiers leaked into .so:"
    echo "$HITS"
    exit 1
fi
echo "  ✅ strings-grep clean"

# Move .so under ai/, drop the .py, run import + behaviour checks
mv "$SO_FILE" "ai/$SO_FILE"
rm -f license_core.py

"$PY" - <<'PY'
import os, sys, time
sys.path.insert(0, ".")

# Import via the compiled .so only — no .py present
from ai.license_core import (
    is_license_valid, revalidate_license, needs_revalidation,
    PRO_TOOLS, LICENSE_FILE, FREE_TRIAL_LIMITS, activate,
    load_license, check_premium, gate_tool,
)

assert isinstance(PRO_TOOLS, frozenset) and len(PRO_TOOLS) > 0, "PRO_TOOLS must be a non-empty frozenset"
assert FREE_TRIAL_LIMITS.get("delimit_deliberate") == 3, "FREE_TRIAL_LIMITS missing delimit_deliberate=3"
assert "delimit_deliberate" in PRO_TOOLS, "PRO_TOOLS must include delimit_deliberate"
assert callable(activate), "activate must be callable"
assert callable(revalidate_license), "revalidate_license must be callable"

# Known-valid: pro tier, recent last_validated_at
valid_recent = {"tier": "pro", "valid": True, "last_validated_at": time.time()}
assert is_license_valid(valid_recent) is True, "recent pro license should be valid"
assert needs_revalidation(valid_recent) is False, "recent pro license should not need revalidation"

# Known-invalid: pro tier but last_validated_at > 44 days ago (beyond hard cutoff)
expired = {"tier": "pro", "valid": True, "last_validated_at": time.time() - 60 * 86400}
assert is_license_valid(expired) is False, "expired (60d) pro license must be invalid"
assert needs_revalidation(expired) is True, "expired (60d) pro license must need revalidation"

# Free tier never valid for Pro
free = {"tier": "free", "valid": True}
assert is_license_valid(free) is False, "free tier must not pass is_license_valid"

# valid=False forces invalid even when timestamp is recent
revoked = {"tier": "pro", "valid": False, "last_validated_at": time.time()}
assert is_license_valid(revoked) is False, "revoked license must be invalid"

# Legacy file with no timestamps — should signal needs_revalidation=True
legacy = {"tier": "pro", "valid": True}
assert needs_revalidation(legacy) is True, "legacy license without timestamps must need revalidation"

print("ALL_OK")
PY

echo "✅ test-license-core-so: all checks passed"
