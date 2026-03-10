# Delimit

**API governance CLI for development teams.** Detect breaking API changes, enforce policies, and maintain audit trails across your services.

[![npm](https://img.shields.io/npm/v/delimit)](https://www.npmjs.com/package/delimit)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)

## Install

```bash
npm install -g delimit
```

## Quick Start

```bash
# Check current governance status
delimit status

# Set up governance for this repo (advisory mode by default)
delimit install --mode advisory

# Validate an API spec
delimit validate api/openapi.yaml
```

## Governance Modes

Delimit supports three enforcement levels. You choose:

```bash
delimit install --mode advisory   # Warnings only, no blocking
delimit install --mode guarded    # Soft enforcement with overrides
delimit install --mode enforce    # Strict policy enforcement
```

### Scope Control

```bash
delimit install --scope repo      # Current repository only
delimit install --scope global    # All repositories on this machine
```

## What It Does

- **API Change Detection** — Identifies breaking changes in OpenAPI/Swagger specs
- **Policy Enforcement** — Applies configurable governance rules to API changes
- **Evidence Collection** — Records audit trail of all governance decisions
- **MCP Integration** — Works with Claude, Gemini, and other AI coding tools via Model Context Protocol

## Usage with CI

For CI/CD integration, use the GitHub Action instead:

```yaml
- uses: delimit-ai/delimit-action@v1
```

See [delimit-action](https://github.com/delimit-ai/delimit-action) for full CI setup.

## Commands

| Command | Description |
|---------|-------------|
| `delimit status` | Show current governance state |
| `delimit install` | Set up governance hooks and config |
| `delimit validate <spec>` | Validate an API specification |
| `delimit doctor` | Diagnose configuration issues |
| `delimit uninstall` | Clean removal of all hooks and config |

## Uninstall

```bash
delimit uninstall
```

Cleanly removes all hooks, PATH modifications, and configuration files.

## Configuration

Create `.delimit/policies.yml` in your repository:

```yaml
rules:
  - id: no_endpoint_removal
    change_types: [endpoint_removed]
    severity: error
    message: "Endpoints cannot be removed without deprecation"
```

## Links

- [GitHub Action](https://github.com/delimit-ai/delimit-action) — CI/CD integration
- [Website](https://delimit.ai) — Documentation and platform
- [Issues](https://github.com/delimit-ai/delimit/issues) — Bug reports and feature requests

## License

MIT
