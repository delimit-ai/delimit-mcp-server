#!/bin/bash
# Sync gateway Python files into npm bundle before publish.
# Source of truth: /home/delimit/delimit-gateway/  (override: GATEWAY_OVERRIDE)
# Destination: ./gateway/ (relative to npm-delimit root)
#
# FAIL-CLOSED ALLOWLIST MODEL (replaces the old fail-OPEN blocklist):
#   The bundle used to rsync gateway/ wholesale and subtract a hand-maintained
#   list of proprietary paths — so ANY new internal module leaked by default.
#   Now sync copies the full tree, then PRUNES everything that is NOT in
#   bundle-allowlist.txt. A new internal gateway module is therefore never
#   carried into this PUBLIC repo or the npm tarball unless it is consciously
#   added to the allowlist. bundle-allowlist.txt IS the security boundary.
#
# This runs as part of prepublishOnly so the npm package always contains the
# latest allowlisted gateway code. Drift is impossible; leakage is fail-closed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NPM_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
GATEWAY_SRC="${GATEWAY_OVERRIDE:-/home/delimit/delimit-gateway}"
ALLOWLIST="$NPM_ROOT/bundle-allowlist.txt"

# ── Verify gateway source exists ─────────────────────────────────────
if [ ! -d "$GATEWAY_SRC/ai" ]; then
    echo "⚠️  Gateway source not found at $GATEWAY_SRC"
    echo "   Skipping sync (CI or customer machine — bundle as-is)"
    exit 0
fi
if [ ! -f "$ALLOWLIST" ]; then
    echo "❌ sync-gateway: bundle-allowlist.txt not found — refusing to sync (fail-closed)."
    exit 1
fi

echo "🔄 Syncing gateway → npm bundle (allowlist-driven, fail-closed)..."

# ── Build the allowlist set (bundle-relative gateway paths) ──────────
# Strip comments/blanks; drop the .so glob (a build artifact, not a source
# file to copy). license_core.py is NOT in the allowlist (it is proprietary and
# ships as a .so); we keep it transiently below only so build-license-core.sh
# can compile + strip it.
# Sources kept TRANSIENTLY after the prune so the proprietary build step can
# compile them to .so and strip the plaintext. They are never shipped as .py.
# deliberation joined this set on 2026-09-19: bundle-classification.md always
# classified it "PROPRIETARY — ships as compiled .so, not source", but nothing
# built it, so only the .pyi stub shipped and every tool that imported it
# (delimit_scan, delimit_quickstart, delimit_deliberation_status) crashed on a
# clean install.
LICENSE_CORE_SRC="gateway/ai/license_core.py"
PROPRIETARY_SRCS="gateway/ai/license_core.py gateway/ai/deliberation.py"
ALLOW_PATHS="$(grep -vE '^\s*(#|$)' "$ALLOWLIST" | grep '^gateway/' | grep -v '\*' | sort -u)"
ALLOW_COUNT="$(printf '%s\n' "$ALLOW_PATHS" | grep -c '^gateway/' || true)"
echo "  Allowlist: $ALLOW_COUNT gateway path(s)"

# ── Sync ai/, core/, tasks/ (full tree first) ────────────────────────
for sub in ai core tasks; do
    [ -d "$GATEWAY_SRC/$sub" ] || continue
    rsync -a --delete \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        "$GATEWAY_SRC/$sub/" "$NPM_ROOT/gateway/$sub/"
done

# ── requirements.txt (bundle metadata, always shipped by postinstall path) ──
cp "$GATEWAY_SRC/requirements.txt" "$NPM_ROOT/gateway/requirements.txt" 2>/dev/null || true

# ── VERSION marker (LED-1889 version truth) ──────────────────────────
# Written from package.json (single canonical version source) so the bundled
# MCP server — and the ~/.delimit/server copy that `delimit setup` installs —
# self-reports the true shipped version instead of a hardcoded constant.
node "$SCRIPT_DIR/sync-version-sources.js" --bundle-only
PKG_VERSION="$(cat "$NPM_ROOT/gateway/VERSION")"

# ── PRUNE: delete every synced gateway file NOT on the allowlist ─────
# (Exempt: license_core.py, kept transiently for the .so compile step.)
PRUNED=0
while IFS= read -r abs; do
    rel="gateway/${abs#"$NPM_ROOT/gateway/"}"
    case "$rel" in
        *__pycache__*|*.pyc) continue ;;
    esac
    case " $PROPRIETARY_SRCS " in *" $rel "*) continue ;; esac
    if ! grep -qxF "$rel" <<< "$ALLOW_PATHS"; then
        rm -f "$abs"
        PRUNED=$((PRUNED+1))
    fi
done < <(find "$NPM_ROOT/gateway/ai" "$NPM_ROOT/gateway/core" "$NPM_ROOT/gateway/tasks" -type f 2>/dev/null)

# Remove any now-empty directories left behind by pruning.
find "$NPM_ROOT/gateway/ai" "$NPM_ROOT/gateway/core" "$NPM_ROOT/gateway/tasks" \
    -type d -empty -delete 2>/dev/null || true

echo "  Pruned $PRUNED non-allowlisted file(s)"

# ── Optionally sync to installed dev server (if present) ─────────────
# This is the LOCAL developer MCP server (~/.delimit/server) on our own machine
# and intentionally receives the FULL gateway (it is not the shipped bundle).
# Release builds must opt in explicitly before writing to this live installation.
# Customer installs receive the allowlisted bundle via npm postinstall instead.
INSTALLED_SERVER="$HOME/.delimit/server"
if [ "${SKIP_SERVER_SYNC:-}" = "1" ]; then
    echo "  ⏭️  Skipping installed server sync (SKIP_SERVER_SYNC=1)"
elif [ "${SYNC_INSTALLED_SERVER:-}" != "1" ]; then
    echo "  ⏭️  Skipping installed server sync (default); set SYNC_INSTALLED_SERVER=1 to opt in."
elif [ -n "${GATEWAY_OVERRIDE:-}" ] && [ "${FORCE_INSTALLED_SERVER_SYNC:-}" != "1" ]; then
    echo "  ⛔ GATEWAY_OVERRIDE selects a pinned export, not the live gateway; refusing installed server rsync --delete. Set FORCE_INSTALLED_SERVER_SYNC=1 as well to override." >&2
    exit 1
elif [ -L "$INSTALLED_SERVER/ai" ] || [ -L "$INSTALLED_SERVER/core" ]; then
    # 2026-09-15 incident: ~/.delimit/server/{ai,core} were symlinks into the
    # live gateway checkout, so this rsync --delete rewrote that checkout's
    # working tree (33 tracked files + deletions) on every sync. Never write
    # through a symlink; the dev server must be a real directory.
    echo "  ⛔ installed dev server ai/ or core/ is a symlink ($INSTALLED_SERVER); refusing to rsync --delete through it. Replace it with a real copy (delimit setup) or set SKIP_SERVER_SYNC=1." >&2
    exit 1
elif [ -d "$INSTALLED_SERVER/ai" ]; then
    echo "  Syncing to installed dev server ($INSTALLED_SERVER)..."
    rsync -a --delete --exclude='__pycache__' --exclude='*.pyc' \
        "$GATEWAY_SRC/ai/" "$INSTALLED_SERVER/ai/"
    rsync -a --delete --exclude='__pycache__' --exclude='*.pyc' \
        "$GATEWAY_SRC/core/" "$INSTALLED_SERVER/core/" 2>/dev/null || true
    if [ -n "${PKG_VERSION:-}" ]; then
        printf '%s\n' "$PKG_VERSION" > "$INSTALLED_SERVER/VERSION"
    fi
    echo "  ✅ installed dev server synced"
fi

# ── Report ────────────────────────────────────────────────────────────
AI_COUNT=$(find "$NPM_ROOT/gateway/ai" -name '*.py' 2>/dev/null | wc -l)
CORE_COUNT=$(find "$NPM_ROOT/gateway/core" -name '*.py' 2>/dev/null | wc -l)
TASKS_COUNT=$(find "$NPM_ROOT/gateway/tasks" -name '*.py' 2>/dev/null | wc -l)
echo "  ✅ ai/: $AI_COUNT .py   core/: $CORE_COUNT .py   tasks/: $TASKS_COUNT .py (allowlisted only)"

# ── Self-verify: nothing outside the allowlist survived (except transient) ──
LEAKED=0
while IFS= read -r abs; do
    rel="gateway/${abs#"$NPM_ROOT/gateway/"}"
    # Same transient exemption as the prune above: proprietary sources are kept
    # only long enough for the .so build step, which strips them immediately.
    case " $PROPRIETARY_SRCS " in *" $rel "*) continue ;; esac
    if ! grep -qxF "$rel" <<< "$ALLOW_PATHS"; then
        echo "  ❌ NON-ALLOWLISTED FILE SURVIVED SYNC: $rel"
        LEAKED=1
    fi
done < <(find "$NPM_ROOT/gateway/ai" "$NPM_ROOT/gateway/core" "$NPM_ROOT/gateway/tasks" -type f ! -path '*__pycache__*' ! -name '*.pyc' 2>/dev/null)
if [ $LEAKED -ne 0 ]; then
    echo "❌ Sync failed — non-allowlisted paths in working-tree bundle"
    exit 1
fi

# ── Credential scan on synced gateway files ─────────────────────────
echo -n "  Credential scan... "
CRED_HITS=$(grep -rEin '["'"'"'](?:password|passwd|secret|api_key|apikey|token|auth_token|access_token|private_key)["'"'"']\s*:\s*["'"'"'][^"'"'"']{4,}["'"'"']' "$NPM_ROOT/gateway/" --include="*.py" --include="*.js" --include="*.json" 2>/dev/null | grep -v 'environ\|getenv\|process\.env\|os\.environ\|example\|placeholder\|REDACTED\|your_\|change.me\|TODO\|FIXME\|xxx\|None\|null\|undefined\|test_password\|test_secret' || true)
if [ -n "$CRED_HITS" ]; then
    echo "FAILED"
    echo "  Hardcoded credentials detected in gateway bundle:"
    echo "$CRED_HITS" | while read -r line; do echo "    $line"; done
    exit 1
fi
echo "clean"

echo "Gateway sync complete (allowlist-driven)"
