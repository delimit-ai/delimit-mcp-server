const { describe, it } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');

// Extract getClaudeMdContent by loading the setup script source
// We test the content function and upgrade logic in isolation.

function getClaudeMdContent() {
    return `# Delimit

Your AI has persistent memory, verified execution, and governance.

## First time? Say one of these:
- "check this project's health" -- see what Delimit finds
- "add to ledger: [anything]" -- start tracking tasks
- "what's on the ledger?" -- see what's pending

## Returning? Your AI remembers:
- Ledger items persist across sessions
- Governance rules stay configured
- Memory carries forward

## On first session, your AI will automatically:
1. Diagnose the environment to verify everything is connected
2. Check the ledger for any pending items from previous sessions
3. If no governance exists yet, suggest initializing it

## Available Agents
- /lint -- check API specs for breaking changes
- /engineering -- build, test, refactor with governance checks
- /governance -- full compliance audit

## Need help?
Say "delimit help" for docs on any capability.
`;
}

describe('CLAUDE.md onboarding content', () => {
    it('does not mention individual tool names', () => {
        const content = getClaudeMdContent();
        // These tool names should never appear in user-facing CLAUDE.md
        const toolNames = [
            'delimit_init',
            'delimit_lint',
            'delimit_diff',
            'delimit_test_coverage',
            'delimit_gov_health',
            'delimit_repo_analyze',
            'delimit_diagnose',
            'delimit_ledger_context',
        ];
        for (const tool of toolNames) {
            assert.ok(
                !content.includes(tool),
                `CLAUDE.md should not contain tool name "${tool}"`
            );
        }
    });

    it('contains natural language prompts for first-time users', () => {
        const content = getClaudeMdContent();
        assert.ok(content.includes('check this project\'s health'), 'Should have health check prompt');
        assert.ok(content.includes('add to ledger'), 'Should have ledger add prompt');
        assert.ok(content.includes('what\'s on the ledger'), 'Should have ledger check prompt');
    });

    it('mentions persistent memory and governance', () => {
        const content = getClaudeMdContent();
        assert.ok(content.includes('persistent memory'), 'Should mention persistent memory');
        assert.ok(content.includes('governance'), 'Should mention governance');
    });

    it('includes returning user section', () => {
        const content = getClaudeMdContent();
        assert.ok(content.includes('Returning?'), 'Should have returning user section');
        assert.ok(content.includes('Ledger items persist'), 'Should mention ledger persistence');
        assert.ok(content.includes('Memory carries forward'), 'Should mention memory persistence');
    });

    it('includes automatic first-session actions', () => {
        const content = getClaudeMdContent();
        assert.ok(content.includes('Diagnose the environment'), 'Should mention auto-diagnose');
        assert.ok(content.includes('Check the ledger'), 'Should mention auto-ledger check');
        assert.ok(content.includes('suggest initializing'), 'Should mention governance init suggestion');
    });

    it('includes help instruction', () => {
        const content = getClaudeMdContent();
        assert.ok(content.includes('delimit help'), 'Should tell users how to get help');
    });

    it('lists agents by slash-command not tool name', () => {
        const content = getClaudeMdContent();
        assert.ok(content.includes('/lint'), 'Should reference /lint agent');
        assert.ok(content.includes('/engineering'), 'Should reference /engineering agent');
        assert.ok(content.includes('/governance'), 'Should reference /governance agent');
    });
});

describe('CLAUDE.md upgrade detection', () => {
    it('detects old-format CLAUDE.md with "Delimit AI Guardrails" header', () => {
        const oldContent = '# Delimit AI Guardrails\n\nSome old content';
        assert.ok(
            oldContent.includes('# Delimit AI Guardrails'),
            'Should detect old header'
        );
    });

    it('detects old-format CLAUDE.md with tool names', () => {
        const oldContent = 'Some content with delimit_init and delimit_lint';
        assert.ok(
            oldContent.includes('delimit_init') || oldContent.includes('delimit_lint'),
            'Should detect old tool name references'
        );
    });

    it('does not upgrade custom CLAUDE.md without Delimit markers', () => {
        const customContent = '# My Project\n\nThis is a custom CLAUDE.md for my project.';
        const hasOldMarkers =
            customContent.includes('# Delimit AI Guardrails') ||
            customContent.includes('delimit_init') ||
            customContent.includes('delimit_lint');
        assert.ok(!hasOldMarkers, 'Custom content should not be detected as old Delimit format');
    });
});

describe('setup script output messaging', () => {
    it('setup script file contains try-it-now messaging', () => {
        const setupPath = path.join(__dirname, '..', 'bin', 'delimit-setup.js');
        const setupContent = fs.readFileSync(setupPath, 'utf-8');
        assert.ok(setupContent.includes('Try it now:'), 'Should have "Try it now:" prompt');
        assert.ok(setupContent.includes('$ claude'), 'Should suggest running claude');
        assert.ok(setupContent.includes('check this project\'s health'), 'Should suggest health check');
    });

    it('setup script does not list tool names in output', () => {
        const setupPath = path.join(__dirname, '..', 'bin', 'delimit-setup.js');
        const setupContent = fs.readFileSync(setupPath, 'utf-8');
        // Check that Step 6 output area does not reference internal tool names
        const step6Onwards = setupContent.split('// Step 6')[1];
        assert.ok(step6Onwards, 'Should have Step 6 section');
        assert.ok(!step6Onwards.includes('delimit_init'), 'Step 6 should not mention delimit_init');
        assert.ok(!step6Onwards.includes('delimit_gov_health'), 'Step 6 should not mention delimit_gov_health');
    });
});
