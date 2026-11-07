'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const ROOT = path.resolve(__dirname, '..');
const CHECK_SCRIPT = path.join(ROOT, 'scripts', 'check-claude-plugin-limits.mjs');
const BUILD_SCRIPT = path.join(ROOT, 'scripts', 'build-claude-plugin.mjs');

test('check-claude-plugin-limits passes on the committed plugin files', () => {
    const res = spawnSync(process.execPath, [CHECK_SCRIPT], {
        cwd: ROOT,
        encoding: 'utf8',
    });
    assert.equal(res.status, 0, res.stdout + res.stderr);
    assert.match(res.stdout, /PASS: All plugin limits and review rules satisfied/);
    assert.match(res.stdout, /Total files: \d+ \/ 512/);
});

test('check-claude-plugin-limits catches files exceeding 256 KiB', () => {
    const pluginDir = path.join(ROOT, 'claude-plugin-bundled');
    const oversizeFile = path.join(pluginDir, 'oversize-test-fixture.tmp');
    try {
        // Write 257 KiB of dummy text
        fs.writeFileSync(oversizeFile, 'A'.repeat(257 * 1024), 'utf8');
        const res = spawnSync(process.execPath, [CHECK_SCRIPT], {
            cwd: ROOT,
            encoding: 'utf8',
        });
        assert.equal(res.status, 1);
        assert.match(res.stderr, /exceeds 256 KiB limit/);
    } finally {
        if (fs.existsSync(oversizeFile)) fs.unlinkSync(oversizeFile);
    }
});

test('check-claude-plugin-limits boundary: exactly 262,144 bytes fails, 262,143 passes', () => {
    const pluginDir = path.join(ROOT, 'claude-plugin-bundled');
    const boundaryFile = path.join(pluginDir, 'boundary-size-fixture.tmp');
    try {
        // 262,143 bytes (< 256 KiB) must pass
        fs.writeFileSync(boundaryFile, Buffer.alloc(262143, 'a'));
        const passRes = spawnSync(process.execPath, [CHECK_SCRIPT], {
            cwd: ROOT,
            encoding: 'utf8',
        });
        assert.equal(passRes.status, 0, passRes.stdout + passRes.stderr);
        assert.match(passRes.stdout, /PASS: All plugin limits and review rules satisfied/);

        const buildPass = spawnSync(process.execPath, [BUILD_SCRIPT], {
            cwd: ROOT,
            encoding: 'utf8',
        });
        assert.equal(buildPass.status, 0, buildPass.stdout + buildPass.stderr);

        // exactly 262,144 bytes (256 KiB) must fail under the >= limit rule
        fs.writeFileSync(boundaryFile, Buffer.alloc(262144, 'a'));
        const failRes = spawnSync(process.execPath, [CHECK_SCRIPT], {
            cwd: ROOT,
            encoding: 'utf8',
        });
        assert.equal(failRes.status, 1, 'Expected 262,144 bytes to fail limit check');
        assert.match(failRes.stderr, /exceeds 256 KiB limit/);

        const buildFail = spawnSync(process.execPath, [BUILD_SCRIPT], {
            cwd: ROOT,
            encoding: 'utf8',
        });
        assert.equal(buildFail.status, 1, 'Expected 262,144 bytes to fail build check');
        assert.match(buildFail.stderr, /exceeds limit of 262144 bytes/);
    } finally {
        if (fs.existsSync(boundaryFile)) fs.unlinkSync(boundaryFile);
    }
});

test('check-claude-plugin-limits boundary: exactly 512 files fails, 511 passes', () => {
    const pluginDir = path.join(ROOT, 'claude-plugin-bundled');
    const fixtureDir = path.join(pluginDir, 'boundary-count-fixtures');
    try {
        fs.mkdirSync(fixtureDir, { recursive: true });

        function countNonFixtureFiles(dir) {
            let count = 0;
            for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
                const full = path.join(dir, entry.name);
                if (entry.isDirectory()) {
                    if (full !== fixtureDir) {
                        count += countNonFixtureFiles(full);
                    }
                } else if (entry.isFile()) {
                    count++;
                }
            }
            return count;
        }

        const baseCount = countNonFixtureFiles(pluginDir);
        const neededFor511 = 511 - baseCount;
        for (let i = 0; i < neededFor511; i++) {
            fs.writeFileSync(path.join(fixtureDir, `count-${i}.txt`), 'ok');
        }

        const passRes = spawnSync(process.execPath, [CHECK_SCRIPT], {
            cwd: ROOT,
            encoding: 'utf8',
        });
        assert.equal(passRes.status, 0, passRes.stdout + passRes.stderr);
        assert.match(passRes.stdout, /Total files: 511 \/ 512/);
        assert.match(passRes.stdout, /PASS: All plugin limits and review rules satisfied/);

        const buildPass = spawnSync(process.execPath, [BUILD_SCRIPT], {
            cwd: ROOT,
            encoding: 'utf8',
        });
        assert.equal(buildPass.status, 0, buildPass.stdout + buildPass.stderr);

        // Add 1 more file to reach exactly 512 files
        fs.writeFileSync(path.join(fixtureDir, `count-${neededFor511}.txt`), 'ok');

        const failRes = spawnSync(process.execPath, [CHECK_SCRIPT], {
            cwd: ROOT,
            encoding: 'utf8',
        });
        assert.equal(failRes.status, 1, 'Expected 512 files to fail limit check');
        assert.match(failRes.stderr, /File count 512 exceeds limit of 512/);

        const buildFail = spawnSync(process.execPath, [BUILD_SCRIPT], {
            cwd: ROOT,
            encoding: 'utf8',
        });
        assert.equal(buildFail.status, 1, 'Expected 512 files to fail build check');
        assert.match(buildFail.stderr, /exceeds maximum limit of 512/);
    } finally {
        if (fs.existsSync(fixtureDir)) {
            fs.rmSync(fixtureDir, { recursive: true, force: true });
        }
    }
});

test('check-claude-plugin-limits catches non-shell scripts in subfolders', () => {
    const hooksDir = path.join(ROOT, 'claude-plugin-bundled', 'hooks');
    const forbiddenScript = path.join(hooksDir, 'forbidden.js');
    try {
        fs.writeFileSync(forbiddenScript, 'console.log("bad");', 'utf8');
        const res = spawnSync(process.execPath, [CHECK_SCRIPT], {
            cwd: ROOT,
            encoding: 'utf8',
        });
        assert.equal(res.status, 1);
        assert.match(res.stderr, /Non-shell script found in subfolder/);
    } finally {
        if (fs.existsSync(forbiddenScript)) fs.unlinkSync(forbiddenScript);
    }
});

test('build-claude-plugin is reproducible and passes limits check', () => {
    const buildRes = spawnSync(process.execPath, [BUILD_SCRIPT], {
        cwd: ROOT,
        encoding: 'utf8',
    });
    assert.equal(buildRes.status, 0, buildRes.stdout + buildRes.stderr);
    assert.match(buildRes.stdout, /SUCCESS: Bundled plugin meets all Anthropic review limits/);

    const checkRes = spawnSync(process.execPath, [CHECK_SCRIPT], {
        cwd: ROOT,
        encoding: 'utf8',
    });
    assert.equal(checkRes.status, 0, checkRes.stdout + checkRes.stderr);
});
