#!/bin/bash
# Delimit OS — the AI developer operating system
# Type 'delimit' to launch the TUI, or 'delimit <command>' for CLI tools
#
# Usage:
#   delimit              → Launch TUI (interactive terminal dashboard)
#   delimit --quick      → Quick status (non-interactive)
#   delimit think        → Trigger deliberation
#   delimit build        → Start autonomous build loop
#   delimit ask <query>  → Ask the swarm
#   delimit lint <spec>  → Lint an API spec
#   delimit init         → Initialize governance in current repo
#   delimit setup        → Configure AI assistants

set -e

DELIMIT_HOME="${DELIMIT_HOME:-$HOME/.delimit}"
GATEWAY="$DELIMIT_HOME/server/ai"

# If no args, launch TUI (interactive if terminal, quick if piped)
if [ $# -eq 0 ]; then
    if [ -f "$GATEWAY/tui.py" ]; then
        if [ -t 1 ] && [ -t 0 ]; then
            cd "$DELIMIT_HOME/server" && exec python3 -m ai.tui
        else
            cd "$DELIMIT_HOME/server" && exec python3 -m ai.tui --quick
        fi
    else
        # Fallback to npm CLI
        exec delimit-cli "$@"
    fi
fi

# Route commands
case "$1" in
    --quick|-q)
        if [ -f "$GATEWAY/tui.py" ]; then
            cd "$DELIMIT_HOME/server" && exec python3 -m ai.tui --quick
        else
            exec delimit-cli status
        fi
        ;;
    think|deliberate)
        shift
        QUESTION="${*:-What should we build next based on the current ledger and signals?}"
        echo "[Delimit OS] Triggering deliberation..."
        cd "$DELIMIT_HOME/server" && python3 -c "
from ai.deliberation import deliberate
import json
result = deliberate('''$QUESTION''', mode='dialogue', max_rounds=3)
if 'error' in result:
    print(f'Error: {result[\"error\"]}')
elif result.get('mode') == 'single_model_reflection':
    print(f'Model: {result.get(\"model\", \"?\")}')
    print(f'\\nAdvocate:\\n{result.get(\"advocate\", \"\")[:500]}')
    print(f'\\nCritic:\\n{result.get(\"critic\", \"\")[:500]}')
    print(f'\\nSynthesis:\\n{result.get(\"synthesis\", \"\")}')
else:
    print(f'Verdict: {result.get(\"final_verdict\", \"no consensus\")[:500]}')
    print(f'Rounds: {result.get(\"rounds\", 0)}')
" 2>&1
        ;;
    build|loop)
        shift
        echo "[Delimit OS] Starting autonomous build loop..."
        echo "Checking ledger for next task..."
        cd "$DELIMIT_HOME/server" && python3 -c "
from ai.ledger_manager import get_context
import json
result = get_context()
items = result.get('next_up', [])
if items:
    print(f'Next up: {items[0].get(\"id\", \"?\")} [{items[0].get(\"priority\", \"?\")}] {items[0].get(\"title\", \"?\")[:60]}')
    print(f'Total open: {result.get(\"open_items\", 0)}')
else:
    print('Ledger is clear — nothing to build.')
" 2>&1
        ;;
    ask)
        shift
        QUERY="$*"
        if [ -z "$QUERY" ]; then
            echo "Usage: delimit ask <question>"
            exit 1
        fi
        echo "[Delimit OS] Checking context..."
        cd "$DELIMIT_HOME/server" && python3 -c "
from ai.ledger_manager import get_context
import json
result = get_context()
print(json.dumps(result, indent=2)[:2000])
" 2>&1
        ;;
    status)
        if [ -f "$GATEWAY/tui.py" ]; then
            cd "$GATEWAY/.." && exec python3 -m ai.tui --quick
        else
            exec delimit-cli status
        fi
        ;;
    *)
        # Pass through to delimit-cli for all other commands
        exec delimit-cli "$@"
        ;;
esac
