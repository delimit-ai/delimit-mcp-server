#!/usr/bin/env node
/**
 * Delimit Governance Rules for Cursor
 *
 * Cursor doesn't have a hook system like Claude Code or Codex,
 * so governance enforcement happens server-side via MCP tool calls.
 * This adapter manages the .cursorrules and .cursor/rules/ files
 * that guide Cursor's behavior.
 */

const fs = require('fs');
const path = require('path');
const { upsertManagedSection } = require('../lib/managed-section');

// LED-213: Import canonical template for cross-model parity
const { getDelimitSection } = require('../lib/delimit-template');

const HOME = process.env.HOME || '';
const CURSOR_DIR = path.join(HOME, '.cursor');
const CURSOR_RULES_DIR = path.join(CURSOR_DIR, 'rules');
const CURSORRULES_FILE = path.join(HOME, '.cursorrules');

/**
 * Install Delimit governance rules into Cursor.
 * Creates both .cursorrules (legacy) and .cursor/rules/delimit.md (new).
 */
function installRules(version) {
    const rules = getDelimitRules(version);

    // Install to .cursor/rules/delimit.md (new location, Cursor 0.45+).
    // LED-1180 follow-up: use upsertManagedSection so user-customized
    // content above/below the delimit:start/end markers is preserved.
    // The previous implementation did fs.writeFileSync(rulesFile, rules)
    // — full overwrite — which clobbered any user customizations on every
    // `delimit-cli setup`.
    let action = 'unchanged';
    let rulesFile = null;
    if (fs.existsSync(CURSOR_DIR)) {
        fs.mkdirSync(CURSOR_RULES_DIR, { recursive: true });
        rulesFile = path.join(CURSOR_RULES_DIR, 'delimit.md');
        const result = upsertManagedSection(rulesFile, rules, version);
        action = result.action;
    }

    return {
        installed: true,
        action,
        paths: [CURSORRULES_FILE, path.join(CURSOR_RULES_DIR, 'delimit.md')],
    };
}

/**
 * Remove Delimit rules from Cursor.
 */
function uninstallRules() {
    const removed = [];

    // Remove from .cursor/rules/
    const rulesFile = path.join(CURSOR_RULES_DIR, 'delimit.md');
    if (fs.existsSync(rulesFile)) {
        fs.unlinkSync(rulesFile);
        removed.push(rulesFile);
    }

    return { removed };
}

function getDelimitRules(version) {
    // LED-213: Use canonical Consensus 123 template for Cursor parity
    return getDelimitSection();
}

module.exports = { installRules, uninstallRules, getDelimitRules };

// CLI entry point
if (require.main === module) {
    const action = process.argv[2] || 'install';
    const version = process.argv[3] || '3.11.9';
    if (action === 'install') {
        const result = installRules(version);
        console.log(`Installed Delimit rules to Cursor: ${result.paths.join(', ')}`);
    } else if (action === 'uninstall') {
        const result = uninstallRules();
        console.log(`Removed: ${result.removed.join(', ') || 'nothing to remove'}`);
    }
}
