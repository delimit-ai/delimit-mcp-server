# Delimit Lite (Claude Plugin)

## Getting Started

1. Type `/delimit-lite:resume` at the start of a session to surface prior context and open tasks.
2. Record decisions and tasks with `/delimit-lite:record` (or ask Claude to "record this").
3. Wrap up with `/delimit-lite:handoff` to store a structured handoff before ending work.

## What It Stores and Where

- **Storage location:** Records are saved as plain JSON and JSONL files in local directories (`~/.delimit/ledger/` for ledger entries and project memories under `~/.delimit/`).
- **What is stored:** Explicitly stated decisions, tasks, handoff summaries, and project tags.
- **Privacy & offline:** Record contents remain entirely on your local machine and are never transmitted over the network.

## Installing delimit-cli

Delimit Lite uses Markdown slash commands and skills that instruct Claude to run `delimit-cli`. To install the CLI globally:

```sh
npm install -g delimit-cli
```

Alternatively, commands can run on-demand via `npx delimit-cli@4.23.0`.

## Installation

Install directly from the Delimit repository marketplace:

```sh
/plugin install delimit-lite --marketplace delimit-ai/delimit-mcp-server
```

Or install from the Anthropic Claude Plugin Directory when available.

## Included Slash Commands and Skills

- **resume** (`/delimit-lite:resume`): Inspect open work, active branch context, and recent decisions when explicitly requested by the user.
- **record** (`/delimit-lite:record`): Record explicitly stated decisions and tasks into the Delimit ledger when explicitly requested.
- **handoff** (`/delimit-lite:handoff`): Generate structured, verified session handoffs capturing completed work, test results, blockers, and next steps when explicitly requested.

All commands and skills run only upon explicit user invocation.

## Directory Compliance

This lite plugin is specifically designed to meet all Claude Plugin Directory policies:
- No background or local MCP server processes (`.mcp.json` omitted).
- No command or event hooks (`hooks/` omitted).
- No executable scripts or package manifests.
- Static Markdown slash commands, skills, and UTF-8 documentation only, with a square PNG icon.
