# Changelog

## [3.15.9] - 2026-03-30

### Added
- **Agent Swarm**: 20 agents across 4 ventures with namespace isolation, tiered approvals, and central governor
- **Prompt Playbook**: versioned, reusable prompt templates with {{variable}} syntax
- **Multi-model code review**: consolidated feedback from Claude, Grok, Gemini, Codex
- **PII/secret redaction**: scan and redact API keys, emails, SSNs before sending to LLMs
- **Collision detection**: prevent two AI models from editing the same file
- **Prompt drift detection**: track when same task behaves differently across models
- **Cost vs Efficacy dashboard**: which model is best for your codebase
- **Project config**: committable `delimit.yml` with per-repo AI governance
- **`delimit resume`**: show what you were working on last session
- **`delimit quickstart`**: clone demo project and guided walkthrough
- **`delimit try`**: zero-risk demo that saves a Markdown report
- **Change management policy**: docs freshness check before deploy
- **GTM metrics tracking**: tasks, deploys, revenue per venture

### Changed
- Hero messaging: "Stop Re-Explaining Your Codebase" (pain-first)
- Progressive disclosure: 5 workflows instead of 162 tools
- Deliberation engine now uses official SDKs (anthropic, openai, google-genai)
- 4-model deliberation: Claude (CLI) + Grok + Gemini + Codex
- Setup auto-updates, regenerates shims, fixes config permissions
- Boot screen shows dynamic version and tool count with gradient colors
- All confirmation prompts show "Enter = Yes" hint
- Pricing page updated: Free/Pro/Enterprise tiers

### Fixed
- 37 missing @mcp.tool() decorators restored (125 to 171 tools)
- Stale .so binaries shadowing updated .py source files
- Codex config.toml duplicate TOML entries
- CI green across Python 3.10/3.11/3.12 (7 test files fixed)
- Setup self-update infinite loop guard
- Pro module stubs overwriting full source files

### Security
- 7 proprietary modules stubbed in npm bundle
- Deliberation engine kept private (stub in npm, full on server)
- PII scrubbed from all public packages
- Security check runs before every npm publish

## [3.13.3] - 2026-03-27

### Changed
- Postinstall message now shows full quick-start guide (init, lint, setup) with links to dashboard and docs

## [3.13.1] - 2026-03-27

### Added
- `delimit init` guided onboarding wizard with framework auto-detection (Express, NestJS, FastAPI, Django, Flask, Fastify, Hono, Next.js)
- Interactive preset selection (strict/default/relaxed) with context-aware defaults
- First lint runs automatically after init — see governance results in under 1 second
- Zero-Spec baseline auto-saved for FastAPI/Express/NestJS projects on first init
- GitHub Action workflow generation with confirmation prompt
- `--yes` flag for non-interactive CI usage

### Changed
- `delimit init` now detects CI provider (GitHub Actions, GitLab) and adapts workflow generation
- OpenAPI spec detection expanded to 17 common file locations

## [3.12.0] - 2026-03-26

### Added
- Cross-model hook system: session-start, pre-tool, and pre-commit hooks for Claude Code, Codex, and Gemini CLI
- `delimit export` and `delimit import` commands for shareable governance config
- `delimit hook <event>` commands for manual hook invocation
- `delimit uninstall` removes hooks from all AI tools cleanly
- Pre-push hooks for catching governance violations before remote push
- Cursor and Codex adapters for native integration

### Changed
- "Keep Building." success message displayed on lint/diff/doctor pass
- Zero-config action improvements for smoother CI integration

## [3.11.10] - 2026-03-24

### Added
- Cursor adapter with `.cursor/rules/delimit.md` support (Cursor 0.45+)
- Codex adapters: `codex-skill.js` (governance) and `codex-security.js`
- Setup `--dry-run` previews config changes before writing
- Uninstall `--dry-run` with backups for all 4 AI assistants
- Post-install config validation (checks all assistant configs)
- npm publish workflow with provenance and approval gates
- Setup matrix tests (13 new tests across all AI assistants)

### Changed
- Repo renamed to `delimit-ai/delimit-mcp-server`
- Description: "Unify Claude Code, Codex, Cursor, and Gemini CLI with persistent context, governance, and multi-model debate."
- Clear Free vs Pro tier boundaries in README

### Security
- Hardened `.npmignore` (blocks .env, credentials, keys)
- Supply chain security section in SECURITY.md

## [3.11.9] - 2026-03-23

### Added
- Auto-detect API keys and CLIs on `delimit init` and `delimit version`
- `delimit_quickstart` MCP tool (60-second guided first-run)
- Deliberation cost tracking (estimate + actual per model)
- Input sanitization: `_sanitize_path`, `_sanitize_subprocess_arg`
- GitHub Action smoke test workflow

### Fixed
- Gemini deliberation HTTP 400 (ADC credentials + jamsons project)
- Deliberation timeout: parallelized round 1 (46% faster)
- Sensor dedup: titles include repo/issue to prevent duplicates
- Test-mode guard prevents ledger pollution from tests

### Changed
- Governance default mode: advisory -> guarded (blocks critical actions)
- Ledger tools now route through governance loop (113/116 tools)
- Dialogue deliberation capped at 4 rounds (was 6)
- Per-model API timeout: 120s -> 45s (fail fast)

## [3.11.8] - 2026-03-23

### Fixed
- Codex compatibility: 11 type coercion fixes (string->list/dict)
- CI green: FastMCP fallback, env var injection, mock patterns
- Dashboard dark/light mode with CSS variables

## [2.4.0] - 2026-03-15

### Added
- 29 real CLI tests covering init, lint, diff, explain, doctor, presets, and error handling
- Auto-write GitHub Actions workflow file on `delimit init`

### Improved
- Version now read from package.json instead of hardcoded
- Error handling across all commands

## [2.3.2] - 2026-03-09

### Fixed
- Clean --help output (legacy commands hidden)
- File existence checks before lint/diff operations
- --policy flag accepts preset names (strict, default, relaxed)

## [2.3.0] - 2026-03-07

### Added
- Policy presets: strict (all errors), default (balanced), relaxed (warnings only)
- `delimit doctor` command for environment diagnostics
- `delimit explain` command with 7 output templates

## [2.0.0] - 2026-02-28

### Added
- Deterministic diff engine (23 change types, 10 breaking)
- Policy enforcement with exit code 1 on violations
- Semver classification (MAJOR/MINOR/PATCH/NONE)
- Zero-Spec extraction for FastAPI, NestJS, Express
