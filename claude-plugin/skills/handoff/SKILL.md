---
name: handoff
description: Use when the user asks for a handoff or a work session reaches a stopping point.
---

At the stopping point, update relevant ledger items using `delimit_ledger_update` or `delimit_ledger_done`, then call `delimit_handoff_create` with:
- `task_description`: what this piece of work is;
- `completed`: what was done, including test results as they actually ran (e.g. "12/12 unit tests passed");
- `not_completed`: unfinished work;
- `blockers` and `assumptions`, when there are any;
- `files_modified`: the files you changed;
- `next_action`: the single next step.

Keep every claim tied to work that actually happened. Don't report tests as passing unless you ran them in this session.
