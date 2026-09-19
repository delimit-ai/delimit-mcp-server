const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const ROOT = path.resolve(__dirname, '..');

test('customer scripts are explicit: installation and Muse survive, new release helpers do not', () => {
    // Exercise npm's real packlist with the production manifest in an isolated
    // fixture, so adding the sentinel never mutates a release worktree.
    const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-pack-scripts-'));
    try {
        fs.copyFileSync(path.join(ROOT, 'package.json'), path.join(fixture, 'package.json'));
        fs.copyFileSync(path.join(ROOT, '.npmignore'), path.join(fixture, '.npmignore'));
        fs.cpSync(path.join(ROOT, 'scripts'), path.join(fixture, 'scripts'), { recursive: true });
        fs.writeFileSync(path.join(fixture, 'scripts', 'new-release-only-helper.js'), '// release only\n');
        const packed = spawnSync('npm', ['pack', '--dry-run', '--json', '--ignore-scripts'], {
            cwd: fixture, encoding: 'utf8', timeout: 30000,
        });
        assert.equal(packed.status, 0, packed.stderr || String(packed.error || ''));
        const scripts = JSON.parse(packed.stdout)[0].files.map(f => f.path)
            .filter(p => p.startsWith('scripts/')).sort();
        assert.deepEqual(scripts, ['scripts/muse_mcp.py', 'scripts/postinstall.js']);
        const manifest = JSON.parse(fs.readFileSync(path.join(ROOT, 'package.json'), 'utf8'));
        assert.equal(manifest.scripts.postinstall, 'node scripts/postinstall.js');
        assert.deepEqual(manifest.files.filter(p => p.startsWith('scripts/') || p.startsWith('!scripts/')).sort(), scripts);
    } finally {
        fs.rmSync(fixture, { recursive: true, force: true });
    }
});
