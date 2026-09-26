'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const cli = path.join(__dirname, '..', 'bin', 'delimit-cli.js');

function tempHome(t) {
    const home = fs.mkdtempSync(path.join('/tmp', 'delimit-mcp-test-'));
    t.after(() => fs.rmSync(home, { recursive: true, force: true }));
    return home;
}

function launch(home, args = []) {
    return spawnSync(process.execPath, [cli, 'mcp', ...args], {
        env: { ...process.env, HOME: home, DELIMIT_HOME: path.join(home, '.delimit'), DELIMIT_NON_INTERACTIVE: '1' },
        encoding: 'utf8', timeout: 30000,
    });
}

test('mcp preserves stdout for JSON-RPC and passes toolset without changing assistant configs', t => {
    const home = tempHome(t);
    const { version } = require('../package.json');
    const serverDir = path.join(home, '.delimit', 'plugin-server', version);
    const python = path.join(serverDir, 'venv', 'bin', 'python');
    fs.mkdirSync(path.join(serverDir, 'ai'), { recursive: true });
    fs.mkdirSync(path.dirname(python), { recursive: true });
    fs.writeFileSync(path.join(serverDir, 'ai', 'server.py'), '# fixture\n');
    fs.writeFileSync(path.join(serverDir, 'requirements.txt'), '# fixture\n');
    fs.writeFileSync(path.join(serverDir, 'VERSION'), `${version}\n`);
    fs.writeFileSync(path.join(serverDir, 'venv', '.delimit-deps-ok'), `${version}\n`);
    fs.writeFileSync(python, '#!/bin/sh\nprintf \'{"toolset":"%s"}\\n\' "$DELIMIT_TOOLSET"\n', { mode: 0o755 });
    const config = path.join(home, '.mcp.json');
    fs.writeFileSync(config, '{"sentinel":true}\n');
    const result = launch(home, ['--toolset', 'records']);
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.stdout, '{"toolset":"records"}\n');
    assert.equal(fs.readFileSync(config, 'utf8'), '{"sentinel":true}\n');
    assert.deepEqual(fs.readdirSync(home).sort(), ['.delimit', '.mcp.json']);
    assert.equal(fs.existsSync(path.join(home, '.claude')), false);
});

test('mcp reports missing Python on stderr without writing assistant configs', t => {
    const home = tempHome(t);
    const saved = { HOME: process.env.HOME, PATH: process.env.PATH };
    t.after(() => { process.env.HOME = saved.HOME; process.env.PATH = saved.PATH; });
    process.env.HOME = home;
    process.env.PATH = '/nonexistent';
    assert.throws(() => require('../lib/mcp-launcher').ensureMcpInstall(), /Python 3\.9\+ is required/);
    assert.equal(fs.existsSync(path.join(home, '.mcp.json')), false);
    assert.equal(fs.existsSync(path.join(home, '.claude')), false);
});

test('mcp reports a failed venv without changing assistant configs', t => {
    const home = tempHome(t);
    const bin = path.join(home, 'fake-bin');
    fs.mkdirSync(bin);
    fs.writeFileSync(path.join(bin, 'python3'), '#!/bin/sh\nif [ "$1" = "-c" ]; then printf "3.11\\n"; else exit 1; fi\n', { mode: 0o755 });
    const saved = { HOME: process.env.HOME, PATH: process.env.PATH };
    t.after(() => { process.env.HOME = saved.HOME; process.env.PATH = saved.PATH; });
    process.env.HOME = home;
    process.env.PATH = bin;
    assert.throws(() => require('../lib/mcp-launcher').ensureMcpInstall(), /Could not create the Delimit Python venv/);
    assert.equal(fs.existsSync(path.join(home, '.mcp.json')), false);
    assert.equal(fs.existsSync(path.join(home, '.claude')), false);
});

test('plugin venv is versioned and never touches the setup venv', t => {
    const home = tempHome(t);
    const userVenv = path.join(home, '.delimit', 'venv');
    fs.mkdirSync(path.join(userVenv, 'bin'), { recursive: true });
    fs.writeFileSync(path.join(userVenv, 'bin', 'python'), '# user venv sentinel\n');
    const bin = path.join(home, 'fake-bin');
    fs.mkdirSync(bin);
    fs.writeFileSync(path.join(bin, 'python3'), '#!/bin/sh\nif [ "$1" = "-c" ]; then printf "3.11\\n"; else exit 1; fi\n', { mode: 0o755 });
    const saved = { HOME: process.env.HOME, PATH: process.env.PATH };
    t.after(() => { process.env.HOME = saved.HOME; process.env.PATH = saved.PATH; });
    process.env.HOME = home;
    process.env.PATH = bin;
    // A stale plugin venv without the deps marker (an interrupted install) is rebuilt, not reused.
    const { version } = require('../package.json');
    const stale = path.join(home, '.delimit', 'plugin-server', version, 'venv', 'bin');
    fs.mkdirSync(stale, { recursive: true });
    fs.writeFileSync(path.join(stale, 'python'), '# half-installed\n');
    assert.throws(() => require('../lib/mcp-launcher').ensureMcpInstall(), /Could not create the Delimit Python venv/);
    assert.equal(fs.existsSync(path.join(stale, 'python')), false);
    assert.equal(fs.readFileSync(path.join(userVenv, 'bin', 'python'), 'utf8'), '# user venv sentinel\n');
});

test('launcher fallback Python deps stay in lockstep with delimit setup', () => {
    const fs = require('fs');
    const path = require('path');
    const { FALLBACK_PY_DEPS } = require('../lib/mcp-launcher');
    const setup = fs.readFileSync(path.join(__dirname, '..', 'bin', 'delimit-setup.js'), 'utf8');
    for (const dep of FALLBACK_PY_DEPS) assert.ok(setup.includes(dep), `setup no longer pins ${dep}`);
});
