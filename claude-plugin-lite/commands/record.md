---
description: Record a stated decision or task into the local Delimit ledger
argument-hint: [decision or task]
---

When the user invokes `/delimit-lite:record` (or explicitly asks to record a stated decision or task):

1. Check if `delimit` is available in PATH, or run via `npx delimit-cli@4.23.0`:
   - If `delimit` is not installed, inform the user they can install it globally with `npm install -g delimit-cli`.
2. Save the decision or task via `delimit remember` (or `npx delimit-cli@4.23.0 remember "<content>"`), adding appropriate tags such as `--tag decision` or `--tag task`. If arguments were provided (`$ARGUMENTS`), record them; otherwise use the explicitly stated decision or task.
3. Include the user's wording and any explicit owner or deadline.
4. Ask for essential missing details only when the record would otherwise be misleading. Never infer a commitment or extract one from chat history unless the user asks you to.
