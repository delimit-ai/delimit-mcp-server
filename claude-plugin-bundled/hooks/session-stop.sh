#!/usr/bin/env bash
# At session end, offer to save a handoff if meaningful work happened.

cat << 'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "Stop",
    "additionalContext": "If this session included meaningful work — edited files, made decisions, completed tasks, or left something unfinished — offer to create a Delimit handoff with `/handoff` so future sessions (or a different assistant) can pick up exactly where you left off. Skip this if the session was purely exploratory or conversational with no persistent changes."
  }
}
EOF
exit 0
