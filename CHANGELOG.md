# Changelog

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
