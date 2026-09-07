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
STUB="$AI_DIR/license_core.pyi"
STUB_BACKUP=""
BUILD_STARTED=0
BUILD_SUCCEEDED=0

cleanup_generated_intermediates() {
    rm -rf -- \
        "$AI_DIR/license_core.build" \
        "$AI_DIR/license_core.dist" \
        "$AI_DIR/license_core.onefile-build"
    rm -f -- \
        "$AI_DIR/license_core.c" \
        "$AI_DIR/license_core.const" \
        "$AI_DIR/license_core.o"
}

restore_reviewed_stub() {
    if [ -n "$STUB_BACKUP" ] && [ -f "$STUB_BACKUP" ]; then
        cp -p -- "$STUB_BACKUP" "$STUB"
    fi
}

cleanup() {
    local status=$?

    if [ "$BUILD_STARTED" -eq 1 ]; then
        cleanup_generated_intermediates || status=1
    fi
    restore_reviewed_stub || status=1

    # A failed compile must not leave a stale or partially generated binary
    # that a later pack could mistake for reviewed output.
    if [ "$BUILD_STARTED" -eq 1 ] && [ "$BUILD_SUCCEEDED" -ne 1 ]; then
        find "$AI_DIR" -maxdepth 1 -type f \
            -name 'license_core.cpython-*-*.so' -delete
    fi

    if [ -n "$STUB_BACKUP" ]; then
        rm -f -- "$STUB_BACKUP"
    fi

    trap - EXIT
    exit "$status"
}

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

if [ ! -f "$STUB" ]; then
    echo "❌ Reviewed type stub not found: $STUB"
    exit 1
fi

# Nuitka rewrites license_core.pyi in place. Preserve the reviewed bundle
# artifact before invoking the compiler and restore it on every exit path.
STUB_BACKUP="$(mktemp "${TMPDIR:-/tmp}/delimit-license-core-pyi.XXXXXX")"
cp -p -- "$STUB" "$STUB_BACKUP"
trap cleanup EXIT

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
BUILD_STARTED=1
find "$AI_DIR" -maxdepth 1 -type f \
    -name 'license_core.cpython-*-*.so' -delete
cleanup_generated_intermediates
cd "$AI_DIR"
"$PY" -m nuitka --module --quiet --remove-output --output-dir=. license_core.py

# The generated stub is not authoritative. Restore the committed/reviewed
# bytes before any later packaging guard can inspect or ship the tree.
restore_reviewed_stub
if ! cmp -s -- "$STUB_BACKUP" "$STUB"; then
    echo "❌ Failed to restore reviewed license_core.pyi byte-for-byte"
    exit 1
fi
echo "   ✅ restored reviewed license_core.pyi byte-for-byte"

# ── Verify output ────────────────────────────────────────────────────
SO_FILE="$(ls -1 license_core.cpython-*-*.so 2>/dev/null | head -1 || true)"
if [ -z "$SO_FILE" ] || [ ! -f "$SO_FILE" ]; then
    echo "❌ Compile failed — no .so produced in $AI_DIR"
    ls -la "$AI_DIR"/license_core* 2>&1 || true
    exit 1
fi

SO_SIZE="$(stat -c%s "$SO_FILE")"
echo "   ✅ produced: $SO_FILE ($SO_SIZE bytes)"

# ── Linux ABI compatibility guard ───────────────────────────────────
# The public Linux artifact supports Ubuntu 22.04 (glibc 2.35). Building on
# a floating runner can silently add newer symbol requirements even when the
# extension imports on the build host. Inspect the artifact itself and fail
# closed before npm can pack an incompatible binary.
GLIBC_CEILING="2.35"
if ! command -v readelf >/dev/null 2>&1; then
    echo "❌ readelf not found; cannot verify Linux ABI compatibility"
    exit 1
fi

GLIBC_REQUIREMENTS="$(
    readelf --version-info "$SO_FILE" 2>/dev/null \
        | grep -oE 'GLIBC_[0-9]+(\.[0-9]+)+' \
        | sed 's/^GLIBC_//' \
        | sort -Vu \
        || true
)"
if [ -z "$GLIBC_REQUIREMENTS" ]; then
    echo "❌ Could not determine GLIBC requirements for $SO_FILE"
    exit 1
fi

MAX_GLIBC="$(printf '%s\n' "$GLIBC_REQUIREMENTS" | tail -n 1)"
if [ "$(printf '%s\n%s\n' "$GLIBC_CEILING" "$MAX_GLIBC" | sort -V | tail -n 1)" != "$GLIBC_CEILING" ]; then
    echo "❌ $SO_FILE requires GLIBC_$MAX_GLIBC; maximum supported is GLIBC_$GLIBC_CEILING (Ubuntu 22.04)"
    exit 1
fi
echo "   ✅ GLIBC requirement $MAX_GLIBC <= $GLIBC_CEILING (Ubuntu 22.04 compatible)"

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

BUILD_SUCCEEDED=1
echo "✅ build-license-core complete: $SO_FILE"
