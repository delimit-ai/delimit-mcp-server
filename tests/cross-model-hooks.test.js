const { describe, it, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');
const { execSync } = require('child_process');

// CI environments do not have the full gateway/lib stack installed, so tests
// that spawn the CLI via execSync or depend on uncommitted lib changes will
// fail.  Skip them in CI and let them run locally where the full tree is present.
const SKIP_IN_CI = process.env.CI ? 'requires full CLI stack (not available in CI)' : false;

// Prevent governance shim from showing banner during tests
process.env.DELIMIT_WRAPPED = 'true';

// Module under test
const crossModelHooks = require('../lib/cross-model-hooks');

// Test helpers
const ORIGINAL_HOME = process.env.HOME;
let tmpDir;

function setupTmpHome() {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-hooks-test-'));
    process.env.HOME = tmpDir;
    return tmpDir;
}

function teardownTmpHome() {
    process.env.HOME = ORIGINAL_HOME;
    if (tmpDir && fs.existsSync(tmpDir)) {
        fs.rmSync(tmpDir, { recursive: true, force: true });
    }
}

// -----------------------------------------------------------------------
// Detection tests
// -----------------------------------------------------------------------

describe('detectAITools', () => {
    beforeEach(() => { setupTmpHome(); });
    afterEach(() => { teardownTmpHome(); });

    it('detects Claude Code when ~/.claude/settings.json exists', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });
        fs.writeFileSync(path.join(claudeDir, 'settings.json'), '{}');

        const detected = crossModelHooks.detectAITools();
        const claude = detected.find(t => t.id === 'claude');
        assert.ok(claude, 'Claude Code should be detected');
        assert.strictEqual(claude.name, 'Claude Code');
    });

    it('detects Codex when ~/.codex/ directory exists', () => {
        fs.mkdirSync(path.join(tmpDir, '.codex'), { recursive: true });

        const detected = crossModelHooks.detectAITools();
        const codex = detected.find(t => t.id === 'codex');
        assert.ok(codex, 'Codex should be detected');
        assert.strictEqual(codex.name, 'Codex CLI');
    });

    it('detects Gemini CLI when ~/.gemini/ directory exists', () => {
        fs.mkdirSync(path.join(tmpDir, '.gemini'), { recursive: true });

        const detected = crossModelHooks.detectAITools();
        const gemini = detected.find(t => t.id === 'gemini');
        assert.ok(gemini, 'Gemini CLI should be detected');
        assert.strictEqual(gemini.name, 'Gemini CLI');
    });

    it('returns an array (possibly with tools detected from PATH binaries)', () => {
        // With a clean HOME, config-dir-based detection does not fire.
        // Tools may still be detected via CLI binaries on PATH (claude, codex, gemini).
        const detected = crossModelHooks.detectAITools();
        assert.ok(Array.isArray(detected), 'Should return an array');
        // Every detected entry should have the required shape
        for (const tool of detected) {
            assert.ok(tool.id, 'Each tool should have an id');
            assert.ok(tool.name, 'Each tool should have a name');
            assert.ok(tool.configPath, 'Each tool should have a configPath');
        }
    });

    it('detects multiple tools simultaneously', () => {
        fs.mkdirSync(path.join(tmpDir, '.claude'), { recursive: true });
        fs.writeFileSync(path.join(tmpDir, '.claude', 'settings.json'), '{}');
        fs.mkdirSync(path.join(tmpDir, '.codex'), { recursive: true });
        fs.mkdirSync(path.join(tmpDir, '.gemini'), { recursive: true });

        const detected = crossModelHooks.detectAITools();
        assert.ok(detected.find(t => t.id === 'claude'), 'Claude should be detected');
        assert.ok(detected.find(t => t.id === 'codex'), 'Codex should be detected');
        assert.ok(detected.find(t => t.id === 'gemini'), 'Gemini should be detected');
    });
});

// -----------------------------------------------------------------------
// Claude Code hook installation tests
// -----------------------------------------------------------------------

describe('installClaudeHooks', () => {
    beforeEach(() => { setupTmpHome(); });
    afterEach(() => { teardownTmpHome(); });

    it('creates settings.json with SessionStart and PreToolUse hooks in nested format with conditional if fields', { skip: SKIP_IN_CI }, () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });

        const tool = {
            id: 'claude',
            name: 'Claude Code',
            configPath: path.join(claudeDir, 'settings.json'),
        };
        const hookConfig = { session_start: true, pre_tool: true, pre_commit: true, conditional_hooks: true, deploy_audit: true };

        const changes = crossModelHooks.installClaudeHooks(tool, hookConfig);

        assert.ok(changes.includes('SessionStart'));
        assert.ok(changes.includes('PreToolUse'));
        assert.ok(changes.includes('PreCommit'));
        // LED-234 conditional hooks
        assert.ok(changes.includes('PostToolUse:spec-lint'), 'Should install PostToolUse spec-lint');
        assert.ok(changes.includes('PreToolUse:doctor'), 'Should install PreToolUse doctor');
        assert.ok(changes.includes('PreToolUse:deploy-gate'), 'Should install PreToolUse deploy-audit');

        const config = JSON.parse(fs.readFileSync(tool.configPath, 'utf-8'));
        assert.ok(config.hooks.SessionStart, 'SessionStart hooks should exist');
        assert.ok(config.hooks.PreToolUse, 'PreToolUse hooks should exist');
        assert.ok(config.hooks.PostToolUse, 'PostToolUse hooks should exist');

        // SessionStart uses nested format
        const sessionGroup = config.hooks.SessionStart[0];
        assert.ok(sessionGroup.hooks, 'SessionStart should use nested format');
        assert.ok(sessionGroup.hooks[0].command.includes('delimit') || sessionGroup.hooks[0].command.includes('hooks'));
        assert.strictEqual(sessionGroup.if, undefined, 'SessionStart should have no if condition');

        // PreToolUse should have the spec-scoped hook in nested format
        const preToolGroup = config.hooks.PreToolUse.find(
            g => g.hooks && g.hooks.some(h => h.command.includes('hook pre-tool'))
        );
        assert.ok(preToolGroup, 'PreToolUse pre-tool hook group should exist');
        assert.strictEqual(preToolGroup.matcher, 'Edit|Write');
        assert.ok(preToolGroup.if, 'PreToolUse pre-tool hook should have an if condition');
        assert.ok(preToolGroup.if.includes('path_matches'), 'if condition should use path_matches');
        assert.ok(preToolGroup.if.includes('openapi'), 'if condition should mention openapi');
        assert.ok(preToolGroup.if.includes('swagger'), 'if condition should mention swagger');

        // PreToolUse should also have the pre-commit hook scoped to Bash
        const preCommitGroup = config.hooks.PreToolUse.find(
            g => g.hooks && g.hooks.some(h => h.command.includes('hook pre-commit'))
        );
        assert.ok(preCommitGroup, 'PreToolUse pre-commit hook group should exist');
        assert.strictEqual(preCommitGroup.matcher, 'Bash');
        assert.ok(preCommitGroup.if, 'PreCommit hook should have an if condition');
        assert.ok(preCommitGroup.if.includes('git commit'), 'if condition should mention git commit');
        assert.ok(preCommitGroup.if.includes('git push'), 'if condition should mention git push');
    });

    it('does not duplicate hooks on repeated installation', { skip: SKIP_IN_CI }, () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });

        const tool = {
            id: 'claude',
            name: 'Claude Code',
            configPath: path.join(claudeDir, 'settings.json'),
        };
        const hookConfig = { session_start: true, pre_tool: true, pre_commit: true, conditional_hooks: true, deploy_audit: true };

        // Install twice
        crossModelHooks.installClaudeHooks(tool, hookConfig);
        const changes2 = crossModelHooks.installClaudeHooks(tool, hookConfig);

        assert.strictEqual(changes2.length, 0, 'Second install should make no changes');

        const config = JSON.parse(fs.readFileSync(tool.configPath, 'utf-8'));
        assert.strictEqual(config.hooks.SessionStart.length, 1, 'Should have exactly one SessionStart hook');
        // pre-tool + pre-commit + doctor + deploy-audit = 4
        assert.strictEqual(config.hooks.PreToolUse.length, 4, 'Should have exactly four PreToolUse hooks');
        // spec-lint + STR-2202 agent-record = 2
        assert.strictEqual(config.hooks.PostToolUse.length, 2, 'Should have exactly two PostToolUse hooks (spec-lint + agent-record)');
    });

    it('preserves existing settings.json content', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });
        const existingConfig = { theme: 'dark', permissions: { allow: ['Read'] } };
        fs.writeFileSync(path.join(claudeDir, 'settings.json'), JSON.stringify(existingConfig));

        const tool = {
            id: 'claude',
            name: 'Claude Code',
            configPath: path.join(claudeDir, 'settings.json'),
        };
        const hookConfig = { session_start: true, pre_tool: true, pre_commit: true };

        crossModelHooks.installClaudeHooks(tool, hookConfig);

        const config = JSON.parse(fs.readFileSync(tool.configPath, 'utf-8'));
        assert.strictEqual(config.theme, 'dark', 'Existing settings should be preserved');
        assert.ok(config.hooks, 'Hooks should be added');
    });

    it('respects hookConfig flags', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });

        const tool = {
            id: 'claude',
            name: 'Claude Code',
            configPath: path.join(claudeDir, 'settings.json'),
        };
        const hookConfig = { session_start: false, pre_tool: true, pre_commit: false, conditional_hooks: false };

        const changes = crossModelHooks.installClaudeHooks(tool, hookConfig);

        assert.ok(!changes.includes('SessionStart'), 'SessionStart should not be installed');
        assert.ok(changes.includes('PreToolUse'), 'PreToolUse should be installed');
        assert.ok(!changes.includes('PreCommit'), 'PreCommit should not be installed');
        assert.ok(!changes.includes('PostToolUse:spec-lint'), 'Conditional hooks should not be installed');

        const config = JSON.parse(fs.readFileSync(tool.configPath, 'utf-8'));
        assert.ok(!config.hooks.SessionStart, 'No SessionStart entry should exist');
        assert.strictEqual(config.hooks.PreToolUse.length, 1, 'Only pre-tool hook, no pre-commit or conditional');
    });

    it('upgrades existing flat-format pre-tool hook to nested format with if condition', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });

        // Simulate old flat-format hook already installed
        const oldConfig = {
            hooks: {
                PreToolUse: [{
                    type: 'command',
                    command: 'npx delimit-cli hook pre-tool',
                    matcher: 'Edit|Write|Bash',
                }],
            },
        };
        const configPath = path.join(claudeDir, 'settings.json');
        fs.writeFileSync(configPath, JSON.stringify(oldConfig));

        const tool = { id: 'claude', name: 'Claude Code', configPath };
        const hookConfig = { session_start: false, pre_tool: true, pre_commit: true, conditional_hooks: false };

        const changes = crossModelHooks.installClaudeHooks(tool, hookConfig);

        assert.ok(changes.includes('PreToolUse (upgraded)'), 'Should report upgrade');

        const config = JSON.parse(fs.readFileSync(configPath, 'utf-8'));
        const preToolGroup = config.hooks.PreToolUse.find(
            g => g.hooks && g.hooks.some(h => h.command.includes('hook pre-tool'))
        );
        assert.ok(preToolGroup, 'Upgraded hook group should exist');
        assert.ok(preToolGroup.if, 'Upgraded hook should have if condition');
        assert.strictEqual(preToolGroup.matcher, 'Edit|Write', 'Matcher should be narrowed');
        assert.ok(preToolGroup.hooks[0].command.includes('$TOOL_NAME'), 'Command should include $TOOL_NAME');
    });
});

// -----------------------------------------------------------------------
// Codex hook installation tests
// -----------------------------------------------------------------------

describe('installCodexHooks', () => {
    beforeEach(() => { setupTmpHome(); });
    afterEach(() => { teardownTmpHome(); });

    it('creates instructions.md with governance block', { skip: SKIP_IN_CI }, () => {
        const codexDir = path.join(tmpDir, '.codex');
        fs.mkdirSync(codexDir, { recursive: true });

        const tool = {
            id: 'codex',
            name: 'Codex CLI',
            configPath: path.join(codexDir, 'config.json'),
            instructionsPath: path.join(codexDir, 'instructions.md'),
        };
        const hookConfig = { session_start: true, pre_tool: true, pre_commit: true };

        const changes = crossModelHooks.installCodexHooks(tool, hookConfig);

        assert.ok(changes.includes('instructions.md'));
        assert.ok(changes.includes('pre-commit hook'));

        const instructions = fs.readFileSync(tool.instructionsPath, 'utf-8');
        assert.ok(instructions.includes('delimit:hooks-start'));
        assert.ok(instructions.includes('<!-- delimit:start'), 'Should contain Delimit managed section marker');

        const config = JSON.parse(fs.readFileSync(tool.configPath, 'utf-8'));
        assert.ok(config.hooks['pre-commit'].includes('delimit-cli hook pre-commit'));
    });

    it('preserves existing instructions.md content', () => {
        const codexDir = path.join(tmpDir, '.codex');
        fs.mkdirSync(codexDir, { recursive: true });
        fs.writeFileSync(path.join(codexDir, 'instructions.md'), '# My Custom Instructions\n\nDo good things.\n');

        const tool = {
            id: 'codex',
            name: 'Codex CLI',
            configPath: path.join(codexDir, 'config.json'),
            instructionsPath: path.join(codexDir, 'instructions.md'),
        };
        const hookConfig = { session_start: true, pre_tool: true, pre_commit: true };

        crossModelHooks.installCodexHooks(tool, hookConfig);

        const instructions = fs.readFileSync(tool.instructionsPath, 'utf-8');
        assert.ok(instructions.includes('# My Custom Instructions'), 'Existing content preserved');
        assert.ok(instructions.includes('delimit:hooks-start'), 'Delimit block added');
    });
});

// -----------------------------------------------------------------------
// Gemini hook installation tests
// -----------------------------------------------------------------------

describe('installGeminiHooks', () => {
    beforeEach(() => { setupTmpHome(); });
    afterEach(() => { teardownTmpHome(); });

    it('creates settings.json and GEMINI.md', { skip: SKIP_IN_CI }, () => {
        const geminiDir = path.join(tmpDir, '.gemini');
        fs.mkdirSync(geminiDir, { recursive: true });

        const tool = {
            id: 'gemini',
            name: 'Gemini CLI',
            configPath: path.join(geminiDir, 'settings.json'),
        };
        const hookConfig = { session_start: true, pre_tool: true, pre_commit: true };

        const changes = crossModelHooks.installGeminiHooks(tool, hookConfig);

        assert.ok(changes.includes('customInstructions'));
        assert.ok(changes.includes('GEMINI.md'));

        const config = JSON.parse(fs.readFileSync(tool.configPath, 'utf-8'));
        assert.ok(config.customInstructions.includes('<!-- delimit:start') || config.customInstructions.includes('delimit:start'), 'Should contain Delimit managed section marker');

        const geminiMd = fs.readFileSync(path.join(geminiDir, 'GEMINI.md'), 'utf-8');
        assert.ok(geminiMd.includes('# Delimit'), 'GEMINI.md should contain Delimit governance template');
    });
});

// -----------------------------------------------------------------------
// Hook removal tests
// -----------------------------------------------------------------------

describe('removeClaudeHooks', () => {
    beforeEach(() => { setupTmpHome(); });
    afterEach(() => { teardownTmpHome(); });

    it('removes Delimit hooks from settings.json (nested format)', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });

        const config = {
            hooks: {
                SessionStart: [
                    { matcher: '', hooks: [{ type: 'command', command: 'npx delimit-cli hook session-start' }] },
                    { matcher: '', hooks: [{ type: 'command', command: 'some-other-tool' }] },
                ],
                PreToolUse: [
                    { matcher: 'Edit|Write', if: "Edit && path_matches('**/openapi*')", hooks: [{ type: 'command', command: 'npx delimit-cli hook pre-tool $TOOL_NAME' }] },
                    { matcher: 'Bash', if: "Bash && input_contains('git commit')", hooks: [{ type: 'command', command: 'npx delimit-cli hook pre-commit' }] },
                    { matcher: 'Bash', if: "command matches 'git commit'", hooks: [{ type: 'command', command: 'npx delimit-cli doctor' }] },
                ],
                PostToolUse: [
                    { matcher: 'Edit|Write', hooks: [{ type: 'command', command: 'npx delimit-cli lint "$DELIMIT_FILE_PATH"' }] },
                ],
            }
        };
        fs.writeFileSync(path.join(claudeDir, 'settings.json'), JSON.stringify(config));

        const removed = crossModelHooks.removeClaudeHooks();
        assert.ok(removed, 'Should report changes');

        const updated = JSON.parse(fs.readFileSync(path.join(claudeDir, 'settings.json'), 'utf-8'));
        assert.strictEqual(updated.hooks.SessionStart.length, 1, 'Non-delimit hook preserved');
        assert.strictEqual(updated.hooks.SessionStart[0].hooks[0].command, 'some-other-tool');
        assert.ok(!updated.hooks.PreToolUse, 'Empty PreToolUse array should be removed');
        assert.ok(!updated.hooks.PostToolUse, 'Empty PostToolUse array should be removed');
    });

    it('removes Delimit hooks in flat format (backward compat)', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });

        const config = {
            hooks: {
                SessionStart: [
                    { type: 'command', command: 'npx delimit-cli hook session-start' },
                    { type: 'command', command: 'some-other-tool' },
                ],
            }
        };
        fs.writeFileSync(path.join(claudeDir, 'settings.json'), JSON.stringify(config));

        const removed = crossModelHooks.removeClaudeHooks();
        assert.ok(removed, 'Should report changes');

        const updated = JSON.parse(fs.readFileSync(path.join(claudeDir, 'settings.json'), 'utf-8'));
        assert.strictEqual(updated.hooks.SessionStart.length, 1, 'Non-delimit hook preserved');
        assert.strictEqual(updated.hooks.SessionStart[0].command, 'some-other-tool');
    });
});

describe('removeCodexHooks', () => {
    beforeEach(() => { setupTmpHome(); });
    afterEach(() => { teardownTmpHome(); });

    it('removes delimit block from instructions.md', () => {
        const codexDir = path.join(tmpDir, '.codex');
        fs.mkdirSync(codexDir, { recursive: true });
        fs.writeFileSync(
            path.join(codexDir, 'instructions.md'),
            '# My stuff\n\n<!-- delimit:hooks-start -->\nDelimit block\n<!-- delimit:hooks-end -->\n\nMore stuff\n'
        );
        fs.writeFileSync(
            path.join(codexDir, 'config.json'),
            JSON.stringify({ hooks: { 'pre-commit': 'npx delimit-cli hook pre-commit' } })
        );

        const removed = crossModelHooks.removeCodexHooks();
        assert.ok(removed);

        const instructions = fs.readFileSync(path.join(codexDir, 'instructions.md'), 'utf-8');
        assert.ok(!instructions.includes('delimit:hooks-start'));
        assert.ok(instructions.includes('# My stuff'));

        const config = JSON.parse(fs.readFileSync(path.join(codexDir, 'config.json'), 'utf-8'));
        assert.ok(!config.hooks, 'Hooks object should be removed when empty');
    });
});

describe('removeGeminiHooks', () => {
    beforeEach(() => { setupTmpHome(); });
    afterEach(() => { teardownTmpHome(); });

    it('removes customInstructions and GEMINI.md', () => {
        const geminiDir = path.join(tmpDir, '.gemini');
        fs.mkdirSync(geminiDir, { recursive: true });
        fs.writeFileSync(
            path.join(geminiDir, 'settings.json'),
            JSON.stringify({ customInstructions: 'run: npx delimit-cli hook session-start', mcpServers: {} })
        );
        fs.writeFileSync(
            path.join(geminiDir, 'GEMINI.md'),
            '# Delimit Governance\n\nStuff\n'
        );

        const removed = crossModelHooks.removeGeminiHooks();
        assert.ok(removed);

        const config = JSON.parse(fs.readFileSync(path.join(geminiDir, 'settings.json'), 'utf-8'));
        assert.ok(!config.customInstructions, 'customInstructions should be removed');
        assert.ok(!fs.existsSync(path.join(geminiDir, 'GEMINI.md')), 'GEMINI.md should be deleted');
    });
});

// -----------------------------------------------------------------------
// installAllHooks integration test
// -----------------------------------------------------------------------

describe('installAllHooks', () => {
    beforeEach(() => { setupTmpHome(); });
    afterEach(() => { teardownTmpHome(); });

    it('installs hooks for all detected tools', () => {
        // Create tool directories
        fs.mkdirSync(path.join(tmpDir, '.claude'), { recursive: true });
        fs.writeFileSync(path.join(tmpDir, '.claude', 'settings.json'), '{}');
        fs.mkdirSync(path.join(tmpDir, '.codex'), { recursive: true });
        fs.mkdirSync(path.join(tmpDir, '.gemini'), { recursive: true });

        const hookConfig = { session_start: true, pre_tool: true, pre_commit: true };
        const { tools, results } = crossModelHooks.installAllHooks(hookConfig);

        assert.ok(tools.length >= 3, 'Should detect at least 3 tools');
        assert.strictEqual(results.length, tools.length, 'Should have matching number of results');

        // Verify each tool got hooks
        for (const result of results) {
            assert.ok(result.changes.length > 0, `${result.tool.name} should have changes`);
        }
    });
});

// -----------------------------------------------------------------------
// removeAllHooks integration test
// -----------------------------------------------------------------------

describe('removeAllHooks', () => {
    beforeEach(() => { setupTmpHome(); });
    afterEach(() => { teardownTmpHome(); });

    it('removes hooks from all tools that had them', () => {
        // Install hooks first
        fs.mkdirSync(path.join(tmpDir, '.claude'), { recursive: true });
        fs.writeFileSync(path.join(tmpDir, '.claude', 'settings.json'), '{}');
        fs.mkdirSync(path.join(tmpDir, '.codex'), { recursive: true });
        fs.mkdirSync(path.join(tmpDir, '.gemini'), { recursive: true });

        const hookConfig = { session_start: true, pre_tool: true, pre_commit: true };
        crossModelHooks.installAllHooks(hookConfig);

        // Now remove
        const removed = crossModelHooks.removeAllHooks();
        assert.ok(removed.length > 0, 'Should have removed from at least one tool');

        // Verify Claude hooks are gone
        const claudeConfig = JSON.parse(
            fs.readFileSync(path.join(tmpDir, '.claude', 'settings.json'), 'utf-8')
        );
        assert.ok(!claudeConfig.hooks || Object.keys(claudeConfig.hooks).length === 0);
    });
});

// -----------------------------------------------------------------------
// Hook config loading tests
// -----------------------------------------------------------------------

describe('loadHookConfig', () => {
    it('returns defaults when no config file exists', () => {
        const config = crossModelHooks.loadHookConfig();
        assert.strictEqual(config.session_start, true);
        assert.strictEqual(config.pre_tool, true);
        assert.strictEqual(config.pre_commit, true);
    });
});

// -----------------------------------------------------------------------
// CLI integration: hook commands execute without errors
// -----------------------------------------------------------------------

describe('CLI hook commands', () => {
    it('hook session-start runs without error', { skip: SKIP_IN_CI }, () => {
        const cliPath = path.join(__dirname, '..', 'bin', 'delimit-cli.js');
        // Should not throw
        const result = execSync(`node "${cliPath}" hook session-start 2>&1`, {
            encoding: 'utf-8',
            timeout: 15000,
        });
        assert.ok(typeof result === 'string', 'Should produce output');
        assert.ok(result.includes('[Delimit]'), 'Output should contain Delimit prefix');
    });

    it('hook pre-tool runs without error', { skip: SKIP_IN_CI }, () => {
        const cliPath = path.join(__dirname, '..', 'bin', 'delimit-cli.js');
        const result = execSync(`node "${cliPath}" hook pre-tool Edit 2>&1`, {
            encoding: 'utf-8',
            timeout: 15000,
        });
        // pre-tool may produce no output if no staged files, that is fine
        assert.ok(typeof result === 'string');
    });

    it('hook pre-commit runs without error', { skip: SKIP_IN_CI }, () => {
        const cliPath = path.join(__dirname, '..', 'bin', 'delimit-cli.js');
        const result = execSync(`node "${cliPath}" hook pre-commit 2>&1`, {
            encoding: 'utf-8',
            timeout: 5000,
        });
        assert.ok(typeof result === 'string');
    });

    it('hook session-start completes in under 10 seconds', { skip: SKIP_IN_CI }, () => {
        const cliPath = path.join(__dirname, '..', 'bin', 'delimit-cli.js');
        const start = Date.now();
        execSync(`node "${cliPath}" hook session-start 2>&1`, {
            encoding: 'utf-8',
            timeout: 10000,
        });
        const elapsed = Date.now() - start;
        assert.ok(elapsed < 10000, `Hook took ${elapsed}ms, should be under 10000ms`);
    });
});

// -----------------------------------------------------------------------
// Deliberation helper tests (LED-201)
// -----------------------------------------------------------------------

describe('countPendingStrategyItems', () => {
    beforeEach(() => { setupTmpHome(); });
    afterEach(() => { teardownTmpHome(); });

    it('returns 0 when no ledger directory exists', () => {
        const count = crossModelHooks.countPendingStrategyItems();
        assert.strictEqual(count, 0);
    });

    it('returns 0 when ledger has no strategy items', () => {
        const ledgerDir = path.join(tmpDir, '.delimit', 'ledger');
        fs.mkdirSync(ledgerDir, { recursive: true });
        fs.writeFileSync(path.join(ledgerDir, 'tasks.json'), JSON.stringify([
            { id: 'T-001', status: 'open', category: 'bug', priority: 'P1' },
        ]));

        const count = crossModelHooks.countPendingStrategyItems();
        assert.strictEqual(count, 0);
    });

    it('counts open P0 items regardless of category', () => {
        const ledgerDir = path.join(tmpDir, '.delimit', 'ledger');
        fs.mkdirSync(ledgerDir, { recursive: true });
        fs.writeFileSync(path.join(ledgerDir, 'items.json'), JSON.stringify([
            { id: 'S-001', status: 'open', category: 'feature', priority: 'P0' },
            { id: 'S-002', status: 'closed', category: 'strategy', priority: 'P0' },
        ]));

        const count = crossModelHooks.countPendingStrategyItems();
        assert.strictEqual(count, 1);
    });

    it('counts open strategy items regardless of priority', () => {
        const ledgerDir = path.join(tmpDir, '.delimit', 'ledger');
        fs.mkdirSync(ledgerDir, { recursive: true });
        fs.writeFileSync(path.join(ledgerDir, 'items.json'), JSON.stringify([
            { id: 'S-001', status: 'open', category: 'strategy', priority: 'P1' },
            { id: 'S-002', status: 'in_progress', category: 'deliberation', priority: 'P2' },
        ]));

        const count = crossModelHooks.countPendingStrategyItems();
        assert.strictEqual(count, 2);
    });

    it('ignores closed and done items', () => {
        const ledgerDir = path.join(tmpDir, '.delimit', 'ledger');
        fs.mkdirSync(ledgerDir, { recursive: true });
        fs.writeFileSync(path.join(ledgerDir, 'items.json'), JSON.stringify([
            { id: 'S-001', status: 'closed', category: 'strategy', priority: 'P0' },
            { id: 'S-002', status: 'done', category: 'strategy', priority: 'P0' },
            { id: 'S-003', status: 'open', category: 'strategy', priority: 'P0' },
        ]));

        const count = crossModelHooks.countPendingStrategyItems();
        assert.strictEqual(count, 1);
    });
});

describe('getTopStrategyItem', () => {
    beforeEach(() => { setupTmpHome(); });
    afterEach(() => { teardownTmpHome(); });

    it('returns null when no ledger exists', () => {
        const item = crossModelHooks.getTopStrategyItem();
        assert.strictEqual(item, null);
    });

    it('returns the highest priority open strategy item', () => {
        const ledgerDir = path.join(tmpDir, '.delimit', 'ledger');
        fs.mkdirSync(ledgerDir, { recursive: true });
        fs.writeFileSync(path.join(ledgerDir, 'items.json'), JSON.stringify([
            { id: 'S-001', status: 'open', category: 'strategy', priority: 'P1', title: 'Lower priority' },
            { id: 'S-002', status: 'open', category: 'strategy', priority: 'P0', title: 'Top priority' },
            { id: 'S-003', status: 'open', category: 'strategy', priority: 'P2', title: 'Lowest priority' },
        ]));

        const item = crossModelHooks.getTopStrategyItem();
        assert.ok(item);
        assert.strictEqual(item.id, 'S-002');
        assert.strictEqual(item.title, 'Top priority');
    });

    it('ignores closed items', () => {
        const ledgerDir = path.join(tmpDir, '.delimit', 'ledger');
        fs.mkdirSync(ledgerDir, { recursive: true });
        fs.writeFileSync(path.join(ledgerDir, 'items.json'), JSON.stringify([
            { id: 'S-001', status: 'closed', category: 'strategy', priority: 'P0', title: 'Closed' },
            { id: 'S-002', status: 'open', category: 'strategy', priority: 'P2', title: 'Open item' },
        ]));

        const item = crossModelHooks.getTopStrategyItem();
        assert.ok(item);
        assert.strictEqual(item.id, 'S-002');
    });
});

describe('hookSessionStart with strategy items', () => {
    beforeEach(() => { setupTmpHome(); });
    afterEach(() => { teardownTmpHome(); });

    it('shows deliberation notice when P0 strategy items exist', async () => {
        const ledgerDir = path.join(tmpDir, '.delimit', 'ledger');
        fs.mkdirSync(ledgerDir, { recursive: true });
        fs.writeFileSync(path.join(ledgerDir, 'items.json'), JSON.stringify([
            { id: 'S-001', status: 'open', category: 'strategy', priority: 'P0', title: 'Test' },
            { id: 'S-002', status: 'open', category: 'strategy', priority: 'P0', title: 'Test 2' },
        ]));

        // Capture stdout
        let output = '';
        const originalWrite = process.stdout.write;
        process.stdout.write = (chunk) => { output += chunk; return true; };

        try {
            await crossModelHooks.hookSessionStart();
        } finally {
            process.stdout.write = originalWrite;
        }

        assert.ok(output.includes('2 strategic decisions pending deliberation'), `Output should mention pending deliberations, got: ${output}`);
        assert.ok(output.includes('delimit deliberate'), 'Output should suggest running delimit deliberate');
    });

    it('does not show deliberation notice when show_strategy_items is false', async () => {
        const ledgerDir = path.join(tmpDir, '.delimit', 'ledger');
        fs.mkdirSync(ledgerDir, { recursive: true });
        fs.writeFileSync(path.join(ledgerDir, 'items.json'), JSON.stringify([
            { id: 'S-001', status: 'open', category: 'strategy', priority: 'P0', title: 'Test' },
        ]));

        // Write a delimit.yml that disables show_strategy_items in the global config dir
        const globalConfigDir = path.join(tmpDir, '.delimit');
        fs.writeFileSync(path.join(globalConfigDir, 'delimit.yml'), 'hooks:\n  show_strategy_items: false\n');

        let output = '';
        const originalWrite = process.stdout.write;
        process.stdout.write = (chunk) => { output += chunk; return true; };

        try {
            await crossModelHooks.hookSessionStart();
        } finally {
            process.stdout.write = originalWrite;
        }

        assert.ok(!output.includes('pending deliberation'), 'Should not show deliberation notice when disabled');
    });
});

describe('loadHookConfig with deliberation settings', () => {
    it('defaults include deliberate_on_commit as false', () => {
        const config = crossModelHooks.loadHookConfig();
        assert.strictEqual(config.deliberate_on_commit, false);
    });

    it('defaults include show_strategy_items as true', () => {
        const config = crossModelHooks.loadHookConfig();
        assert.strictEqual(config.show_strategy_items, true);
    });

    it('defaults include conditional_hooks as true', () => {
        const config = crossModelHooks.loadHookConfig();
        assert.strictEqual(config.conditional_hooks, true);
    });

    it('defaults include deploy_audit as true', () => {
        const config = crossModelHooks.loadHookConfig();
        assert.strictEqual(config.deploy_audit, true);
    });
});

// -----------------------------------------------------------------------
// LED-234: Conditional hooks tests
// -----------------------------------------------------------------------

describe('LED-234: Conditional Claude Code hooks', () => {
    beforeEach(() => { setupTmpHome(); });
    afterEach(() => { teardownTmpHome(); });

    it('installs PostToolUse spec-lint hook with correct if condition', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });

        const tool = { id: 'claude', name: 'Claude Code', configPath: path.join(claudeDir, 'settings.json') };
        const hookConfig = { session_start: false, pre_tool: false, pre_commit: false, conditional_hooks: true };

        crossModelHooks.installClaudeHooks(tool, hookConfig);

        const config = JSON.parse(fs.readFileSync(tool.configPath, 'utf-8'));
        assert.ok(config.hooks.PostToolUse, 'PostToolUse should exist');

        const specLintGroup = config.hooks.PostToolUse.find(
            g => g.hooks && g.hooks.some(h => h.command.includes('delimit-cli lint'))
        );
        assert.ok(specLintGroup, 'Spec-lint hook group should exist');
        assert.strictEqual(specLintGroup.matcher, 'Edit|Write');
        assert.ok(specLintGroup.if, 'Should have an if condition');
        assert.ok(specLintGroup.if.includes('openapi'), 'if should match openapi');
        assert.ok(specLintGroup.if.includes('swagger'), 'if should match swagger');
        assert.ok(specLintGroup.if.includes('openapi') && specLintGroup.if.includes('swagger'), 'if should match openapi/swagger file patterns');
        assert.ok(specLintGroup.hooks[0].command.includes('$DELIMIT_FILE_PATH'), 'Command should reference file path');
        assert.strictEqual(specLintGroup.hooks[0].timeout, 30, 'Should have 30s timeout');
    });

    it('installs PreToolUse doctor hook with git commit condition', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });

        const tool = { id: 'claude', name: 'Claude Code', configPath: path.join(claudeDir, 'settings.json') };
        const hookConfig = { session_start: false, pre_tool: false, pre_commit: false, conditional_hooks: true };

        crossModelHooks.installClaudeHooks(tool, hookConfig);

        const config = JSON.parse(fs.readFileSync(tool.configPath, 'utf-8'));
        const doctorGroup = config.hooks.PreToolUse.find(
            g => g.hooks && g.hooks.some(h => h.command.includes('delimit-cli doctor'))
        );
        assert.ok(doctorGroup, 'Doctor hook group should exist');
        assert.strictEqual(doctorGroup.matcher, 'Bash');
        assert.ok(doctorGroup.if.includes('git commit'), 'if should match git commit');
        assert.strictEqual(doctorGroup.hooks[0].timeout, 15, 'Should have 15s timeout');
    });

    it('installs PreToolUse deploy-audit hook with deploy/publish conditions', { skip: SKIP_IN_CI }, () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });

        const tool = { id: 'claude', name: 'Claude Code', configPath: path.join(claudeDir, 'settings.json') };
        const hookConfig = { session_start: false, pre_tool: false, pre_commit: false, conditional_hooks: true, deploy_audit: true };

        crossModelHooks.installClaudeHooks(tool, hookConfig);

        const config = JSON.parse(fs.readFileSync(tool.configPath, 'utf-8'));
        const deployGroup = config.hooks.PreToolUse.find(
            g => g.hooks && g.hooks.some(h => h.command.includes('hook deploy-gate'))
        );
        assert.ok(deployGroup, 'Deploy-audit hook group should exist');
        assert.strictEqual(deployGroup.matcher, 'Bash');
        assert.ok(deployGroup.if.includes('npm publish'), 'if should match npm publish');
        assert.ok(deployGroup.if.includes('deploy'), 'if should match deploy');
        assert.ok(deployGroup.if.includes('release'), 'if should match release');
        assert.strictEqual(deployGroup.hooks[0].timeout, 30, 'Should have 30s timeout');
    });

    it('skips deploy-audit when deploy_audit is false', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });

        const tool = { id: 'claude', name: 'Claude Code', configPath: path.join(claudeDir, 'settings.json') };
        const hookConfig = { session_start: false, pre_tool: false, pre_commit: false, conditional_hooks: true, deploy_audit: false };

        const changes = crossModelHooks.installClaudeHooks(tool, hookConfig);

        assert.ok(!changes.includes('PreToolUse:deploy-gate'), 'deploy-audit should not be installed');
        assert.ok(changes.includes('PostToolUse:spec-lint'), 'spec-lint should still be installed');
        assert.ok(changes.includes('PreToolUse:doctor'), 'doctor should still be installed');
    });

    it('skips all conditional hooks when conditional_hooks is false', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });

        const tool = { id: 'claude', name: 'Claude Code', configPath: path.join(claudeDir, 'settings.json') };
        const hookConfig = { session_start: false, pre_tool: false, pre_commit: false, conditional_hooks: false };

        const changes = crossModelHooks.installClaudeHooks(tool, hookConfig);

        assert.strictEqual(changes.length, 0, 'No hooks should be installed');
        const config = JSON.parse(fs.readFileSync(tool.configPath, 'utf-8'));
        assert.ok(!config.hooks.PostToolUse, 'No PostToolUse hooks');
    });

    it('preserves existing non-delimit hooks when installing conditional hooks', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });

        const existingConfig = {
            permissions: { allow: ['Read'] },
            hooks: {
                PostToolUse: [
                    { matcher: '', hooks: [{ type: 'command', command: '/usr/local/bin/my-custom-hook' }] },
                ],
                PreToolUse: [
                    { matcher: 'Bash', hooks: [{ type: 'command', command: '/usr/local/bin/pre-bash-hook' }] },
                ],
            },
        };
        fs.writeFileSync(path.join(claudeDir, 'settings.json'), JSON.stringify(existingConfig));

        const tool = { id: 'claude', name: 'Claude Code', configPath: path.join(claudeDir, 'settings.json') };
        const hookConfig = { session_start: false, pre_tool: false, pre_commit: false, conditional_hooks: true, deploy_audit: true };

        crossModelHooks.installClaudeHooks(tool, hookConfig);

        const config = JSON.parse(fs.readFileSync(tool.configPath, 'utf-8'));
        assert.strictEqual(config.permissions.allow[0], 'Read', 'Existing config preserved');

        // Existing hooks still present
        const customPostHook = config.hooks.PostToolUse.find(
            g => g.hooks && g.hooks.some(h => h.command === '/usr/local/bin/my-custom-hook')
        );
        assert.ok(customPostHook, 'Existing PostToolUse hook preserved');

        const customPreHook = config.hooks.PreToolUse.find(
            g => g.hooks && g.hooks.some(h => h.command === '/usr/local/bin/pre-bash-hook')
        );
        assert.ok(customPreHook, 'Existing PreToolUse hook preserved');

        // Delimit hooks also present
        assert.ok(config.hooks.PostToolUse.length >= 2, 'PostToolUse should have original + spec-lint');
        assert.ok(config.hooks.PreToolUse.length >= 3, 'PreToolUse should have original + doctor + deploy-audit');
    });
});

// -----------------------------------------------------------------------
// STR-2202: "tools fire tools" — HOOK half
// -----------------------------------------------------------------------

describe('STR-2202 SessionStart digest + heartbeat echo', () => {
    beforeEach(() => { setupTmpHome(); });
    afterEach(() => { teardownTmpHome(); });

    it('embeds the digest + heartbeat echo in the SessionStart script by default', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });
        const tool = { id: 'claude', name: 'Claude Code', configPath: path.join(claudeDir, 'settings.json') };

        crossModelHooks.installClaudeHooks(tool, { session_start: true });

        const script = fs.readFileSync(path.join(claudeDir, 'hooks', 'delimit'), 'utf-8');
        assert.ok(script.includes('digest-*.json'), 'script reads the latest daily digest');
        assert.ok(script.includes('stuck_over_24h'), 'script surfaces the stuck-dispatch count');
        assert.ok(script.includes('from ai.heartbeat import check_staleness'), 'script surfaces heartbeat anomalies');
        // Never-block contract: the echo is time-boxed and fails open.
        assert.ok(script.includes("timeout 6 python3 - <<'DGEOF'"), 'echo is time-boxed');
        assert.ok(/DGEOF' 2>\/dev\/null \|\| true/.test(script), 'echo fails open (|| true)');
        // Skipped for subagents so a narrow task does not pull orchestrator context.
        assert.ok(script.includes('"$DELIMIT_SESSION_TYPE" != "subagent"'), 'echo skipped for subagents');
    });

    it('omits the digest echo when session_digest_echo is false', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });
        const tool = { id: 'claude', name: 'Claude Code', configPath: path.join(claudeDir, 'settings.json') };

        crossModelHooks.installClaudeHooks(tool, { session_start: true, session_digest_echo: false });

        const script = fs.readFileSync(path.join(claudeDir, 'hooks', 'delimit'), 'utf-8');
        assert.ok(!script.includes('digest-*.json'), 'digest echo omitted when flag is false');
        assert.ok(!script.includes("<<'DGEOF'"), 'digest echo block omitted when flag is false');
    });
});

describe('LED-1962 SessionStart auto-revive last soul', () => {
    beforeEach(() => { setupTmpHome(); });
    afterEach(() => { teardownTmpHome(); });

    it('embeds the auto-revive-last-soul block in the SessionStart script by default', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });
        const tool = { id: 'claude', name: 'Claude Code', configPath: path.join(claudeDir, 'settings.json') };

        crossModelHooks.installClaudeHooks(tool, { session_start: true });

        const script = fs.readFileSync(path.join(claudeDir, 'hooks', 'delimit'), 'utf-8');
        // The marker printed into the new session, and the module it imports.
        assert.ok(script.includes('Auto-revived working context'), 'emits the auto-revive marker');
        assert.ok(script.includes('session_phoenix'), 'imports ai.session_phoenix');
        assert.ok(script.includes('find_most_recent_soul_across_projects'),
            'picks the globally most-recent soul');
        // Never-block contract: time-boxed + fails open, orchestrator-only.
        assert.ok(script.includes("timeout 6 python3 - <<'RVEOF'"), 'revive is time-boxed');
        assert.ok(/RVEOF' 2>\/dev\/null \|\| true/.test(script), 'revive fails open (|| true)');
        assert.ok(script.includes('"$DELIMIT_SESSION_TYPE" != "subagent"'),
            'revive skipped for subagents');
    });

    it('omits the auto-revive block when session_auto_revive is false', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });
        const tool = { id: 'claude', name: 'Claude Code', configPath: path.join(claudeDir, 'settings.json') };

        crossModelHooks.installClaudeHooks(tool, { session_start: true, session_auto_revive: false });

        const script = fs.readFileSync(path.join(claudeDir, 'hooks', 'delimit'), 'utf-8');
        assert.ok(!script.includes('Auto-revived working context'),
            'auto-revive marker omitted when flag is false');
        assert.ok(!script.includes("<<'RVEOF'"), 'auto-revive block omitted when flag is false');
    });

    it('generates a syntactically valid bash script (bash -n) with the revive block', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });
        const tool = { id: 'claude', name: 'Claude Code', configPath: path.join(claudeDir, 'settings.json') };

        crossModelHooks.installClaudeHooks(tool, { session_start: true });

        const hookPath = path.join(claudeDir, 'hooks', 'delimit');
        // Throws (non-zero exit) if the generated script is not valid bash.
        execSync(`bash -n ${JSON.stringify(hookPath)}`, { stdio: 'pipe' });
    });

    it('Codex instructions.md carries the strengthened first-action revive text', () => {
        const codexDir = path.join(tmpDir, '.codex');
        fs.mkdirSync(codexDir, { recursive: true });
        const tool = {
            id: 'codex',
            name: 'Codex CLI',
            configPath: path.join(codexDir, 'config.json'),
            instructionsPath: path.join(codexDir, 'instructions.md'),
        };

        crossModelHooks.installCodexHooks(tool, { session_start: true, pre_tool: true, pre_commit: true });

        const instructions = fs.readFileSync(tool.instructionsPath, 'utf-8');
        assert.ok(/Session start.*FIRST/.test(instructions),
            'session-start line elevated to a FIRST action');
        assert.ok(instructions.includes('delimit_revive'), 'names delimit_revive');
        assert.ok(/switching agents/.test(instructions),
            'explains the cross-agent (quota-switch) resume rationale');
    });
});

describe('STR-2202 subagent flight-recorder (PostToolUse)', () => {
    beforeEach(() => { setupTmpHome(); });
    afterEach(() => { teardownTmpHome(); });

    it('installs a PostToolUse hook matched to the subagent (Task|Agent) tool', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });
        const tool = { id: 'claude', name: 'Claude Code', configPath: path.join(claudeDir, 'settings.json') };

        const changes = crossModelHooks.installClaudeHooks(tool, { session_start: true, conditional_hooks: false });

        assert.ok(changes.includes('PostToolUse:agent-record'), 'agent-record change reported');
        const config = JSON.parse(fs.readFileSync(tool.configPath, 'utf-8'));
        const group = config.hooks.PostToolUse.find(
            g => (g.hooks || []).some(h => (h.command || '').includes('delimit-agent-record'))
        );
        assert.ok(group, 'agent-record PostToolUse group exists');
        assert.strictEqual(group.matcher, 'Task|Agent', 'matches the subagent-spawn tool by either name');
    });

    it('writes a recorder script that records dispatch + completion with model/task_type/outcome/venture', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });
        const tool = { id: 'claude', name: 'Claude Code', configPath: path.join(claudeDir, 'settings.json') };

        crossModelHooks.installClaudeHooks(tool, { session_start: true });

        const recorderPath = path.join(claudeDir, 'hooks', 'delimit-agent-record');
        assert.ok(fs.existsSync(recorderPath), 'recorder script written');
        const s = fs.readFileSync(recorderPath, 'utf-8');
        // Reads the harness PostToolUse event fields.
        assert.ok(s.includes('tool_input') && s.includes('tool_response'), 'reads spawn input + result');
        assert.ok(s.includes('subagent_type'), 'reads subagent_type');
        // Records dispatch AND completion (fires post-completion).
        assert.ok(s.includes('dispatch_task(') && s.includes('complete_task('), 'records dispatch + completion');
        // Carries the LED-3720 instrumentation fields.
        assert.ok(s.includes('task_type=subagent_type'), 'carries task_type');
        assert.ok(s.includes('venture=venture'), 'carries venture');
        assert.ok(s.includes('"outcome": outcome'), 'carries outcome');
        assert.ok(s.includes('"model": subagent_type'), 'carries model proxy');
        // Never-disrupt contract: time-boxed + always exits 0.
        assert.ok(s.includes('timeout 6 python3'), 'time-boxed');
        assert.ok(/exit 0\s*$/.test(s.trim()), 'always exits 0');
        // Has a bundled-backend path and a direct-write fallback.
        assert.ok(s.includes('from ai import agent_dispatch as ad'), 'prefers bundled backend');
        assert.ok(s.includes('tasks.json'), 'has direct-write fallback');
    });

    it('is not installed when agent_record is false', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });
        const tool = { id: 'claude', name: 'Claude Code', configPath: path.join(claudeDir, 'settings.json') };

        const changes = crossModelHooks.installClaudeHooks(tool, { session_start: true, agent_record: false, conditional_hooks: false });

        assert.ok(!changes.includes('PostToolUse:agent-record'), 'not reported');
        assert.ok(!fs.existsSync(path.join(claudeDir, 'hooks', 'delimit-agent-record')), 'no recorder script');
    });

    it('is not installed when session_start is false (lifecycle-gated)', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });
        const tool = { id: 'claude', name: 'Claude Code', configPath: path.join(claudeDir, 'settings.json') };

        const changes = crossModelHooks.installClaudeHooks(tool, { session_start: false, conditional_hooks: false });
        assert.ok(!changes.includes('PostToolUse:agent-record'), 'not installed without session lifecycle');
    });

    it('does not duplicate the recorder on repeated installation', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });
        const tool = { id: 'claude', name: 'Claude Code', configPath: path.join(claudeDir, 'settings.json') };

        crossModelHooks.installClaudeHooks(tool, { session_start: true });
        crossModelHooks.installClaudeHooks(tool, { session_start: true });

        const config = JSON.parse(fs.readFileSync(tool.configPath, 'utf-8'));
        const recorders = config.hooks.PostToolUse.filter(
            g => (g.hooks || []).some(h => (h.command || '').includes('delimit-agent-record'))
        );
        assert.strictEqual(recorders.length, 1, 'exactly one recorder group');
    });

    it('is stripped by removeClaudeHooks (reversible)', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });
        const tool = { id: 'claude', name: 'Claude Code', configPath: path.join(claudeDir, 'settings.json') };

        crossModelHooks.installClaudeHooks(tool, { session_start: true });
        crossModelHooks.removeClaudeHooks();

        const config = JSON.parse(fs.readFileSync(tool.configPath, 'utf-8'));
        const recorders = (config.hooks && config.hooks.PostToolUse || []).filter(
            g => (g.hooks || []).some(h => (h.command || '').includes('delimit-agent-record'))
        );
        assert.strictEqual(recorders.length, 0, 'recorder removed on uninstall');
    });
});

// -----------------------------------------------------------------------
// SessionEnd hook — deterministic capture floor on real session exit
// -----------------------------------------------------------------------

describe('SessionEnd hook install', () => {
    beforeEach(() => { setupTmpHome(); });
    afterEach(() => { teardownTmpHome(); });

    it('installs a SessionEnd hook in the NESTED shape pointing at delimit-session-end', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });
        const tool = { id: 'claude', name: 'Claude Code', configPath: path.join(claudeDir, 'settings.json') };

        const changes = crossModelHooks.installClaudeHooks(tool, { session_start: true, conditional_hooks: false });

        assert.ok(changes.includes('SessionEnd'), 'SessionEnd change should be reported');

        const config = JSON.parse(fs.readFileSync(tool.configPath, 'utf-8'));
        assert.ok(Array.isArray(config.hooks.SessionEnd), 'SessionEnd should be an array');
        assert.strictEqual(config.hooks.SessionEnd.length, 1, 'exactly one SessionEnd group');

        const group = config.hooks.SessionEnd[0];
        // NESTED shape: { matcher, hooks: [{ type, command, timeout }] }
        assert.strictEqual(group.matcher, '', 'matcher present (nested shape)');
        assert.ok(Array.isArray(group.hooks), 'nested hooks[] array present');
        assert.strictEqual(group.hooks[0].type, 'command');
        assert.ok(group.hooks[0].command.includes('delimit-session-end'), 'points at delimit-session-end script');
        assert.strictEqual(group.hooks[0].timeout, 10, 'time-boxed under 10s');

        // The deployed script exists and is executable.
        const scriptPath = path.join(claudeDir, 'hooks', 'delimit-session-end');
        assert.ok(fs.existsSync(scriptPath), 'delimit-session-end script deployed');
        assert.ok((fs.statSync(scriptPath).mode & 0o111) !== 0, 'script is executable');
    });

    it('NEVER writes a FLAT-shape SessionEnd entry (Claude Code silently ignores flat)', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });
        const tool = { id: 'claude', name: 'Claude Code', configPath: path.join(claudeDir, 'settings.json') };

        crossModelHooks.installClaudeHooks(tool, { session_start: true, conditional_hooks: false });

        const config = JSON.parse(fs.readFileSync(tool.configPath, 'utf-8'));
        for (const group of config.hooks.SessionEnd) {
            // A flat entry carries command/type at the top level and no hooks[].
            assert.strictEqual(group.command, undefined, 'no top-level command (would be flat)');
            assert.strictEqual(group.type, undefined, 'no top-level type (would be flat)');
            assert.ok(Array.isArray(group.hooks), 'every group uses nested hooks[]');
        }
    });

    it('the deployed script does a deterministic, no-LLM floor capture (git + ledger + transcript tail)', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });
        const tool = { id: 'claude', name: 'Claude Code', configPath: path.join(claudeDir, 'settings.json') };

        crossModelHooks.installClaudeHooks(tool, { session_start: true });

        const s = fs.readFileSync(path.join(claudeDir, 'hooks', 'delimit-session-end'), 'utf-8');
        // Reads the SessionEnd event JSON from stdin for transcript_path.
        assert.ok(s.includes('SESSIONEND_EVENT_JSON'), 'reads the SessionEnd event JSON');
        assert.ok(s.includes('transcript_path'), 'reads transcript_path');
        // Deterministic floor: git state + ledger + transcript tail, NO model call.
        assert.ok(s.includes('rev-parse') && s.includes('status'), 'captures git state');
        assert.ok(s.includes('operations.jsonl'), 'captures cheap ledger context');
        assert.ok(s.includes('"source": "deterministic"'), 'floor is deterministic (no LLM)');
        assert.ok(s.includes('.last_capture'), 'stamps .last_capture for next revive');
        // Never-block contract: time-boxed + always exits 0.
        assert.ok(s.includes("timeout 8 python3"), 'time-boxed (<10s)');
        assert.ok(/exit 0\s*$/.test(s.trim()), 'always exits 0 (never blocks exit)');
    });

    it('is idempotent — installing twice yields exactly one SessionEnd group', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });
        const tool = { id: 'claude', name: 'Claude Code', configPath: path.join(claudeDir, 'settings.json') };

        crossModelHooks.installClaudeHooks(tool, { session_start: true, conditional_hooks: false });
        const changes2 = crossModelHooks.installClaudeHooks(tool, { session_start: true, conditional_hooks: false });

        assert.ok(!changes2.includes('SessionEnd'), 'second install reports no SessionEnd change');
        const config = JSON.parse(fs.readFileSync(tool.configPath, 'utf-8'));
        const delimitGroups = config.hooks.SessionEnd.filter(
            g => (g.hooks || []).some(hh => (hh.command || '').includes('delimit'))
        );
        assert.strictEqual(delimitGroups.length, 1, 'exactly one delimit SessionEnd group');
    });

    it('no-clobber — preserves a pre-existing user SessionEnd hook', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });
        // User already has their own SessionEnd hook (unrelated to delimit).
        const userConfig = {
            hooks: {
                SessionEnd: [
                    { matcher: '', hooks: [{ type: 'command', command: '/home/user/.scripts/my-cleanup.sh' }] },
                ],
            },
        };
        fs.writeFileSync(path.join(claudeDir, 'settings.json'), JSON.stringify(userConfig));

        const tool = { id: 'claude', name: 'Claude Code', configPath: path.join(claudeDir, 'settings.json') };
        crossModelHooks.installClaudeHooks(tool, { session_start: true, conditional_hooks: false });

        const config = JSON.parse(fs.readFileSync(tool.configPath, 'utf-8'));
        const commands = config.hooks.SessionEnd.flatMap(g => (g.hooks || []).map(h => h.command || ''));
        assert.ok(commands.some(c => c === '/home/user/.scripts/my-cleanup.sh'), 'user SessionEnd hook MUST be preserved');
        assert.ok(commands.some(c => c.includes('delimit-session-end')), 'delimit SessionEnd hook added alongside');
        assert.strictEqual(config.hooks.SessionEnd.length, 2, 'user hook + delimit hook = 2 groups');
    });

    it('is not installed when session_start is false (lifecycle-gated)', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });
        const tool = { id: 'claude', name: 'Claude Code', configPath: path.join(claudeDir, 'settings.json') };

        const changes = crossModelHooks.installClaudeHooks(tool, { session_start: false, conditional_hooks: false });
        assert.ok(!changes.includes('SessionEnd'), 'not installed without session lifecycle');
        const config = JSON.parse(fs.readFileSync(tool.configPath, 'utf-8'));
        assert.ok(!config.hooks.SessionEnd, 'no SessionEnd entry');
    });

    it('is stripped by removeClaudeHooks (reversible)', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });
        const tool = { id: 'claude', name: 'Claude Code', configPath: path.join(claudeDir, 'settings.json') };

        crossModelHooks.installClaudeHooks(tool, { session_start: true });
        crossModelHooks.removeClaudeHooks();

        const config = JSON.parse(fs.readFileSync(tool.configPath, 'utf-8'));
        const remaining = (config.hooks && config.hooks.SessionEnd || []).filter(
            g => (g.hooks || []).some(h => (h.command || '').includes('delimit'))
        );
        assert.strictEqual(remaining.length, 0, 'delimit SessionEnd removed on uninstall');
    });
});

// -----------------------------------------------------------------------
// LED-234: Helper function tests
// -----------------------------------------------------------------------

describe('findClaudeHookGroup', () => {
    it('finds hook in nested format', () => {
        const groups = [
            { matcher: 'Bash', hooks: [{ type: 'command', command: 'npx delimit-cli doctor' }] },
        ];
        const result = crossModelHooks.findClaudeHookGroup(groups, 'delimit-cli doctor');
        assert.ok(result, 'Should find the hook group');
        assert.strictEqual(result.matcher, 'Bash');
    });

    it('finds hook in flat format', () => {
        const groups = [
            { type: 'command', command: 'npx delimit-cli hook pre-tool', matcher: 'Edit' },
        ];
        const result = crossModelHooks.findClaudeHookGroup(groups, 'delimit-cli hook pre-tool');
        assert.ok(result, 'Should find flat-format hook');
    });

    it('returns null when not found', () => {
        const groups = [
            { matcher: 'Bash', hooks: [{ type: 'command', command: 'some-other-tool' }] },
        ];
        const result = crossModelHooks.findClaudeHookGroup(groups, 'delimit-cli');
        assert.strictEqual(result, null);
    });

    it('returns null for non-array input', () => {
        assert.strictEqual(crossModelHooks.findClaudeHookGroup(undefined, 'test'), null);
        assert.strictEqual(crossModelHooks.findClaudeHookGroup(null, 'test'), null);
    });
});

describe('migrateToNestedFormat', () => {
    it('converts flat format to nested', () => {
        const flat = { type: 'command', command: 'test-cmd', matcher: 'Edit', if: 'some condition' };
        const nested = crossModelHooks.migrateToNestedFormat(flat);

        assert.ok(nested.hooks, 'Should have hooks array');
        assert.strictEqual(nested.hooks[0].type, 'command');
        assert.strictEqual(nested.hooks[0].command, 'test-cmd');
        assert.strictEqual(nested.matcher, 'Edit');
        assert.strictEqual(nested.if, 'some condition');
    });

    it('returns already-nested format unchanged', () => {
        const nested = { matcher: 'Bash', hooks: [{ type: 'command', command: 'test' }] };
        const result = crossModelHooks.migrateToNestedFormat(nested);
        assert.strictEqual(result, nested, 'Should return same object');
    });
});

// -----------------------------------------------------------------------
// CLI deliberate command tests (LED-201)
// -----------------------------------------------------------------------

describe('CLI deliberate command', () => {
    it('deliberate --list runs without error', { skip: SKIP_IN_CI }, () => {
        const cliPath = path.join(__dirname, '..', 'bin', 'delimit-cli.js');
        const result = execSync(`node "${cliPath}" deliberate --list 2>&1`, {
            encoding: 'utf-8',
            timeout: 15000,
        });
        assert.ok(typeof result === 'string');
        // Should either say no pending items or list a count
        assert.ok(
            result.includes('pending') || result.includes('No pending') || result.includes('strategy'),
            'Output should discuss strategy items'
        );
    });

    it('deliberate with no args runs without error', { skip: SKIP_IN_CI }, () => {
        const cliPath = path.join(__dirname, '..', 'bin', 'delimit-cli.js');
        const result = execSync(`node "${cliPath}" deliberate 2>&1`, {
            encoding: 'utf-8',
            timeout: 5000,
        });
        assert.ok(typeof result === 'string');
        assert.ok(result.includes('Deliberation'), 'Output should include Deliberation header');
    });

    it('deliberate with a question saves pending.json', { skip: SKIP_IN_CI }, () => {
        const cliPath = path.join(__dirname, '..', 'bin', 'delimit-cli.js');
        const result = execSync(`node "${cliPath}" deliberate "Is this API change safe?" 2>&1`, {
            encoding: 'utf-8',
            timeout: 15000,
        });
        assert.ok(result.includes('Is this API change safe?'), 'Output should echo the question');
        assert.ok(result.includes('delimit_deliberate'), 'Output should mention the MCP tool');

        // Verify pending.json was created
        const HOME = process.env.HOME || os.homedir();
        const pendingPath = path.join(HOME, '.delimit', 'deliberation', 'pending.json');
        assert.ok(fs.existsSync(pendingPath), 'pending.json should be created');

        const pending = JSON.parse(fs.readFileSync(pendingPath, 'utf-8'));
        assert.strictEqual(pending.question, 'Is this API change safe?');
        assert.strictEqual(pending.status, 'pending');
        assert.ok(pending.created, 'Should have a created timestamp');

        // Clean up
        try { fs.unlinkSync(pendingPath); } catch {}
    });
});
