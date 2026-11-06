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
    const pluginDir = path.join(ROOT, 'claude-plugin');
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

test('check-claude-plugin-limits catches non-shell scripts in subfolders', () => {
    const hooksDir = path.join(ROOT, 'claude-plugin', 'hooks');
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
