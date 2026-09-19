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
set -euo pipefail

GATEWAY_SRC="${GATEWAY_OVERRIDE:-/home/delimit/delimit-gateway}"

# NOTE: in a git WORKTREE, .git is a FILE containing "gitdir: ...", not a
# directory — so test for either. (Caught in first use: the guard refused a
# perfectly valid clean worktree.)
if [ ! -e "$GATEWAY_SRC/.git" ] || ! git -C "$GATEWAY_SRC" rev-parse --git-dir >/dev/null 2>&1; then
    echo "⚠️  gateway-clean-guard: $GATEWAY_SRC is not a git checkout — cannot bind the artifact."
    echo "    Refusing: a release must be reproducible from committed source."
    exit 1
fi

DIRTY="$(git -C "$GATEWAY_SRC" status --porcelain 2>/dev/null | grep -vE '^\?\? ' || true)"
HEAD_SHA="$(git -C "$GATEWAY_SRC" rev-parse HEAD)"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# The workflow is the existing source of truth; an environment override cannot
# silently select a different clean checkout from the reviewed CI source.
EXPECTED_SHA="$(node -e '
  const fs = require("fs"), path = require("path");
  const root = path.resolve(process.argv[1], "..");
  const yaml = require(require.resolve("js-yaml", { paths: [root] }));
  const pin = yaml.load(fs.readFileSync(path.join(root, ".github/workflows/publish.yml"), "utf8"))?.env?.GATEWAY_SOURCE_SHA;
  if (typeof pin !== "string" || !/^[0-9a-f]{40}$/.test(pin)) {
    console.error("gateway-clean-guard: missing or invalid full workflow GATEWAY_SOURCE_SHA");
    process.exit(1);
  }
  if (process.env.GATEWAY_SOURCE_SHA !== undefined && process.env.GATEWAY_SOURCE_SHA !== pin) {
    console.error("gateway-clean-guard: environment pin differs from workflow GATEWAY_SOURCE_SHA");
    process.exit(1);
  }
  process.stdout.write(pin);
' "$SCRIPT_DIR")"
if [ "$HEAD_SHA" != "$EXPECTED_SHA" ]; then
    echo "❌ gateway-clean-guard: source HEAD $HEAD_SHA differs from required $EXPECTED_SHA"
    exit 1
fi
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
