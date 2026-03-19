# delimit

Your AI Remembers. Verifies. Ships.

[![npm](https://img.shields.io/npm/v/delimit-cli)](https://www.npmjs.com/package/delimit-cli)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)

Governance layer for AI coding assistants. Your AI verifies its own work -- confirms tests ran, catches breaking API changes, audits security, and enforces policies. Works with Claude Code, Codex, and Cursor.

## Install

```bash
npx delimit-cli setup
```

10 seconds. No API keys. No account. Installs into your existing AI coding assistant.

## What it does

Your AI agent gains the ability to verify its own work:

- **Test verification** -- confirms tests actually ran, measures coverage
- **Security audit** -- scans dependencies, detects hardcoded secrets and anti-patterns
- **API governance** -- catches breaking changes in OpenAPI specs before they ship
- **Repo analysis** -- code quality, health checks, config validation
- **Deploy tracking** -- plan, build, publish, verify, rollback
- **Multi-model consensus** -- multiple AI models deliberate on strategic decisions

## Real examples

These happened in a single session:

| Command | Result |
|---------|--------|
| "fix the 502 error" | Traced Vercel to Caddy to Docker, found wrong IP, fixed, verified |
| "run test coverage" | 299 to 1,113 tests, zero written manually |
| "run consensus on pricing" | 3 AI models debated, reached unanimous agreement |

## Free vs Pro

**Free**: lint, diff, policy, semver, test coverage, security audit, repo analysis, zero-spec extraction, and more.

**Pro ($10/mo)**: governance, deploy tracking, memory/vault, multi-model deliberation, evidence collection. Activate with `delimit activate YOUR_KEY`.

## Also works in CI

```yaml
- uses: delimit-ai/delimit-action@v1
  with:
    spec: api/openapi.yaml
```

## Links

- [delimit.ai](https://delimit.ai)
- [GitHub](https://github.com/delimit-ai/delimit)
- [Pricing](https://delimit.ai/pricing)
- [Docs](https://delimit.ai/docs)

MIT License
