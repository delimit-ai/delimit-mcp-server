#!/bin/bash
# Sync gateway Python files into npm bundle before publish.
# Source of truth: /home/delimit/delimit-gateway/
# Destination: ./gateway/ (relative to npm-delimit root)
#
# This runs as part of prepublishOnly to guarantee the npm package
# always contains the latest gateway code. Drift is impossible.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NPM_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
GATEWAY_SRC="${GATEWAY_OVERRIDE:-/home/delimit/delimit-gateway}"

# ── Verify gateway source exists ─────────────────────────────────────
if [ ! -d "$GATEWAY_SRC/ai" ]; then
    echo "⚠️  Gateway source not found at $GATEWAY_SRC"
    echo "   Skipping sync (CI or customer machine — bundle as-is)"
    exit 0
fi

echo "🔄 Syncing gateway → npm bundle..."

# ── Proprietary paths to EXCLUDE from the bundle ─────────────────────
# DERIVED from package.json's "files" "!"-negations (single source of truth)
# via scripts/list-blocked-gateway-paths.js, so this list can never drift
# below what package.json blocks. Previously this was a hand-maintained array
# that only covered ~5 of the 27 blocked paths — the missing ~22 would have
# been rsync'd into the PUBLIC repo on the next release (IP leak).
#
# Each entry is a repo-relative path like "gateway/ai/social.py" or a
# directory like "gateway/ai/self_repair/".
BLOCKED_PATHS=()
while IFS= read -r line; do
    [ -n "$line" ] && BLOCKED_PATHS+=("$line")
done < <(node "$SCRIPT_DIR/list-blocked-gateway-paths.js")

echo "  Derived ${#BLOCKED_PATHS[@]} blocked path(s) from package.json"

# license_core.py is a special case: it is package.json-blocked BUT must
# survive this sync so build-license-core.sh can compile it to a native .so
# and then strip the plaintext. Keep it here; build-license-core.sh removes it.
LICENSE_CORE_SRC="gateway/ai/license_core.py"

# Build rsync --exclude args for the ai/ subtree (all blocked paths live under
# gateway/ai/). Paths are made relative to the ai/ source root.
AI_EXCLUDES=()
for p in "${BLOCKED_PATHS[@]}"; do
    [ "$p" = "$LICENSE_CORE_SRC" ] && continue          # keep for compile step
    case "$p" in
        gateway/ai/*) AI_EXCLUDES+=( "--exclude=${p#gateway/ai/}" ) ;;
    esac
done

# ── Sync ai/ directory ───────────────────────────────────────────────
rsync -a --delete \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    "${AI_EXCLUDES[@]}" \
    "$GATEWAY_SRC/ai/" "$NPM_ROOT/gateway/ai/"

# ── Belt-and-suspenders: remove any blocked path rsync still copied ──
# (covers blocked paths outside ai/, and guards against rsync pattern edge
# cases). license_core.py is intentionally kept for the compile step.
for p in "${BLOCKED_PATHS[@]}"; do
    [ "$p" = "$LICENSE_CORE_SRC" ] && continue
    rm -rf "${NPM_ROOT:?}/$p"
done

# ── Sync core/ directory ─────────────────────────────────────────────
rsync -a --delete \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    "$GATEWAY_SRC/core/" "$NPM_ROOT/gateway/core/"

# ── Sync tasks/ directory ────────────────────────────────────────────
rsync -a --delete \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    "$GATEWAY_SRC/tasks/" "$NPM_ROOT/gateway/tasks/"

# ── Sync requirements.txt ────────────────────────────────────────────
cp "$GATEWAY_SRC/requirements.txt" "$NPM_ROOT/gateway/requirements.txt" 2>/dev/null || true

# ── Also sync to installed server (if present) ────────────────────────
# Skip with SKIP_SERVER_SYNC=1 to avoid disconnecting active MCP sessions
INSTALLED_SERVER="$HOME/.delimit/server"
if [ "${SKIP_SERVER_SYNC:-}" = "1" ]; then
    echo "  ⏭️  Skipping installed server sync (SKIP_SERVER_SYNC=1)"
elif [ -d "$INSTALLED_SERVER/ai" ]; then
    echo "  Syncing to installed server ($INSTALLED_SERVER)..."
    rsync -a --delete \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        "$GATEWAY_SRC/ai/" "$INSTALLED_SERVER/ai/"
    rsync -a --delete \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        "$GATEWAY_SRC/core/" "$INSTALLED_SERVER/core/" 2>/dev/null || true
    echo "  ✅ installed server synced"
fi

# ── Report ────────────────────────────────────────────────────────────
AI_COUNT=$(find "$NPM_ROOT/gateway/ai" -name '*.py' -not -name '__pycache__' | wc -l)
CORE_COUNT=$(find "$NPM_ROOT/gateway/core" -name '*.py' -not -name '__pycache__' | wc -l)
TASKS_COUNT=$(find "$NPM_ROOT/gateway/tasks" -name '*.py' -not -name '__pycache__' | wc -l)

echo "  ✅ ai/: $AI_COUNT files"
echo "  ✅ core/: $CORE_COUNT files"
echo "  ✅ tasks/: $TASKS_COUNT files"

# ── Self-verify: no blocked path survived the sync (working tree) ─────
# Checks every package.json-blocked path (except the transient license_core.py
# compile input, which build-license-core.sh strips next). This catches drift
# in THIS script's own copy/delete logic before the bundle is committed.
LEAKED=0
for p in "${BLOCKED_PATHS[@]}"; do
    [ "$p" = "$LICENSE_CORE_SRC" ] && continue
    if [ -e "$NPM_ROOT/$p" ]; then
        echo "  ❌ PROPRIETARY PATH LEAKED INTO BUNDLE: $p"
        LEAKED=1
    fi
done
if [ $LEAKED -ne 0 ]; then
    echo "❌ Sync failed — proprietary paths in working-tree bundle"
    exit 1
fi

# ── Run credential scan on synced gateway files ─────────────────────
echo -n "  Credential scan... "
CRED_HITS=$(grep -rEin '["'"'"'](?:password|passwd|secret|api_key|apikey|token|auth_token|access_token|private_key)["'"'"']\s*:\s*["'"'"'][^"'"'"']{4,}["'"'"']' "$NPM_ROOT/gateway/" --include="*.py" --include="*.js" --include="*.json" 2>/dev/null | grep -v 'environ\|getenv\|process\.env\|os\.environ\|example\|placeholder\|REDACTED\|your_\|change.me\|TODO\|FIXME\|xxx\|None\|null\|undefined\|test_password\|test_secret' || true)
if [ -n "$CRED_HITS" ]; then
    echo "FAILED"
    echo "  Hardcoded credentials detected in gateway bundle:"
    echo "$CRED_HITS" | while read -r line; do echo "    $line"; done
    echo "  Fix: replace hardcoded values with env var lookups"
    exit 1
fi
echo "clean"

echo "Gateway sync complete"
