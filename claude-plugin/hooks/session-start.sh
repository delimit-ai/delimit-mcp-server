#!/usr/bin/env bash
# Surface open Delimit work at the start of every session.

cat << 'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "**Delimit is active.** Call `delimit_ledger_context` and `delimit_handoff_list` now to show the user their open tasks and any handoff from their last session — do this before asking what they want to work on. If both return empty, say so in one line and tell the user they can record a decision or task with `/record` or a handoff with `/handoff`."
  }
}
EOF
exit 0
