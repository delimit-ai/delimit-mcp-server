# `</>` Delimit

**Keep the state. Change the model.**

Keep decisions, records, and handoffs across Claude Code, Codex, Cursor, Antigravity, GitHub Copilot, and Muse. Bring configured models together for hard calls. For code repositories, Delimit also offers a merge gate and local signed attestations.

[![npm](https://img.shields.io/npm/v/delimit-cli)](https://www.npmjs.com/package/delimit-cli)
[![GitHub Action](https://img.shields.io/badge/GitHub%20Action-latest-blue)](https://github.com/marketplace/actions/delimit-merge-gate-for-ai-written-code)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Glama Score](https://glama.ai/mcp/servers/delimit-ai/delimit-mcp-server/badges/score.svg)](https://glama.ai/mcp/servers/delimit-ai/delimit-mcp-server/score)

Glama directory grades: License A · Quality A · Maintenance A. Observed September 15, 2026, for this public repository. [View the listing](https://glama.ai/mcp/servers/delimit-ai/delimit-mcp-server).

```console
$ delimit wrap -- claude "fix the flaky test in tests/api.spec.ts"

✓ repo_diagnose
✓ security_audit       0 critical · 0 secrets
✓ test_smoke           165/165
✓ changed_files        1
✓ attestation signed   att_a05050eb8e13277e
                       delimit.attestation.v1 · HMAC-SHA256
                       receipt → .delimit/attestations/att_a05050eb8e13277e.json
```

Every wrapped run emits a local `delimit.attestation.v1` bundle: repo head before/after, changed files, gate results, and an HMAC-SHA256 signature. Verify it offline with `delimit seal-verify`; hosted replay is not available yet. Advisory by default; flip to enforcing when you're ready.

---

## Fastest path to value: `delimit check`

Zero-config PR safety gate. No `init`, no setup, no account, no keys — run it on any repo and it applies deterministic checks (breaking API changes + leaked secrets) to your staged or modified files.

```bash
npx delimit-cli check
```

```console
$ delimit check

  Delimit Check

  Policy: default | Base: HEAD | Specs: 1

  + api/openapi.yaml — clean

  PASSED — no breaking changes, no leaked secrets
```

When a check finds a breaking change or a leaked secret, it prints the offending file and the issue and exits non-zero — so it drops straight into a pre-commit hook or CI step.

Common options:

```bash
delimit check --staged          # only check staged files
delimit check --base main       # compare against a git ref (default HEAD)
delimit check --fix             # show migration guidance for violations
delimit check --record          # write a content-pinned record of this check
```

`--record` writes a content-pinned record of the check to `.delimit/records/check-<ts>.json` (or a path you name). That record is the precursor to the signed, replayable Seal attestation — the same evidence shape, pinned to the exact content you checked.

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
#   tool: delimit_deliberate — uses your configured model CLIs or API keys
```

`delimit setup` gives the Codex MCP server a 30-minute tool timeout so a
multi-round panel can return its completed transcript instead of being detached
by the client while its models are still responding.

**Capture the signed, replayable attestation.** After a gate event (deploy / security / test / audit), record the evidence bundle and verify it any time.

```text
delimit_evidence_collect   → signed evidence bundle for the audit trail        (Pro)
delimit_evidence_verify    → confirm a bundle hasn't been tampered with         (Pro)
delimit_seal_verify        → check a Delimit Seal receipt against its bundled   (Free)
                             Layer-0 constitution — offline-verifiable
```

Every receipt is offline-verifiable with `npx delimit-cli seal-verify <receipt.json>`. Hosted receipt replay is not available yet.

### 3. Context that survives sessions and models

Decisions, constraints, and tasks persist across sessions. Continue in Claude Code, Codex, Cursor, Antigravity, GitHub Copilot, or Muse with the saved record.

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

### Lead chat, Copilot CLI and Muse Code sessions

Install and authenticate the official client separately, under the same OS user
who runs Delimit. Delimit does not install these clients, purchase a plan, log you
in, or verify your subscription allowance. The interactive lead order is
Claude → Codex → Antigravity → Muse → Copilot, subject to existing configuration,
availability and native-client checks. This changes only lead chat, not review
panels or background routing. Existing paid API fallback remains opt-in.

```bash
npx delimit-cli chat
npx delimit-cli chat --model muse
npx delimit-cli chat --model copilot
npx delimit-cli chat --project /path/to/repository --model muse
```

You can launch from your home directory without changing directories. That is
an unscoped owner workspace: Delimit loads workspace instructions but does not
infer a venture or restore the newest unrelated handoff. A stale active-venture
pointer is not selected. Use `--project` to bind an existing Git repository;
missing or invalid project paths fail before launching a model. Root/workspace
exit does not claim a project-bound capture. Recover or save continuity against
the actual task scope through existing Delimit tools.

Muse and Copilot are native interactive harnesses, not new model votes or
automatic build-loop backends. An explicit `--model muse` or `--model copilot`
stays on that selection without probing or silently switching providers.
In the default lead chain a failed native launch can advance to the next lead;
an intentional interrupt exits. No successful handoff is inferred from a failed
or unavailable capture. Native permission approvals remain active. Copilot launches
with automatic updates and remote/export features disabled for this session.

**Muse preparation:** first verify that your account permits Standard
`muse-spark-1.3` on the intended billing route. Back up your native
`$XDG_CONFIG_HOME/muse/settings.json` (or `~/.config/muse/settings.json` when
`XDG_CONFIG_HOME` is unset), and merge these fields into its existing JSON object;
preserve unrelated settings and authentication:

```json
{
  "model": "muse-spark-1.3",
  "context": {
    "foreign_personal_rules": false,
    "foreign_personal_skills": false
  }
}
```

The explicit Muse launch requires that selection and exclusion of foreign
personal rules/skills; it does not change native settings for you. A configured
provider must be `meta`, and a configured endpoint must be
`https://api.meta.ai/v1`. `META_API_KEY` or `MODEL_API_KEY` environment overrides
block this launch until you resolve the intended billing route. Do not switch
credentials or models merely to bypass that check. Standard selection is not
proof of zero retention, subscription billing, or private-data clearance.

Current binary discovery supports Copilot in `~/.local/bin`, `/usr/local/bin`,
or `/usr/bin`, and Muse's versioned `~/.local/bin/muse-bin-<version>-R<revision>`
distribution. Muse uses that executable directly, not its updater launcher.
Other installation layouts are not automatically discovered. The native Muse
context settings were checked with Muse Code 1.2.1; recheck compatibility when
upgrading the client.

Delimit supplies a compact project-bound startup instruction, not an automatic
import of your ledger, memory, or complete conversation. Optional local guidance
can live in `$DELIMIT_HOME/harness-bootstrap.md` (default
`~/.delimit/harness-bootstrap.md`); the combined startup text must fit within
16 KiB. Project `AGENTS.md`/`CLAUDE.md` files at or above 64 KiB are rejected
instead of silently truncated. Recover and save a scoped handoff through your
available Delimit tools. These launchers do **not** automatically register a
native MCP connection or prove cross-harness continuity.

When both project instruction files exist, the startup packet lists both files
with their sizes and hashes and tells the harness to read them completely,
including guidance hidden by native file precedence. It does not copy their
contents into the startup packet, silently merge files, or override the existing
instruction hierarchy. User-global guidance remains opt-in through the local
bootstrap file; Delimit never copies a different client's private rules by default.
This manifest is a loading instruction, not proof that the model performed every
read. If required guidance is inaccessible or conflicts, resolve that gap before
allowing edits. Oversized native project files still fail closed without rewriting
the originals.

Optional command shims can be installed with `delimit setup --harness-shims`.
Ordinary setup only refreshes already-managed Muse/Copilot shims; it does not
newly shadow your native commands. Custom shims are preserved. With the opt-in,
a conflicting custom file stops installation of both additional shims.

For existing launcher-managed continuity, clean exit finalizes only a bound
handoff. If that is unavailable, Delimit reports the specific reason and writes
a private local diagnostic receipt under its `sessions/` directory. That receipt
is not a conversation backup. Existing handoffs are not replaced by empty ones;
resume the exact native session and explicitly save a project-bound handoff.

---

## Think and Build

`delimit think` runs a multi-model deliberation using available configured providers. `delimit build` starts a detached planning worker that inspects the repository and writes briefs; it does not execute ledger tasks. `delimit vault` stores secrets as plaintext JSON in owner-only (0600) files.

Works across any configuration, from a single model on a budget to a full panel.

---

## Try it in 2 minutes

```bash
npx delimit-cli doctor            # Checks your local setup and suggests fixes
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

- **`delimit wrap`** — pipe `claude -p`, `cursor`, `aider`, `codex`, or any AI-assisted CLI through a signed governance gate. Snapshots the git diff before/after, runs lint + tests, HMAC-signs an `att_*` attestation, and writes the receipt locally for offline verification. Advisory by default; `--enforce` blocks CI on policy violations; `--max-time <s>` is a kill switch that tags the attestation as a `liability_incident` and prints a cross-model handoff command.
- **`delimit trust-page`** — renders a directory of attestations into a static HTML trust page + JSON Feed 1.1 feed. Single file, no framework, offline-renderable. Deploy anywhere.
- **`delimit ai-sbom`** — aggregates attestations into a CycloneDX 1.6 bill-of-materials with AI-specific fields (detected models per vendor, tool-call surface, policy gate counts). Pipe straight into procurement.
- **Cross-model by construction** — `wrap` uses the same local attestation schema across supported command-line producers. Delimit keeps session context across Claude Code, Codex, Cursor, Antigravity, GitHub Copilot, and Muse.

```bash
# Gate any AI-assisted CLI
delimit wrap -- claude -p "add tests for payments"
#   → att_7d556843c84fb881 signed, receipt written locally

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

- **`delimit doctor`** -- checks your local setup and suggests fixes. Supports `--ci` for pipelines and `--fix` for supported repairs.
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

Deliberations use your configured model CLIs or API keys. Provider availability depends on your setup.

### v4.18

- **MCP Registry record published on every release** -- the official registry entry now tracks the npm version automatically. Publisher pinned and checksum-verified; the tag gate asserts that both `server.json` version fields match the package version.
- **Tool descriptions** -- 16 previously undocumented parameters documented from their signatures; 26 Pro-gated tools state the prerequisite and the unlicensed-call response.
- **Release guards** -- bundle-classification guard no longer flakes on SIGPIPE.

### v4.17

- **Cross-model continuity on fresh installs** -- `delimit_soul_capture` / `delimit_revive` work without any internal modules via the public `session_continuity` module; captured souls carry deterministic provenance (venture, transcript identity, capture key) for reliable resume across model switches. Existing soul files load unchanged.
- **Security audit** -- fewer false positives on test fixtures and documentation dummies; suppressed findings are reported, never silently dropped.

### v4.16

- **Fail-closed bundle allowlist** -- the npm package ships only explicitly reviewed gateway files; two CI guards block any unclassified or non-allowlisted file from entering the tarball.
- **Session-end auto-capture** installs in the settings shape Claude Code expects, so end-of-session handoff fires instead of being silently dropped.
- **Always-on commit-author audit** workflow catches identity drift on the repo.

### v4.1

- **TUI** -- terminal-native Ventures panel, real `delimit think` and `delimit build` commands
- **Security hardening** -- notify.py stubbed in npm, axios pinned against supply chain attacks
- **Free tier restructure** -- deliberations use Gemini Flash + GPT-4o-mini (cost: <$20/mo)
- **Zero-config onboarding** -- auto-detect framework, scan, and first evidence in one command
- **Assistant permissions** -- `delimit setup` configures MCP access for Claude Code, Codex, Cursor, and Antigravity and sets approval defaults for some clients; review these settings for your environment

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

[View on GitHub Marketplace](https://github.com/marketplace/actions/delimit-merge-gate-for-ai-written-code) | [See a live demo (23 breaking changes)](https://github.com/delimit-ai/delimit-action-demo/pull/2)

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
verify with `npx delimit-cli seal-verify <receipt.json>`), plus SLSA
provenance on npm.

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
- **Memory & vault** -- persistent context and local plaintext secret files across sessions
- **Evidence collection** -- governance audit trail for compliance
- **Deploy pipeline** -- governed build, publish, and rollback
- **OS layer** -- agent identity, execution plans, approval gates

---

## What It Detects

Delimit checks OpenAPI and JSON Schema changes using deterministic rules for the cases each surface supports. Examples include removed endpoints, newly required parameters, and changed response fields. Review the result alongside your contract; rule coverage differs between the CLI and GitHub Action.

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

Obsidian Mind is a great Obsidian vault template for Claude Code users who want persistent memory via markdown files. Delimit takes a different approach: it keeps a shared record across Claude Code, Codex, Cursor, Antigravity, GitHub Copilot, and Muse. Your memory, ledger, and governance travel with you when you switch models. Delimit also adds API governance (28-type breaking change detection), CI gates, git hooks, and policy enforcement that Obsidian Mind doesn't cover. Use Obsidian Mind if you're all-in on Claude + Obsidian. Use Delimit if you switch between models or need governance.

**Does this work without Claude Code?**

Yes. Delimit supports Claude Code, Codex, Cursor, Antigravity, GitHub Copilot, and Muse. The `remember`/`recall` commands work standalone. The MCP server integrates with clients that support the Model Context Protocol.

**Is this free?**

Delimit lists Free, Pro, and Enterprise plans. Multi-model deliberation uses your configured model CLIs or API keys. See [current plan details](https://delimit.ai/pricing).

---

## Local data and network calls

Delimit stores its ledger, records, and local attestations on your machine. Some setup and session paths make network requests by default:

- Setup checks for a newer CLI release and can download the Pro module tarball.
- The Claude Code SessionStart hook and installed shims can check for and install updates.
- License activation contacts the license provider and sends your customer email, license ID, and machine hash to delimit.ai.
- Multi-model deliberation sends your prompt and relevant context to the model providers you configure, through your CLI logins or API keys.

`gateway/ai/supabase_sync.py` is not bundled with this package. Webhook notifications are sent only when `DELIMIT_WEBHOOK_URL` is configured.

---

## Links

- [delimit.ai](https://delimit.ai) -- homepage
- [Dashboard](https://app.delimit.ai) -- governance console
- [Docs](https://delimit.ai/docs) -- full documentation
- [GitHub Action](https://github.com/marketplace/actions/delimit-merge-gate-for-ai-written-code) -- Marketplace listing
- [Quickstart](https://github.com/delimit-ai/delimit-mcp-server) -- try it in 2 minutes
- [npm](https://www.npmjs.com/package/delimit-cli) -- CLI package
- [Pricing](https://delimit.ai/pricing) -- free tier + Pro

MIT License
