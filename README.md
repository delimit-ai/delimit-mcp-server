# `</>` Delimit

**The merge gate for AI-written code, with signed, replayable attestation.**

Wrap any AI coding assistant (Claude Code, Codex, Cursor, Gemini CLI) with a governance chain that runs your gates, records what changed, and signs a replayable receipt for every merge.

[![npm](https://img.shields.io/npm/v/delimit-cli)](https://www.npmjs.com/package/delimit-cli)
[![Tests](https://img.shields.io/badge/tests-4800%2B%20passing-brightgreen)](https://github.com/delimit-ai/delimit-mcp-server)
[![GitHub Action](https://img.shields.io/badge/GitHub%20Action-latest-blue)](https://github.com/marketplace/actions/delimit-api-governance)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Glama Score](https://glama.ai/mcp/servers/delimit-ai/delimit-mcp-server/badges/score.svg)](https://glama.ai/mcp/servers/delimit-ai/delimit-mcp-server/score)

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
  <a href="https://delimit.ai/methodology/mcp-attestation">Methodology</a> · <a href="https://delimit.ai/reports/cal-com-v2-attestation">cal.com v2 worked example</a> · <a href="https://delimit.ai/docs/workflow">Workflow guide</a> · <a href="https://delimit.ai">Website</a>
</p>

---

## Worked examples

Real, reproducible merge-gate runs against public API specs:

- **[OpenAI OpenAPI: a year of AI frontier evolution under a cross-vendor merge gate](https://delimit.ai/reports/openai-openapi-attestation)** — OpenAI (openai/openai-openapi)
- **[Stripe v1 OpenAPI: 57 days under a merge gate](https://delimit.ai/reports/stripe-openapi-attestation)** — Stripe (stripe/openapi)
- **[Anthropic API: 76 days under a cross-vendor merge gate](https://delimit.ai/reports/anthropic-api-attestation)** — Anthropic (anthropics/anthropic-sdk-python)
- **[Twilio v2010 OpenAPI: 55 days under a merge gate](https://delimit.ai/reports/twilio-api-attestation)** — Twilio (twilio/twilio-oai)
- **[Docusign eSignature v2.1 OpenAPI: 46 days under a merge gate](https://delimit.ai/reports/docusign-esign-attestation)** — Docusign (docusign/OpenAPI-Specifications)
- **[Supabase Auth OpenAPI: 57 days under a merge gate](https://delimit.ai/reports/supabase-auth-openapi-attestation)** — Supabase Auth (supabase/auth)
- **[cal.com v2 OpenAPI: 60 days under a merge gate](https://delimit.ai/reports/cal-com-v2-attestation)** — cal.com (calcom/cal.com)
- **[EU TED v3 procurement API: $ref'd component-schema drift under a merge gate](https://delimit.ai/reports/eu-ted-v3-attestation)** — European Commission (TED v3 Public API)
- **[Cross-agent handoff: one artifact, four CLIs](https://delimit.ai/reports/cross-agent-handoff)** — Cross-CLI session handoff (worked example)
- **[delimit-mcp-server (self-attestation): same merge gate, third artifact class](https://delimit.ai/reports/delimit-mcp-server-tdqs)** — delimit-mcp-server (self-attestation)

See the full index at **[delimit.ai/reports](https://delimit.ai/reports)**. For the schema and signing methodology behind every report, see **[delimit.ai/methodology/mcp-attestation](https://delimit.ai/methodology/mcp-attestation)**.

---

## Golden Path — your first 10 minutes

The merge gate for AI-written code, end to end: lint a spec, see exactly what breaks, classify the bump, settle the hard calls with multiple models, and walk away with a signed, replayable attestation. Then keep the context that survives across sessions and models.

### 1. Install

```bash
npx delimit-cli scan          # discovery: finds your OpenAPI specs, frameworks, security issues, tests
npx delimit-cli init          # wire up the merge-gate config (--preset strict | default | relaxed)
```

`scan` (`delimit_scan`) reports what Delimit can do for this repo. `init` (`delimit_init`) drops in the policy preset and merge-gate config. No account, no keys.

### 2. The merge gate, end to end

**Lint the spec change (the gate).** Baseline vs. proposed, with policy applied — one pass/fail verdict.

```bash
npx delimit-cli lint old.yaml new.yaml       # tool: delimit_lint
```

**See exactly what breaks.** Pure structural diff — added/removed/modified endpoints, schemas, params, no policy.

```bash
npx delimit-cli diff old.yaml new.yaml       # tool: delimit_diff
```

**Classify the bump.** Deterministic MAJOR/MINOR/PATCH/NONE — same input, same answer, every time.

```text
delimit_semver       → MAJOR/MINOR/PATCH/NONE + next version string
delimit_impact       → blast radius: scans your dependency manifest for downstream callers (informational)
```

**Settle the hard calls.** When the gate verdict is a judgment call, put it to multiple models and let them debate to consensus.

```bash
npx delimit-cli deliberate "Is dropping the deprecated v1 /users field a safe MINOR?"
#   tool: delimit_deliberate — 3 free, then bring your own key
```

**Capture the signed, replayable attestation.** After a gate event (deploy / security / test / audit), record the evidence bundle and verify it any time.

```text
delimit_evidence_collect   → signed evidence bundle for the audit trail        (Pro)
delimit_evidence_verify    → confirm a bundle hasn't been tampered with         (Pro)
delimit_seal_verify        → check a Delimit Seal receipt against its bundled   (Free)
                             Layer-0 constitution — offline-verifiable
```

Every receipt is offline-verifiable: `npx delimit-cli seal-verify <receipt.json>`, or open its `delimit.ai/att/<id>` replay URL.

### 3. Context that survives sessions and models

Decisions, constraints, and tasks persist across sessions and across AI assistants — switch from Claude Code to Codex, Cursor, or Gemini CLI without losing the thread.

**Memory** — persist and recall the *why*, not just the diff.

```bash
npx delimit-cli remember "v1 /users field is frozen until Q3 — downstream billing depends on it"
#   tool: delimit_memory_store
npx delimit-cli recall billing               # local recall over your saved memories (Free)
```

For semantic recall by meaning across sessions, the assistant calls `delimit_memory_search` (Pro) directly.

**Ledger** — one task list, shared across every assistant and session.

```text
delimit_ledger_add        → record a task/bug/feature/strategic item
delimit_ledger_context    → session-start: top open items by priority (what's queued)
delimit_ledger_done       → close with a note (auto-captures a PR URL as ship proof)
```

That's the loop: gate the change, sign the proof, keep the context. Run it once on a real spec and you've used the whole merge gate.

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

## What's New

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

## Earlier releases

*The highest state of AI governance — earlier features still active.*

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

## Adopt with minimum privilege

You don't have to trust a large tool surface on day one. The safe on-ramp:

**Phase 1 — read-only governance (free, no account).** Start with the tools that
only read your repo and write reports: `delimit_lint`, `delimit_diff`,
`delimit_semver`, `delimit_policy`, `delimit_explain`, `delimit_scan`, and
`delimit_seal_verify`. If your MCP client supports per-tool allowlists, grant
exactly those. Nothing in this set executes, deploys, or posts anywhere.

**Phase 2 — opt into side effects deliberately.** Tools that write evidence
bundles, open PR comments, or run deploys (`delimit_security_audit`,
`delimit_deploy_*`, agent orchestration) are tier-gated; enable them once
phase 1 has earned its keep in your CI.

**Pin the Action to a commit SHA.** `@v1` is a floating tag. For
supply-chain-sensitive pipelines, pin the exact commit and bump on review:

```yaml
- uses: delimit-ai/delimit-action@<commit-sha>   # gh api repos/delimit-ai/delimit-action/git/refs/tags/v1
```

**Keep BYOK keys out of plaintext config.** If you bring your own model keys
for deliberation, store them with `delimit_secret_store` (encrypted vault,
access-logged via `delimit_secret_access_log`) rather than in dotfiles.

Our own releases ship under the same discipline: every release carries a
signed, replayable Seal receipt (see the latest
[release assets](https://github.com/delimit-ai/delimit-mcp-server/releases) —
verify with `npx delimit-cli seal-verify <receipt.json>` or at its
`delimit.ai/att/<id>` replay URL), plus SLSA provenance on npm.

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
npx delimit-cli wrap -- claude -p "..."          # Gate any AI-assisted CLI + signed attestation
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

28 change types (17 breaking, 11 non-breaking) -- deterministic rules, not AI inference. Same input always produces the same result.

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
| 28 | `field_requirement_relaxed` | Required field `nickname` became optional (context-aware severity) |

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

Obsidian Mind is a great Obsidian vault template for Claude Code users who want persistent memory via markdown files. Delimit takes a different approach: it's an MCP server that works across Claude Code, Codex, Gemini CLI, and Cursor. Your memory, ledger, and governance travel with you when you switch models. Delimit also adds API governance (28-type breaking change detection), CI gates, git hooks, and policy enforcement that Obsidian Mind doesn't cover. Use Obsidian Mind if you're all-in on Claude + Obsidian. Use Delimit if you switch between models or need governance.

**Does this work without Claude Code?**

Yes. Delimit works with Claude Code, Codex (OpenAI), Gemini CLI (Google), and Cursor. The `remember`/`recall` commands work standalone with zero config. The MCP server integrates with any client that supports the Model Context Protocol.

**Is this free?**

The free tier includes API governance, persistent memory, zero-spec extraction, project scanning, and 3 multi-model deliberations. Pro ($10/mo) adds unlimited deliberation, security audit, test verification, deploy pipeline, and agent orchestration. Premium ($50-100/mo) adds priority support and team features. Enterprise is custom: see [delimit.ai/pricing](https://delimit.ai/pricing).

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
