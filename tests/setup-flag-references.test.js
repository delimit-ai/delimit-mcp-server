const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const ROOT = path.resolve(__dirname, '..');

// Every `delimit setup --flag` the CLI prints or runs must be an option
// `delimit setup` accepts. `doctor` once recommended and auto-ran
// `delimit setup --all`, which exits "unknown option" (LED-5815).
function setupOptions() {
    const res = spawnSync(process.execPath, [path.join(ROOT, 'bin/delimit-cli.js'), 'setup', '--help'], {
        encoding: 'utf8', env: { ...process.env, DELIMIT_NO_AUTO_UPDATE: '1', FORCE_COLOR: '0' },
    });
    assert.equal(res.status, 0, res.stderr);
    return new Set([...res.stdout.matchAll(/^\s+(?:-\w, )?(--[a-z][a-z-]*)/gm)].map(m => m[1]));
}

function sourceFiles() {
    const out = [];
    for (const dir of ['bin', 'lib']) {
        for (const name of fs.readdirSync(path.join(ROOT, dir))) {
            if (name.endsWith('.js')) out.push(path.join(dir, name));
        }
    }
    return out;
}

test('every referenced `setup --flag` is a real setup option', () => {
    const options = setupOptions();
    assert.ok(options.has('--yes') && options.has('--dry-run'), [...options].join(' '));
    const bad = [];
    for (const rel of sourceFiles()) {
        const text = fs.readFileSync(path.join(ROOT, rel), 'utf8');
        for (const m of text.matchAll(/delimit(?:-cli)? setup((?: --[a-z][a-z-]*)+)/g)) {
            for (const flag of m[1].trim().split(/\s+/)) {
                if (!options.has(flag)) bad.push(`${rel}: ${m[0]}`);
            }
        }
    }
    assert.deepEqual(bad, []);
});
