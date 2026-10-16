'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const plugin = path.resolve(__dirname, '../claude-plugin');
const declarations = JSON.parse(fs.readFileSync(path.join(plugin, 'hooks/hooks.json'), 'utf8')).hooks;

function snapshotDirectory(directory, prefix = '') {
    return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
        const name = path.join(prefix, entry.name);
        return entry.isDirectory()
            ? [name, ...snapshotDirectory(path.join(directory, entry.name), name)]
            : [name];
    }).sort();
}

// Exercise the commands Claude actually loads, rather than a dormant helper.
for (const [event, script, expected] of [
    ['SessionStart', 'session-start.sh', /delimit_ledger_context.*delimit_handoff_list/],
    ['Stop', 'session-stop.sh', /offer to create a Delimit handoff/],
]) {
    test(`${event} emits fixed instructions without consulting local records or input`, {
        skip: process.platform === 'win32'
            ? 'These declared hooks require POSIX bash and cat; native Windows hook execution is unsupported.'
            : false,
    }, () => {
        const root = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-plugin-hook-'));
        try {
            const home = path.join(root, 'home');
            const project = path.join(root, 'project');
            const bin = path.join(root, 'bin');
            for (const dir of [home, project, bin]) fs.mkdirSync(dir);
            const secret = 'synthetic-hook-private-sentinel';
            fs.writeFileSync(path.join(project, 'package.json'), JSON.stringify({ name: secret }));
            fs.mkdirSync(path.join(home, '.delimit'));
            fs.writeFileSync(path.join(home, '.delimit', 'local-record.json'), secret);
            // Only cat is available; git, node, package installers and network
            // clients are absent from this deliberately limited PATH.
            fs.symlinkSync('/bin/cat', path.join(bin, 'cat'));
            const before = snapshotDirectory(home);
            assert.equal(declarations[event].length, 1);
            assert.equal(declarations[event][0].hooks.length, 1);
            const hook = declarations[event][0].hooks[0];
            assert.equal(hook.type, 'command');
            assert.equal(hook.command, `bash "${'${CLAUDE_PLUGIN_ROOT}'}/hooks/${script}"`);
            const result = spawnSync('/bin/bash', ['--noprofile', '--norc', path.join(plugin, 'hooks', script)], {
                cwd: project,
                env: { HOME: home, PATH: bin, CLAUDE_PLUGIN_ROOT: plugin, SYNTHETIC_CREDENTIAL: secret },
                input: JSON.stringify({ prompt: secret, cwd: project }),
                encoding: 'utf8', timeout: 2000,
            });
            assert.equal(result.status, 0, result.stderr);
            assert.equal(result.stderr, '');
            const context = JSON.parse(result.stdout).hookSpecificOutput;
            assert.equal(context.hookEventName, event);
            assert.match(context.additionalContext, expected);
            assert.ok(!result.stdout.includes(secret));
            assert.deepEqual(snapshotDirectory(home), before);
            assert.equal(fs.readFileSync(path.join(home, '.delimit', 'local-record.json'), 'utf8'), secret);
        } finally {
            fs.rmSync(root, { recursive: true, force: true });
        }
    });
}
