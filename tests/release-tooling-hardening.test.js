const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { makeTmpGitRepo } = require('./_git-hermetic');
const ROOT = path.resolve(__dirname, '..');

function copy(repo, rel) {
    const dest = path.join(repo.dir, rel);
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.copyFileSync(path.join(ROOT, rel), dest);
}
function write(repo, rel, content, mode) {
    const dest = path.join(repo.dir, rel);
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.writeFileSync(dest, content, mode ? { mode } : undefined);
}
function run(repo, command, args, env = {}) {
    return spawnSync(command, args, { cwd: repo.dir, env: { ...repo.env, ...env }, encoding: 'utf8' });
}
function versionFixture() {
    const repo = makeTmpGitRepo();
    copy(repo, 'scripts/sync-version-sources.js');
    write(repo, 'package.json', JSON.stringify({ version: '9.8.7' }));
    write(repo, 'server.json', JSON.stringify({ version: '1.0.0', packages: [{ version: '1.0.0' }] }));
    write(repo, 'gateway/VERSION', '1.0.0\n');
    write(repo, 'gateway/ai/server.py', '_VERSION_FALLBACK = "1.0.0"\n');
    repo.run('git add .'); repo.run('git commit -qm fixture');
    return repo;
}

test('version lifecycle writes and stages fallback, marker and both registry fields', () => {
    const repo = versionFixture();
    try {
        const rejected = run(repo, 'node', ['scripts/sync-version-sources.js', '--bundle-only'], { npm_lifecycle_event: 'version' });
        assert.notEqual(rejected.status, 0);
        assert.match(rejected.stderr, /cannot skip staging/);
        const result = run(repo, 'node', ['scripts/sync-version-sources.js']);
        assert.equal(result.status, 0, result.stderr);
        assert.match(repo.run('git show :gateway/ai/server.py'), /"9.8.7"/);
        assert.equal(repo.run('git show :gateway/VERSION'), '9.8.7');
        const registry = JSON.parse(repo.run('git show :server.json'));
        assert.equal(registry.version, '9.8.7'); assert.equal(registry.packages[0].version, '9.8.7');
        assert.equal(repo.run('git diff --name-only'), '');
    } finally { repo.cleanup(); }
});

test('gateway sync repairs overwritten fallback without staging or changing registry', () => {
    const repo = versionFixture();
    try {
        copy(repo, 'scripts/sync-gateway.sh');
        write(repo, 'bundle-allowlist.txt', 'gateway/ai/server.py\ngateway/core/__init__.py\ngateway/tasks/__init__.py\ngateway/VERSION\n');
        for (const dir of ['source/ai', 'source/core', 'source/tasks', 'gateway/core', 'gateway/tasks']) {
            fs.mkdirSync(path.join(repo.dir, dir), { recursive: true });
        }
        write(repo, 'source/core/__init__.py', '# core\n');
        write(repo, 'source/tasks/__init__.py', '# tasks\n');
        write(repo, 'source/ai/server.py', '_VERSION_FALLBACK = "4.15.0"\n');
        const beforeIndex = repo.run('git write-tree');
        const result = run(repo, 'bash', ['scripts/sync-gateway.sh'], {
            GATEWAY_OVERRIDE: path.join(repo.dir, 'source'), SKIP_SERVER_SYNC: '1',
        });
        assert.equal(result.status, 0, result.stdout + result.stderr);
        assert.match(fs.readFileSync(path.join(repo.dir, 'gateway/ai/server.py'), 'utf8'), /"9.8.7"/);
        assert.equal(repo.run('git write-tree'), beforeIndex);
        assert.equal(JSON.parse(fs.readFileSync(path.join(repo.dir, 'server.json'))).version, '1.0.0');
        const first = fs.readFileSync(path.join(repo.dir, 'gateway/ai/server.py'), 'utf8');
        assert.equal(run(repo, 'node', ['scripts/sync-version-sources.js', '--bundle-only']).status, 0);
        assert.equal(fs.readFileSync(path.join(repo.dir, 'gateway/ai/server.py'), 'utf8'), first);
    } finally { repo.cleanup(); }
});

test('gateway sync leaves installed server alone unless explicitly opted in and forced for an override', () => {
    const repo = versionFixture();
    try {
        copy(repo, 'scripts/sync-gateway.sh');
        write(repo, 'bundle-allowlist.txt', 'gateway/ai/server.py\ngateway/core/__init__.py\ngateway/tasks/__init__.py\ngateway/VERSION\n');
        write(repo, 'source/ai/server.py', '_VERSION_FALLBACK = "4.15.0"\n');
        write(repo, 'source/core/__init__.py', '# source core\n');
        write(repo, 'source/tasks/__init__.py', '# source tasks\n');
        write(repo, 'gateway/core/__init__.py', '# old core\n');
        write(repo, 'gateway/tasks/__init__.py', '# old tasks\n');
        const home = path.join(repo.dir, 'fake-home');
        const installed = path.join(home, '.delimit/server');
        write(repo, 'fake-home/.delimit/server/ai/sentinel.py', '# preserve me\n');
        write(repo, 'fake-home/.delimit/server/core/sentinel.py', '# preserve me\n');
        write(repo, 'fake-home/.delimit/server/VERSION', 'old-version\n');
        const env = {
            HOME: home, GATEWAY_OVERRIDE: path.join(repo.dir, 'source'),
            SKIP_SERVER_SYNC: '', SYNC_INSTALLED_SERVER: '', FORCE_INSTALLED_SERVER_SYNC: '',
        };
        const sync = extra => run(repo, 'bash', ['scripts/sync-gateway.sh'], { ...env, ...extra });

        const defaultRun = sync();
        assert.equal(defaultRun.status, 0, defaultRun.stdout + defaultRun.stderr);
        assert.match(defaultRun.stdout, /Skipping installed server sync \(default\); set SYNC_INSTALLED_SERVER=1/);
        assert.ok(fs.existsSync(path.join(installed, 'ai/sentinel.py')));
        assert.ok(fs.existsSync(path.join(installed, 'core/sentinel.py')));
        assert.equal(fs.readFileSync(path.join(installed, 'VERSION'), 'utf8'), 'old-version\n');

        const skipped = sync({ SKIP_SERVER_SYNC: '1', SYNC_INSTALLED_SERVER: '1', FORCE_INSTALLED_SERVER_SYNC: '1' });
        assert.equal(skipped.status, 0, skipped.stdout + skipped.stderr);
        assert.match(skipped.stdout, /Skipping installed server sync \(SKIP_SERVER_SYNC=1\)/);
        assert.ok(fs.existsSync(path.join(installed, 'ai/sentinel.py')));

        const refused = sync({ SYNC_INSTALLED_SERVER: '1' });
        assert.notEqual(refused.status, 0);
        assert.match(refused.stderr, /GATEWAY_OVERRIDE selects a pinned export.*FORCE_INSTALLED_SERVER_SYNC=1/);
        assert.ok(fs.existsSync(path.join(installed, 'ai/sentinel.py')));
        assert.ok(fs.existsSync(path.join(installed, 'core/sentinel.py')));

        const forced = sync({ SYNC_INSTALLED_SERVER: '1', FORCE_INSTALLED_SERVER_SYNC: '1' });
        assert.equal(forced.status, 0, forced.stdout + forced.stderr);
        assert.equal(fs.existsSync(path.join(installed, 'ai/sentinel.py')), false);
        assert.equal(fs.existsSync(path.join(installed, 'core/sentinel.py')), false);
        assert.match(fs.readFileSync(path.join(installed, 'ai/server.py'), 'utf8'), /4\.15\.0/);
        assert.equal(fs.readFileSync(path.join(installed, 'VERSION'), 'utf8'), '9.8.7\n');

        fs.rmSync(path.join(installed, 'core'), { recursive: true });
        fs.symlinkSync(path.join(repo.dir, 'source/core'), path.join(installed, 'core'), 'dir');
        const symlinkRefused = sync({ SYNC_INSTALLED_SERVER: '1', FORCE_INSTALLED_SERVER_SYNC: '1' });
        assert.notEqual(symlinkRefused.status, 0);
        assert.match(symlinkRefused.stderr, /is a symlink.*refusing to rsync --delete/);
    } finally { repo.cleanup(); }
});

test('missing fallback and staging failures block version synchronization', () => {
    const repo = versionFixture();
    try {
        write(repo, 'gateway/ai/server.py', '# fallback disappeared\n');
        assert.notEqual(run(repo, 'node', ['scripts/sync-version-sources.js']).status, 0);
        write(repo, 'gateway/ai/server.py', '_VERSION_FALLBACK = "1.0.0"\n');
        write(repo, '.git/index.lock', 'test lock');
        const result = run(repo, 'node', ['scripts/sync-version-sources.js']);
        assert.notEqual(result.status, 0);
        assert.match(result.stderr, /FAILED to stage/);
    } finally { repo.cleanup(); }
});

test('local source guard requires full workflow pin, clean matching source and matching environment', () => {
    const repo = makeTmpGitRepo();
    const source = makeTmpGitRepo();
    try {
        copy(repo, 'scripts/gateway-clean-guard.sh');
        fs.symlinkSync(path.join(ROOT, 'node_modules'), path.join(repo.dir, 'node_modules'), 'dir');
        const head = source.run('git rev-parse HEAD');
        const workflow = pin => write(repo, '.github/workflows/publish.yml', `env:\n  GATEWAY_SOURCE_SHA: '${pin}'\n`);
        const guard = env => run(repo, 'bash', ['scripts/gateway-clean-guard.sh'], { GATEWAY_OVERRIDE: source.dir, GATEWAY_SOURCE_SHA: head, ...env });
        workflow(head); assert.equal(guard().status, 0);
        workflow('0'.repeat(40)); assert.notEqual(guard({ GATEWAY_SOURCE_SHA: '0'.repeat(40), GATEWAY_CLEAN_GUARD_ACK: '1' }).status, 0);
        workflow(head.slice(0, 7)); assert.notEqual(guard().status, 0);
        workflow(''); assert.notEqual(guard().status, 0);
        workflow(head); assert.notEqual(guard({ GATEWAY_SOURCE_SHA: '1'.repeat(40) }).status, 0);
        write(source, 'README.md', 'dirty\n'); assert.notEqual(guard().status, 0);
    } finally { source.cleanup(); repo.cleanup(); }
});

test('release push executes a rejecting pre-push hook and stops before PR/tagging', () => {
    const repo = makeTmpGitRepo();
    const remote = makeTmpGitRepo();
    try {
        copy(repo, 'scripts/release.sh');
        write(repo, 'scripts/security-check.sh', '#!/bin/bash\nexit 0\n');
        write(repo, 'scripts/gateway-clean-guard.sh', '#!/bin/bash\nexit 0\n');
        write(repo, 'package.json', '{"version":"1.0.0"}\n');
        write(repo, 'package-lock.json', '{}\n'); write(repo, 'server.json', '{}\n');
        write(repo, 'gateway/VERSION', '1.0.0\n');
        write(repo, 'fake-bin/npm', '#!/bin/bash\nif [ "$1" = version ]; then echo "{\\"version\\":\\"1.0.1\\"}" > package.json; fi\nexit 0\n', 0o755);
        write(repo, 'fake-bin/gh', '#!/bin/bash\ntouch gh-was-invoked\nexit 1\n', 0o755);
        repo.run('git branch -M main'); repo.run('git add .'); repo.run('git commit -qm fixture');
        repo.run(`git remote add origin ${remote.dir}`);
        write(repo, '.git/hooks/pre-push', '#!/bin/bash\ntouch hook-was-invoked\nexit 1\n', 0o755);
        const result = run(repo, 'bash', ['scripts/release.sh', '1.0.1'], { PATH: path.join(repo.dir, 'fake-bin') + ':' + repo.env.PATH });
        assert.notEqual(result.status, 0);
        assert.ok(fs.existsSync(path.join(repo.dir, 'hook-was-invoked')), result.stdout + result.stderr);
        assert.equal(fs.existsSync(path.join(repo.dir, 'gh-was-invoked')), false);
        assert.equal(repo.run('git tag --list'), '');
        assert.equal(remote.run('git branch --list release/v1.0.1'), '');
    } finally { remote.cleanup(); repo.cleanup(); }
});
