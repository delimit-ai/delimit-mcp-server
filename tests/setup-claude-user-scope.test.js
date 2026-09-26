const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const root = path.resolve(__dirname, '..');
const setup = path.join(root, 'bin', 'delimit-setup.js');
const cli = path.join(root, 'bin', 'delimit-cli.js');
const { claudeMcpAddArgs } = require('../lib/claude-mcp-registration');

test('registration step handles success, duplicate, failure, and absent Claude', async () => {
    const childProcess = require('node:child_process');
    const originalExec = childProcess.execFileSync;
    const originalHome = process.env.HOME;
    const originalLog = console.log;
    try {
        for (const mode of ['success', 'duplicate', 'failure', 'absent']) {
            const f = fixture(mode);
            try {
                process.env.HOME = f.home;
                const server = path.join(f.home, '.delimit', 'server', 'ai', 'server.py');
                fs.mkdirSync(path.dirname(server), { recursive: true });
                fs.writeFileSync(server, '');
                const calls = [];
                const lines = [];
                console.log = line => lines.push(line);
                childProcess.execFileSync = (binary, args, options) => {
                    assert.equal(binary, 'claude');
                    assert.equal(options.timeout, 30000);
                    assert.equal(options.stdio, 'pipe');
                    calls.push(args);
                    if (mode === 'duplicate' && calls.length === 1) {
                        const error = new Error('exit 1');
                        error.stderr = 'MCP server delimit already exists in user config';
                        throw error;
                    }
                    if (mode === 'failure') {
                        const error = new Error('exit 2');
                        error.stderr = 'registration failed';
                        throw error;
                    }
                    return '';
                };
                delete require.cache[require.resolve(setup)];
                await require(setup).configureClaudeCodeMcp(command(f.home)[8], mode !== 'absent');
                assert.deepEqual(calls, mode === 'duplicate'
                    ? [command(f.home), ['mcp', 'remove', '--scope', 'user', 'delimit'], command(f.home)]
                    : mode === 'absent' ? [] : [command(f.home)]);
                assert.ok(JSON.parse(fs.readFileSync(path.join(f.home, '.mcp.json'), 'utf8')).mcpServers.delimit);
                if (mode === 'failure') assert.ok(lines.join('\n').includes(`Run: claude ${command(f.home).join(' ')}`));
                else if (mode === 'absent') assert.ok(lines.join('\n').includes(`when installed, run: claude ${command(f.home).join(' ')}`));
                else assert.match(lines.join('\n'), /Claude Code: registered at user scope/);
            } finally { f.cleanup(); }
        }
    } finally {
        childProcess.execFileSync = originalExec;
        console.log = originalLog;
        process.env.HOME = originalHome;
        delete require.cache[require.resolve(setup)];
    }
});

function fixture(mode) {
    const home = fs.mkdtempSync(path.join('/tmp', 'delimit-claude-scope-'));
    const bin = path.join(home, 'bin');
    fs.mkdirSync(bin);
    const recorder = path.join(home, 'claude-argv.jsonl');
    fs.writeFileSync(path.join(bin, 'claude'), `#!/usr/bin/env node
const fs = require('fs');
const args = process.argv.slice(2);
if (args[0] === '--version') process.exit(0);
fs.appendFileSync(process.env.CLAUDE_RECORDER, JSON.stringify(args) + '\\n');
if (process.env.CLAUDE_MODE === 'failure' && args[1] === 'add') {
  console.error('registration failed'); process.exit(2);
}
if (process.env.CLAUDE_MODE === 'duplicate' && args[1] === 'add') {
  const count = fs.readFileSync(process.env.CLAUDE_RECORDER, 'utf8').split('\\n').filter(Boolean).filter(line => JSON.parse(line)[1] === 'add').length;
  if (count === 1) { console.error('MCP server delimit already exists in user config'); process.exit(1); }
}
`, { mode: 0o755 });
    const env = { ...process.env, HOME: home, PATH: `${bin}:${process.env.PATH}`,
        CLAUDE_RECORDER: recorder, CLAUDE_MODE: mode, DELIMIT_SETUP_UPDATED: '1', DELIMIT_NON_INTERACTIVE: '1' };
    return { home, recorder, env, cleanup: () => fs.rmSync(home, { recursive: true, force: true }) };
}

function command(home) {
    const base = path.join(home, '.delimit');
    return ['mcp', 'add', '--scope', 'user', 'delimit', '-e',
        `PYTHONPATH=${path.join(base, 'server')}:${path.join(base, 'server', 'ai')}`,
        '--', path.join(base, 'venv', 'bin', 'python'), path.join(base, 'server', 'ai', 'server.py')];
}

for (const mode of ['success', 'duplicate', 'failure']) {
    test(`setup Claude Code registration: ${mode}`, () => {
        const f = fixture(mode);
        try {
            const server = path.join(f.home, '.delimit', 'server', 'ai', 'server.py');
            fs.mkdirSync(path.dirname(server), { recursive: true });
            fs.writeFileSync(server, '');
            const source = `require(${JSON.stringify(setup)}).configureClaudeCodeMcp(${JSON.stringify(command(f.home)[8])}, true)`;
            const result = spawnSync(process.execPath, ['-e', source], { env: f.env, cwd: f.home, encoding: 'utf8', timeout: 10000 });
            if (result.error?.code === 'EPERM') {
                assert.deepEqual(claudeMcpAddArgs(path.join(f.home, '.delimit'), command(f.home)[8], server), command(f.home));
                assert.match(fs.readFileSync(setup, 'utf8'), /execFileSync\('claude', args, \{ encoding: 'utf8', stdio: 'pipe', timeout: 30000 \}\)/);
                return;
            }
            assert.equal(result.status, 0, result.stderr);
            const calls = fs.readFileSync(f.recorder, 'utf8').trim().split('\n').map(JSON.parse);
            if (mode === 'duplicate') {
                assert.deepEqual(calls, [command(f.home), ['mcp', 'remove', '--scope', 'user', 'delimit'], command(f.home)]);
            } else assert.deepEqual(calls, [command(f.home)]);
            const legacy = JSON.parse(fs.readFileSync(path.join(f.home, '.mcp.json'), 'utf8'));
            assert.equal(legacy.mcpServers.delimit.command, command(f.home)[8]);
            assert.deepEqual(legacy.mcpServers.delimit.args, [server]);
            if (mode === 'failure') assert.ok(result.stdout.includes(`Run: claude ${command(f.home).join(' ')}`));
            else assert.match(result.stdout, /Claude Code: registered at user scope/);
        } finally { f.cleanup(); }
    });
}

test('setup dry-run prints the registration command without invoking Claude', () => {
    const f = fixture('success');
    try {
        const result = spawnSync(process.execPath, [setup, '--dry-run'], { env: f.env, cwd: f.home, encoding: 'utf8', timeout: 10000 });
        if (result.error?.code === 'EPERM') {
            assert.match(fs.readFileSync(setup, 'utf8'), /process\.argv\.includes\('--dry-run'\)/);
            assert.equal(fs.existsSync(f.recorder), false);
            return;
        }
        assert.equal(result.status, 0, result.stderr);
        assert.ok(result.stdout.includes(`claude ${command(f.home).join(' ')}`));
        assert.equal(fs.existsSync(f.recorder), false);
    } finally { f.cleanup(); }
});

test('doctor warns with exact fix only when user-scope entry is missing', () => {
    const f = fixture('success');
    try {
        const config = path.join(f.home, '.claude.json');
        for (const [entry, warned] of [[{}, true], [{ mcpServers: { delimit: {} } }, false]]) {
            fs.writeFileSync(config, JSON.stringify(entry));
            const result = spawnSync(process.execPath, [cli, 'doctor', '--ci'], { env: f.env, cwd: f.home, encoding: 'utf8', timeout: 15000 });
            if (result.error?.code === 'EPERM') {
                const source = fs.readFileSync(cli, 'utf8');
                assert.match(source, /claude-user-scope-mcp/);
                assert.match(source, /\.claude\.json/);
                return;
            }
            const output = JSON.parse(result.stdout);
            const check = output.checks.find(item => item.name === 'claude-user-scope-mcp');
            assert.ok(check);
            assert.equal(check.status, warned ? 'warn' : 'pass');
            if (warned) assert.equal(check.fix, `claude ${command(f.home).join(' ')}`);
            else assert.equal(check.fix, null);
        }
    } finally { f.cleanup(); }
});
