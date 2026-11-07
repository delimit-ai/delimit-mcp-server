---
name: handoff
description: Use when the user asks for a handoff or a work session reaches a stopping point.
---

At the stopping point, capture session state using the Delimit CLI:

1. Check if `delimit` is available in PATH, or run via `npx delimit-cli@4.23.0`:
   - If `delimit` is not installed, inform the user they can install it globally with `npm install -g delimit-cli`.
2. Inspect handoff status with `delimit handoff check` (or `npx delimit-cli@4.23.0 handoff check`).
3. Save the handoff record via `delimit remember` (or `npx delimit-cli@4.23.0 remember --tag handoff "<summary>"`), detailing:
   - `task_description`: what this piece of work is;
   - `completed`: what was done, including test results as they actually ran (e.g. "679/679 unit tests passed");
   - `not_completed`: unfinished work;
   - `blockers` and `assumptions`, when there are any;
   - `files_modified`: the files you changed;
   - `next_action`: the single next step.

Keep every claim tied to work that actually happened. Don't report tests as passing unless you ran them in this session.
