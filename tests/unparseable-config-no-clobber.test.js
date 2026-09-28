// Regression suite (audit 2026-09-28): a user-owned JSON config that exists but
// is NOT plain JSON (JSONC comments, a trailing comma, a half-written file)
// must be left byte-identical by setup and hook installation.
//
// Before this fix every merge site used `catch { config = {} }` and then wrote
// the Delimit-only object back, which silently deleted the user's
// permissions.deny rules, env, model, and every other MCP server. The session
// start auto-update runs `delimit-cli setup` unattended, so one stray comma was
// enough to wipe a user's Claude Code settings with no prompt and no backup.

const { describe, it, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');

process.env.DELIMIT_WRAPPED = 'true';
process.env.DELIMIT_NO_AUTO_UPDATE = '1';

const crossModelHooks = require('../lib/cross-model-hooks');

const ORIGINAL_HOME = process.env.HOME;
const ORIGINAL_CWD = process.cwd();
let tmpDir;

const JSONC_SETTINGS = [
    '{',
    '  // user comment',
    '  "permissions": { "deny": ["Bash(rm -rf:*)"], "allow": ["Read"] },',
    '  "env": { "FOO": "bar" },',
    '  "model": "opus",',
    '}',
    '',
].join('\n');

function setupTmpHome() {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-unparseable-'));
    process.env.HOME = tmpDir;
    return tmpDir;
}

function teardownTmpHome() {
    process.chdir(ORIGINAL_CWD);
    process.env.HOME = ORIGINAL_HOME;
    if (tmpDir && fs.existsSync(tmpDir)) fs.rmSync(tmpDir, { recursive: true, force: true });
}

function withStderrSilenced(fn) {
    const orig = process.stderr.write;
    process.stderr.write = () => true;
    try { return fn(); } finally { process.stderr.write = orig; }
}

describe('unparseable user JSON configs are never rewritten', () => {
    beforeEach(() => { setupTmpHome(); });
    afterEach(() => { teardownTmpHome(); });

    it('installClaudeHooks leaves an unparseable global settings.json byte-identical', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });
        const settingsPath = path.join(claudeDir, 'settings.json');
        fs.writeFileSync(settingsPath, JSONC_SETTINGS);
        const workDir = path.join(tmpDir, 'work');
        fs.mkdirSync(workDir);
        process.chdir(workDir);

        const changes = withStderrSilenced(() => crossModelHooks.installClaudeHooks(
            { id: 'claude', name: 'Claude Code', configPath: settingsPath },
            { session_start: true, pre_tool: true, pre_commit: true },
        ));

        assert.strictEqual(fs.readFileSync(settingsPath, 'utf-8'), JSONC_SETTINGS);
        assert.ok(changes.some(c => c.startsWith('skipped:')), 'skip must be reported');
    });

    it('installClaudeHooks leaves an unparseable project settings.json byte-identical but still updates a valid global file', () => {
        const claudeDir = path.join(tmpDir, '.claude');
        fs.mkdirSync(claudeDir, { recursive: true });
        const globalPath = path.join(claudeDir, 'settings.json');
        fs.writeFileSync(globalPath, JSON.stringify({ permissions: { deny: ['X'] } }, null, 2));
        const projectDir = path.join(tmpDir, 'proj');
        fs.mkdirSync(path.join(projectDir, '.claude'), { recursive: true });
        const projectPath = path.join(projectDir, '.claude', 'settings.json');
        fs.writeFileSync(projectPath, JSONC_SETTINGS);
        process.chdir(projectDir);

        withStderrSilenced(() => crossModelHooks.installClaudeHooks(
            { id: 'claude', name: 'Claude Code', configPath: globalPath },
            { session_start: true, pre_tool: true, pre_commit: true },
        ));

        assert.strictEqual(fs.readFileSync(projectPath, 'utf-8'), JSONC_SETTINGS);
        const global = JSON.parse(fs.readFileSync(globalPath, 'utf-8'));
        assert.deepStrictEqual(global.permissions, { deny: ['X'] }, 'valid global keys preserved');
        assert.ok(global.hooks && Object.keys(global.hooks).length > 0, 'valid global file still receives hooks');
    });

    it('installGeminiHooks leaves JSONC settings.json byte-identical and still writes GEMINI.md', () => {
        const geminiDir = path.join(tmpDir, '.gemini');
        fs.mkdirSync(geminiDir, { recursive: true });
        const settingsPath = path.join(geminiDir, 'settings.json');
        fs.writeFileSync(settingsPath, JSONC_SETTINGS);

        withStderrSilenced(() => crossModelHooks.installGeminiHooks(
            { id: 'gemini', name: 'Gemini CLI', configPath: settingsPath },
            { session_start: true },
        ));

        assert.strictEqual(fs.readFileSync(settingsPath, 'utf-8'), JSONC_SETTINGS);
        assert.ok(fs.existsSync(path.join(geminiDir, 'GEMINI.md')), 'GEMINI.md still managed');
    });

    it('installAntigravityHooks leaves JSONC settings.json byte-identical', () => {
        const agDir = path.join(tmpDir, '.gemini', 'antigravity-cli');
        fs.mkdirSync(agDir, { recursive: true });
        const settingsPath = path.join(agDir, 'settings.json');
        fs.writeFileSync(settingsPath, JSONC_SETTINGS);

        withStderrSilenced(() => crossModelHooks.installAntigravityHooks(
            { id: 'antigravity', name: 'Antigravity CLI', configPath: settingsPath },
            { session_start: true },
        ));

        assert.strictEqual(fs.readFileSync(settingsPath, 'utf-8'), JSONC_SETTINGS);
    });

    it('installCodexHooks leaves an unparseable config.json byte-identical', () => {
        const codexDir = path.join(tmpDir, '.codex');
        fs.mkdirSync(codexDir, { recursive: true });
        const configPath = path.join(codexDir, 'config.json');
        fs.writeFileSync(configPath, JSONC_SETTINGS);

        withStderrSilenced(() => crossModelHooks.installCodexHooks(
            { id: 'codex', name: 'Codex', configPath },
            { pre_commit: true },
        ));

        assert.strictEqual(fs.readFileSync(configPath, 'utf-8'), JSONC_SETTINGS);
    });

    it('setup configureClaudeCodeMcp leaves an unparseable ~/.mcp.json byte-identical', async () => {
        const mcpPath = path.join(tmpDir, '.mcp.json');
        const userMcp = '{\n  // mine\n  "mcpServers": { "github": { "command": "gh-mcp" }, },\n}\n';
        fs.writeFileSync(mcpPath, userMcp);
        // MCP_CONFIG is resolved from HOME at module load, so load a fresh copy.
        const setupPath = require.resolve('../bin/delimit-setup.js');
        delete require.cache[setupPath];
        const setup = require(setupPath);
        const origLog = console.log;
        console.log = () => {};
        try {
            await setup.configureClaudeCodeMcp('/usr/bin/python3', false);
        } finally {
            console.log = origLog;
            delete require.cache[setupPath];
        }
        assert.strictEqual(fs.readFileSync(mcpPath, 'utf-8'), userMcp);
    });

    it('setup configureClaudeCodeMcp still merges into a valid ~/.mcp.json', async () => {
        const mcpPath = path.join(tmpDir, '.mcp.json');
        fs.writeFileSync(mcpPath, JSON.stringify({ mcpServers: { github: { command: 'gh-mcp' } } }));
        const setupPath = require.resolve('../bin/delimit-setup.js');
        delete require.cache[setupPath];
        const setup = require(setupPath);
        const origLog = console.log;
        console.log = () => {};
        try {
            await setup.configureClaudeCodeMcp('/usr/bin/python3', false);
        } finally {
            console.log = origLog;
            delete require.cache[setupPath];
        }
        const merged = JSON.parse(fs.readFileSync(mcpPath, 'utf-8'));
        assert.deepStrictEqual(merged.mcpServers.github, { command: 'gh-mcp' });
        assert.ok(merged.mcpServers.delimit, 'delimit entry added');
    });
});
