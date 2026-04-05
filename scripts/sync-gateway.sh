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

# ── Proprietary files to EXCLUDE from npm bundle ─────────────────────
# These are Pro-only or internal and must never ship in the public package
EXCLUDE=(
    "social_target.py"
    "social.py"
    "founding_users.py"
    "inbox_daemon.py"
    "deliberation.py"
)

# ── Sync ai/ directory ───────────────────────────────────────────────
rsync -a --delete \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    "$GATEWAY_SRC/ai/" "$NPM_ROOT/gateway/ai/"

# ── Remove proprietary files that rsync copied ───────────────────────
for f in "${EXCLUDE[@]}"; do
    rm -f "$NPM_ROOT/gateway/ai/$f"
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
INSTALLED_SERVER="$HOME/.delimit/server"
if [ -d "$INSTALLED_SERVER/ai" ]; then
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

# ── Verify no proprietary files leaked ────────────────────────────────
LEAKED=0
for f in "${EXCLUDE[@]}"; do
    if [ -f "$NPM_ROOT/gateway/ai/$f" ]; then
        echo "  ❌ PROPRIETARY FILE LEAKED: $f"
        LEAKED=1
    fi
done
if [ $LEAKED -ne 0 ]; then
    echo "❌ Sync failed — proprietary files in bundle"
    exit 1
fi

echo "✅ Gateway sync complete"
