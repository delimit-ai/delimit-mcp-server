# Data handling

The plugin starts `delimit mcp --toolset panel` on your machine. Listing models or checking status does not run a model. Running `delimit_deliberate` **sends the question, supplied context, and subsequent model responses to the participating model providers**. CLI routes use those CLIs' own services; API routes call the configured provider endpoint. The inspected engine's defaults name OpenAI, Anthropic, xAI, and Google routes. The engine passes each model's raw response to other models in later rounds.

The engine can append local context even when the caller supplies an empty `context`: a ledger summary with up to three item titles, model names, git commit and change count, and local governance status. If `context_files` is used, the engine reads the named files, redacts secrets and personal information, caps their size, and includes the resulting content and file paths in model prompts. Redaction cannot guarantee removal of every sensitive detail. The panel skill requires the user's approval for the shared context and each file.

## Hosted free route and Delimit services

If no user model is enabled and at least two hosted models are configured locally, the engine can use Delimit-provided keys for up to three free deliberations. The inspected hosted configuration permits Gemini and Codex routes and calls their provider endpoints directly. No prompt relay to a Delimit-hosted inference endpoint appears in that path. Hosted access can require `delimit.ai` sign-in. The engine uses an account identifier with a configured Supabase quota table to read and update the lifetime count. The quota calls do not include the question or context. If hosted keys or sign-in are unavailable, status can report `mode: none` or require sign-in; a no-key installation does not guarantee free model calls.

On Pro, **optional cloud sync** can send up to 2,000 characters each of the question and context, plus verdict and metadata, to the Supabase project configured by `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` or `~/.delimit/secrets/supabase.json`. It is off without those settings, and `DELIMIT_DISABLE_CLOUD_SYNC=1` disables it. If that project is operated by Delimit, Delimit receives the synced excerpt. The inspected code does not establish server-side retention or deletion periods for hosted quota state or optional sync, so we cannot state one. The provider and Claude Code services also have their own data policies.

## Local storage

- `~/.delimit/models.json` can hold model configuration and, if added that way, API keys. Prefer environment variables or CLI authentication and protect this file.
- The launcher stores its versioned server copy and Python environment under `~/.delimit/plugin-server/<version>`.
- Pro completed deliberations write full transcripts to `~/.delimit/deliberations/` by default, or to an explicit `save_path`. The engine may create local signed attestations. Free completed deliberations do not write full transcripts, even with `save_path`; they append a metadata-only event to `~/.delimit/sessions/deliberation_events.jsonl`. Older transcripts are not deleted on downgrade.
- Usage, quota and operational log files under `~/.delimit` can record counters, tool names, timestamps, outcomes, and sometimes response previews. There is no separate telemetry endpoint added by this plugin. Optional cloud sync and hosted quota requests are the Delimit network paths described above.

Uninstalling the plugin does not delete local files. Review `~/.delimit` and any explicit transcript destination before removing data. This plugin does not set a local transcript expiration period.

## Support

pro@delimit.ai · https://github.com/delimit-ai/delimit-mcp-server/issues
