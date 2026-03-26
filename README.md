# `</>` Delimit

Unify Claude Code, Codex, Cursor, and Gemini CLI with persistent context, governance, and multi-model debate.

[![npm](https://img.shields.io/npm/v/delimit-cli)](https://www.npmjs.com/package/delimit-cli)
[![GitHub Action](https://img.shields.io/badge/GitHub%20Action-v1.6.0-blue)](https://github.com/marketplace/actions/delimit-api-governance)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Glama](https://glama.ai/mcp/servers/delimit-ai/delimit-mcp-server/badges/score.svg)](https://glama.ai/mcp/servers/delimit-ai/delimit-mcp-server)

Persistent ledger, API governance, security orchestration, and multi-model deliberation — all shared across Claude Code, Codex, Cursor, and Gemini CLI.

---

## GitHub Action

Zero-config — auto-detects your OpenAPI spec:

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

That's it. Delimit auto-fetches the base branch spec, diffs it, and posts a PR comment with:

- Breaking changes with severity badges
- Semver classification (major/minor/patch)
- Step-by-step migration guide
- Policy violations

[View on GitHub Marketplace →](https://github.com/marketplace/actions/delimit-api-governance) · [See a live demo (23 breaking changes) →](https://github.com/delimit-ai/delimit-action-demo/pull/2)

### Example PR comment

> **Breaking Changes Detected**
>
> | Change | Path | Severity |
> |--------|------|----------|
> | endpoint_removed | `DELETE /pets/{petId}` | error |
> | type_changed | `/pets:GET:200[].id` (string → integer) | warning |
> | enum_value_removed | `/pets:GET:200[].status` | warning |
>
> **Semver**: MAJOR (1.0.0 → 2.0.0)
>
> **Migration Guide**: 3 steps to update your integration

---

## CLI + MCP Toolkit

Governance tools for AI coding assistants (Claude Code, Codex, Cursor, Gemini CLI):

```bash
npx delimit-cli setup
```

No API keys. No account. Installs in 10 seconds.

### CLI commands (all free)

```bash
npx delimit-cli setup                            # Install into all AI assistants
npx delimit-cli setup --dry-run                  # Preview changes first
npx delimit-cli lint api/openapi.yaml            # Check for breaking changes
npx delimit-cli diff old.yaml new.yaml           # Compare two specs
npx delimit-cli explain old.yaml new.yaml        # Generate migration guide
npx delimit-cli init --preset strict             # Initialize policies
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

## What it catches

10 categories of breaking changes:

| Change | Example |
|--------|---------|
| Endpoint removed | `DELETE /users/{id}` disappeared |
| HTTP method removed | `PATCH /orders` no longer exists |
| Required parameter added | New required header on `GET /items` |
| Field removed from response | `email` dropped from user object |
| Type changed | `id` went from string to integer |
| Enum value removed | `status: "pending"` no longer valid |
| Response code removed | `200 OK` response dropped |
| Parameter removed | `sort` query param removed |
| Required field added to request | Body now requires `tenant_id` |
| Format changed | `date-time` changed to `date` |

Detection is deterministic — rules, not AI inference. Same input always produces the same result.

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
- [Docs](https://delimit.ai/docs) -- full documentation
- [GitHub Action](https://github.com/marketplace/actions/delimit-api-governance) -- Marketplace listing
- [Quickstart](https://github.com/delimit-ai/delimit-mcp-server) -- try it in 2 minutes
- [npm](https://www.npmjs.com/package/delimit-cli) -- CLI package
- [Pricing](https://delimit.ai/pricing) -- free tier + Pro

MIT License
