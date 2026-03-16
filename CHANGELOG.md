# Changelog

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
