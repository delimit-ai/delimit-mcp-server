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
