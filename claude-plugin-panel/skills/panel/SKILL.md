---
name: panel
description: Use when the user explicitly asks for a multi-model panel or deliberation on a decision.
---

Use `delimit_deliberation_status` and `delimit_models` with `action="list"` to identify the available route before running a panel. Tell the user if the route uses their configured model CLIs/API keys or hosted keys. A status check does not authorize a model call.

Frame one concrete question. Send only background context the user has approved for sharing with the panel. If the requested context is unclear, ask which details may be sent. Do not pass `context_files` unless the user approves each file: the engine reads the files and sends redacted, size-capped contents to models. Explain that the engine may also append local ledger, model, and git-status metadata automatically; see PRIVACY.md. If this is unacceptable to the user, do not call the panel.

Call `delimit_deliberate` with `question` and the approved `context`. Use `mode`, `max_rounds`, or `scope` only when they serve the request. Check for an error or blocked result before presenting a verdict. Report each model's actual position from the returned responses, where available, then state agreements, disagreements, and the tool's verdict. If the tool gives only truncated final responses, say so; do not invent missing positions or imply unanimity when `status` is `no_consensus`.
