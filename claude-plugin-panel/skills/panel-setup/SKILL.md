---
name: panel-setup
description: Use when the user asks which panel models are available or how to set up multi-model deliberation.
---

Call `delimit_models` with `action="list"` and `delimit_deliberation_status`. Explain the reported model backends, quota, sign-in state, and any license or configuration error as shown. Do not call `delimit_deliberate` for setup.

For user-owned models, explain that the engine can discover installed `codex`, `claude`, or `gemini` CLIs and supported provider credentials, or read explicit entries in `~/.delimit/models.json`. `delimit_models` supports `list`, `detect`, `add`, and `remove`; `detect`, `add`, and `remove` can write configuration. The `add` implementation accepts `grok`, `gemini`, `openai`, or `anthropic` provider templates, despite its broader parameter description; do not promise that `add` accepts `codex`. Never ask a user to paste an API key into chat or pass one as a tool argument. Point them to environment variables or local configuration instead. Explain that configured API or CLI providers receive the question and shared context when a panel runs. With no user models, a hosted-key route may be available and can require Delimit sign-in; if neither route is available, report that no panel can run.
