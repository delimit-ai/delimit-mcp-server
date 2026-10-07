# Delimit Claude plugin: release provenance

One row per (plugin, version), for every plugin listed in
`.claude-plugin/marketplace.json`. The first cell is the plugin name from the
marketplace entry, the second its `plugin.json` version. Rows are append-only,
and each plugin's versions must strictly increase. CI checks this file with
`scripts/claude-plugin-release-guard.js`. The procedure is in
[claude-plugin-release.md](claude-plugin-release.md).

This file is kept outside every plugin folder on purpose. The directory scans
the plugin folder, so a provenance edit there would look like a new plugin
version.

Only `delimit` (folder `claude-plugin/`) is listed in the Claude plugin
directory: listing `plugin_018cApt644QshHNw7fmjQn64`, install id
`delimit@anthropic-plugin-directory`.

A listing in the Claude plugin directory, a passed directory scan and a clean
install show only that the plugin is distributed and installs. They do not
show demand, and they are not a certification or an endorsement by Anthropic.

| Plugin | Version | Source commit | Tag | delimit-cli pin | Directory scan result + warnings | Published at | Clean-install acceptance |
|---|---|---|---|---|---|---|---|
| delimit | 1.0.0 | `430bb43` (#246, 2026-09-25 23:50 ET) | `delimit--v1.0.0` | 4.20.0 | Not submitted. Superseded before the 2026-09-26 10:20 ET submission (LED-5721). | Not published | Not run against the directory |
| delimit | 1.0.1 | `dd03b6d` (#248, 2026-09-26 01:02 ET) | `delimit--v1.0.1` | 4.20.0 | Not submitted. Pre-submission portal validation (owner, 2026-09-26, per ledger note (written 07:15 ET; icon fix committed 07:14 ET); ledger record LED-5721, 2026-09-26T11:15Z): "7 checks, 1 warning, 1 policy hold". Warning: no icon (fixed in 1.0.2). Policy hold: "Runs a pinned npx package — prefer a package that ships a lockfile, or vendor readable source" (answered by delimit-cli 4.20.1 shipping npm-shrinkwrap.json, pinned in 1.0.3). Informational: "Local MCP server not on claude.ai". Superseded before submission. | Not published | Not run against the directory |
| delimit | 1.0.2 | `1c0a288` (#249, 2026-09-26 07:14 ET) | `delimit--v1.0.2` | 4.20.0 | Not submitted. Superseded before submission. | Not published | Not run against the directory |
| delimit | 1.0.3 | `041b0ad` (#250, 2026-09-26 08:31 ET) | `delimit--v1.0.3` | 4.20.1 | Not submitted. Superseded before submission. | Not published | Not run against the directory |
| delimit | 1.0.4 | `9b417f8` (#251, 2026-09-26 08:57 ET); reviewed_commit `9b417f8061bf40bbf3bf93abd18cab0234154bac` | `delimit--v1.0.4` | 4.20.1 | Submitted 2026-09-26 10:20 ET at tag `delimit--v1.0.4`, scheduled check only (ledger record LED-5721, 2026-09-26T14:21Z). Security scan passed 10:30 ET, then held for content-policy review because it runs a pinned npx package (ledger record LED-5721, 2026-09-26T14:30Z). Portal, about 2026-10-01 20:30 ET: "Scan passed, with directory policy warnings", "Version passed, ready to publish". Review tab (owner screenshot 2026-10-02 16:45 ET), informational, no action required: `UNKNOWN_KEY` x4, "Unrecognized field in plugin.json" (documentationUrl, privacyPolicyUrl, supportUrl, termsOfServiceUrl; the directory reads them, Claude Code ignores them); `LOCAL_MCP_SERVER`, "Runs a local program for an MCP server"; "Local MCP server: not on claude.ai" (.mcp.json · mcpServers.delimit is stdio). Waived by review: `LAUNCHER_PACKAGE_REVIEW`, "Runs a pinned npx or uvx package" (.mcp.json · mcpServers.delimit). The public directory cache reports `checks.review.state: "none"`. | 2026-10-02 16:28 ET. The owner clicked Publish at 16:27 ET in the directory portal; Auto-publish was Off. Listed for Claude Code and Cowork. | Passed 2026-10-02 about 16:44 ET, in a throwaway HOME with no claude.ai login, on Claude Code 2.1.288. `claude plugin install delimit@anthropic-plugin-directory` installed `1.0.4-9b417f8061bf` (listing `plugin_018cApt644QshHNw7fmjQn64`), and all 8 files match `9b417f8` by sha256. The exact `.mcp.json` command completed the MCP handshake: delimit 4.20.1, 10 records tools. The cold start took 26 s, including the npx fetch and venv; the warm start took 2 s. A ledger write, its read-back and a handoff all passed. `claude mcp list` reported Connected, and `claude plugin validate --strict` passed. Not tested: the interactive /plugin Discover view, Cowork, and a signed-in `@synced` install. |
| delimit | 1.0.5 | `8a9b66f` (#270, 2026-10-05 22:16 ET); head `7fe8589e4b199dcc43bd25dcfd0296c59036c7a8` | `delimit--v1.0.5` (pending) | 4.21.0 | Not yet submitted. Adds SessionStart hook to auto-surface open work across sessions. Tag to be cut before submission. | Not yet published | Not yet run |
| delimit | 1.0.6 | `c078b34` (#271, 2026-10-05 22:22 ET); head `af7a294ef4eefca5e14998f4361bf427c8fc6646` | `delimit--v1.0.6` (pending) | 4.21.0 | Not yet submitted. Pre-review improvements: Python floor 3.10, hash-locked deps (requirements-plugin.lock), privacy docs. Tag to be cut before submission. | Not yet published | Not yet run |
| delimit | 1.0.7 | `916b73d` (#273, 2026-10-07) | `delimit--v1.0.7` | 4.21.0 | Submitted but held: "Policy hold" with 4 new findings vs 1.0.4 — 4 unrecognized keyword values (continuity, context, state, sessions) and anchor lock missing for resume.mjs. Fixed in 1.0.8. | Not yet published | Not yet run |
| delimit | 1.0.8 | pending (this PR, #274, 2026-10-07) | `delimit--v1.0.8` (pending) | 4.21.0 | Fixes 4 Policy Hold findings from v1.0.7: removes 4 unrecognized keywords from plugin.json (continuity, context, state, sessions), restoring keywords to the approved v1.0.4 set (records, tasks, handoffs, mcp). Adds hooks/package.json + hooks/package-lock.json to satisfy anchor lock requirement for resume.mjs (no npm deps; Node built-ins only). No functional changes. | Not yet published | Not yet run |
| delimit-governance | 1.0.0 | pending (this PR) | `delimit-governance--v1.0.0` (pending) | N/A | Not yet submitted. New plugin providing governance skills: api-check, explain-change, spec-health. | Not yet published | Not yet run |
| delimit-panel | 1.0.0 | pending (this PR) | `delimit-panel--v1.0.0` (pending) | N/A | Not yet submitted. New plugin providing multi-model panel skills: panel-setup, panel. | Not yet published | Not yet run |
