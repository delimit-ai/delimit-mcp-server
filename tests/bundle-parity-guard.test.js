/**
 * Tests for the FAIL-CLOSED bundle guards (fix/bundle-fail-closed-allowlist).
 *
 * The bundle was inverted from a fail-OPEN blocklist (`gateway/` + a hand-
 * maintained list of `!` exclusions) to a fail-CLOSED allowlist
 * (bundle-allowlist.txt IS the security boundary). These tests prove the two
 * guards that enforce it:
 *
 *   Guard A — scripts/check-bundle-classification.sh
 *     Every gateway/ai file must be classified (allowlist OR internal-exclude).
 *     A NEW, unclassified file is a hard FAIL.
 *
 *   Guard B — scripts/check-bundle-parity.sh
 *     `npm pack` must ship EXACTLY the allowlist — any extra shipped gateway
 *     path (a leak) or any missing allowlisted path (a mis-exclusion) fails.
 *
 * Both guards are exercised against throwaway fixtures so they never depend on
 * the real repo's committed state.
 */

const { describe, it } = require('node:test');
const assert = require('node:assert');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const REPO_ROOT = path.join(__dirname, '..');

function writeFile(dir, rel, body = '# content\n') {
  const p = path.join(dir, rel);
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, body);
}

function runScript(dir, script, env = {}) {
  try {
    const out = execFileSync('bash', [path.join(dir, 'scripts', script)], {
      encoding: 'utf8',
      cwd: dir,
      env: { ...process.env, ...env },
    });
    return { code: 0, out };
  } catch (e) {
    return { code: e.status, out: (e.stdout || '') + (e.stderr || '') };
  }
}

// ── Guard A — classification ────────────────────────────────────────────────
describe('check-bundle-classification.sh (fail-closed classification)', () => {
  function makeFixture({ allow, exclude, aiFiles }) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'classify-fix-'));
    fs.mkdirSync(path.join(dir, 'scripts'), { recursive: true });
    fs.copyFileSync(
      path.join(REPO_ROOT, 'scripts', 'check-bundle-classification.sh'),
      path.join(dir, 'scripts', 'check-bundle-classification.sh')
    );
    fs.writeFileSync(
      path.join(dir, 'bundle-allowlist.txt'),
      '# allowlist\n' + allow.join('\n') + '\n'
    );
    fs.writeFileSync(
      path.join(dir, 'bundle-internal-exclude.txt'),
      '# exclude\n' + exclude.join('\n') + '\n'
    );
    // No gateway SOURCE tree — force committed-bundle mode via the fixture's
    // own gateway/ai. Point GATEWAY_OVERRIDE at a non-existent path so the
    // guard falls back to the fixture bundle.
    for (const f of aiFiles) writeFile(dir, f);
    return dir;
  }

  const env = { GATEWAY_OVERRIDE: '/nonexistent/gateway' };

  it('PASSES when every gateway/ai file is classified', () => {
    const dir = makeFixture({
      allow: ['gateway/ai/server.py'],
      exclude: ['gateway/ai/swarm.py'],
      aiFiles: ['gateway/ai/server.py', 'gateway/ai/swarm.py'],
    });
    const r = runScript(dir, 'check-bundle-classification.sh', env);
    assert.strictEqual(r.code, 0, r.out);
    assert.match(r.out, /every gateway\/ai file is classified/i);
  });

  it('FAILS on a NEW unclassified gateway/ai file', () => {
    const dir = makeFixture({
      allow: ['gateway/ai/server.py'],
      exclude: ['gateway/ai/swarm.py'],
      aiFiles: [
        'gateway/ai/server.py',
        'gateway/ai/swarm.py',
        'gateway/ai/brand_new_internal.py', // neither list
      ],
    });
    const r = runScript(dir, 'check-bundle-classification.sh', env);
    assert.strictEqual(r.code, 1, 'unclassified file must fail the guard');
    assert.match(r.out, /CLASSIFICATION FAILURE/);
    assert.match(r.out, /brand_new_internal\.py/);
  });

  it('ignores __pycache__, .pyc and .so artifacts', () => {
    const dir = makeFixture({
      allow: ['gateway/ai/server.py'],
      exclude: [],
      aiFiles: [
        'gateway/ai/server.py',
        'gateway/ai/__pycache__/server.cpython-310.pyc',
        'gateway/ai/license_core.cpython-310-x86_64-linux-gnu.so',
      ],
    });
    const r = runScript(dir, 'check-bundle-classification.sh', env);
    assert.strictEqual(r.code, 0, r.out);
  });
});

// ── Guard B — tarball parity against the allowlist ──────────────────────────
describe('check-bundle-parity.sh (fail-closed allowlist parity)', () => {
  function makeFixture({ files, allow, gatewayFiles }) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'parity-fix-'));
    fs.mkdirSync(path.join(dir, 'scripts'), { recursive: true });
    fs.copyFileSync(
      path.join(REPO_ROOT, 'scripts', 'check-bundle-parity.sh'),
      path.join(dir, 'scripts', 'check-bundle-parity.sh')
    );
    fs.writeFileSync(
      path.join(dir, 'package.json'),
      JSON.stringify({ name: 'fixture', version: '0.0.0', files }, null, 2)
    );
    fs.writeFileSync(
      path.join(dir, 'bundle-allowlist.txt'),
      '# allowlist\n' + allow.join('\n') + '\n'
    );
    for (const f of gatewayFiles) writeFile(dir, f);
    return dir;
  }

  it('PASSES when the pack equals the allowlist exactly', () => {
    const dir = makeFixture({
      files: ['gateway/ai/server.py', 'gateway/ai/license.py'],
      allow: ['gateway/ai/server.py', 'gateway/ai/license.py'],
      gatewayFiles: ['gateway/ai/server.py', 'gateway/ai/license.py'],
    });
    const r = runScript(dir, 'check-bundle-parity.sh');
    assert.strictEqual(r.code, 0, r.out);
    assert.match(r.out, /ships exactly the allowlist/i);
  });

  it('FAILS when the pack ships a path NOT in the allowlist (leak)', () => {
    const dir = makeFixture({
      // package.json ships an internal file the allowlist does not permit.
      files: ['gateway/ai/server.py', 'gateway/ai/swarm.py'],
      allow: ['gateway/ai/server.py'],
      gatewayFiles: ['gateway/ai/server.py', 'gateway/ai/swarm.py'],
    });
    const r = runScript(dir, 'check-bundle-parity.sh');
    assert.strictEqual(r.code, 1, 'a shipped non-allowlisted path must fail');
    assert.match(r.out, /PARITY VIOLATION/);
    assert.match(r.out, /swarm\.py/);
  });

  it('FAILS when an allowlisted path is MISSING from the pack', () => {
    const dir = makeFixture({
      files: ['gateway/ai/server.py'], // does not ship license.py
      allow: ['gateway/ai/server.py', 'gateway/ai/license.py'],
      gatewayFiles: ['gateway/ai/server.py', 'gateway/ai/license.py'],
    });
    const r = runScript(dir, 'check-bundle-parity.sh');
    assert.strictEqual(r.code, 1, 'a missing allowlisted path must fail');
    assert.match(r.out, /MISSING from npm pack/);
    assert.match(r.out, /license\.py/);
  });

  it('accepts the compiled license_core .so via the allowlist glob', () => {
    const dir = makeFixture({
      files: ['gateway/ai/server.py', 'gateway/ai/license_core.cpython-*-*.so'],
      allow: ['gateway/ai/server.py', 'gateway/ai/license_core.cpython-*-*.so'],
      gatewayFiles: [
        'gateway/ai/server.py',
        'gateway/ai/license_core.cpython-310-x86_64-linux-gnu.so',
      ],
    });
    const r = runScript(dir, 'check-bundle-parity.sh');
    assert.strictEqual(r.code, 0, r.out);
  });

  it('honours the emergency override', () => {
    const dir = makeFixture({
      files: ['gateway/ai/server.py', 'gateway/ai/swarm.py'],
      allow: ['gateway/ai/server.py'],
      gatewayFiles: ['gateway/ai/server.py', 'gateway/ai/swarm.py'],
    });
    const r = runScript(dir, 'check-bundle-parity.sh', {
      DELIMIT_ALLOW_COMMITTED_LEAK: '1',
    });
    assert.strictEqual(r.code, 0, r.out);
    assert.match(r.out, /overriding/i);
  });
});
