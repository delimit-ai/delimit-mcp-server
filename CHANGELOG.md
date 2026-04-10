# Changelog

## [4.1.49] - 2026-04-09

### Fixed (full preservation audit follow-up to 4.1.48)
- **Project `.claude/settings.json` hooks clobber** — `installClaudeHooks` was replacing the project-level `.claude/settings.json` hooks object with the merged-with-global config, propagating global hooks into every project file and wiping any project-local hooks the user had set. Now merges only Delimit-owned hook groups (entries whose command contains `delimit`) into existing project hooks; project-specific user hooks survive.
- **Gemini `general.defaultApprovalMode` clobber** — `delimit-cli setup` was force-setting Gemini's `defaultApprovalMode` to `auto_edit` on every run, overwriting whatever the user had chosen (e.g. `manual`). Now only sets it when missing.
- **`~/.claude.json` MCP hooks replacement** — `lib/hooks-installer.js` (opt-in via `delimit-cli hooks install`) replaced `preCommand` / `postCommand` / `authentication` / `audit` keys on every install. Now only fills in missing keys, preserving any user-chosen MCP hook commands.

### Added
- **`tests/setup-no-clobber.test.js`** — dedicated regression suite that runs setup helpers against synthetic fresh-user HOME directories with pre-populated user customizations (project hooks, Gemini approval mode, custom MCP hook commands) and asserts none get clobbered. 5 tests, all passing.

### Audit results
- Audited every `fs.writeFileSync` in `bin/delimit-setup.js`, `lib/cross-model-hooks.js`, `lib/hooks-installer.js`, `adapters/cursor-rules.js`, and `scripts/postinstall.js`.
- All remaining writes are either delimit-owned (shims, hook scripts, generated `delimit.md`), guarded by `!fs.existsSync` (models.json, social_target_config.json, codex empty file), or surgical merges that preserve user content (`.mcp.json` mcpServers, `.claude/settings.json` allowList, `.codex/config.toml` mcp_servers.delimit block, `.cursor/mcp.json` mcpServers, rc-file PATH append).
- The full preservation contract is now: `delimit-cli setup` may safely run on any user machine, including via the shim auto-update flow, without destroying user state. New installs and upgrades are equivalent for everything except delimit-owned files.

### Tests
- 129/129 passing (was 124).

## [4.1.48] - 2026-04-09

### Fixed
- **CRITICAL: CLAUDE.md clobber on upgrade** — `delimit-cli setup` used a loose heuristic (`# Delimit` + `delimit_ledger_context` or `# Delimit AI Guardrails`) to decide whether to replace a user's entire `CLAUDE.md` with the stock template. Any founder-customized CLAUDE.md that happened to mention `delimit_ledger_context` got clobbered on every upgrade — this included 4.1.47's auto-update flow, which destroyed custom auto-trigger rules, paying-customer protection blocks, and incident-derived escalation rules. The clobber path is removed entirely. `upsertDelimitSection` now either upserts between `<!-- delimit:start -->` / `<!-- delimit:end -->` markers (preserving user content above and below), or — if no markers exist — appends the managed section at the bottom, preserving all existing user content verbatim.
- Same fix applied to `GEMINI.md` in `lib/cross-model-hooks.js` (previously did a whole-file overwrite if it did not contain the detection phrase).
- Detection marker changed from the prose phrase `Consensus 123` to the stable structural marker `<!-- delimit:start`, so future template copy changes never break preservation logic.

### Security
- **axios** bumped from `1.13.6` → `1.15.0` to patch GHSA-3p68-rc4w-qgx5 (NO_PROXY hostname normalization bypass → SSRF, severity: critical).

### Changed
- Stock `CLAUDE.md` template is now minimal (auto-trigger lifecycle, code/commit/deploy gates, audit trail, links). Founder-only sections (Paying Customers, Strategic/Business Operations, Escalation Rules, venture portfolio context) are no longer shipped in the npm package — they belong in `~/.delimit/CLAUDE.md` or `~/.claude/CLAUDE.md` (never touched by `delimit-cli setup`).

### Tests
- Added two regression tests in `tests/setup-onboarding.test.js` covering (a) the exact legacy-content-preservation case and (b) the founder-customized CLAUDE.md pattern that triggered the 2026-04-09 incident.

## [4.1.45] - 2026-04-09

### Fixed
- **Shim rename-hack removed** — install no longer races with npm reinstalls that clobbered `/usr/bin/claude` back to a symlink, causing `[Delimit] claude not found in PATH` mid-session. Shim now relies purely on `$HOME/.delimit/shims` being first in `PATH` plus a PATH-strip lookup for the real binary. Fixes regressions from the claude-real rename+wrap install mechanism.
- Shim exit screen parity and CLI lint output parity (LED-078, LED-087).

## [4.20.0] - 2026-04-20

*The highest state of AI governance.*

### Added
- `delimit doctor` — 14 prescriptive diagnostics with `--ci` JSON, `--fix` auto-repair, health score
- `delimit status` — visual terminal dashboard with `--json`, `--watch` live refresh
- `delimit simulate` — policy dry-run (terraform plan for API governance)
- `delimit report` — governance report with `--since`, `--format md|html|json`
- Memory hardening — SHA-256 integrity hash + source_model on every `remember`
- Tag-based publishing — `scripts/release.sh` + GitHub Actions workflow
- Gateway sync gate — `prepublishOnly` auto-syncs from gateway (drift impossible)
- YouTube auto-publish pipeline — `scripts/record-and-upload.sh`
- VS Code extension v0.2.0 — simulate + report commands
- 17 new tests for v4.20 features (total: 123)

### Fixed
- Flaky test timeouts (5s → 15s for hook/deliberate tests)
- PostToolUse hook test updated for tightened spec patterns
- Dashboard workspace filter now filters data by project (LED-330)
- Hardcoded usernames scrubbed from gateway (moved to env var)

### Changed
- README updated with v4.20 features and new quick-start commands
- Landing page: tool count 176 → 186, doctor + simulate cards, YouTube embed
- Demo GIF updated with clean mock data (Acme API)

## [4.1.0] - 2026-04-03

### Added
- v3.15.13: self-extending swarm, 8 new modules, PII scrub (#12) (21e68042)
- Add 4 missing swarm modules to npm bundle (8c094d8f)
- Add change management policy + docs checker to npm bundle (cc2c3431)
- Add agent swarm + central governor to npm bundle (LED-274/275/276) (17186db2)
- Add delimit.yml project config + sync server.py (STR-049) (dc2d93c9)
- Add Prompt Playbook to npm bundle (STR-048) (a2f88c72)
- Add project scan to setup + delimit resume command (STR-046, STR-047) (1d7ea31e)
- Add background auto-update to governance shims (f9288095)
- Add --yes flag to setup command, fix auto-update hook (c678586b)
- Sync gateway server.py + new modules to npm bundle (a0657b53)
- Add deploy-gate hook: blocks deploys on import errors (LED-024 feedback) (029fae4b)
- Add social scanning instruction to CLAUDE.md template (99531620)
- Add community templates: bug report, feature request, PR template (70d608cc)
- Add update check to SessionStart hook (09f584b3)
- Add delimit quickstart command — guided 5-min onboarding (LED-267) (bd90518a)
- Add subtle delays between setup steps for polished feel (4aeed8e3)
- Add confirmation gates to setup flow — no more silent blast-through (a8d5573a)
- Update hero GIF with boot screen recording, add try + init demos (0028535e)
- Add 'Enter = Yes' hints to confirm prompts (ee786231)
- Fix action name mismatch, add try to CLI list, update hero GIF (c5ab5f1f)
- Add 'Pick your first win' funnel to README (LED-266) (57c46528)
- Add delimit try + enhanced doctor with preview/undo (LED-264, LED-265) (6af647d1)
- Add golden-path smoke tests for onboarding flow (Round 4 consensus) (1fe75d20)
- Add beta capture loop after demo and init commands (LED-263) (4e699a70)
- Add delimit demo command — proves governance value in 5 minutes (LED-262) (05936eac)
- Add weekly drift monitoring workflow template to init wizard (LED-260) (506c5485)
- tool chaining, visibility tiers, conditional hooks, social workflow (ea3f9089)
- LED-234: Add Claude Code conditional hooks using the if field (7c1daa65)
- LED-232: Add automated weekly activity tweet workflow (988295bb)
- Add Delimit governance badge to README (56208edd)
- Add FUNDING.yml for GitHub Sponsors (ce39f471)
- Add animated demo GIF and 27 change types table to README (LED-211) (94140fa6)
- Fix Claude Code Action: add checkout step (addad959)
- Fix Claude Code Action: add explicit github_token (45da9c14)
- Add OpenAPI spec for MCP server API (307090d7)
- Add Claude Code Action workflow for @claude mentions on PRs/issues (7803fdee)
- Update README: zero-config action, fix Glama badge, add Cursor (6f4026be)
- Add Cursor/Codex adapters, enforce governance, update branding (4c8c7d59)
- git hooks call real tools + CLAUDE.md workflow triggers (2e4c9832)
- auto-update CLAUDE.md with marker-based upsert (LED-006) (7b46a68a)
- add package-lock.json (0 vulnerabilities, required for Glama AAA) (00d78d93)
- add Pro tool stubs (full source in private repo) (37d41565)
- update: server.json for MCP Registry — v3.8.1 + new description (1ca5756e)
- silent API key auto-detect + Anthropic/OpenAI model support (a618de2c)
- auto-detect API keys on setup + startup deliberation status (LED-099) (84894118)
- Gemini CLI adapter + live demo link + dynamic tool detection (b361ad14)
- clean: remove pycache from package, add files field, update description (0649538a)
- first-run onboarding — action-oriented, no tool names (LED-052) (9ce8cc90)
- first-run onboarding for setup — action-oriented CLAUDE.md, try-it-now prompt (b036a990)
- setup now configures Gemini CLI alongside Claude Code/Codex/Cursor (v3.5.1) (3e27c8ca)
- add delimit activate CLI command for license key activation (7060b9c4)
- Add glama.json for Glama MCP server listing (d8c4f1c2)
- v3.2.0 — CLAUDE.md first-run prompt + help/diagnose tools (6fdd699d)
- setup auto-detects Claude Code, Codex, and Cursor (v3.1.0) (922a9c42)
- delimit setup — one-command MCP governance install (v3.0.0) (fef24f4e)
- add Dockerfile for non-GitHub CI usage (LED-025) (23582fb1)
- Add CLI test suite with 29 tests using node:test (58ad14e1)
- auto-write workflow file on init (dcb21fc1)
- Add file existence checks for lint, diff, explain commands (f607b8cd)
- delimit init auto-detects specs and outputs workflow YAML (ec7c6fdd)
- v2.3.0 — policy presets, updated README, new positioning (2056d8b5)
- add policy presets to init command (912795e2)
- v2.2.0: Add NestJS + Express Zero-Spec support to CLI (09aa9609)
- Add NestJS support to zero-spec CLI bridge (91f51b2d)
- Add v1 CLI commands: init, lint, diff, explain (Phase 2) (e6451587)

### Fixed
- remove ALL delimit TOML entries before appending, prevent duplicates (14145078)
- prevent infinite self-update loop in setup (13ece796)
- chmod Codex config at top of shim, before any exec path (1fd393ed)
- shim auto-fixes Codex config.toml permissions before launch (78c50547)
- chmod 644 existing Codex config.toml on every setup run (2f159145)
- set 644 permissions on Codex config.toml (78fb6bd0)
- create Codex config.toml if .codex dir exists or codex in PATH (13617f4c)
- remove stale .so binaries that shadow updated .py source (b6b488dd)
- Pro module stubs overwriting full source files (f28f0813)
- Fix setup self-update: re-exec via global binary not old __dirname (972cfa6d)
- Gemini CLI is @google/gemini-cli on npm, not pip (7d041f27)
- Gemini install message should be pip only, not Claude (d9ec13d5)
- Fix shim: correct install message per tool (Claude/Codex/Gemini) (e6535b4a)
- Fix shim tool count: count both @mcp.tool decorators and mcp.tool()() calls (c974d4f7)
- Fix setup duplicating hooks: use local binary, dedup npx vs bare (305b9d00)
- Fix 'Configured for' summary to list all tools actually configured (e0cd7144)
- Fix governance shim: robust binary lookup to prevent "not found" errors (962d86c7)
- v3.11.11: zero-config action, Glama badge fix, pre-push hook (e35444b4)
- Fix server.json version, update setup banner tagline (f8e03ae5)
- add 'delimit version' subcommand + version bump (be4d7743)
- CRITICAL — lint/diff/explain now work without setup for npx users (14ea648c)
- resolve gateway path for npx users — lint/diff/explain work without setup (b9218b98)
- clean uninstall — removes MCP config from Claude/Codex/Gemini, shims (c5ed29d0)
- update demo link text to 23 breaking changes (bc7a3ec1)
- add mcpName for MCP Registry publish (d001c95a)
- Dockerfile for Glama inspection + Glama badge + clean header (13b0904c)
- replace ASCII banner with clean header (mobile-friendly for Glama) (c555276b)
- update tests for new setup flow (governance wrapping, scan) (40e409c5)
- v3.9.4: fix Pro source leak — restore stubs for deliberation + governance (07d25b3c)
- rename demo link to delimit-action-demo (f66111a1)
- v3.8.0: scan tool, release sync, CLI-first deliberation, enforce fix (53ecd30c)
- point test script to correct file (tests/setup-onboarding.test.js) (ba97abd6)
- governance test fixes bundled (v3.6.3) (937f95b8)
- bundle updated gateway with init tool + audited descriptions (v3.1.1) (33688216)
- remove broken install script, add delimit-cli bin alias (v3.0.2) (4c70462d)
- Remove STRATEGY.md, node_modules, fix README and package.json (a988ae2b)

### Changed
- v4.1.0 — TUI, security hardening, free tier restructure (cdcca6bf)
- security: publish v3.11.2 — clean npm package, zero venture references (22620e5f)
- revert glama.json to clean state (63624339)
- Bump to v2.3.1 — clean help output (bd62fdd1)
- Clean up CLI help: show only v1 commands, update description (897e8de7)

### Documentation
- Update CHANGELOG + README per change management policy (v3.15.x) (57fb733f)
- Rewrite README hero: lead with pain not features (STR-039) (ab1d9fe1)
- Rewrite README: governance-first messaging, demo command, v3.14 features (e534dcbb)
- v3.9.5: DELIMIT banner on install screen + README (fb963ee7)
- link to live demo repo (6 breaking changes) (770eb427)
- update: README + CLAUDE.md + description to governance toolkit positioning (832a961a)
- add live PR comment demo link (6c2ecd60)
- complete README rewrite — leads with Action, verified claims, real examples (9b5e5ae3)
- update README to match "Your AI Remembers. Verifies. Ships." framing (e1514789)
- update npm README for new positioning (v3.4.0) (8f83b614)
- add SECURITY.md (7827909f)
- add CODE_OF_CONDUCT.md (06d1f419)
- add CONTRIBUTING.md (4e4cf0fd)
- streamline README for v2.4.0 (d2877ceb)
- update README with simplified spec input (07a67506)
- rewrite README as canonical project landing page (b07f212f)
- Update README for npm listing: correct install command and v1 commands (130df523)

### Tests
- release: v2.4.0 — real test suite, auto-workflow init (09488934)
- Phase 3: Zero-Spec Mode in CLI (6d80ed83)

### CI/CD
- Deploy Agent Swarm v1.2 — 4 ventures, 20 agents, namespace isolation (dff83019)
- Switch Claude Code Action to OAuth token auth (9e3383a0)
- release: v3.11.8 — workflow triggers, git hooks, Codex audit fixes (0d9e38e6)
- release: v3.11.7 — workflow triggers in CLAUDE.md + git hooks call real tools (6365911a)
- update: Action badge to v1.6.0 (27 change types) (b391cb9e)
- v3.10.2: sync gateway with CI fixes, Pro stubs intact (d29377d1)
- add test workflow for Node 18/20/22 matrix (42b3e600)
- Dogfood: use delimit-action on our own PRs (2c184e2c)

### Chores
- add MIT LICENSE (bb016157)
- remove stale .gitignore (ba40355c)
- remove stale test-hook.js (c82a2334)
- remove stale test-decision-engine.js (9082bab3)
- remove stale delimit.yml (368e657f)
- remove stale package-lock.json (e2a63a3d)
- remove stale package.json (a810745c)
- remove stale hooks/update-delimit.sh (833406f2)
- remove stale hooks/test-hooks.sh (9f828ee1)
- remove stale hooks/pre-write-hook.js (9b3154c8)
- remove stale hooks/pre-web-hook.js (de075a98)
- remove stale hooks/pre-tool-hook.js (cdd7da15)
- remove stale hooks/pre-task-hook.js (62e008a0)
- remove stale hooks/pre-submit-hook.js (ba4d681f)
- remove stale hooks/pre-search-hook.js (c2cfaac5)
- remove stale hooks/pre-read-hook.js (b62cc4be)
- remove stale hooks/pre-mcp-hook.js (18059a3a)
- remove stale hooks/pre-bash-hook.js (93793ef3)
- remove stale hooks/post-write-hook.js (0ea6417e)
- remove stale hooks/post-tool-hook.js (10bef4f0)
- remove stale hooks/post-response-hook.js (b0451baf)
- remove stale hooks/post-mcp-hook.js (70407b2c)
- remove stale hooks/post-bash-hook.js (a54e06ea)
- remove stale hooks/message-governance-hook.js (99aac7e9)
- remove stale hooks/message-auth-hook.js (854d1495)
- remove stale hooks/install-hooks.sh (dda69b77)
- remove stale hooks/evidence-status.sh (a4bce3b5)
- remove stale hooks/git/pre-push (79a11631)
- remove stale hooks/git/pre-commit (e0817614)
- remove stale hooks/git/commit-msg (5c31c8d9)
- remove stale hooks/models/xai-pre.js (5aca2592)
- remove stale hooks/models/xai-post.js (953b02a0)
- remove stale hooks/models/windsurf-pre.js (bb0512e7)
- remove stale hooks/models/windsurf-post.js (b082cdb0)
- remove stale hooks/models/openai-pre.js (1b5d69c6)
- remove stale hooks/models/openai-post.js (f56af431)
- remove stale hooks/models/gemini-pre.js (ebb987de)
- remove stale hooks/models/gemini-post.js (496270b0)
- remove stale hooks/models/cursor-pre.js (6e4d4ac1)
- remove stale hooks/models/cursor-post.js (90982e79)
- remove stale hooks/models/codex-pre.js (346c1798)
- remove stale hooks/models/codex-post.js (224f7421)
- remove stale hooks/models/claude-pre.js (3a8247df)
- remove stale hooks/models/claude-post.js (b7599b84)
- remove stale tests/cli.test.js (da071756)
- remove stale tests/fixtures/openapi.yaml (8fefcc13)
- remove stale tests/fixtures/openapi-changed.yaml (e71cfa46)
- remove stale scripts/infect.js (c0cfe6da)
- remove stale lib/proxy-handler.js (13b87b08)
- remove stale lib/platform-adapters.js (2e502c93)
- remove stale lib/hooks-installer.js (2925457c)
- remove stale lib/decision-engine.js (c9992a8a)
- remove stale lib/auth-setup.js (cc7a6e6a)
- remove stale lib/api-engine.js (c14befd2)
- remove stale lib/agent.js (87303c9e)
- remove stale bin/delimit.js (c62be56d)
- remove stale bin/delimit-cli.js (6c91327e)

### Other
- Sync swarm + metrics to npm bundle (LED-277/278) (41a38b1a)
- Stub 6 proprietary modules in npm bundle — keep server-side only (b3399bec)
- Keep deliberation engine private — replace with stub in npm bundle (28a334ba)
- Bump to 3.15.2 — includes Prompt Playbook + PII sanitization (5c55e827)
- Bump to 3.14.45 (25634baf)
- Bump to 3.14.44 (dc082ada)
- Sanitize PII in npm bundle docstrings (04172a3f)
- Always update MCP paths on setup — never assume existing config is correct (f78babc9)
- Full gateway sync: 16 missing Python modules added to npm bundle (8c991f25)
- Setup self-updates before running: never generate stale shims (54966c7e)
- Always regenerate governance shims on setup (not just first install) (8add6659)
- Make shim version + tool count fully dynamic (3d045cdc)
- Show dynamic version on boot screen banner (4e545a36)
- Auto-update on session start: npm install + setup --yes when newer version available (2d0b3c35)
- Bump to 3.14.11 (a274fbf0)
- Use gradient ASCII banner: purple → magenta → orange (c4ec9643)
- Bump to 3.14.9 for npm publish (39e33e1d)
- Bump version to 3.14.0 — Governance Cockpit release (7815bbb1)
- Enhance init wizard: compliance templates, evidence, gate status (LED-258) (bfd884b9)
- v3.13.3: Improved postinstall welcome with quick-start guide (f58f32d9)
- LED-240: Enhanced CLI first-run experience + anonymous install ping (a7b89798)
- v3.13.1: Guided init wizard with framework detection and first lint (606bf45e)
- Release delimit-cli@3.13.0 — rate limiter bundled (79210701)
- Release delimit-cli@3.12.1 — deliberation hooks + CLI (d0e2c4db)
- LED-201: Wire deliberation into hooks + CLI deliberate command (2572c6d7)
- release: v3.12.0 — cross-model hooks, config export/import, Keep Building. (d2adcdcc)
- Revert breaking API changes: restore uptime integer type and errors/warnings field names (7aecf8ce)
- Update health and lint API responses (91b5fc17)
- LED-129: Pre-push hook runs tests before allowing push (3dbf47ae)
- Update CLAUDE.md template branding and GitHub URL (9fac51be)
- Update CHANGELOG with 3.11.8-3.11.10 releases (bcc8dae6)
- Wire local API server into setup flow (STR-057) (223a647d)
- release: v3.11.4 — CLAUDE.md auto-update with versioned markers (66db96dd)
- security: remove infect.js, hardcoded paths, stale shell scripts (ddb2b1a8)
- security: remove all Jamsons Doctrine references from gateway stubs (1d802a2b)
- security: remove jamsons adapters from public repo (c976fa8a)
- release: v3.11.1 — MCP/AI keywords for npm discoverability (ba984f17)
- release: v3.11.0 — agent identity, secrets broker, approval gates (78557ea8)
- update: CLI description to match brand positioning (75ff6842)
- update: demo link to PR #2 (27 change types) (4c40e174)
- publish: MCP Registry v3.10.4 — officially listed (d11922eb)
- v4.0.0: governance wrapping — shim install with live preview + opt-in (64608e7f)
- v3.9.3: gov_health free, startup governance check, dashboard MVP (e922d6b3)
- v3.9.2: dynamic Pro module version from package.json, git history scrubbed (d6c834d2)
- v3.9.1: download Pro modules from delimit.ai CDN (public URL, no auth needed) (b1462fd4)
- v3.9.0: Pro source removed from public package — compiled modules download at install (e4fe7baf)
- v3.8.2: Gemini governance trigger + history scrub (116ffb1a)
- security: remove node_modules and jamsons adapters from public repo (f67f3b92)
- v3.8.1: governance trigger in all instruction files + MCP server description (7c525231)
- v3.7.1: CLI-first deliberation + gateway sync + path cleanup (1555691d)
- v3.7.0: cross-model positioning + models configure + release sync (4c9cbcb7)
- update description: governance toolkit, not just API checks (049d09ec)
- sync: gateway source with path fixes + 79 tools (091d5328)
- security: remove all hardcoded internal paths from shipped package (087317ed)
- security: pin Python deps + isolated venv install (LED-092) (4839a669)
- release: v3.6.0 — governance loop + project-local ledger + venture auto-detection (f31dcd1d)
- release: v3.5.0 — ledger tools + deliberation engine + all 77 real tools (6acd7429)
- release: v3.3.0 — premium gating + license system + stub hiding (3071ba71)
- Read version from package.json instead of hardcoding (fe764ee7)
- Bump to v2.3.2 — error handling improvements (e9aed0c6)
- Update --policy help text to mention preset names (e5a08378)
- Replace legacy doctor with v1-focused setup checker (73324ee2)
- Rename package to delimit-cli for npm publish (6778f481)
- Bump to 2.1.0: Update description and keywords for v1 launch (1bc8e723)
- Initial commit: Delimit NPM package with governance hooks (a189918c)

### Completed Ledger Items
- **LED-001**: [P1] Build onboarding wizard
- **LED-002**: [P1] Fix Grok 403 in deliberation
- **LED-003**: [P0] Set up Lemon Squeezy Pro product ($10/mo)
- **LED-004**: [P0] Submit to official MCP Registry
- **LED-005**: [P1] Show HN post for Delimit
- **LED-006**: [P1] Auto-update CLAUDE.md and AI instruction files
- **LED-007**: [P1] Add Gemini CLI adapter to delimit setup
- **LED-008**: [P1] Publish to PyPI as delimit-mcp
- **LED-009**: [P0] Submit to Glama.ai for awesome-mcp-servers PR
- **LED-010**: [P0] Wire Report: Fix xAI circuit breaker in orchestrator MCP session
- **LED-011**: [P0] Wire Report: Fix injuries ETL — ESPN 403 + DB corruption
- **LED-012**: [P0] Wire Report: Build priority signal briefing for Edge subscribers
- **LED-013**: [P1] Deliberation engine built — dialogue + debate modes
- **LED-014**: [P1] Wire Report: Build signal performance scorecard
- **LED-015**: [P1] Wire Report: Frontpage two-tier update (raw cache + AI synthesis)
- **LED-017**: [P1] Wire Report: WR-LT-005 — AI synthesis for live event clusters
- **LED-018**: [P1] Wire Report: RSS feed with real content
- **LED-019**: [P2] Wire Report: Expand headshots to all leagues (NFL, MLB, NHL, WNBA)
- **LED-020**: [P2] Wire Report: Addiction mechanics — since-last-visit markers, N new updates
- **LED-021**: [P1] Wire Report: Rebuild wireintel.db — VACUUM or fresh migration
- **LED-022**: [P0] DomainVested: 100+ features shipped — comprehensive SaaS rebuild
- **LED-023**: [P1] DomainVested: Fix ExpiredDomains.net scraper HTML parsing
- **LED-024**: [P1] DomainVested: Fix IMAP auth for email ingestion
- **LED-026**: [P1] DomainVested: Import more NameBio comp data (.io, .co, .dev recent sales)
- **LED-027**: [P1] DomainVested: Unblock Opportunities feed with real data
- **LED-028**: [P1] DomainVested: Domain teardown content campaign on @domainvested X
- **LED-029**: [P1] DomainVested: Vertex AI integration (Gemini fallback + embeddings)
- **LED-030**: [P0] LiveTube: User accounts + NextAuth v4 authentication
- **LED-032**: [P0] LiveTube: Stripe payment rails + Pro subscription ($29/mo)
- **LED-034**: [P1] LiveTube: Boost/spotlight credit system
- **LED-036**: [P1] LiveTube: Custom alerts engine (Phase 1)
- **LED-038**: [P1] LiveTube: Developer API docs + settings page
- **LED-040**: [P1] LiveTube: Vertex AI SDK migration + retry logic
- **LED-042**: [P1] LiveTube: 5-section homepage + mobile UX overhaul + focus group fixes
- **LED-044**: [P1] LiveTube: Content pipeline — innertube primary, Kick OAuth, 9 API keys
- **LED-046**: [P1] LiveTube→WireReport: Port clustering engine to WireIntel
- **LED-047**: [P1] LiveTube→WireReport: Sports channel registry + YouTube/Kick discovery
- **LED-048**: [P1] LiveTube→WireReport: Article stub generation from event clusters
- **LED-049**: [P2] LiveTube→WireReport: X media embedding + LIVE NOW section + alerts
- **LED-050**: [P1] LiveTube: Fix Docker networking (port proxy broken)
- **LED-043**: [P0] Build ChatOps dashboard as command center
- **LED-045**: [P1] Background agents get blocked on Edit/Write permissions
- **LED-051**: [P1] Auto-add deliberation/focus group findings to ledger
- **LED-052**: [P0] First-run onboarding must show value in under 5 minutes
- **LED-053**: [P0] Governance layer: all tools report results, auto-update ledger
- **LED-054**: [P1] DomainVested: Comp sales with context — resale velocity + buyer type
- **LED-055**: [P2] DomainVested: Domain blacklist/spam history check
- **LED-056**: [P1] Post content for first 5 users — Twitter, Reddit, Indie Hackers
- **LED-057**: [P1] DomainVested: Lightweight domain summary endpoint for Compare page
- **LED-058**: [P0] Build delimit_deploy_site tool — one-command Vercel deploy
- **LED-059**: [P1] DomainVested: Flip potential score — resale timeline + profit estimate
- **LED-060**: [P2] DomainVested: Domain age premium calculator
- **LED-061**: [P0] DomainVested: Consistency audit — verdict/flip/action must agree
- **LED-062**: [P1] Brand: Add SVG logo to site, favicon, GitHub org avatar, npm
- **LED-063**: [P0] Governance trigger shipped in npm — instruction files + MCP description
- **LED-064**: [P0] Security: removed jamsons adapters + node_modules from public repo
- **LED-065**: [P0] ChatOps: Build app.delimit.ai into a unified project management interface
- **LED-066**: [P0] Split repos: free tools public, Pro tools private, npm bundles both
- **LED-067**: [P0] License: add periodic re-validation (30 day) with 7 day grace period
- **LED-068**: [P0] Pro code protection: evaluate Nuitka/PyArmor vs .pyc for cross-Python compatibility
- **LED-069**: [P1] Deliberation: tool-augmented debate — models can call delimit tools during consensus
- **LED-070**: [P1] Outreach wave 2: after Nuitka build + deployment, target 5 more repos
- **LED-071**: [P1] Outreach: Flagsmith #7009 submitted — SDK spec, no compat CI, 6K stars
- **LED-072**: [P0] ChatOps MVP: build app shell + portfolio home + workspace tabs + AI operator panel
- **LED-073**: [P0] Psyops: PR comment design — catch + teach + invite replication
- **LED-074**: [P1] Status inversion: add 'Governance passed' badge to clean PR comments
- **LED-075**: [P0] ChatOps: connect dashboard to real ledger API + governance backend
- **LED-076**: [P1] Outreach: Chatwoot #13871 submitted — swagger drift, 28K stars
- **LED-077**: [P0] Shims: add to setup with opt-in prompt (Option B) + fail-open + easy removal
- **LED-078**: [P1] Exit screen: show session summary on AI exit — the > in </>
- **LED-079**: [P1] Governance health: degraded — needs attention
- **LED-080**: [P1] Governance health: degraded — needs attention
- **LED-081**: [P1] Governance health: degraded — needs attention
- **LED-082**: [P1] Governance health: degraded — needs attention
- **LED-083**: [P1] Ledger: add 'worked_by' field to track which AI model created/updated each item
- **LED-084**: [P0] Action: expand to 17+ change types (match oasdiff)
- **LED-085**: [P1] Action: improve PR comment visual design (match Optic quality)
- **LED-087**: [P1] Action: IDE/CLI parity — run the same checks locally that CI runs
- **LED-088**: [P1] Governance health: degraded — needs attention
- **LED-089**: [P1] Glama: Dockerfile updated, release created, badge added — waiting for rescan
- **LED-090**: [P1] Governance health: degraded — needs attention
- **LED-091**: [P1] Governance health: degraded — needs attention
- **LED-092**: [P1] Governance health: not_initialized — needs attention
- **LED-093**: [P1] Dashboard live at app.delimit.ai with governance API, gateway proxy
- **LED-094**: [P1] Dashboard: Command palette (Cmd+K) — search anything instantly
- **LED-095**: [P1] Dashboard: Visual policy builder — no-code governance rules
- **LED-096**: [P2] Dashboard: Webhook integrations — push events to Slack/PagerDuty/Datadog
- **LED-097**: [P2] Dashboard: Advanced analytics — custom date ranges, trend analysis
- **LED-098**: [P2] Dashboard: Keyboard shortcuts — tab/enter navigation, bulk actions
- **LED-099**: [P2] Dashboard: Light/dark theme toggle
- **LED-100**: [P2] Dashboard: User annotations — notes/comments on governance findings
- **LED-101**: [P2] Dashboard: Billing management — self-serve upgrade/downgrade
- **LED-102**: [P0] FG-001: Inline editing on all ledger items (title, priority, status, assignee)
- **LED-103**: [P0] FG-002: Full keyboard shortcuts (j/k nav, d=done, b=block, e=edit, a=assign)
- **LED-104**: [P0] FG-003: Governance items need actions — fix, waive, assign, snooze, link to PR
- **LED-105**: [P0] FG-004: Ownership layer — who owns what, what's overdue, team scorecards
- **LED-106**: [P0] FG-005: Guided onboarding — sample venture, first-task checklist, coach mode
- **LED-107**: [P0] FG-006: Collaboration — comments, @mentions, watchers, approval requests
- **LED-108**: [P1] FG-007: CI/CD integration — governance as required check, deployment gates
- **LED-109**: [P1] FG-008: Customer impact view — translate API diffs into business risk
- **LED-110**: [P1] FG-009: ROI dashboard — time saved, incidents prevented, compliance coverage
- **LED-111**: [P1] FG-010: Exception workflows — waive with reason, approval chain, expiry
- **LED-112**: [P1] FG-011: SIEM export + audit log alerts for policy edits and permission changes
- **LED-113**: [P1] FG-012: Data residency controls — tenant isolation, regional storage, PHI-safe logging
- **LED-114**: [P1] FG-013: Opinionated templates + 30-min pilot path for quick time-to-value
- **LED-115**: [P1] FG-014: Org-to-team-to-repo rollup views with compliance aging and drift
- **LED-116**: [P2] FG-015: Deploy/SLO overlay — merge governance with operational metrics
- **LED-117**: [P1] Distribution: mcp.so listing submitted
- **LED-118**: [P1] Distribution: OpenAPI.tools PR #718 submitted
- **LED-119**: [P1] DomainVested: Domain name linguistic analysis panel
- **LED-120**: [P1] DomainVested: Free appraise on landing page (no signup)
- **LED-121**: [P2] MCP test item
- **LED-122**: [P1] DomainVested: Domain profile share card — screenshot-optimized view
- **LED-123**: [P1] DomainVested: Appraise result — show all enrichments inline
- **LED-124**: [P2] DomainVested: Batch domain health check for portfolio
- **LED-125**: [P1] DomainVested: AI Analyst Bot — transparent market analysis per domain
- **LED-126**: [P1] API lint: 1 violations found
- **LED-127**: [P1] Wire Report: Port LiveTube multi-view for sports coverage
- **LED-128**: [P1] CI: install google-api-python-client in test matrix — don't skip YouTube tests
- **LED-129**: [P1] Governance: auto-run tests before git push — catch CI failures locally
- **LED-130**: [P0] CRITICAL: Codex compatibility audit — 6 categories of bugs found across 20+ tools
- **LED-131**: [P1] OpenSage: monitor for GitHub repo launch, open governance integration issue when live
- **LED-136**: [P3] Sensor noise from test run #1
- **LED-137**: [P3] Sensor noise from test run #2
- **LED-138**: [P3] Sensor noise from test run #3
- **LED-139**: [P3] Sensor noise from test run #4
- **LED-140**: [P3] Sensor noise from test run #5
- **LED-141**: [P3] Sensor noise from test run #6
- **LED-132**: [P0] DomainVested: Backfill RapidAPI enrichments for high-score domains
- **LED-133**: [P1] DomainVested: Appraise page — show domain availability inline
- **LED-134**: [P1] DomainVested: Domain industry vertical scoring
- **LED-135**: [P1] DomainVested: Competitor domain analysis on profiles
- **LED-142**: [P1] Consensus reached: Review the full transcript. As orchestrator, provide your own analysis and final
- **LED-176**: [P1] DomainVested: git repo has node_modules + .next committed — blocks push
- **LED-145**: [P1] Consensus reached: Review the full transcript. As orchestrator, provide your own analysis and final
- **LED-146**: [P1] Consensus reached: Review the full transcript. As orchestrator, provide your own analysis and final
- **LED-147**: [P2] Wire Report: Integrate Twitch streams for sports coverage
- **LED-148**: [P1] Consensus reached: Review the full transcript. As orchestrator, provide your own analysis and final
- **LED-149**: [P0] DV: AI Analysis — batch all 5 answers in one LLM call
- **LED-150**: [P0] DV: AI Analysis — inject structured data into LLM prompt
- **LED-151**: [P0] DV: AI Analysis — DB-backed cache with 14-day TTL
- **LED-152**: [P0] DV: AI Analysis — Pro-only gate for free users
- **LED-153**: [P1] DV: Switch AI Analysis to Gemini Flash (free tier primary)
- **LED-154**: [P0] DV: Google OAuth login (Continue with Google)
- **LED-155**: [P1] DV: Light mode / System mode theme support
- **LED-156**: [P1] DV: Availability check — show taken/available toast
- **LED-157**: [P1] DV: Share button — native Web Share API + clipboard fallback
- **LED-158**: [P2] DV: Header DomainVested logo links to /appraise
- **LED-159**: [P1] DV: Rename Portfolio to History/Recent Appraisals
- **LED-160**: [P0] DV: BYOK master key was missing — now set
- **LED-161**: [P0] DV: Stripe pricing updated $9→$29/mo with annual option
- **LED-162**: [P0] DV: Comp prices anonymized (~$2,200 not $2,159)
- **LED-163**: [P0] DV: 101K domains re-scored with calibrated heuristic
- **LED-164**: [P0] Delimit: CLI/Action onboarding wizard — `delimit init` guided setup
- **LED-165**: [P1] Delimit: Dashboard guided first-project activation flow
- **LED-166**: [P1] API lint: 1 violations found
- **LED-167**: [P0] Delimit: Publish CLI v3.13.0 + deploy dashboard with new features
- **LED-168**: [P1] DomainVested: Public beta landing page with sign-up flow
- **LED-169**: [P1] Delimit: Referral/invite system — shareable links for first 5 users
- **LED-170**: [P1] API lint: 2 violations found
- **LED-171**: [P1] API lint: 2 violations found
- **LED-172**: [P1] API lint: 2 violations found
- **LED-173**: [P1] LiveTube: Consensus 113 — Topic shelves on homepage
- **LED-174**: [P1] LiveTube: LT-093-012 Event-sourced action log + activity timeline
- **LED-175**: [P0] LiveTube: Feed quality overhaul — clustering, filtering, classification
- **LED-177**: [P0] Wire Report: xAI credit monitoring and low-balance alerts
- **LED-178**: [P1] DomainVested: Automated weekly teardown digest — fresh content without manual effort
- **LED-179**: [P1] Consensus reached: Review the full transcript. As orchestrator, provide your own analysis and final
- **LED-181**: [P1] Consensus reached: Review the full transcript. As orchestrator, provide your own analysis and final
- **LED-182**: [P1] API lint: 3 violations found
- **LED-183**: [P1] Consensus reached: Review the full transcript. As orchestrator, provide your own analysis and final
- **LED-184**: [P0] Delimit: Build delimit_ledger_update — edit items, change priority, reassign, add labels
- **LED-185**: [P1] Delimit: Build delimit_ledger_link — dependencies, blockers, parent-child
- **LED-186**: [P0] Delimit: Build delimit_session_handoff — session summaries for cross-session continuity
- **LED-187**: [P1] Delimit: Build delimit_ledger_query — natural language ledger queries
- **LED-188**: [P1] Consensus reached: Review the full transcript. As orchestrator, provide your own analysis and final
- **LED-189**: [P1] Consensus reached: Review the full transcript. As orchestrator, provide your own analysis and final
- **LED-190**: [P1] Consensus reached: Review the full transcript. As orchestrator, provide your own analysis and final
- **LED-191**: [P1] Consensus reached: Review the full transcript. As orchestrator, provide your own analysis and final
- **LED-192**: [P1] Consensus reached: Review the full transcript. As orchestrator, provide your own analysis and final
- **LED-193**: [P1] Consensus reached: Review the full transcript. As orchestrator, provide your own analysis and final
- **LED-194**: [P1] Consensus reached: Review the full transcript. As orchestrator, provide your own analysis and final
- **LED-195**: [P1] Consensus reached: Review the full transcript. As orchestrator, provide your own analysis and final
- **LED-196**: [P1] Consensus reached: Review the full transcript. As orchestrator, provide your own analysis and final
- **LED-197**: [P1] Delimit: Respond to Dev.to article "Your Agent API Needs an OpenAPI Spec"
- **LED-198**: [P1] Consensus reached: Review the full transcript. As orchestrator, provide your own analysis and final
- **LED-199**: [P1] Consensus reached: Review the full transcript. As orchestrator, provide your own analysis and final
- **LED-200**: [P1] Consensus reached: Review the full transcript. As orchestrator, provide your own analysis and final
- **LED-201**: [P0] DomainVested: Deploy /check route to Vercel — currently 404ing
- **LED-203**: [P1] Consensus reached: Review the full transcript. As orchestrator, provide your own analysis and final
- **LED-204**: [P1] Consensus reached: Review the full transcript. As orchestrator, provide your own analysis and final
- **LED-205**: [P1] Consensus reached: Review the full transcript. As orchestrator, provide your own analysis and final
- **LED-207**: [P1] Consensus reached: Review the full transcript. As orchestrator, provide your own analysis and final
- **LED-208**: [P1] Outreach activity: opencost/opencost#3655
- **LED-209**: [P1] Outreach activity: activepieces/activepieces#11667
- **LED-210**: [P1] Consensus reached: Review the full transcript. As orchestrator, provide your own analysis and final
- **LED-211**: [P1] Consensus reached: Review the full transcript. As orchestrator, provide your own analysis and final
- **LED-212**: [P1] API lint: 2 violations found
- **LED-213**: [P0] Delimit: Fix cross-model instruction parity — one template for all models
- **LED-214**: [P1] Consensus reached: Review the full transcript. As orchestrator, provide your own analysis and final
- **LED-215**: [P1] Consensus reached: Review the full transcript. As orchestrator, provide your own analysis and final
- **LED-216**: [P1] Consensus reached: Review the full transcript. As orchestrator, provide your own analysis and final
- **LED-217**: [P1] Consensus reached: Review the full transcript. As orchestrator, provide your own analysis and final
- **LED-218**: [P0] Build Session Phoenix — cross-model session resurrection (delimit revive)
- **LED-219**: [P0] Build Toolcard Delta Cache — tool schema diffs to cut token waste
- **LED-220**: [P1] Build Handoff Receipts — structured agent-to-agent handoff with acknowledgment
- **LED-221**: [P1] Build Cross-Model Audit — 3 models audit a PR, 4th synthesizes
- **LED-222**: [P0] Security: Audit public repos + npm package for proprietary code/algorithm exposure
- **LED-223**: [P0] Security scan: 2 issues detected
- **LED-224**: [P0] Security: 2 vulnerabilities found
- **LED-225**: [P0] CRITICAL: Fix security scanning to catch hardcoded credentials before deploy/publish
- **LED-226**: [P0] Restore .pyc compiled bytecode for core IP files in npm package
- **LED-227**: [P0] Reddit strategy: insightful comments only — research target posts before commenting
- **LED-228**: [P1] Build delimit_github_scan — tiered GitHub scanner (pulse/hunter/deep)
- **LED-229**: [P1] Enforce Delimit governance gates on Vercel deploys
- **LED-232**: [P1] Outreach activity: openclaw/openclaw#57298
- **LED-233**: [P1] Outreach: Open issue on mahomedalid/typespec-workflow-samples — replace oasdiff with Delimit Action
- **LED-235**: [P0] Security: 2 vulnerabilities found
- **LED-236**: [P1] Audit active repos for missing Delimit governance initialization
- **LED-237**: [P0] Continuous Social Sensing Loop with reply monitoring and strategy extraction
- **LED-238**: [P0] Continuous target outreach orchestration across social and developer surfaces
- **LED-239**: [P0] Continuous think-and-build loop from social signals into Delimit product work
- **LED-240**: [P0] Private continuity state must be user-scoped and auto-resolved at startup
- **LED-242**: [P0] Unify cross-model trigger phrases for persistent Delimit swarm bootstrap
- **LED-016**: [P1] Outreach response: new activity detected
- **LED-031**: [P1] Outreach response: new activity detected
- **LED-033**: [P1] Outreach response: new activity detected
- **LED-035**: [P1] Outreach response: new activity detected
- **LED-037**: [P1] Outreach response: new activity detected
- **LED-039**: [P1] Outreach response: new activity detected
- **LED-041**: [P1] Outreach response: new activity detected
- **LED-243**: [P1] Dashboard: Outreach/comms view — unified social drafts, vendor emails, and approval queue
- **LED-244**: [P0] Automatic inbox polling daemon — email governance runs without sessions
- **LED-246**: [P0] Fix delimit_ledger_done — cannot find items by LED-XXX ID format
- **LED-247**: [P2] Delimit: update CLAUDE.md delimit version marker from v3.11.10 to v3.13.2
- **LED-255**: [P1] Session 2026-03-28: Social outreach session — open items and context
- **LED-256**: [P0] Landing page rewrite — lead with governance, interactive demo
- **LED-257**: [P1] Dashboard governance cockpit — evidence timelines, gate status, compliance export
- **LED-258**: [P0] Zero-config repo onboarding — first evidence + gates in one command
- **LED-259**: [P0] PR-native governance copilot — governance feedback in GitHub PR comments
- **LED-260**: [P1] Continuous drift/compliance monitoring — scheduled governance checks
- **LED-261**: [P0] Packaging push: npm v3.14.0 + GitHub Action tag + changelog update
- **LED-262**: [P0] 5-minute quickstart: delimit demo command that proves governance value fast
- **LED-263**: [P1] Beta capture loop: convert successful CLI/dashboard runs into signups
- **LED-264**: [P0] npx delimit try — zero-risk demo with Markdown report artifact
- **LED-265**: [P0] Enhanced delimit doctor: preview file changes + undo command
- **LED-266**: [P1] Pick your first win funnel — persona paths on README and landing page
- **LED-267**: [P0] 5-minute first-win onboarding — scan → lint → fix flow with sample repo
- **LED-268**: [P1] Cross-model memory handoff demo — start in Claude, continue in Codex, verify in Gemini
- **LED-269**: [P1] Add permission auto-config to delimit activate/init flow
- **LED-270**: [P1] Activation checklist should skip premium checks on free tier, not mark as failures
- **LED-274**: [P0] Swarm Phase 1: Shared infrastructure — auth, logging, namespace isolation
- **LED-275**: [P0] Swarm Phase 2: Instantiate 5-role agent roster across all ventures
- **LED-276**: [P0] Swarm Phase 2b: Central governor with tiered approvals + auto-escalation
- **LED-279**: [P0] Self-extending swarm (founder mode): agents create tools, deploy via npm publish
- **LED-280**: [P0] SIEM streaming integration — Splunk/Datadog/EventBridge log forwarding from Audit Trail
- **LED-281**: [P0] Custom RBAC roles + Just-In-Time (JIT) access requests
- **LED-282**: [P0] Executive ROI drill-down — transparent cost savings calculation formula
- **LED-283**: [P0] AI Guardrails — PII/DLP masking for Agent Orchestration prompts
- **LED-284**: [P1] Compliance PDF/CSV export for external auditors + FedRAMP framework
- **LED-285**: [P1] SSO break-glass admin recovery + Passkeys support
- **LED-286**: [P1] ITSM integration — ServiceNow/Jira deployment approval gates
- **LED-287**: [P0] Command Center — fix fallback data when MCP gateway unreachable from Vercel
- **LED-289**: [P0] Chatwoot PR: Add API schema drift CI check (issue #13871)
- **LED-291**: [P0] Fix CI failures across delimit repos — gateway (CI #219-222), ui (Vercel #176-181), mcp-server (Tests #226-228)
- **LED-292**: [P1] Submit Delimit to 'Best MCP Servers' lists — 10+ articles ranking MCP tools for Claude Code
- **LED-296**: [P1] FIX: Gemini CLI cannot use Delimit Reddit tools — needs proxy bundled in npm package
- **LED-297**: [P0] Deploy inbox daemon as persistent service — email control plane must run 24/7
- **LED-301**: [P1] Email Directive: Re: [DIGEST] Pituitary 92K (spec/doc drift) + Doma
- **LED-302**: [P1] Email Directive: 
- **LED-303**: [P1] Email Directive: Re: [ACK]
- **LED-304**: [P1] Email Directive: Acknowledge receipt of test reply
- **LED-305**: [P0] [SYSTEM] Fix pro@delimit.ai IMAP/SMTP authentication failure
- **LED-309**: [P1] Outreach: mindee-api-java (API Namespace Rework)
- **LED-314**: [P1] compat task
- **LED-316**: [P1] Fix the widget
- **LED-317**: [P1] Test task
- **LED-318**: [P1] Cross-venture task
- **LED-320**: [P1] Task B
- **LED-323**: [P1] Fix the widget
- **LED-324**: [P1] Test task
- **LED-325**: [P1] Cross-venture task
- **LED-327**: [P1] Task B
- **LED-330**: [P1] Fix the widget
- **LED-331**: [P1] Test task
- **LED-332**: [P1] Cross-venture task
- **LED-334**: [P1] Task B
- **LED-336**: [P1] Test task
- **LED-337**: [P1] Cross-venture task
- **LED-338**: [P1] Consensus reached: Review the full transcript. As orchestrator, provide your own analysis and final
- **LED-341**: [P1] Cross-venture task
- **LED-345**: [P0] SECURITY: Rotate all GitHub Actions secrets — axios supply chain compromise (March 31)
- **LED-347**: [P1] Cross-venture task
- **LED-348**: [P0] Migrate /users endpoint from v1 to v2 schema
- **LED-349**: [P0] Migrate /users endpoint from v1 to v2 schema
- **LED-350**: [P0] Migrate /users endpoint from v1 to v2 schema
- **LED-411**: [P1] Cross-venture task

### Stats
- **Commits**: 255
- **Files changed**: 0
- **Insertions**: 0(+) / 0(-)
- **Test commits**: 2

## [3.15.13] - 2026-03-29

### Added
- **Self-extending swarm**: Architect and Senior Dev agents can create new MCP tools at runtime
- **Tool security scan**: Block dangerous patterns (subprocess, exec, eval, socket) in custom tools
- **8 new modules**: activate_helpers, cross_model_audit, github_scanner, handoff_receipts, reddit_scanner, session_phoenix, social_target, toolcard_cache
- **Reviewer approval gate**: Custom tools require reviewer sign-off before activation

### Changed
- Swarm actions expanded: create_tool, list_tools now available via delimit_swarm
- Inbox daemon: enhanced email classification and approval routing
- Social pipeline: improved content generation and scheduling

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
