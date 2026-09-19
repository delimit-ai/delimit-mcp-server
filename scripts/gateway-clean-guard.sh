#!/bin/bash
# Guard — the GATEWAY SOURCE tree must be clean before we sync it into a release.
#
# WHY THIS EXISTS
# ---------------
# `scripts/sync-gateway.sh` rsyncs /home/delimit/delimit-gateway into the bundle
# at publish time. `scripts/clean-tree-guard.js` already blocks publishing from
# a dirty NPM tree — but it checks THIS repo, not the gateway. So an uncommitted
# edit sitting in the gateway working tree (a parallel worker's in-flight branch,
# a debug print, a half-finished refactor) is copied straight into the tarball
# and shipped to every customer, with nothing in this repo's history recording
# what was actually published.
#
# Observed 2026-09-19: the gateway checkout was on another worker's branch with
# 13 modified files while a release was being prepared here. That is precisely
# the exact-artifact-binding failure the release policy names as a HOLD.
#
# This is a HOLD, not a nag: a release must be reproducible from committed
# state. Set GATEWAY_CLEAN_GUARD_ACK=1 only for a deliberate, recorded
# exception — it prints loudly and still names every dirty path.
set -uo pipefail

GATEWAY_SRC="${GATEWAY_OVERRIDE:-/home/delimit/delimit-gateway}"

if [ ! -d "$GATEWAY_SRC/.git" ]; then
    echo "⚠️  gateway-clean-guard: $GATEWAY_SRC is not a git checkout — cannot bind the artifact."
    echo "    Refusing: a release must be reproducible from committed source."
    exit 1
fi

DIRTY="$(git -C "$GATEWAY_SRC" status --porcelain 2>/dev/null | grep -vE '^\?\? ' || true)"
HEAD_SHA="$(git -C "$GATEWAY_SRC" rev-parse --short HEAD 2>/dev/null || echo unknown)"
BRANCH="$(git -C "$GATEWAY_SRC" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"

echo "🔗 gateway-clean-guard: $GATEWAY_SRC @ $BRANCH ($HEAD_SHA)"

if [ -n "$DIRTY" ]; then
    COUNT="$(printf '%s\n' "$DIRTY" | wc -l | tr -d ' ')"
    echo "❌ gateway source tree has $COUNT uncommitted change(s):"
    printf '%s\n' "$DIRTY" | sed 's/^/     /'
    echo ""
    echo "   These would be rsynced into the tarball and published, while this"
    echo "   repo's history would record nothing about them."
    echo "   Commit them, stash them, or point GATEWAY_OVERRIDE at a clean"
    echo "   checkout of the intended revision."
    if [ "${GATEWAY_CLEAN_GUARD_ACK:-}" = "1" ]; then
        echo ""
        echo "   ⚠️  GATEWAY_CLEAN_GUARD_ACK=1 — proceeding under a recorded exception."
        exit 0
    fi
    exit 1
fi

echo "   ✅ gateway source is clean; artifact binds to $BRANCH@$HEAD_SHA"
exit 0
