# Data handling

The plugin starts `delimit mcp --toolset records` locally. The records tools read and write ledger and handoff data in plain JSON and JSONL files under `~/.delimit`; those files belong to you. The launcher also stores its own server copy and Python venv under `~/.delimit/plugin-server/<version>`. It does not change Claude, Codex, or other assistant configuration files.

On first launch, `npx` fetches `delimit-cli` from the npm registry and `pip` may fetch the server's Python requirements from the Python package index. The records tools themselves do not send record contents to a service. Claude Code still processes the content you choose to show it under its own terms.

Uninstalling the plugin leaves `~/.delimit` untouched. Inspect and delete that directory yourself if you want to remove the records.

The session-start hook reads local records only and sends nothing anywhere.

## Where records live

- Records are kept in one local ledger under `~/.delimit/ledger/`. Each record is tagged with the project it was created in: the CLI-detected name (package, Python project, git remote, or directory fallback; temporary scratch projects use `unsorted`). Listing shows records from all projects; the tag tells you where each one came from.
- The server also keeps local operational logs under `~/.delimit` (`tool_usage.jsonl`, `events/`, `traces/`). They contain tool names, timestamps and outcomes, not your record content, and they stay local.
- Once installed, the records workflow runs with no network access. We tested it in a network-isolated environment.

## Support / contact

pro@delimit.ai · https://github.com/delimit-ai/delimit-mcp-server/issues
