const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

// Exercise the actual setup config block without running installation or touching HOME.
const setupSource = fs.readFileSync(path.join(__dirname, '..', 'bin', 'delimit-setup.js'), 'utf8');
const configSource = setupSource.split('    // Step 3: Configure Claude Code MCP')[1]
    .split('    // Checkpoint: MCP is configured')[0];
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const configure = new AsyncFunction(
    'fs', 'path', 'os', 'DELIMIT_HOME', 'MCP_CONFIG', 'CLAUDE_DIR',
    'python', 'step', 'logp', 'green', 'yellow', 'log',
    configSource,
);

const jsonTargets = [
    '.mcp.json',
    '.cursor/mcp.json',
    '.gemini/settings.json',
    '.gemini/antigravity-cli/settings.json',
];

async function runSetup(home) {
    for (const dir of ['.claude', '.codex', '.cursor', '.gemini/antigravity-cli']) {
        fs.mkdirSync(path.join(home, dir), { recursive: true });
    }
    await configure(
        fs, path, { homedir: () => home }, path.join(home, '.delimit'),
        path.join(home, '.mcp.json'), path.join(home, '.claude'),
        '/managed/python', () => {}, async () => {}, s => s, s => s, () => {},
    );
}

function withHome(test) {
    return async () => {
        const home = fs.mkdtempSync('/tmp/delimit-setup-env-');
        try { await test(home); } finally { fs.rmSync(home, { recursive: true, force: true }); }
    };
}

describe('setup preserves assistant MCP environment', () => {
    it('keeps user env keys and refreshes managed paths in every existing config', withHome(async home => {
        for (const target of jsonTargets) {
            const file = path.join(home, target);
            fs.mkdirSync(path.dirname(file), { recursive: true });
            fs.writeFileSync(file, JSON.stringify({
                mcpServers: {
                    other: { command: 'other-server' },
                    delimit: {
                        command: 'stale-python', args: ['stale-server'], cwd: 'stale-cwd',
                        env: {
                            PYTHONPATH: 'stale-path',
                            DELIMIT_RATE_LIMIT_DELIBERATE: '20',
                            FOO_API_KEY: 'user-secret',
                        },
                    },
                },
            }));
        }
        const codexFile = path.join(home, '.codex/config.toml');
        fs.mkdirSync(path.dirname(codexFile), { recursive: true });
        fs.writeFileSync(codexFile, `[settings]\ntheme = "dark"\n\n[mcp_servers.delimit]\ncommand = "stale-python"\nargs = ["stale-server"]\ncwd = "stale-cwd"\n\n[mcp_servers.delimit.env]\nPYTHONPATH = "stale-path"\nDELIMIT_RATE_LIMIT_DELIBERATE = "20"\nFOO_API_KEY = "user-secret"\n\n[mcp_servers.other]\ncommand = "other-server"\n`);

        await runSetup(home);
        await runSetup(home); // Auto-updates run setup repeatedly.

        const serverDir = path.join(home, '.delimit/server');
        for (const target of jsonTargets) {
            const config = JSON.parse(fs.readFileSync(path.join(home, target), 'utf8'));
            const entry = config.mcpServers.delimit;
            assert.deepEqual(entry.env, {
                PYTHONPATH: serverDir,
                DELIMIT_RATE_LIMIT_DELIBERATE: '20',
                FOO_API_KEY: 'user-secret',
            }, target);
            assert.equal(entry.command, '/managed/python', target);
            assert.deepEqual(entry.args, [path.join(serverDir, 'mcp-server.py')], target);
            assert.equal(entry.cwd, serverDir, target);
            assert.deepEqual(config.mcpServers.other, { command: 'other-server' }, target);
        }
        const toml = fs.readFileSync(codexFile, 'utf8');
        assert.match(toml, /\[settings\]\ntheme = "dark"/);
        assert.match(toml, /\[mcp_servers\.other\]\ncommand = "other-server"/);
        assert.match(toml, /command = "\/managed\/python"/);
        assert.ok(toml.includes(`PYTHONPATH = "${serverDir}:${path.join(serverDir, 'ai')}"`));
        assert.match(toml, /DELIMIT_RATE_LIMIT_DELIBERATE = "20"/);
        assert.match(toml, /FOO_API_KEY = "user-secret"/);
        assert.equal((toml.match(/^FOO_API_KEY = /gm) || []).length, 1);
        assert.doesNotMatch(toml, /stale-path|stale-python|stale-server|stale-cwd/);
    }));

    it('creates fresh entries with only the managed environment', withHome(async home => {
        await runSetup(home);
        const serverDir = path.join(home, '.delimit/server');
        for (const target of jsonTargets) {
            const entry = JSON.parse(fs.readFileSync(path.join(home, target), 'utf8')).mcpServers.delimit;
            assert.deepEqual(entry.env, { PYTHONPATH: serverDir }, target);
            assert.equal(entry.command, '/managed/python', target);
        }
        const toml = fs.readFileSync(path.join(home, '.codex/config.toml'), 'utf8');
        assert.match(toml, /\[mcp_servers\.delimit\.env\]/);
        assert.equal((toml.match(/^PYTHONPATH = /gm) || []).length, 1);
        assert.doesNotMatch(toml, /DELIMIT_RATE_LIMIT_DELIBERATE|FOO_API_KEY/);
    }));
});
