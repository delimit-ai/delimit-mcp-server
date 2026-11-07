---
description: Resume prior session context, open tasks, and recent decisions
---

When the user invokes `/delimit-lite:resume` (or explicitly asks to resume, check prior context, or view open work):

1. Check if `delimit` is available in PATH, or run via `npx delimit-cli@4.23.0`:
   - If `delimit` is not installed, inform the user they can install it globally with `npm install -g delimit-cli`.
2. Inspect open tasks and recent project memories:
   - Run `delimit resume` (or `npx delimit-cli@4.23.0 resume`) to view last session state, recent memories, and git context.
   - Run `delimit status` (or `npx delimit-cli@4.23.0 status`) to view overall governance readiness, ledger status, and active models.
3. Summarize the current tasks, decisions, unfinished work, and next steps for the user.
4. State clearly when no prior records are available.
