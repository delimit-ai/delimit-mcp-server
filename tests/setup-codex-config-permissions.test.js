const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

// Audit 2026-09-28 F8: setup used to force an existing ~/.codex/config.toml to
// 0644 on every run. That file commonly holds MCP env tokens, so a user's 0600
// config became world-readable. Setup may only ADD the owner read/write bits it
// needs; it must never widen group/other access.
//
// Exercise the real setup config block (same extraction as
// setup-env-preservation.test.js) without running installation or touching HOME.
const setupSource = fs.readFileSync(path.join(__dirname, '..', 'bin', 'delimit-setup.js'), 'utf8');
const helperSource = setupSource.split('async function configureClaudeCodeMcp(')[1]
    .split('\nasync function main(')[0];
const makeClaudeHelper = new Function(
    'fs', 'path', 'DELIMIT_HOME', 'MCP_CONFIG', 'logp', 'green', 'log', 'yellow',
    'claudeMcpAddArgs', 'displayClaudeCommand', 'execFileSync',
    `return async function configureClaudeCodeMcp(${helperSource}`,
);
const configSource = setupSource.split('    // Step 3: Configure Claude Code MCP')[1]
    .split('    // Checkpoint: MCP is configured')[0];
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const configure = new AsyncFunction(
    'fs', 'path', 'os', 'DELIMIT_HOME', 'MCP_CONFIG', 'CLAUDE_DIR',
    'python', 'step', 'logp', 'green', 'yellow', 'log', 'hasClaude', 'configureClaudeCodeMcp',
    configSource,
);

async function runSetup(home) {
    fs.mkdirSync(path.join(home, '.claude'), { recursive: true });
    const delimitHome = path.join(home, '.delimit');
    const mcpConfig = path.join(home, '.mcp.json');
    const { claudeMcpAddArgs, displayClaudeCommand } = require('../lib/claude-mcp-registration');
    const configureClaudeCodeMcp = makeClaudeHelper(fs, path, delimitHome, mcpConfig,
        async () => {}, s => s, () => {}, s => s,
        claudeMcpAddArgs, displayClaudeCommand, () => {});
    await configure(
        fs, path, { homedir: () => home }, delimitHome,
        mcpConfig, path.join(home, '.claude'),
        '/managed/python', () => {}, async () => {}, s => s, s => s, () => {}, false, configureClaudeCodeMcp,
    );
}

function withHome(test) {
    return async () => {
        const home = fs.mkdtempSync('/tmp/delimit-setup-codex-perms-');
        try { await test(home); } finally { fs.rmSync(home, { recursive: true, force: true }); }
    };
}

function writeCodexConfig(home, mode) {
    const file = path.join(home, '.codex', 'config.toml');
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, '[mcp_servers.github.env]\nGITHUB_TOKEN = "placeholder-not-a-secret"\n');
    fs.chmodSync(file, mode);
    return file;
}

const modeOf = file => fs.statSync(file).mode & 0o777;
const fmt = m => '0' + m.toString(8);

describe('setup never widens ~/.codex/config.toml permissions', () => {
    it('keeps a private 0600 config private', withHome(async home => {
        const file = writeCodexConfig(home, 0o600);
        await runSetup(home);
        assert.equal(fmt(modeOf(file)), fmt(0o600));
        assert.match(fs.readFileSync(file, 'utf8'), /\[mcp_servers\.delimit\]/);
        assert.match(fs.readFileSync(file, 'utf8'), /GITHUB_TOKEN = "placeholder-not-a-secret"/);
    }));

    it('adds only the owner read/write bits it needs (0400 -> 0600)', withHome(async home => {
        const file = writeCodexConfig(home, 0o400);
        await runSetup(home);
        assert.equal(fmt(modeOf(file)), fmt(0o600));
        assert.match(fs.readFileSync(file, 'utf8'), /\[mcp_servers\.delimit\]/);
    }));

    it('keeps a group-private 0640 config without adding world read', withHome(async home => {
        const file = writeCodexConfig(home, 0o640);
        await runSetup(home);
        assert.equal(fmt(modeOf(file)), fmt(0o640));
    }));

    it('leaves an already-0644 config at 0644 (no behavior change)', withHome(async home => {
        const file = writeCodexConfig(home, 0o644);
        await runSetup(home);
        assert.equal(fmt(modeOf(file)), fmt(0o644));
    }));
});
