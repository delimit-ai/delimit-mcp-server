const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const ROOT = path.resolve(__dirname, '..');

// Runs scripts/check-server-json.sh against a copy of the record so a failing
// fixture never touches the real server.json.
function runGuard(mutate, env = {}) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'server-json-guard-'));
    try {
        fs.mkdirSync(path.join(dir, 'scripts'));
        fs.copyFileSync(path.join(ROOT, 'scripts/check-server-json.sh'), path.join(dir, 'scripts/check-server-json.sh'));
        fs.copyFileSync(path.join(ROOT, 'package.json'), path.join(dir, 'package.json'));
        const record = JSON.parse(fs.readFileSync(path.join(ROOT, 'server.json'), 'utf8'));
        if (mutate) mutate(record);
        fs.writeFileSync(path.join(dir, 'server.json'), JSON.stringify(record, null, 2));
        return spawnSync('bash', ['scripts/check-server-json.sh'], {
            cwd: dir, encoding: 'utf8', env: { ...process.env, ...env },
        });
    } finally {
        fs.rmSync(dir, { recursive: true, force: true });
    }
}

test('the shipped server.json passes the registry guard', () => {
    const res = runGuard(null, { FORCE_COLOR: '3' });
    assert.equal(res.status, 0, res.stdout + res.stderr);
});

test('a description over 100 chars fails even when FORCE_COLOR is set', () => {
    for (const color of ['0', '3']) {
        const res = runGuard(r => { r.description = 'x'.repeat(101); }, { FORCE_COLOR: color });
        assert.notEqual(res.status, 0, `FORCE_COLOR=${color} passed a 101-char description`);
        assert.match(res.stdout + res.stderr, /101 chars/);
    }
});
