# Delimit for Claude Code

Keep the state. Change the model.

This plugin adds three skills for recording stated decisions and tasks, creating handoffs, and reading open work when you resume. Its MCP server uses the records toolset. Records are plain local files that other tools can read.

## Requirements

- Node.js 18 or later and `npx`
- Python 3.9 or later with `venv` and `pip`

## Install

Add the marketplace, then install the plugin in Claude Code:

```sh
claude plugin marketplace add delimit-ai/delimit-mcp-server
claude plugin install delimit@delimit
```

The first launch downloads the pinned `delimit-cli` package from npm and may download Python requirements from the Python package index. It creates the plugin's own copy of the local MCP server under `~/.delimit/plugin-server/<version>` with its own Python venv inside that directory, separate from any `~/.delimit/venv`; it never overwrites an existing `~/.delimit/server`. It does not run `delimit setup` or register another assistant configuration.

## What happens at session start

On startup, resume, clear, and compaction, a local hook shows the latest pending handoff and up to five open decisions or tasks for this project. It prints nothing when there are no records. Use `/delimit:handoff` to save a handoff and the resume skill to list records.

## Data and removal

The records tools read and write ledger and handoff data under `~/.delimit` as plain JSON and JSONL files you own. They do not send record contents over the network. The launcher starts a local stdio server. See [PRIVACY.md](PRIVACY.md).

Uninstall with `claude plugin uninstall delimit@delimit`. Your records under `~/.delimit` remain. To delete them too, inspect that directory and remove it yourself.

The skills follow explicit requests and available records. They cannot reconstruct work that was never recorded or guarantee recovery in another assistant. Other tools can read the plain files if configured to do so.

## Where records live

- Records are kept in one local ledger under `~/.delimit/ledger/`. Each record is tagged with the project it was created in: the CLI-detected name (package, Python project, git remote, or directory fallback; temporary scratch projects use `unsorted`). Listing shows records from all projects; the tag tells you where each one came from.
- The server also keeps local operational logs under `~/.delimit` (`tool_usage.jsonl`, `events/`, `traces/`). They contain tool names, timestamps and outcomes, not your record content, and they stay local.
- Once installed, the records workflow runs with no network access. We tested it in a network-isolated environment.

## Support / contact

pro@delimit.ai · https://github.com/delimit-ai/delimit-mcp-server/issues
