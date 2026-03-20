# Delimit

Catch breaking API changes before they reach production.

[![npm](https://img.shields.io/npm/v/delimit-cli)](https://www.npmjs.com/package/delimit-cli)
[![GitHub Action](https://img.shields.io/badge/GitHub%20Action-v1.4.0-blue)](https://github.com/marketplace/actions/delimit-api-governance)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)

Delimit diffs your OpenAPI spec on every pull request. Breaking changes get flagged, semver gets classified, and your team gets a migration guide — automatically.

---

## GitHub Action

Add to any repo with an OpenAPI spec:

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

[View on GitHub Marketplace →](https://github.com/marketplace/actions/delimit-api-governance) · [See a live PR comment →](https://github.com/delimit-ai/delimit-quickstart/pull/1)

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

### CLI commands

```bash
npx delimit-cli lint api/openapi.yaml           # Check for breaking changes
npx delimit-cli diff old.yaml new.yaml           # Compare two specs
npx delimit-cli explain old.yaml new.yaml        # Generate migration guide
npx delimit-cli init --preset strict             # Initialize policies
npx delimit-cli doctor                           # Check setup health
```

### What the MCP toolkit adds

When installed into your AI coding assistant, Delimit provides:

- **API governance** -- lint, diff, policy enforcement, semver classification
- **Test verification** -- confirms tests actually ran, measures coverage
- **Security audit** -- scans dependencies, detects secrets and anti-patterns
- **Persistent ledger** -- tracks tasks across sessions, auto-creates items from governance
- **Multi-model consensus** -- Grok, Gemini, and Codex debate until they agree
- **Zero-spec extraction** -- generate OpenAPI specs from FastAPI, Express, or NestJS source

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
- [Quickstart](https://github.com/delimit-ai/delimit-quickstart) -- try it in 2 minutes
- [npm](https://www.npmjs.com/package/delimit-cli) -- CLI package
- [Pricing](https://delimit.ai/pricing) -- free tier + Pro

MIT License
