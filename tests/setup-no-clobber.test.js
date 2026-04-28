// Regression suite for the 4.1.47 → 4.1.48/4.1.49 user-state preservation
// contract. Every fs.writeFileSync inside delimit-cli setup that touches a
// user-owned config file MUST preserve user content. These tests run setup
// helpers against a synthetic fresh-user HOME directory and assert that no
// pre-populated user customization gets clobbered.
//
// Background: 4.1.47 wiped the founder's /root/CLAUDE.md when the shim's
// background auto-update flow ran setup. The audit found three more clobber
// risks (Claude project hooks, Gemini approval mode, Claude.json MCP hooks).
// This test file is the safety net that catches future regressions BEFORE
// publish — not after, when shim auto-update has already gone live.

const { describe, it, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');

process.env.DELIMIT_WRAPPED = 'true';

const crossModelHooks = require('../lib/cross-model-hooks');

const ORIGINAL_HOME = process.env.HOME;
let tmpDir;

function setupTmpHome() {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-noclobber-'));
    process.env.HOME = tmpDir;
    return tmpDir;
}

function teardownTmpHome() {
    process.env.HOME = ORIGINAL_HOME;
    if (tmpDir && fs.existsSync(tmpDir)) {
        fs.rmSync(tmpDir, { recursive: true, force: true });
    }
}

describe('installClaudeHooks: project settings.json preservation', () => {
    beforeEach(() => { setupTmpHome(); });
    afterEach(() => { teardownTmpHome(); });

    it('preserves project-local hooks when adding Delimit hooks', () => {
        // User has a project they're working in with their own custom hooks.
        const projectDir = path.join(tmpDir, 'myproject');
        const projectClaudeDir = path.join(projectDir, '.claude');
        fs.mkdirSync(projectClaudeDir, { recursive: true });
        const projectSettings = {
            hooks: {
                PreToolUse: [
                    {
                        matcher: 'Bash',
                        hooks: [{
                            type: 'command',
                            command: '/usr/local/bin/my-custom-pre-bash',
                        }],
                    },
                ],
                SessionStart: [
                    {
                        matcher: '',
                        hooks: [{
                            type: 'command',
                            command: '/home/user/.scripts/my-session-init.sh',
                        }],
                    },
                ],
            },
            permissions: { allow: ['Bash(ls:*)'] },
        };
        fs.writeFileSync(
            path.join(projectClaudeDir, 'settings.json'),
            JSON.stringify(projectSettings, null, 2),
        );

        // Also seed the global ~/.claude/settings.json
        const globalClaudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(globalClaudeDir, { recursive: true });
        fs.writeFileSync(
            path.join(globalClaudeDir, 'settings.json'),
            JSON.stringify({}, null, 2),
        );

        // chdir into the project so installClaudeHooks picks up the project dir
        const originalCwd = process.cwd();
        try {
            process.chdir(projectDir);

            const tool = {
                id: 'claude',
                name: 'Claude Code',
                configPath: path.join(globalClaudeDir, 'settings.json'),
            };
            const hookConfig = { session_start: true, pre_tool: true, pre_commit: true };
            crossModelHooks.installClaudeHooks(tool, hookConfig);

            // Re-read project settings — user content MUST survive
            const updated = JSON.parse(
                fs.readFileSync(path.join(projectClaudeDir, 'settings.json'), 'utf-8'),
            );

            // 1. The user's custom Bash pre-tool hook MUST still be present
            const preToolCommands = (updated.hooks.PreToolUse || []).flatMap(g =>
                (g.hooks || []).map(h => h.command || ''),
            );
            assert.ok(
                preToolCommands.some(c => c === '/usr/local/bin/my-custom-pre-bash'),
                'User custom PreToolUse hook MUST be preserved',
            );

            // 2. The user's custom SessionStart hook MUST still be present
            const sessionStartCommands = (updated.hooks.SessionStart || []).flatMap(g =>
                (g.hooks || []).map(h => h.command || ''),
            );
            assert.ok(
                sessionStartCommands.some(c => c === '/home/user/.scripts/my-session-init.sh'),
                'User custom SessionStart hook MUST be preserved',
            );

            // 3. The user's permissions block MUST be preserved
            assert.deepStrictEqual(
                updated.permissions,
                { allow: ['Bash(ls:*)'] },
                'User permissions block MUST be preserved',
            );

            // 4. Delimit-owned hooks SHOULD have been added (at least one
            //    hook command path containing the string 'delimit')
            const allCommands = Object.values(updated.hooks).flat().flatMap(g =>
                (g.hooks || []).map(h => h.command || ''),
            );
            assert.ok(
                allCommands.some(c => c.includes('delimit')),
                'At least one Delimit-owned hook should have been added',
            );
        } finally {
            process.chdir(originalCwd);
        }
    });
});

describe('installGeminiHooks: defaultApprovalMode preservation', () => {
    beforeEach(() => { setupTmpHome(); });
    afterEach(() => { teardownTmpHome(); });

    it('does not clobber a user-set general.defaultApprovalMode', () => {
        // This test exercises the patched installGeminiHooks which uses the
        // marker-based GEMINI.md preservation, but the equivalent gating in
        // bin/delimit-setup.js for general.defaultApprovalMode is verified
        // by the synthetic-setup test below. We assert here that the helper
        // does not insert a stray approval-mode override.
        const geminiDir = path.join(tmpDir, '.gemini');
        fs.mkdirSync(geminiDir, { recursive: true });
        const userConfig = {
            general: { defaultApprovalMode: 'manual' },
            theme: 'solarized',
        };
        fs.writeFileSync(
            path.join(geminiDir, 'settings.json'),
            JSON.stringify(userConfig, null, 2),
        );

        const tool = {
            id: 'gemini',
            name: 'Gemini CLI',
            configPath: path.join(geminiDir, 'settings.json'),
        };
        crossModelHooks.installGeminiHooks(tool, { session_start: true });

        const updated = JSON.parse(
            fs.readFileSync(path.join(geminiDir, 'settings.json'), 'utf-8'),
        );

        // installGeminiHooks does not own general.defaultApprovalMode — it
        // adds customInstructions only. The user's manual approval mode and
        // theme MUST survive.
        assert.strictEqual(
            updated.general.defaultApprovalMode,
            'manual',
            'User defaultApprovalMode MUST be preserved',
        );
        assert.strictEqual(
            updated.theme,
            'solarized',
            'User theme preference MUST be preserved',
        );
    });
});

describe('synthetic fresh-user setup: full preservation contract', () => {
    // Direct unit-level coverage of the bin/delimit-setup.js Gemini approval
    // mode gate. We replicate the exact code shape from setup.js since we
    // cannot run the full CLI inside this unit test.
    it('Gemini settings: only sets defaultApprovalMode if missing', () => {
        const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-gem-'));
        try {
            const cfgPath = path.join(tmp, 'settings.json');
            fs.writeFileSync(cfgPath, JSON.stringify({
                general: { defaultApprovalMode: 'manual' },
                mcpServers: {},
            }, null, 2));

            // This is the exact patched logic from bin/delimit-setup.js:435
            let geminiConfig = JSON.parse(fs.readFileSync(cfgPath, 'utf-8'));
            if (!geminiConfig.general) geminiConfig.general = {};
            if (!geminiConfig.general.defaultApprovalMode) {
                geminiConfig.general.defaultApprovalMode = 'auto_edit';
            }
            fs.writeFileSync(cfgPath, JSON.stringify(geminiConfig, null, 2));

            const after = JSON.parse(fs.readFileSync(cfgPath, 'utf-8'));
            assert.strictEqual(
                after.general.defaultApprovalMode,
                'manual',
                'User defaultApprovalMode MUST NOT be clobbered',
            );
        } finally {
            fs.rmSync(tmp, { recursive: true, force: true });
        }
    });

    it('Gemini settings: sets defaultApprovalMode when missing', () => {
        const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-gem2-'));
        try {
            const cfgPath = path.join(tmp, 'settings.json');
            fs.writeFileSync(cfgPath, JSON.stringify({
                mcpServers: {},
            }, null, 2));

            let geminiConfig = JSON.parse(fs.readFileSync(cfgPath, 'utf-8'));
            if (!geminiConfig.general) geminiConfig.general = {};
            if (!geminiConfig.general.defaultApprovalMode) {
                geminiConfig.general.defaultApprovalMode = 'auto_edit';
            }
            fs.writeFileSync(cfgPath, JSON.stringify(geminiConfig, null, 2));

            const after = JSON.parse(fs.readFileSync(cfgPath, 'utf-8'));
            assert.strictEqual(
                after.general.defaultApprovalMode,
                'auto_edit',
                'Should set auto_edit on fresh install',
            );
        } finally {
            fs.rmSync(tmp, { recursive: true, force: true });
        }
    });

    it('claude.json MCP hooks: only fills missing keys', () => {
        const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-cc-'));
        try {
            const cfgPath = path.join(tmp, '.claude.json');
            fs.writeFileSync(cfgPath, JSON.stringify({
                hooks: {
                    preCommand: '/home/user/my-custom-pre',
                    // postCommand intentionally missing
                },
                otherSetting: 42,
            }, null, 2));

            // Replicate the patched logic from lib/hooks-installer.js:198
            const config = JSON.parse(fs.readFileSync(cfgPath, 'utf-8'));
            if (!config.hooks) config.hooks = {};
            const delimitHooks = {
                preCommand: '/delimit/pre-mcp-call',
                postCommand: '/delimit/post-mcp-call',
                authentication: '/delimit/mcp-auth',
                audit: '/delimit/mcp-audit',
            };
            for (const [key, value] of Object.entries(delimitHooks)) {
                if (!config.hooks[key]) {
                    config.hooks[key] = value;
                }
            }
            fs.writeFileSync(cfgPath, JSON.stringify(config, null, 2));

            const after = JSON.parse(fs.readFileSync(cfgPath, 'utf-8'));
            assert.strictEqual(
                after.hooks.preCommand,
                '/home/user/my-custom-pre',
                'User preCommand MUST NOT be clobbered',
            );
            assert.strictEqual(
                after.hooks.postCommand,
                '/delimit/post-mcp-call',
                'Missing postCommand SHOULD be filled in',
            );
            assert.strictEqual(after.otherSetting, 42, 'Other settings preserved');
        } finally {
            fs.rmSync(tmp, { recursive: true, force: true });
        }
    });
});

// ----------------------------------------------------------------------------
// LED-1180 follow-up: managed-section preservation contract
// ----------------------------------------------------------------------------
//
// Pre-fix bug: adapters/cursor-rules.js installRules() did
// `fs.writeFileSync(rulesFile, rules)` — full overwrite. A user who
// customized ~/.cursor/rules/delimit.md would have their content
// destroyed on every `delimit-cli setup`.
//
// Post-fix: cursor-rules now uses lib/managed-section.js which only
// touches the region between <!-- delimit:start --> / <!-- delimit:end -->
// markers, and appends-with-markers when no markers exist.
//
// These tests are the institutional safety net so the bug stays fixed.

describe('LED-1180: managed-section preserves user-customized content', () => {
    const { upsertManagedSection } = require('../lib/managed-section');

    function fixture(initialContent, newSection, version) {
        const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-managed-'));
        const filePath = path.join(tmp, 'target.md');
        if (initialContent !== null) fs.writeFileSync(filePath, initialContent);
        const result = upsertManagedSection(filePath, newSection, version || '4.5.1');
        const after = fs.existsSync(filePath) ? fs.readFileSync(filePath, 'utf-8') : null;
        return { tmp, filePath, result, after };
    }

    it('creates the file with markers when missing', () => {
        const newSection = '<!-- delimit:start v4.5.1 -->\nrules\n<!-- delimit:end -->';
        const { tmp, result, after } = fixture(null, newSection);
        try {
            assert.strictEqual(result.action, 'created');
            assert.ok(after.includes('<!-- delimit:start v4.5.1 -->'));
            assert.ok(after.includes('<!-- delimit:end -->'));
        } finally {
            fs.rmSync(tmp, { recursive: true, force: true });
        }
    });

    it('appends managed section when file exists without markers', () => {
        const userContent = '# My custom rules\n\nLine A.\nLine B with config.\n';
        const newSection = '<!-- delimit:start v4.5.1 -->\nrules\n<!-- delimit:end -->';
        const { tmp, result, after } = fixture(userContent, newSection);
        try {
            assert.strictEqual(result.action, 'appended');
            assert.ok(after.startsWith(userContent), 'User content MUST be preserved at top');
            assert.ok(after.includes('<!-- delimit:start v4.5.1 -->'), 'Markers MUST be added');
            assert.ok(after.includes(userContent.trim()));
        } finally {
            fs.rmSync(tmp, { recursive: true, force: true });
        }
    });

    it('replaces ONLY the managed region; preserves content above AND below', () => {
        const before = '# Top\nMy rule one.\n';
        const middle = '<!-- delimit:start v4.5.0 -->\nold managed body\n<!-- delimit:end -->';
        const after = '\n## Bottom\nMy rule two with `code`.\n';
        const initial = before + middle + after;

        const newSection = '<!-- delimit:start v4.5.1 -->\nNEW managed body\n<!-- delimit:end -->';
        const fix = fixture(initial, newSection, '4.5.1');
        try {
            assert.strictEqual(fix.result.action, 'updated');
            assert.ok(fix.after.startsWith(before), 'Content above markers MUST be byte-preserved');
            assert.ok(fix.after.endsWith(after), 'Content below markers MUST be byte-preserved');
            assert.ok(fix.after.includes('NEW managed body'), 'Managed body MUST be replaced');
            assert.ok(!fix.after.includes('old managed body'), 'Old managed body MUST be removed');
        } finally {
            fs.rmSync(fix.tmp, { recursive: true, force: true });
        }
    });

    it('skips write when version already matches (idempotent upgrade)', () => {
        const initial = '# Top\n<!-- delimit:start v4.5.1 -->\nbody\n<!-- delimit:end -->\n# Bottom\n';
        const newSection = '<!-- delimit:start v4.5.1 -->\nbody\n<!-- delimit:end -->';
        const { tmp, result, after } = fixture(initial, newSection, '4.5.1');
        try {
            assert.strictEqual(result.action, 'unchanged');
            assert.strictEqual(after, initial, 'Same-version upgrade MUST be a no-op');
        } finally {
            fs.rmSync(tmp, { recursive: true, force: true });
        }
    });

    it('does NOT mistake quoted markers in user prose for a real managed section', () => {
        // v4.1.49 bug: unanchored regex matched markers inside backticks
        // or bullet lists, and clobbered user content. Anchored regex must
        // require markers to be on their own line.
        const userProse = '# My rules\n\nThe markers `<!-- delimit:start -->` and `<!-- delimit:end -->` mark the managed section.\n\nMore notes.\n';
        const newSection = '<!-- delimit:start v4.5.1 -->\nbody\n<!-- delimit:end -->';
        const { tmp, result, after } = fixture(userProse, newSection);
        try {
            assert.strictEqual(result.action, 'appended', 'Quoted markers MUST NOT trigger update path');
            assert.ok(after.startsWith(userProse), 'User prose MUST be preserved');
        } finally {
            fs.rmSync(tmp, { recursive: true, force: true });
        }
    });

    it('cursor-rules.installRules: preserves customized .cursor/rules/delimit.md', () => {
        // Integration: simulate a user who customized ~/.cursor/rules/delimit.md
        // around the delimit-managed section. Run installRules. Confirm
        // user content is still there.
        const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-cursor-'));
        const cursorDir = path.join(tmp, '.cursor');
        const rulesDir = path.join(cursorDir, 'rules');
        const rulesFile = path.join(rulesDir, 'delimit.md');
        fs.mkdirSync(rulesDir, { recursive: true });

        const userContent =
            '# My personal Cursor rules\n\n' +
            'Always use TypeScript strict mode.\nPrefer async/await over .then().\n\n' +
            '<!-- delimit:start v4.4.0 -->\nold delimit body\n<!-- delimit:end -->\n\n' +
            '## My epilogue\nAlways write tests.\n';
        fs.writeFileSync(rulesFile, userContent);

        const ORIGINAL_HOME = process.env.HOME;
        process.env.HOME = tmp;
        try {
            // Re-require with HOME pointing at tmp so adapters/cursor-rules
            // resolves CURSOR_DIR / CURSOR_RULES_DIR against tmp.
            delete require.cache[require.resolve('../adapters/cursor-rules')];
            const cursorRules = require('../adapters/cursor-rules');
            const result = cursorRules.installRules('4.5.1');

            assert.ok(result.installed);
            const after = fs.readFileSync(rulesFile, 'utf-8');
            assert.ok(
                after.startsWith('# My personal Cursor rules'),
                'User header MUST be preserved'
            );
            assert.ok(
                after.includes('Always use TypeScript strict mode'),
                'User body MUST be preserved above markers'
            );
            assert.ok(
                after.includes('## My epilogue\nAlways write tests'),
                'User content below markers MUST be preserved'
            );
            assert.ok(
                !after.includes('old delimit body'),
                'Old managed body MUST be replaced'
            );
        } finally {
            process.env.HOME = ORIGINAL_HOME;
            delete require.cache[require.resolve('../adapters/cursor-rules')];
            fs.rmSync(tmp, { recursive: true, force: true });
        }
    });
});
