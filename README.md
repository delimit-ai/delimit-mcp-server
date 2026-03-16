# delimit

Catch breaking API changes before they ship.

[![npm](https://img.shields.io/npm/v/delimit-cli)](https://www.npmjs.com/package/delimit-cli)
[![GitHub Action](https://img.shields.io/badge/Marketplace-Delimit-blue)](https://github.com/marketplace/actions/delimit-api-governance)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://img.shields.io/badge/tests-299%20passing-brightgreen)](#)

Deterministic diff engine for OpenAPI specs. Detects breaking changes, classifies semver, enforces policy, and posts PR comments with migration guides. No API keys, no external services.

---

## GitHub Action (recommended)

Add `.github/workflows/api-check.yml`:

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
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.base.sha }}
          path: base
      - uses: delimit-ai/delimit-action@v1
        with:
          old_spec: base/api/openapi.yaml
          new_spec: api/openapi.yaml
```

Runs in **advisory mode** by default -- posts a PR comment but never fails your build. Set `mode: enforce` when you are ready to block merges on breaking changes.

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
delimit lint old.yaml new.yaml
```

### Commands

| Command | What it does |
|---------|-------------|
| `delimit init [--preset]` | Create `.delimit/policies.yml` |
| `delimit lint <old> <new>` | Diff + policy check. Exit 1 on violations. |
| `delimit diff <old> <new>` | Raw diff with `[BREAKING]` / `[safe]` tags |
| `delimit explain <old> <new>` | Human-readable explanation (7 templates) |

---

## What it catches

10 breaking change types, detected deterministically:

| Breaking change | Example |
|----------------|---------|
| Endpoint removed | `DELETE /users/{id}` path deleted |
| Method removed | `PATCH` dropped from `/orders` |
| Required parameter added | New required query param on existing endpoint |
| Parameter removed | `?filter` param deleted |
| Response removed | `200` response code dropped |
| Required field added | New required field in request body |
| Response field removed | `email` field removed from response |
| Type changed | `age` changed from `string` to `integer` |
| Format changed | `date` changed to `date-time` |
| Enum value removed | `status: "pending"` no longer allowed |

Plus 7 non-breaking types (endpoint added, optional field added, etc.) for full change visibility. Every change is classified as `MAJOR`, `MINOR`, `PATCH`, or `NONE`.

---

## Policy presets

```bash
delimit init --preset strict    # All breaking changes are errors. For public/payment APIs.
delimit init --preset default   # Breaking changes error, type changes warn. For most teams.
delimit init --preset relaxed   # Everything is a warning. For internal APIs and prototyping.
```

Or pass inline: `delimit lint --policy strict old.yaml new.yaml`

---

## Custom policies

Create `.delimit/policies.yml`:

```yaml
override_defaults: false

rules:
  - id: protect_v1
    name: Protect V1 API
    change_types: [endpoint_removed, method_removed, field_removed]
    severity: error
    action: forbid
    conditions:
      path_pattern: "^/v1/.*"
    message: "V1 API is frozen. Make changes in V2."
```

---

## Supported formats

- OpenAPI 3.0 and 3.1
- Swagger 2.0
- YAML and JSON

---

## Links

- [delimit.ai](https://delimit.ai) -- Project home
- [GitHub Action on Marketplace](https://github.com/marketplace/actions/delimit-api-governance) -- Install in one click
- [delimit-cli on npm](https://www.npmjs.com/package/delimit-cli) -- CLI package
- [Quickstart repo](https://github.com/delimit-ai/delimit-quickstart) -- Try it in 2 minutes

## License

MIT
