# Data handling

The plugin starts `delimit mcp --toolset records` locally. The records tools read and write ledger and handoff data in plain JSON and JSONL files under `~/.delimit`; those files belong to you. The launcher also stores its own server copy and Python venv under `~/.delimit/plugin-server/<version>`. It does not change Claude, Codex, or other assistant configuration files.

On first launch, `npx` fetches `delimit-cli` from the npm registry and `pip` may fetch the server's Python requirements from the Python package index. The records tools themselves do not send record contents to a service. Claude Code still processes the content you choose to show it under its own terms.

Uninstalling the plugin leaves `~/.delimit` untouched. Inspect and delete that directory yourself if you want to remove the records.

## Where records live

- Inside a git repository, records are grouped for that repository. Outside a repository they go to one default ledger under `~/.delimit/ledger/`.
- The server also keeps local operational logs under `~/.delimit` (`tool_usage.jsonl`, `events/`, `traces/`). They contain tool names, timestamps and outcomes, not your record content, and they stay local.
- Once installed, the records workflow runs with no network access. We tested it in a network-isolated environment.

## Support / contact

pro@delimit.ai · https://github.com/delimit-ai/delimit-mcp-server/issues
