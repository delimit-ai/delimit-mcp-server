# Delimit Panel for Claude Code

Ask several AI models to examine a hard decision. The `panel` skill runs only on an explicit request and reports each available model's position, points of agreement and disagreement, and the returned verdict. No consensus is a valid result. The `panel-setup` skill checks available models and explains setup without running a deliberation.

## Requirements and install

- Node.js 18 or later with `npx`
- Python 3.9 or later with `venv` and `pip`
- At least two available model routes for a normal panel, or an available hosted-key route; a single model can run a self-reflection instead of multi-model consensus, and scope rules can require more

After this plugin is added to the Delimit Claude marketplace, install it with `claude plugin install delimit-panel@delimit`. The first launch downloads the pinned `delimit-cli@4.21.0` package from npm and may install Python requirements. It starts a local stdio MCP server with five tools: `delimit_deliberate`, `delimit_deliberation_status`, `delimit_models`, `delimit_version`, and `delimit_help`. It does not run `delimit setup` or add other assistant configuration.

Ask “Which panel models are available?” to inspect configuration, or explicitly ask “Ask the panel whether …” to deliberate. The skill will frame the question and confirm which context may be shared. Do not include sensitive details you do not want sent to model providers. The engine can also add local ledger summaries, model names, and git status to the prompt automatically. Avoid `context_files` unless you have selected each file for sharing; their redacted, size-capped contents are included in the prompt.

`delimit_models` lists configured routes, while `delimit_deliberation_status` reports whether the current route is BYOK, hosted, or unavailable and gives account/quota state. The engine can use configured model CLIs or API keys; if no user model is enabled, it can fall back to locally configured Delimit-hosted keys when available. Hosted free deliberations are limited to three per signed-in account, subject to availability and other limits. With no usable route, the panel cannot run. Some `delimit_models` operations and deliberations may also return a license gate. Setup never requires putting an API key in a chat message.

## Data and removal

**Running a panel sends the question and shared context to the participating model providers**, through their API or CLI routes. In the hosted-key route, the inspected engine calls provider APIs using locally configured hosted keys and checks Delimit account quota through a configured Supabase service. The quota request contains an account identifier and counters, not the question or context. If optional cloud sync is configured, Pro deliberations can also send a question and context excerpt to that configured Supabase project. See [PRIVACY.md](PRIVACY.md) for the exact paths and limits.

The engine returns the result to Claude Code. On Free, completed deliberations are ephemeral in the engine: it writes a local metadata event, not a full transcript. On Pro, it writes the full transcript under `~/.delimit/deliberations/` or an explicit `save_path`, and can create local attestations. Existing older transcripts remain. Uninstalling the plugin leaves `~/.delimit` intact; inspect that directory before deleting it yourself. Provider and Claude Code retention follow their own terms.
