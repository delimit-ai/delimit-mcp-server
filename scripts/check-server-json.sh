#!/usr/bin/env bash
# Fail-closed guard for server.json, the MCP Registry record.
#
# Runs BEFORE anything publishes (publish.yml validate job) and before a
# registry-only republish (registry-publish.yml). Every rule below is a
# rejection the registry has actually returned or a stale-record failure we
# have actually shipped:
#   - description > 100 chars -> registry 422 (v4.18.0, 2026-09-02)
#   - version drift between server.json and package.json -> record points at
#     an npm version that does not exist (why versions are checked twice)
#   - registry schema drift -> mcp-publisher validate (optional arg 1 = path
#     to a pinned mcp-publisher binary; skipped with a notice if absent)
#
# Usage: bash scripts/check-server-json.sh [path/to/mcp-publisher]
set -euo pipefail

cd "$(dirname "$0")/.."
PUBLISHER="${1:-}"
MAX_DESC=100

fail() { echo "::error file=server.json::$*"; exit 1; }

node -e "JSON.parse(require('fs').readFileSync('server.json','utf8'))" 2>/dev/null \
  || fail "server.json is not valid JSON"

NAME=$(node -p "require('./server.json').name")
DESC=$(node -p "require('./server.json').description")
SRV_VERSION=$(node -p "require('./server.json').version")
SRV_PKG_VERSION=$(node -p "require('./server.json').packages[0].version")
SRV_PKG_ID=$(node -p "require('./server.json').packages[0].identifier")
PKG_VERSION=$(node -p "require('./package.json').version")
PKG_NAME=$(node -p "require('./package.json').name")

# Length in characters, not bytes: the registry counts characters, and the
# canon line is ASCII, but a future em-dash must not be double-counted.
DESC_LEN=$(node -p "[...require('./server.json').description].length")
if [ "$DESC_LEN" -gt "$MAX_DESC" ]; then
  fail "description is ${DESC_LEN} chars; the MCP Registry caps it at ${MAX_DESC} (returns 422). Shorten it."
fi
if [ "$DESC_LEN" -eq 0 ]; then
  fail "description is empty"
fi

if [ "$SRV_VERSION" != "$SRV_PKG_VERSION" ]; then
  fail "server.json version ($SRV_VERSION) != packages[0].version ($SRV_PKG_VERSION)"
fi
if [ "$SRV_VERSION" != "$PKG_VERSION" ]; then
  fail "server.json version ($SRV_VERSION) != package.json version ($PKG_VERSION)"
fi
if [ "$SRV_PKG_ID" != "$PKG_NAME" ]; then
  fail "server.json packages[0].identifier ($SRV_PKG_ID) != package.json name ($PKG_NAME)"
fi
case "$NAME" in
  io.github.delimit-ai/*) ;;
  *) fail "name ($NAME) is outside the io.github.delimit-ai/ namespace the repo's OIDC identity can publish" ;;
esac

if [ -n "$PUBLISHER" ] && [ -x "$PUBLISHER" ]; then
  "$PUBLISHER" validate server.json
else
  echo "::notice::mcp-publisher not supplied; schema validation skipped (static checks passed)"
fi

echo "server.json OK: $NAME@$SRV_VERSION, description ${DESC_LEN}/${MAX_DESC} chars, npm $SRV_PKG_ID@$SRV_PKG_VERSION"
