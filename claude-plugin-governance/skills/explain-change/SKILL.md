---
name: explain-change
description: Use when the user asks for API change impact, reviewer notes, or migration notes.
---

Call `delimit_explain` with local `old_spec` and `new_spec` paths. Use `template: "pr_comment"` for reviewer notes or `template: "migration"` for migration notes. Pass version and API name context only when known. Present the rendered explanation as a draft and report tool errors directly.
