/**
 * LED-5629: `delimit quickstart` threw "ReferenceError: logp is not defined"
 * on its first step, on every path (clone success AND the offline fallback),
 * because logp was only ever defined in bin/delimit-setup.js. Reproduced on a
 * clean `npm i -g delimit-cli@4.19.9` install.
 */

const { describe, it } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');

const CLI = path.join(__dirname, '..', 'bin', 'delimit-cli.js');

describe('LED-5629 delimit quickstart does not crash', () => {
    it('defines every paced-output helper the CLI calls', () => {
        const src = fs.readFileSync(CLI, 'latin1');
        if (/\blogp\(/.test(src)) {
            assert.match(src, /(async\s+)?function\s+logp\s*\(|const\s+logp\s*=/,
                'bin/delimit-cli.js calls logp() but never defines it');
        }
    });

    it('completes the offline fallback path with a failing git clone', () => {
        const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-qs-'));
        const bin = path.join(tmp, 'bin');
        const home = path.join(tmp, 'home');
        const cwd = path.join(tmp, 'work');
        fs.mkdirSync(bin); fs.mkdirSync(home); fs.mkdirSync(cwd);
        // A git that always fails forces the "Creating demo files locally" path
        // without touching the network.
        fs.writeFileSync(path.join(bin, 'git'), '#!/bin/sh\nexit 1\n', { mode: 0o755 });
        try {
            const r = spawnSync(process.execPath, [CLI, 'quickstart'], {
                cwd,
                input: '',
                encoding: 'utf-8',
                timeout: 120000,
                env: {
                    PATH: `${bin}${path.delimiter}${process.env.PATH || ''}`,
                    HOME: home,
                    FORCE_COLOR: '0',
                    CI: '1',
                },
            });
            const out = `${r.stdout}\n${r.stderr}`;
            assert.doesNotMatch(out, /ReferenceError/);
            assert.strictEqual(r.status, 0, out);
            assert.match(out, /Created demo specs locally/);
            assert.match(out, /Created \.delimit\/policies\.yml \(strict\)/);
            const demo = path.join(cwd, 'delimit-demo');
            assert.ok(fs.existsSync(path.join(demo, 'openapi.yaml')));
            assert.ok(fs.existsSync(path.join(demo, 'openapi-changed.yaml')));
            assert.ok(fs.existsSync(path.join(demo, '.delimit', 'policies.yml')));
        } finally {
            fs.rmSync(tmp, { recursive: true, force: true });
        }
    });
});
