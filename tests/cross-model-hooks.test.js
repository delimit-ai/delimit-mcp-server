const { describe, it, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');
const { execSync } = require('child_process');

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
        assert.strictEqual(detected.length, 3);
    });
});

// -----------------------------------------------------------------------
// Claude Code hook installation tests
// -----------------------------------------------------------------------

describe('installClaudeHooks', () => {
    beforeEach(() => { setupTmpHome(); });
    afterEach(() => { teardownTmpHome(); });

    it('creates settings.json with SessionStart and PreToolUse hooks', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });

        const tool = {
            id: 'claude',
            name: 'Claude Code',
            configPath: path.join(claudeDir, 'settings.json'),
        };
        const hookConfig = { session_start: true, pre_tool: true, pre_commit: true };

        const changes = crossModelHooks.installClaudeHooks(tool, hookConfig);

        assert.ok(changes.includes('SessionStart'));
        assert.ok(changes.includes('PreToolUse'));

        const config = JSON.parse(fs.readFileSync(tool.configPath, 'utf-8'));
        assert.ok(config.hooks.SessionStart, 'SessionStart hooks should exist');
        assert.ok(config.hooks.PreToolUse, 'PreToolUse hooks should exist');

        const sessionHook = config.hooks.SessionStart[0];
        assert.strictEqual(sessionHook.type, 'command');
        assert.ok(sessionHook.command.includes('delimit-cli hook session-start'));

        const preToolHook = config.hooks.PreToolUse[0];
        assert.strictEqual(preToolHook.matcher, 'Edit|Write|Bash');
    });

    it('does not duplicate hooks on repeated installation', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });

        const tool = {
            id: 'claude',
            name: 'Claude Code',
            configPath: path.join(claudeDir, 'settings.json'),
        };
        const hookConfig = { session_start: true, pre_tool: true, pre_commit: true };

        // Install twice
        crossModelHooks.installClaudeHooks(tool, hookConfig);
        const changes2 = crossModelHooks.installClaudeHooks(tool, hookConfig);

        assert.strictEqual(changes2.length, 0, 'Second install should make no changes');

        const config = JSON.parse(fs.readFileSync(tool.configPath, 'utf-8'));
        assert.strictEqual(config.hooks.SessionStart.length, 1, 'Should have exactly one SessionStart hook');
        assert.strictEqual(config.hooks.PreToolUse.length, 1, 'Should have exactly one PreToolUse hook');
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
        const hookConfig = { session_start: false, pre_tool: true, pre_commit: true };

        const changes = crossModelHooks.installClaudeHooks(tool, hookConfig);

        assert.ok(!changes.includes('SessionStart'), 'SessionStart should not be installed');
        assert.ok(changes.includes('PreToolUse'), 'PreToolUse should be installed');

        const config = JSON.parse(fs.readFileSync(tool.configPath, 'utf-8'));
        assert.ok(!config.hooks.SessionStart, 'No SessionStart entry should exist');
    });
});

// -----------------------------------------------------------------------
// Codex hook installation tests
// -----------------------------------------------------------------------

describe('installCodexHooks', () => {
    beforeEach(() => { setupTmpHome(); });
    afterEach(() => { teardownTmpHome(); });

    it('creates instructions.md with governance block', () => {
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
        assert.ok(instructions.includes('npx delimit-cli hook session-start'));

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

    it('creates settings.json and GEMINI.md', () => {
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
        assert.ok(config.customInstructions.includes('delimit-cli hook'));

        const geminiMd = fs.readFileSync(path.join(geminiDir, 'GEMINI.md'), 'utf-8');
        assert.ok(geminiMd.includes('Delimit Governance'));
    });
});

// -----------------------------------------------------------------------
// Hook removal tests
// -----------------------------------------------------------------------

describe('removeClaudeHooks', () => {
    beforeEach(() => { setupTmpHome(); });
    afterEach(() => { teardownTmpHome(); });

    it('removes Delimit hooks from settings.json', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });

        const config = {
            hooks: {
                SessionStart: [
                    { type: 'command', command: 'npx delimit-cli hook session-start' },
                    { type: 'command', command: 'some-other-tool' },
                ],
                PreToolUse: [
                    { type: 'command', command: 'npx delimit-cli hook pre-tool', matcher: 'Edit|Write|Bash' },
                ],
            }
        };
        fs.writeFileSync(path.join(claudeDir, 'settings.json'), JSON.stringify(config));

        const removed = crossModelHooks.removeClaudeHooks();
        assert.ok(removed, 'Should report changes');

        const updated = JSON.parse(fs.readFileSync(path.join(claudeDir, 'settings.json'), 'utf-8'));
        assert.strictEqual(updated.hooks.SessionStart.length, 1, 'Non-delimit hook preserved');
        assert.strictEqual(updated.hooks.SessionStart[0].command, 'some-other-tool');
        assert.ok(!updated.hooks.PreToolUse, 'Empty PreToolUse array should be removed');
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

        assert.strictEqual(tools.length, 3, 'Should detect 3 tools');
        assert.strictEqual(results.length, 3, 'Should have 3 results');

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
    it('hook session-start runs without error', () => {
        const cliPath = path.join(__dirname, '..', 'bin', 'delimit-cli.js');
        // Should not throw
        const result = execSync(`node "${cliPath}" hook session-start 2>&1`, {
            encoding: 'utf-8',
            timeout: 5000,
        });
        assert.ok(typeof result === 'string', 'Should produce output');
        assert.ok(result.includes('[Delimit]'), 'Output should contain Delimit prefix');
    });

    it('hook pre-tool runs without error', () => {
        const cliPath = path.join(__dirname, '..', 'bin', 'delimit-cli.js');
        const result = execSync(`node "${cliPath}" hook pre-tool Edit 2>&1`, {
            encoding: 'utf-8',
            timeout: 5000,
        });
        // pre-tool may produce no output if no staged files, that is fine
        assert.ok(typeof result === 'string');
    });

    it('hook pre-commit runs without error', () => {
        const cliPath = path.join(__dirname, '..', 'bin', 'delimit-cli.js');
        const result = execSync(`node "${cliPath}" hook pre-commit 2>&1`, {
            encoding: 'utf-8',
            timeout: 5000,
        });
        assert.ok(typeof result === 'string');
    });

    it('hook session-start completes in under 2 seconds', () => {
        const cliPath = path.join(__dirname, '..', 'bin', 'delimit-cli.js');
        const start = Date.now();
        execSync(`node "${cliPath}" hook session-start 2>&1`, {
            encoding: 'utf-8',
            timeout: 5000,
        });
        const elapsed = Date.now() - start;
        assert.ok(elapsed < 2000, `Hook took ${elapsed}ms, should be under 2000ms`);
    });
});
