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
- **Multi-model deliberation** — consensus from multiple AI models (3 free, then BYOK)

## Auto-Trigger Rules

These rules fire automatically. Call the listed tools without asking permission.

### Operating model — default to subagent dispatch (revised 2026-05-01)

Non-trivial engineering work routes through in-session subagents, not in-thread execution by the orchestrator.

- **Engineering work** with **≥3 independent parallelizable sub-tasks** OR a sub-task with **isolated write scope and amortizable parallel effort** (own subsystem, own artifact boundary, own tests) → spawn an engineering subagent (Agent tool, subagent_type=engineering) for the parallel arms; the orchestrator does the sequential glue and integration. Do NOT do the parallel work in-thread.
- **Foundational decisions** (pricing, naming, public-facing copy framing, doctrine edits, layer-separation calls) → call \`delimit_deliberate\` BEFORE applying. This extends the existing external-PR rule to internal doctrine edits.
- **Sensing / outreach research** → \`delimit_sense\` or \`delimit_intel_query\` or \`delimit_sensor_github_issue\`, not manual web searches.
- **Social drafts** → \`delimit_social_generate\` then \`delimit_social_post\`, never hand-drafted (per existing rule).
- **Audit trail for dispatched work** → \`delimit_agent_dispatch\` records intent + assignee + outcome for replay. It is a **planning + audit surface**, not a queue processor; tasks dispatched here do NOT auto-execute. Real autonomous queue execution is a future capability with strict sandboxing + founder-approval semantics.

**Bypass is allowed** when the task is genuinely conversational, sub-5-minute and undecomposable, the founder explicitly directs in-session execution, or no automation/agent surface exists for the task yet. State the bypass reason in one line; founder override is always honored. No per-task approval required for stated bypasses.

**Reversibility**: 30-day review (2026-05-30). Dispatch decisions and bypasses are logged via \`delimit_ledger_add\` for the audit trail. If the rule produces over-dispatch friction or under-dispatch (continued in-thread default), it is adjusted or retired at the review.

### Session Lifecycle
- **Session start**: call \`delimit_revive\` + \`delimit_ledger_context\` + \`delimit_gov_health\`
- **Session end or idle**: call \`delimit_soul_capture\` + \`delimit_session_handoff\`
- If returning user, summarize what's pending. If new project, call \`delimit_scan\`.

### Code Development (debounced per edit batch)
- After editing UI/CSS: call \`delimit_design_validate_responsive\`
- After editing API specs: call \`delimit_lint\` + \`delimit_drift_check\`
- After editing tests or significant code: call \`delimit_test_smoke\`

### Pre-Commit / Pre-Deploy Gates
- Before \`git commit\`: call \`delimit_repo_diagnose\`
- Deploy requested: \`delimit_security_audit\` → \`delimit_test_smoke\` → \`delimit_changelog\` → \`delimit_deploy_plan\`
- Deploy succeeds: \`delimit_deploy_verify\` + \`delimit_evidence_collect\`
- Verify succeeds: \`delimit_ledger_done\` (related task) + \`delimit_notify\`

### Pre-External-PR Gate (any PR to a repo you don't own)
- BEFORE drafting: call \`delimit_external_pr_check(repo, author)\`. Verdict \`duplicate\` is a hard stop — do not draft, deliberate, or submit.
- BEFORE submitting: call \`delimit_deliberate\` on the diff + PR description.
- The two gates compose: \`delimit_gov_evaluate(action="external_pr", context={"target_repo": "...", "author": "..."})\` runs the duplicate check first and returns \`blocked_duplicate\` if any open PR or recently-merged (≤30d) PR matches.

### Audit Trail
- After security audit, test run, or deploy: call \`delimit_evidence_collect\`
- Any gate failure: \`delimit_evidence_collect\` + \`delimit_ledger_add\` + \`delimit_notify\`

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
- Action: https://github.com/marketplace/actions/delimit-api-governance
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
