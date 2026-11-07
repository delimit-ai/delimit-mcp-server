# Delimit Lite (Claude Plugin)

Keep decisions, tasks, and handoffs organized in local files across coding sessions and assistants.

Delimit Lite is a directory-compliant edition designed for environments that require pure Markdown skills without local background servers, commands, or hooks. It empowers Claude Code to manage persistent workflow records, structured handoffs, and session continuity using the standalone `delimit-cli` tool.

## Installation

Install directly from the Delimit repository marketplace:

```sh
/plugin install delimit-lite --marketplace delimit-ai/delimit-mcp-server
```

Or install from the Anthropic Claude Plugin Directory when available.

## Prerequisites

Delimit Lite uses Markdown skills that instruct Claude how to invoke Delimit via the command line. To take full advantage of persistence:

```sh
npm install -g delimit-cli
```

Alternatively, commands can be run on-demand via `npx delimit-cli@4.23.0`.

## Included Skills

- **resume**: Inspect open work, active branch context, and recent decisions at session start or after switching assistants.
- **record**: Record explicitly stated decisions and tasks into the Delimit ledger with tags and owner attribution.
- **handoff**: Generate structured, verified session handoffs capturing completed work, test results, blockers, and next steps.

## Directory Compliance

This lite plugin is specifically designed to meet all Claude Plugin Directory policies:
- No background or local MCP server processes (`.mcp.json` omitted).
- No command or event hooks (`hooks/` omitted).
- No executable scripts or package manifests.
- Static Markdown skills and UTF-8 documentation only, with a square PNG icon.
