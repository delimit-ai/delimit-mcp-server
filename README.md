# delimit

Catch breaking API changes before they ship.

[![npm](https://img.shields.io/npm/v/delimit-cli)](https://www.npmjs.com/package/delimit-cli)
[![GitHub Action](https://img.shields.io/badge/Marketplace-Delimit-blue)](https://github.com/marketplace/actions/delimit-api-governance)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)

Deterministic diff engine for OpenAPI specs. Detects breaking changes, classifies semver, enforces policy, and posts PR comments with migration guides. No API keys, no external services.

---

## GitHub Action (recommended)

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
      - uses: delimit-ai/delimit-action@v1
        with:
          spec: api/openapi.yaml
```

One input. Delimit fetches the base branch version automatically. Runs in **advisory mode** by default -- posts a PR comment but does not fail your build. Set `mode: enforce` to block merges on breaking changes.

---

## CLI

```bash
npx delimit-cli lint api/openapi.yaml
npx delimit-cli diff old.yaml new.yaml
npx delimit-cli explain old.yaml new.yaml --template migration
```

Or install globally:

```bash
npm install -g delimit-cli
delimit init --preset default
delimit lint api/openapi.yaml
```

### Commands

| Command | What it does |
|---------|-------------|
| `delimit init [--preset]` | Create `.delimit/policies.yml` with a policy preset |
| `delimit lint <spec>` | Diff + policy check. Exit 1 on violations. |
| `delimit diff <old> <new>` | Raw diff with `[BREAKING]` / `[safe]` tags |
| `delimit explain <old> <new>` | Human-readable summary (7 templates) |

### Policy presets

```bash
delimit init --preset strict    # All breaking changes are errors
delimit init --preset default   # Breaking = error, type changes = warn
delimit init --preset relaxed   # Everything is a warning
```

Or inline: `delimit lint --policy strict api/openapi.yaml`

---

## What it catches

10 breaking change types (endpoint removed, method removed, required param added, param removed, response removed, required field added, response field removed, type changed, format changed, enum value removed) plus 7 non-breaking types for full visibility. Every change classified as `MAJOR`, `MINOR`, `PATCH`, or `NONE`.

Supports OpenAPI 3.0, 3.1, and Swagger 2.0 in YAML or JSON.

---

## Links

- [delimit.ai](https://delimit.ai)
- [GitHub Action](https://github.com/marketplace/actions/delimit-api-governance)
- [delimit-cli on npm](https://www.npmjs.com/package/delimit-cli)
- [Quickstart repo](https://github.com/delimit-ai/delimit-quickstart)

## License

MIT
