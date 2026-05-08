#!/bin/bash
# LED-1259: Compile gateway/ai/license_core.py to a native .so via Nuitka,
# then strip the plaintext .py from the bundle so customers cannot grep
# the validation logic for bypass identifiers.
#
# Linux-only first ship. Mac/Windows expansion is filed as a follow-up
# ledger item — non-linux customers will hit the Python fallback in
# license.py (degraded Pro features) until we ship per-platform binaries.
#
# Idempotent: safe to re-run; will rebuild on every invocation.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NPM_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
AI_DIR="$NPM_ROOT/gateway/ai"
SRC="$AI_DIR/license_core.py"

# ── Platform gate ────────────────────────────────────────────────────
UNAME_S="$(uname -s)"
UNAME_M="$(uname -m)"
if [ "$UNAME_S" != "Linux" ]; then
    echo "⚠️  build-license-core: non-Linux host ($UNAME_S) — skipping compile."
    echo "   First ship is linux-only. The bundle will fall back to .py."
    exit 0
fi

if [ ! -f "$SRC" ]; then
    echo "❌ Source not found: $SRC"
    exit 1
fi

# ── Toolchain check ──────────────────────────────────────────────────
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
    echo "❌ python3 not found"
    exit 1
fi

PY_VER="$($PY -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
echo "🔧 build-license-core: python=$PY ($PY_VER), arch=$UNAME_M"

if ! "$PY" -m nuitka --version >/dev/null 2>&1; then
    echo "📦 nuitka not installed — installing via pip..."
    "$PY" -m pip install --quiet --user nuitka
fi

NUITKA_VER="$($PY -m nuitka --version 2>&1 | head -1)"
echo "   nuitka=$NUITKA_VER"

# ── Compile ──────────────────────────────────────────────────────────
echo "🔨 Compiling license_core.py → .so (this takes ~30s)..."
cd "$AI_DIR"
"$PY" -m nuitka --module --quiet --remove-output --output-dir=. license_core.py

# ── Verify output ────────────────────────────────────────────────────
SO_FILE="$(ls -1 license_core.cpython-*-*.so 2>/dev/null | head -1 || true)"
if [ -z "$SO_FILE" ] || [ ! -f "$SO_FILE" ]; then
    echo "❌ Compile failed — no .so produced in $AI_DIR"
    ls -la "$AI_DIR"/license_core* 2>&1 || true
    exit 1
fi

SO_SIZE="$(stat -c%s "$SO_FILE")"
echo "   ✅ produced: $SO_FILE ($SO_SIZE bytes)"

# ── Bypass-identifier scan ───────────────────────────────────────────
# Customers must not be able to `strings | grep` the .so for known
# bypass class names. Fail the build if any leak through.
BYPASS_HITS="$(strings "$SO_FILE" | grep -iE 'DELIMIT_TEST_MODE|DELIMIT_INTERNAL_LICENSE_KEY|JAMSONS' || true)"
if [ -n "$BYPASS_HITS" ]; then
    echo "❌ Bypass identifiers found in compiled .so:"
    echo "$BYPASS_HITS"
    exit 1
fi
echo "   ✅ strings-grep clean (no bypass identifiers)"

# ── Drop the plaintext source from the bundle ────────────────────────
# .npmignore + package.json will also exclude it, but removing here is
# belt-and-suspenders so dev/test inspection of the bundle dir matches
# what gets packed.
rm -f "$AI_DIR/license_core.py"
echo "   ✅ removed plaintext license_core.py from bundle"

echo "✅ build-license-core complete: $SO_FILE"
