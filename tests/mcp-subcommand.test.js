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
    assert.throws(() => require('../lib/mcp-launcher').ensureMcpInstall(), /Python 3\.10\+ is required/);
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

test('plugin lock ships in npm and every requirement is exactly pinned and hashed', () => {
    const root = path.join(__dirname, '..');
    assert.ok(require('../package.json').files.includes('requirements-plugin.lock'));
    const lock = fs.readFileSync(path.join(root, 'requirements-plugin.lock'), 'utf8');
    const entries = lock.replace(/\\\r?\n/g, '').split('\n')
        .map(line => line.trim()).filter(line => line && !line.startsWith('#'));
    assert.ok(entries.length > 4, 'lock must include transitive dependencies');
    for (const entry of entries) {
        assert.match(entry, /^[a-z0-9][a-z0-9._-]*==[^\s;]+/i);
        assert.match(entry, /--hash=sha256:[a-f0-9]{64}(?:\s|$)/);
    }
    for (const dep of require('../lib/mcp-launcher').FALLBACK_PY_DEPS) {
        assert.ok(entries.some(entry => entry.startsWith(`${dep} `)), `lock missing ${dep}`);
    }
});

// Execute the real launcher with isolated filesystem paths and simulated Python
// processes, so install arguments and version selection are tested without PyPI.
function simulatedInstall(t, version, lockPresent = true, installStatus = 0) {
    const home = tempHome(t);
    const root = path.join(__dirname, '..');
    const calls = [];
    const vm = require('node:vm');
    const module = { exports: {} };
    const fakeFs = { ...fs, existsSync(target) {
        if (target === path.join(root, 'requirements-plugin.lock')) return lockPresent;
        if (!lockPresent && path.basename(target) === 'requirements.txt') return false;
        return fs.existsSync(target);
    } };
    const childProcess = { spawnSync(command, args) {
        calls.push({ command, args });
        if (args[0] === '-c') return { status: 0, stdout: version };
        if (args[1] === 'venv') {
            const python = path.join(args[2], process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
            fs.mkdirSync(path.dirname(python), { recursive: true });
            fs.writeFileSync(python, '# fixture');
            return { status: 0 };
        }
        return { status: installStatus };
    } };
    vm.runInNewContext(fs.readFileSync(path.join(root, 'lib/mcp-launcher.js'), 'utf8'), {
        module, __dirname: path.join(root, 'lib'), process,
        require(name) {
            if (name === 'fs') return fakeFs;
            if (name === 'os') return { homedir: () => home };
            if (name === 'child_process') return childProcess;
            if (name === '../bin/delimit-setup') return require('../bin/delimit-setup');
            if (name === '../package.json') return require('../package.json');
            return require(name);
        },
    });
    return { run: module.exports.ensureMcpInstall, calls, home };
}

test('plugin installs the lock with --require-hashes and --no-deps on Python 3.10–3.13', t => {
    for (const version of ['3.10', '3.11', '3.12', '3.13']) {
        const fixture = simulatedInstall(t, version);
        fixture.run();
        const args = fixture.calls.find(call => call.args[1] === 'pip').args;
        assert.deepEqual(Array.from(args), ['-m', 'pip', 'install', '--quiet', '--no-cache-dir',
            '--require-hashes', '--no-deps', '-r', path.join(__dirname, '..', 'requirements-plugin.lock')]);
    }
});

test('plugin rejects Python 3.9 before creating a venv', t => {
    const fixture = simulatedInstall(t, '3.9');
    assert.throws(fixture.run, /Python 3\.10\+ is required/);
    assert.equal(fixture.calls.some(call => call.args[1] === 'venv'), false);
    assert.match(fs.readFileSync(path.join(__dirname, '..', 'claude-plugin/README.md'), 'utf8'), /Python 3\.10 or later/);
});

test('plugin uses fallback pins only when the lock is missing', t => {
    const fixture = simulatedInstall(t, '3.10', false);
    fixture.run();
    const args = fixture.calls.find(call => call.args[1] === 'pip').args;
    assert.deepEqual(Array.from(args).slice(5), require('../lib/mcp-launcher').FALLBACK_PY_DEPS);
});

test('failed hash install never falls back or writes the success marker', t => {
    const fixture = simulatedInstall(t, '3.10', true, 1);
    assert.throws(fixture.run, /Could not install Delimit Python requirements/);
    assert.equal(fixture.calls.filter(call => call.args[1] === 'pip').length, 1);
    const venv = path.join(fixture.home, '.delimit', 'plugin-server', require('../package.json').version, 'venv');
    assert.equal(fs.existsSync(venv), false);
});
