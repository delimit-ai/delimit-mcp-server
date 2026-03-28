# `</>` Delimit

API governance that enforces itself. One workspace for Claude Code, Codex, Cursor, and Gemini CLI.

[![npm](https://img.shields.io/npm/v/delimit-cli)](https://www.npmjs.com/package/delimit-cli)
[![GitHub Action](https://img.shields.io/badge/GitHub%20Action-v1.8.0-blue)](https://github.com/marketplace/actions/delimit-api-governance)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Glama](https://glama.ai/mcp/servers/delimit-ai/delimit-mcp-server/badges/score.svg)](https://glama.ai/mcp/servers/delimit-ai/delimit-mcp-server)
[![API Governance](https://delimit-ai.github.io/badge/pass.svg)](https://github.com/marketplace/actions/delimit-api-governance)

<p align="center">
  <img src="docs/demo.gif" alt="Delimit detecting breaking API changes" width="700">
</p>

---

## Try it in 2 minutes

```bash
npx delimit-cli demo     # See governance in action — no setup needed
npx delimit-cli init     # Set up governance for your project
npx delimit-cli setup    # Configure your AI assistants
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

## What's New in v3.14

- **Evidence timeline + gate status dashboard** -- see every governance decision and when it happened
- **Multi-agent orchestration** -- track which AI assistant works on what, across models
- **Continuous drift monitoring** -- detect spec drift between deployments automatically
- **PR governance copilot** -- gate status posted directly in PR comments
- **Compliance templates** -- SOC2, PCI-DSS, and HIPAA presets out of the box
- **Beta capture in CLI** -- opt into early features from the command line
- **112+ MCP tools** -- governance, context, shipping, observability, and orchestration

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
npx delimit-cli demo                             # Self-contained governance demo
npx delimit-cli init                             # Guided wizard with compliance templates
npx delimit-cli init --preset strict             # Initialize with strict policy
npx delimit-cli setup                            # Install into all AI assistants
npx delimit-cli setup --dry-run                  # Preview changes first
npx delimit-cli lint api/openapi.yaml            # Check for breaking changes
npx delimit-cli diff old.yaml new.yaml           # Compare two specs
npx delimit-cli explain old.yaml new.yaml        # Generate migration guide
npx delimit-cli doctor                           # Check setup health
npx delimit-cli uninstall --dry-run              # Preview removal
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

- **Multi-model deliberation** -- Grok, Gemini, and Codex debate until they agree
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

## Links

- [delimit.ai](https://delimit.ai) -- homepage
- [Dashboard](https://app.delimit.ai) -- governance console
- [Docs](https://delimit.ai/docs) -- full documentation
- [GitHub Action](https://github.com/marketplace/actions/delimit-api-governance) -- Marketplace listing
- [Quickstart](https://github.com/delimit-ai/delimit-mcp-server) -- try it in 2 minutes
- [npm](https://www.npmjs.com/package/delimit-cli) -- CLI package
- [Pricing](https://delimit.ai/pricing) -- free tier + Pro

MIT License
