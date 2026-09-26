/**
 * LED-213: Canonical Delimit instruction template.
 *
 * Single source of truth for the governance section injected into
 * CLAUDE.md, Codex instructions.md, Gemini GEMINI.md / customInstructions,
 * and Cursor .cursorrules.  All models get identical Consensus 123 content.
 */

const fs = require('fs');
const path = require('path');

/**
 * Return the full Consensus 123 governance template (markdown).
 * Used verbatim by Claude, Codex, and Cursor.
 */
function getDelimitSection() {
    const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'package.json'), 'utf-8'));
    const version = pkg.version || '0.0.0';
    return `<!-- delimit:start v${version} -->
# Delimit

Unify all AI coding assistants with persistent context, governance, and multi-model debate.

## What Delimit does
- **API governance** — lint, diff, semver classification, migration guides
- **Persistent context** — memory and ledger survive across sessions and models
- **Security audit** — dependency scanning, secret detection
- **Test verification** — confirm tests actually ran, measure coverage
- **Multi-model deliberation** — uses your configured model CLIs or API keys

## Auto-Trigger Rules

These rules fire automatically. Call the listed tools without asking permission.

### Session Lifecycle
- **Session start (do this FIRST, before anything else)**: call \`delimit_revive\` and load the returned soul — the last task, key decisions, and next steps. This is how you resume after switching agents (e.g. Claude Code → Codex) without losing the plot. Then \`delimit_ledger_context\` + \`delimit_gov_health\`.
- **Session end or idle**: call \`delimit_soul_capture\` + \`delimit_session_handoff\`
- If returning user, summarize what's pending. If new project, call \`delimit_scan\`.

### Memory Rules
Delimit captures routine code changes from git automatically. You do NOT need to call \`memory_store\` for normal work.

Use \`delimit_memory_store\` when capturing:
- **Failed approaches** — "tried X, it didn't work because Y" (git only shows what survived, not dead ends)
- **Architecture decisions** — "chose PostgreSQL over Redis for sessions because Z"
- **Key context** — customer requirements, API constraints, deployment gotchas
- **Things that will confuse the next session** — non-obvious setups, workarounds, tech debt

Do NOT store: routine code changes, file lists, commit messages, status updates — these are in git.

### Code Development (debounced per edit batch)
- After editing UI/CSS: call \`delimit_design_validate_responsive\`
- After editing API specs: call \`delimit_lint\` + \`delimit_drift_check\`
- After editing tests or significant code: call \`delimit_test_smoke\`

### Pre-Commit / Pre-Deploy Gates
- Before \`git commit\`: call \`delimit_repo_diagnose\`
- Deploy requested: \`delimit_security_audit\` → \`delimit_test_smoke\` → \`delimit_changelog\` → \`delimit_deploy_plan\`
- Deploy succeeds: \`delimit_deploy_verify\` + \`delimit_evidence_collect\`
- Verify succeeds: \`delimit_ledger_done\` (related task)

### Pre-External-PR Gate (any PR to a repo you don't own)
- BEFORE drafting: call \`delimit_external_pr_check(repo, author)\`. Verdict \`duplicate\` is a hard stop — do not draft or submit.
- BEFORE submitting: call \`delimit_deliberate\` on the diff + PR description (uses your configured model CLIs or API keys).

### Audit Trail
- After security audit, test run, or deploy: call \`delimit_evidence_collect\`
- Any gate failure: \`delimit_evidence_collect\` + \`delimit_ledger_add\`

Ask the user before publishing, sending messages, deploying, or making other external changes unless they have already authorized the action.

## GitHub Action
Add breaking change detection to any repo:
\`\`\`yaml
- uses: delimit-ai/delimit-action@v1
  with:
    spec: api/openapi.yaml
\`\`\`

## Project-specific overrides

You can add your own rules anywhere **outside** the \`<!-- delimit:start -->\` / \`<!-- delimit:end -->\` markers in this file — \`delimit-cli\` upgrades only touch content between the markers and preserve everything else.

For user-global overrides (rules that apply to every project and every Claude Code session on this machine), put them in \`~/.claude/CLAUDE.md\` or \`~/.delimit/CLAUDE.md\`. Those files are never shipped in the npm package and never overwritten by \`delimit-cli setup\`.

## Links
- Docs: https://delimit.ai/docs
- GitHub: https://github.com/delimit-ai/delimit-mcp-server
- Action: https://github.com/marketplace/actions/delimit-merge-gate-for-ai-written-code
<!-- delimit:end -->`;
}

/**
 * Return a condensed single-line version for Gemini customInstructions (JSON string value).
 * Newlines are literal \\n so it fits inside a JSON string.
 */
function getDelimitSectionCondensed() {
    return getDelimitSection()
        .replace(/<!-- delimit:start[^>]*-->\n?/, '')
        .replace(/<!-- delimit:end -->\n?/, '')
        .replace(/\n/g, '\\n')
        .trim();
}

module.exports = { getDelimitSection, getDelimitSectionCondensed };
