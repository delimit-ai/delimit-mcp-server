#!/bin/bash
# check_identity_strings.sh — LED-3799 PR-time identity-string + secret gate
# for the PUBLIC npm-delimit (delimit-ai/delimit-mcp-server) repo.
#
# Ports delimit-gateway/scripts/check_identity_strings.sh (LED-1409) to this
# Node/npm repo. The gateway runs a PR-time content gate; npm-delimit only had
# the PREPUBLISH tarball scan (scripts/security-check.sh) plus the commit-email
# identity-guard.yml. Neither catches a banned identity STRING committed to a
# source/config/doc file at PR review time. This closes that gap.
#
# What it catches: SHIFT-1 banned identity strings (founder/holdco/account
# handles), personal-email PII, and hardcoded credentials in any git-TRACKED
# source, config, or public-facing doc (package.json, README, CHANGELOG, etc.).
#
# Enumeration: git ls-files (tracked content only). Rationale vs. the gateway's
# filesystem `grep -r`:
#   - In CI (actions/checkout) only tracked files exist, so this is equivalent.
#   - Locally, this repo's working tree also holds UNTRACKED proprietary gateway
#     files (deliberation.py, inbox_daemon.py, ...) that legitimately contain
#     identity strings and never ship to the public repo. A filesystem grep
#     would false-positive on them; git ls-files scans exactly the public
#     deanonymization surface — what is committed to the public repo.
# Because only allowlisted gateway files are tracked here, no BUNDLE_EXCLUDED
# post-filter (as in the gateway script) is required.
#
# Output format: one line per violation as
#   file:line: [reason] excerpt
# Exit code 0 = clean; non-zero = violations found.
#
# Override: lines containing `delimit-security-allow: <reason>` are excluded
# (audited via grep) — use for legitimate fixture cases.
#
# Scope: scans git-tracked .py .js .ts .tsx .json .yaml .yml .toml .md files,
# EXCLUDING tests/ by default (fixtures legitimately carry identity strings and
# fake keys; pass --include-tests to scan them). Unlike the gateway (which
# excludes markdown), this repo INCLUDES markdown so README + CHANGELOG — the
# most public artifacts — are gated (LED-3799 requirement).
#
# Usage:
#   bash scripts/check_identity_strings.sh [path]
#   bash scripts/check_identity_strings.sh --staged          # staged files only
#   bash scripts/check_identity_strings.sh --include-tests   # also scan tests/
#
# Patterns kept in lockstep with scripts/security-check.sh BLOCKLIST.

set -euo pipefail

SCAN_STAGED=0
INCLUDE_TESTS=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --staged) SCAN_STAGED=1; shift ;;
        --include-tests) INCLUDE_TESTS=1; shift ;;
        *) shift ;;  # positional path accepted for CLI parity; enumeration is git-based
    esac
done

# Patterns — kept in lockstep with scripts/security-check.sh BLOCKLIST.
# Adding a new banned term requires updating BOTH files.
BLOCKLIST_REGEX='jamsonsholdings|Bladabah|Domainvested26|Delimit26|home/jamsons|\binfracore\b|\bcrypttrx\b|\.wr_env'

# PII: real-looking personal email addresses (examples/docstrings suppressed).
PII_REGEX='[a-z0-9._%+-]+@(gmail|yahoo|hotmail|outlook|proton|jamsons|wire\.report|domainvested)'

# Hardcoded credentials: assignment of a string literal to a secret-ish name.
CRED_REGEX='(password|passwd|secret|api_key|apikey)[[:space:]]*[:=][[:space:]]*["'"'"'][^"'"'"']{8,}'

# Suppressors — lines matching any of these are EXCLUDED.
SUPPRESSORS='environ|getenv|process\.env|os\.environ|<configured|example|placeholder|REDACTED|\$\{credentials|credentials\.|security-scan-ignore|delimit-security-allow|noreply|e\.g\.|docstring|Args:|Credential resolution'

# Prefer GNU grep. On the CI runner (ubuntu-latest) `grep` is GNU grep; some
# dev shells alias grep to a wrapper, so pin the binary when present.
GREP=grep
for c in /usr/bin/grep /bin/grep; do
    if [ -x "$c" ]; then GREP="$c"; break; fi
done

# Tracked file extensions we scan.
EXTS='\.(py|js|ts|tsx|json|yaml|yml|toml|md)$'

# git ls-files enumeration -> newline list of candidate files.
list_files() {
    if [ "$SCAN_STAGED" = "1" ]; then
        git diff --cached --name-only --diff-filter=ACMR
    else
        git ls-files
    fi | "$GREP" -E "$EXTS" | while IFS= read -r f; do
        [ -f "$f" ] || continue
        if [ "$INCLUDE_TESTS" = "0" ]; then
            case "$f" in
                tests/*|test/*|*/tests/*|*/test/*|*/__tests__/*|*/spec/*|*/fixtures/*) continue ;;
            esac
        fi
        printf '%s\n' "$f"
    done
}

FAIL=0

scan_one() {
    local regex="$1" reason="$2" hits
    hits=$(list_files | while IFS= read -r f; do
        "$GREP" -nEi "$regex" "$f" 2>/dev/null | sed "s|^|$f:|"
    done | "$GREP" -vEi "$SUPPRESSORS" || true)
    if [ -n "$hits" ]; then
        echo "$hits" | while IFS= read -r line; do
            echo "$line" | awk -v reason="$reason" '{
                split($0, parts, ":")
                if (length(parts[1]) > 0 && parts[2] ~ /^[0-9]+$/) {
                    excerpt = substr($0, length(parts[1]) + length(parts[2]) + 3)
                    if (length(excerpt) > 200) excerpt = substr(excerpt, 1, 200) "..."
                    printf "%s:%s: [%s] %s\n", parts[1], parts[2], reason, excerpt
                } else {
                    printf "[%s] %s\n", reason, $0
                }
            }'
        done
        return 1
    fi
    return 0
}

run_check() {
    local label="$1" regex="$2" reason="$3" tmpfile
    tmpfile="$(mktemp)"
    echo -n "  $label... "
    if scan_one "$regex" "$reason" > "$tmpfile" 2>&1; then
        echo "✅ clean"
    else
        echo "❌ FOUND"
        cat "$tmpfile"
        FAIL=1
    fi
    rm -f "$tmpfile"
}

echo "🔍 delimit identity-string + secret gate (LED-3799, npm-delimit)"
if [ "$SCAN_STAGED" = "1" ]; then
    echo "  Scope: staged tracked files"
else
    echo "  Scope: all git-tracked files"
fi
echo ""

run_check "Banned identity strings" "$BLOCKLIST_REGEX" "banned-identity"
run_check "PII (personal email addresses)" "$PII_REGEX" "pii-email"
run_check "Hardcoded credentials" "$CRED_REGEX" "credential"

echo ""
if [ "$FAIL" -ne 0 ]; then
    echo "❌ Identity-string / secret gate FAILED."
    echo ""
    echo "Override: add a comment containing 'delimit-security-allow: <reason>'"
    echo "on the offending line (audited via grep)."
    exit 1
fi

echo "✅ All identity-string + secret checks passed."
exit 0
