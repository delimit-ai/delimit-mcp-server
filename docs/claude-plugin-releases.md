# Delimit Claude plugin: release provenance

One row per plugin version (`claude-plugin/.claude-plugin/plugin.json`). The
rows are append-only, and their versions must strictly increase. CI checks this
file with `scripts/claude-plugin-release-guard.js`. The procedure is in
[claude-plugin-release.md](claude-plugin-release.md).

This file is kept outside `claude-plugin/` on purpose. The directory scans that
folder, so a provenance edit there would look like a new plugin version.

A listing in the Claude plugin directory, a passed directory scan and a clean
install show only that the plugin is distributed and installs. They do not
show demand, and they are not a certification or an endorsement by Anthropic.

| Version | Source commit | Tag | delimit-cli pin | Directory scan result + warnings | Published at | Clean-install acceptance |
|---|---|---|---|---|---|---|
| 1.0.0 | `430bb43` (#246, 2026-09-25 23:50 ET) | `delimit--v1.0.0` | 4.20.0 | Not submitted. Superseded before the 2026-09-26 10:20 ET submission (LED-5721). | Not published | Not run against the directory |
| 1.0.1 | `dd03b6d` (#248, 2026-09-26 01:02 ET) | `delimit--v1.0.1` | 4.20.0 | Not submitted. Superseded before submission. | Not published | Not run against the directory |
| 1.0.2 | `1c0a288` (#249, 2026-09-26 07:14 ET) | `delimit--v1.0.2` | 4.20.0 | Not submitted. Superseded before submission. | Not published | Not run against the directory |
| 1.0.3 | `041b0ad` (#250, 2026-09-26 08:31 ET) | `delimit--v1.0.3` | 4.20.1 | Not submitted. Superseded before submission. | Not published | Not run against the directory |
| 1.0.4 | `9b417f8` (#251, 2026-09-26 08:57 ET; full SHA `9b417f8061bf40bbf3bf93abd18cab0234154bac`) | `delimit--v1.0.4` | 4.20.1 | Submitted 2026-09-26 10:20 ET. The security scan passed, then the version was held for content-policy review because it launches a pinned npx package (LED-5721). The portal shows "Scan passed, with directory policy warnings" and "Version passed, ready to publish" (about 2026-10-01 20:30 ET). Which warnings: pending the owner's screenshot of the Review tab. The public directory cache reports `checks.review.state: "none"`. | 2026-10-02 16:28 ET. The owner clicked Publish at 16:27 ET in the directory portal; Auto-publish was Off. Listed for Claude Code and Cowork. | Passed 2026-10-02 about 16:44 ET, in a throwaway HOME with no claude.ai login, on Claude Code 2.1.288. `claude plugin install delimit@anthropic-plugin-directory` installed `1.0.4-9b417f8061bf`, and all 8 files match `9b417f8` by sha256. The exact `.mcp.json` command completed the MCP handshake: delimit 4.20.1, 10 records tools. The cold start took 26 s, including the npx fetch and venv; the warm start took 2 s. A ledger write, its read-back and a handoff all passed. `claude mcp list` reported Connected, and `claude plugin validate` passed. Not tested: the interactive /plugin Discover view, Cowork, and a signed-in `@synced` install. |
