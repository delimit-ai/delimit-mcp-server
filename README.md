[![MseeP.ai Security Assessment Badge](https://mseep.net/pr/delimit-ai-delimit-mcp-server-badge.png)](https://mseep.ai/app/delimit-ai-delimit-mcp-server)

# `</>` Delimit

**The merge gate for AI-written code — with signed, replayable attestation.**

Wrap any AI coding assistant (Claude Code, Codex, Cursor, Gemini CLI) with a governance chain that runs your gates, records what changed, and signs a replayable receipt for every merge.

[![npm](https://img.shields.io/npm/v/delimit-cli)](https://www.npmjs.com/package/delimit-cli)
[![Tests](https://img.shields.io/badge/tests-165%20passing-brightgreen)](https://github.com/delimit-ai/delimit-mcp-server)
[![GitHub Action](https://img.shields.io/badge/GitHub%20Action-v1.6.0-blue)](https://github.com/marketplace/actions/delimit-api-governance)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Glama](https://glama.ai/mcp/servers/delimit-ai/delimit/badge)](https://glama.ai/mcp/servers/delimit-ai/delimit)

```console
$ delimit wrap -- claude "fix the flaky test in tests/api.spec.ts"

✓ repo_diagnose
✓ security_audit       0 critical · 0 secrets
✓ test_smoke           165/165
✓ changed_files        1
✓ attestation signed   att_a05050eb8e13277e
                       delimit.attestation.v1 · HMAC-SHA256
                       replay → https://delimit.ai/att/att_a05050eb8e13277e
```

Every wrapped run emits a `delimit.attestation.v1` bundle: repo head before/after, changed files, gate results, HMAC-SHA256 signature, and a replay URL. Advisory by default; flip to enforcing when you're ready.

<p align="center">
  <a href="https://github.com/delimit-ai/delimit-action/releases/tag/v1.10.0">See a signed release</a> · <a href="https://delimit.ai/docs/workflow">Workflow guide</a> · <a href="https://delimit.ai">Website</a>
</p>

---

## Think and Build

Beyond the merge gate, Delimit orchestrates multi-model deliberation and autonomous builds. `delimit think` dispatches a strategic question to Claude, Codex, Gemini, and Grok; `delimit build` activates a background daemon that executes ledger tasks through the gate chain. `delimit vault` manages local secrets (AES-256).

Works across any configuration, from a single model on a budget to a full panel.

---

## Try it in 2 minutes

```bash
npx delimit-cli doctor            # 14 prescriptive checks — tells you exactly what to fix
npx delimit-cli status            # Visual dashboard of your entire governance setup
npx delimit-cli simulate          # Dry-run: see what would be blocked before you commit
npx delimit-cli scan              # Instant health grade for your API spec
npx delimit-cli try owner/repo    # Try governance on any GitHub repo
```

No API keys. No account. No config files.

### Pick your first win

**Protect my API** — catch breaking changes before merge:
```bash
npx delimit-cli try
# Creates a sample API, introduces breaking changes, shows what gets blocked.
# Saves a governance report to delimit-report.md
```

**Watch for drift** — detect spec changes without review:
```bash
npx delimit-cli init        # Sets up governance + drift baseline
# Weekly drift checks run automatically via GitHub Action
```

**Run PR copilot** — governance gates on every pull request:
```yaml
# .github/workflows/api-governance.yml
- uses: delimit-ai/delimit-action@v1
  with:
    spec: api/openapi.yaml
# Posts gate status, violations, and remediation in PR comments
```

---

## What's New in v4.3

*Gate every AI-assisted invocation. Ship the receipts.*

- **`delimit wrap`** — pipe `claude -p`, `cursor`, `aider`, `codex`, or any AI-assisted CLI through a signed governance gate. Snapshots the git diff before/after, runs lint + tests, HMAC-signs an `att_*` attestation, emits a public replay URL. Advisory by default; `--enforce` blocks CI on policy violations; `--max-time <s>` is a kill switch that tags the attestation as a `liability_incident` and prints a cross-model handoff command.
- **`delimit trust-page`** — renders a directory of attestations into a static HTML trust page + JSON Feed 1.1 feed. Single file, no framework, offline-renderable. Deploy anywhere.
- **`delimit ai-sbom`** — aggregates attestations into a CycloneDX 1.6 bill-of-materials with AI-specific fields (detected models per vendor, tool-call surface, policy gate counts). Pipe straight into procurement.
- **Cross-model by construction** — `wrap` is agnostic to the producer. Same attestation schema whether the pipe upstream is Claude Code, Cursor, Aider, Codex, or Gemini CLI. Switch producers without losing the audit chain.

```bash
# Gate any AI-assisted CLI
delimit wrap -- claude -p "add tests for payments"
#   → att_7d556843c84fb881 signed, replay: https://delimit.ai/att/att_7d556843c84fb881

# Kill switch + handoff after 60s wall-clock
delimit wrap --max-time 60 -- cursor edit "refactor auth middleware"
#   → if killed: kind=liability_incident
#   → suggested: delimit wrap -- claude -p "refactor auth middleware"

# Render accumulated attestations as a public trust page
delimit trust-page -o ./trust
#   → ./trust/index.html (+ feed.json)

# Build a CycloneDX-AI bill of materials
delimit ai-sbom -o ./ai-sbom.json
#   → components: 4 models detected, 187 gates run
```

## What's New in v4.20

*The highest state of AI governance.*

- **`delimit doctor`** -- 14 prescriptive diagnostics. Every failure prints the exact command to fix it. `--ci` for pipelines, `--fix` for auto-repair.
- **`delimit simulate`** -- policy dry-run. See what would be blocked before you commit. The `terraform plan` for API governance.
- **`delimit status`** -- visual terminal dashboard. Policy, specs, hooks, CI, MCP, models, memory, ledger, evidence, git branch. `--watch` for live refresh.
- **`delimit report`** -- governance report. `--since 7d --format md|html|json`. Audit-friendly output for PRs and compliance.
- **Memory hardening** -- SHA-256 integrity hash + source model tag on every `remember`. Cross-model trust, verified on every `recall`.
- **Tag-based publishing** -- automated gateway sync, no more version drift between source and npm bundle.

### Multi-Model Deliberation

Run your question through 4 AI models simultaneously. They debate each other until unanimous agreement.

```bash
delimit deliberate "Should we build rate limiting in-house or use a managed service?"
```

```
  Round 1 (independent):
    Claude:  Build in-house. Redis sliding window is 50 lines.
    Gemini:  Build. You already have Redis.
    Codex:   Agree — but add circuit breaker for Redis failures.
    Grok:    Build. Managed service costs $200/mo for 50 lines of code.

  Round 2 (deliberation):
    All models: AGREE

  UNANIMOUS CONSENSUS (2 rounds, confidence 94/100)
  Build rate limiting in-house with Redis + circuit breaker.
```

3 free deliberations, then BYOK for unlimited. Works with Grok, Gemini, Claude, GPT-4o.

### v4.1

- **TUI** -- terminal-native Ventures panel, real `delimit think` and `delimit build` commands
- **Security hardening** -- notify.py stubbed in npm, axios pinned against supply chain attacks
- **Free tier restructure** -- deliberations use Gemini Flash + GPT-4o-mini (cost: <$20/mo)
- **Zero-config onboarding** -- auto-detect framework, scan, and first evidence in one command
- **Auto-approve tools** -- `delimit setup` configures permissions for Claude Code, Codex, and Gemini CLI

### v4.0

- **Toolcard Delta Cache** -- SHA256 schema hashing, delta-only transmission, saves tokens
- **Session Phoenix** -- cross-model session resurrection with soul capture
- **Handoff Receipts** -- structured acknowledgment protocol between agents
- **Cross-Model Audit** -- 3 lenses (security, correctness, governance) with deterministic synthesis
- **4-model deliberation** -- Claude + Grok + Gemini + Codex debate until consensus
- **Universal Swarm Triggers** -- "Think and Build", "Keep building", "Ask Delimit"
- **Full governance toolkit** -- lint, diff, policy, evidence, drift, attestation, and swarm orchestration exposed as MCP tools and CLI subcommands

---

## GitHub Action

Zero-config -- auto-detects your OpenAPI spec:

```yaml
- uses: delimit-ai/delimit-action@v1
```

Or with full configuration:

```yaml
name: API Contract Check
on: pull_request

jobs:
  delimit:
    runs-on: ubuntu-latest
    permissions:
      pull-requests: write
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: delimit-ai/delimit-action@v1
        with:
          spec: api/openapi.yaml
```

That's it. Delimit auto-fetches the base branch spec, diffs it, and posts a PR comment with breaking changes, semver classification, migration guides, and governance gate results.

[View on GitHub Marketplace](https://github.com/marketplace/actions/delimit-api-governance) | [See a live demo (23 breaking changes)](https://github.com/delimit-ai/delimit-action-demo/pull/2)

### Example PR comment

> **Breaking Changes Detected**
>
> | Change | Path | Severity |
> |--------|------|----------|
> | endpoint_removed | `DELETE /pets/{petId}` | error |
> | type_changed | `/pets:GET:200[].id` (string -> integer) | warning |
> | enum_value_removed | `/pets:GET:200[].status` | warning |
>
> **Semver**: MAJOR (1.0.0 -> 2.0.0)
>
> **Migration Guide**: 3 steps to update your integration
>
> ### Governance Gates
> | Gate | Status | Chain |
> |------|--------|-------|
> | API Lint | Pass/Fail | lint -> semver -> gov_evaluate |
> | Policy Compliance | Pass/Fail | policy -> evidence_collect |
> | Security Audit | Pass | security_audit -> evidence_collect |
> | Deploy Readiness | Ready/Blocked | deploy_plan -> security_audit |

---

## CLI commands

```bash
npx delimit-cli scan                             # Instant spec health grade + recommendations
npx delimit-cli pr owner/repo#123                # Review any GitHub PR for breaking changes
npx delimit-cli quickstart                       # Clone demo project + guided walkthrough
npx delimit-cli try                              # Zero-risk demo — saves governance report
npx delimit-cli demo                             # Self-contained governance demo
npx delimit-cli init                             # Guided wizard with compliance templates
npx delimit-cli init --preset strict             # Initialize with strict policy
npx delimit-cli setup                            # Install into all AI assistants
npx delimit-cli setup --dry-run                  # Preview changes first
npx delimit-cli lint api/openapi.yaml            # Check for breaking changes
npx delimit-cli diff old.yaml new.yaml           # Compare two specs
npx delimit-cli explain old.yaml new.yaml        # Generate migration guide
npx delimit-cli check                            # Pre-commit governance check
npx delimit-cli check --staged --fix             # Check staged files + show guidance
npx delimit-cli hooks install                    # Install git pre-commit hook
npx delimit-cli hooks install --pre-push         # Also add pre-push hook
npx delimit-cli ci                               # Generate GitHub Action workflow
npx delimit-cli ci --strict --dry-run            # Preview strict workflow
npx delimit-cli remember "Redis uses JWT 15min"   # Save a persistent memory
npx delimit-cli recall redis                     # Search memories
npx delimit-cli recall                           # Show recent memories
npx delimit-cli recall --tag deploy --all        # Filter by tag, show all
npx delimit-cli recall --export                  # Export as markdown
npx delimit-cli forget abc123                    # Delete a memory by ID
npx delimit-cli models                            # Configure deliberation API keys (BYOK wizard)
npx delimit-cli models --status                   # Show current model config
npx delimit-cli status                           # Compact dashboard of your Delimit setup
npx delimit-cli doctor                           # Check setup health
npx delimit-cli uninstall --dry-run              # Preview removal
npx delimit-cli wrap -- claude -p "..."          # Gate any AI-assisted CLI + signed attestation (v4.3)
npx delimit-cli wrap --max-time 60 -- codex "..."# With kill switch + handoff on timeout
npx delimit-cli trust-page -o ./trust            # Render attestations into a static trust page
npx delimit-cli ai-sbom -o ./ai-sbom.json        # Build a CycloneDX-AI bill of materials
```

### What the MCP toolkit adds

When installed into your AI coding assistant, Delimit provides tools across two tiers:

#### Free (no account needed)

- **API governance** -- lint, diff, policy enforcement, semver classification
- **Persistent ledger** -- track tasks across sessions, shared between all AI assistants
- **Zero-spec extraction** -- generate OpenAPI specs from FastAPI, Express, or NestJS source
- **Project scan** -- auto-detect specs, frameworks, security issues, and tests
- **Quickstart** -- guided first-run that proves value in 60 seconds

#### Pro

- **Multi-model deliberation** -- AI models debate until they agree (free: Gemini Flash + GPT-4o-mini; BYOK: any models)
- **Security audit** -- dependency scanning, secret detection, SAST analysis
- **Test verification** -- confirms tests ran, measures coverage, generates new tests
- **Memory & vault** -- persistent context and encrypted secrets across sessions
- **Evidence collection** -- governance audit trail for compliance
- **Deploy pipeline** -- governed build, publish, and rollback
- **OS layer** -- agent identity, execution plans, approval gates

---

## What It Detects

27 change types (17 breaking, 10 non-breaking) -- deterministic rules, not AI inference. Same input always produces the same result.

### Breaking Changes

| # | Change Type | Example |
|---|-------------|---------|
| 1 | `endpoint_removed` | `DELETE /users/{id}` removed entirely |
| 2 | `method_removed` | `PATCH /orders` no longer exists |
| 3 | `required_param_added` | New required header on `GET /items` |
| 4 | `param_removed` | `sort` query parameter removed |
| 5 | `response_removed` | `200 OK` response dropped |
| 6 | `required_field_added` | Request body now requires `tenant_id` |
| 7 | `field_removed` | `email` dropped from response object |
| 8 | `type_changed` | `id` went from `string` to `integer` |
| 9 | `format_changed` | `date-time` changed to `date` |
| 10 | `enum_value_removed` | `status: "pending"` no longer valid |
| 11 | `param_type_changed` | Query param `limit` changed from `integer` to `string` |
| 12 | `param_required_changed` | `filter` param became required |
| 13 | `response_type_changed` | Response `data` changed from `array` to `object` |
| 14 | `security_removed` | OAuth2 security scheme removed |
| 15 | `security_scope_removed` | `write:pets` scope removed from OAuth2 |
| 16 | `max_length_decreased` | `name` maxLength reduced from 255 to 100 |
| 17 | `min_length_increased` | `code` minLength increased from 1 to 5 |

### Non-Breaking Changes

| # | Change Type | Example |
|---|-------------|---------|
| 18 | `endpoint_added` | New `POST /webhooks` endpoint |
| 19 | `method_added` | `PATCH /users/{id}` method added |
| 20 | `optional_param_added` | Optional `format` query param added |
| 21 | `response_added` | `201 Created` response added |
| 22 | `optional_field_added` | Optional `nickname` field added to response |
| 23 | `enum_value_added` | `status: "archived"` value added |
| 24 | `description_changed` | Updated description for `/health` endpoint |
| 25 | `security_added` | API key security scheme added |
| 26 | `deprecated_added` | `GET /v1/users` marked as deprecated |
| 27 | `default_changed` | Default value for `page_size` changed from 10 to 20 |

---

## Policy presets

```bash
npx delimit-cli init --preset strict    # All violations are errors
npx delimit-cli init --preset default   # Balanced (default)
npx delimit-cli init --preset relaxed   # All violations are warnings
```

Or write custom rules in `.delimit/policies.yml`:

```yaml
rules:
  - id: freeze_v1
    name: Freeze V1 API
    change_types: [endpoint_removed, method_removed, field_removed]
    severity: error
    action: forbid
    conditions:
      path_pattern: "^/v1/.*"
    message: "V1 API is frozen. Changes must be made in V2."
```

---

## Supported formats

- OpenAPI 3.0 and 3.1
- Swagger 2.0
- YAML and JSON

---

## FAQ

**How does this compare to Obsidian Mind?**

Obsidian Mind is a great Obsidian vault template for Claude Code users who want persistent memory via markdown files. Delimit takes a different approach: it's an MCP server that works across Claude Code, Codex, Gemini CLI, and Cursor. Your memory, ledger, and governance travel with you when you switch models. Delimit also adds API governance (27-type breaking change detection), CI gates, git hooks, and policy enforcement that Obsidian Mind doesn't cover. Use Obsidian Mind if you're all-in on Claude + Obsidian. Use Delimit if you switch between models or need governance.

**Does this work without Claude Code?**

Yes. Delimit works with Claude Code, Codex (OpenAI), Gemini CLI (Google), and Cursor. The `remember`/`recall` commands work standalone with zero config. The MCP server integrates with any client that supports the Model Context Protocol.

**Is this free?**

The free tier includes API governance, persistent memory, zero-spec extraction, project scanning, and 3 multi-model deliberations. Pro ($10/mo) adds unlimited deliberation, security audit, test verification, deploy pipeline, and agent orchestration.

---

## Telemetry & cloud sync

**Short version: none by default.** Nothing leaves your machine unless you explicitly configure it.

**What's always local (source of truth):**
- `~/.delimit/events/events-YYYY-MM-DD.jsonl` — per-tool-call events (tool name, timestamp, status, model id, session id, trace id). No source code, no prompts, no responses.
- `~/.delimit/ledger/` — your ledger items, work orders, deliberation transcripts.
- `~/.delimit/attestations/` — `delimit wrap` output bundles.

**What's OPT-IN (requires you to provide your own Supabase project credentials):**
- `gateway/ai/supabase_sync.py` mirrors the local event + ledger + work-order + deliberation rows into a Supabase project *you own* so you can view them in `app.delimit.ai`. **It only activates if you set `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` environment variables OR provide `~/.delimit/secrets/supabase.json` with those credentials.** No URL or key is hardcoded in the published package (verify with `grep -r aqbdqxnhzqzswdxifksc $(npm root -g)/delimit-cli/` — zero hits).
- Data scope when enabled: metadata only (tool names, timestamps, IDs, statuses, venture tags). Never source code, prompts, or model responses.

**Kill switch:**
Set `DELIMIT_DISABLE_CLOUD_SYNC=1` in your environment to force all sync operations to no-op even if credentials are present. Local files continue to work normally.

```bash
# Disable cloud sync for a single invocation
DELIMIT_DISABLE_CLOUD_SYNC=1 delimit lint api/openapi.yaml

# Disable for the shell session
export DELIMIT_DISABLE_CLOUD_SYNC=1
```

**Webhook notifications:**
`gateway/ai/notify.py` emits governance events to a webhook endpoint *only if* you configure `DELIMIT_WEBHOOK_URL` explicitly. Unset by default.

If you spot another code path that could phone home without disclosure, file an issue. This section is maintained as ship-truth, not aspirational.

---

## Links

- [delimit.ai](https://delimit.ai) -- homepage
- [Dashboard](https://app.delimit.ai) -- governance console
- [Docs](https://delimit.ai/docs) -- full documentation
- [GitHub Action](https://github.com/marketplace/actions/delimit-api-governance) -- Marketplace listing
- [Quickstart](https://github.com/delimit-ai/delimit-mcp-server) -- try it in 2 minutes
- [npm](https://www.npmjs.com/package/delimit-cli) -- CLI package
- [Pricing](https://delimit.ai/pricing) -- free tier + Pro

MIT License