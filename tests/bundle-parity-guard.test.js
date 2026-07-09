/**
 * Tests for the proprietary-gating parity guard (fix/sync-gateway-exclude-parity).
 *
 * Proves:
 *   1. list-blocked-gateway-paths.js is a faithful single-source-of-truth
 *      projection of package.json's "!gateway/..." negations.
 *   2. check-bundle-parity.sh PASSES on a clean bundle and FAILS when a
 *      package.json-blocked path is present (drift detection).
 *   3. The license_core.py transient-compile input is exempt.
 *
 * The guard tests build a throwaway npm-root fixture (own package.json + copies
 * of the two scripts) so they never depend on the real repo's committed state.
 */

const { describe, it } = require('node:test');
const assert = require('node:assert');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const REPO_ROOT = path.join(__dirname, '..');

function realBlockedFromPackageJson() {
  const pkg = JSON.parse(fs.readFileSync(path.join(REPO_ROOT, 'package.json'), 'utf8'));
  return (pkg.files || [])
    .filter((f) => typeof f === 'string' && f.startsWith('!gateway/'))
    .map((f) => f.slice(1));
}

describe('list-blocked-gateway-paths.js (single source of truth)', () => {
  it('emits exactly the package.json "!gateway/" negations', () => {
    const out = execFileSync('node', [path.join(REPO_ROOT, 'scripts', 'list-blocked-gateway-paths.js')], {
      encoding: 'utf8',
    });
    const emitted = out.split('\n').filter(Boolean);
    assert.deepStrictEqual(emitted, realBlockedFromPackageJson());
    assert.ok(emitted.length >= 20, 'expected the full blocked set, not a hand-trimmed subset');
  });

  it('does NOT emit include patterns (e.g. the license_core .so)', () => {
    const out = execFileSync('node', [path.join(REPO_ROOT, 'scripts', 'list-blocked-gateway-paths.js')], {
      encoding: 'utf8',
    });
    assert.ok(!out.includes('.so'), 'include patterns must not appear in the blocked list');
  });
});

describe('check-bundle-parity.sh (drift guard)', () => {
  function makeFixture(blockedList) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'parity-fix-'));
    fs.mkdirSync(path.join(dir, 'scripts'), { recursive: true });
    // Minimal package.json whose negations we control.
    const files = ['gateway/', ...blockedList.map((b) => '!' + b)];
    fs.writeFileSync(path.join(dir, 'package.json'), JSON.stringify({ name: 'fixture', files }, null, 2));
    // Copy the two scripts under test.
    for (const s of ['check-bundle-parity.sh', 'list-blocked-gateway-paths.js']) {
      fs.copyFileSync(path.join(REPO_ROOT, 'scripts', s), path.join(dir, 'scripts', s));
    }
    return dir;
  }

  function runGuard(dir, env = {}) {
    try {
      const out = execFileSync('bash', [path.join(dir, 'scripts', 'check-bundle-parity.sh')], {
        encoding: 'utf8',
        env: { ...process.env, ...env },
      });
      return { code: 0, out };
    } catch (e) {
      return { code: e.status, out: (e.stdout || '') + (e.stderr || '') };
    }
  }

  function writeFile(dir, rel) {
    const p = path.join(dir, rel);
    fs.mkdirSync(path.dirname(p), { recursive: true });
    fs.writeFileSync(p, '# proprietary\n');
  }

  it('PASSES when no blocked path is present', () => {
    const dir = makeFixture(['gateway/ai/social.py', 'gateway/ai/self_repair/']);
    writeFile(dir, 'gateway/ai/server.py'); // an allowed file
    const r = runGuard(dir);
    assert.strictEqual(r.code, 0, r.out);
    assert.match(r.out, /bundle parity/i);
  });

  it('FAILS when a blocked file is present (drift)', () => {
    const dir = makeFixture(['gateway/ai/social.py', 'gateway/ai/seal/producer.py']);
    writeFile(dir, 'gateway/ai/seal/producer.py'); // planted leak
    const r = runGuard(dir);
    assert.strictEqual(r.code, 1, 'guard must fail on a committed blocked file');
    assert.match(r.out, /PARITY VIOLATION/);
    assert.match(r.out, /seal\/producer\.py/);
  });

  it('FAILS when a blocked directory has any file under it', () => {
    const dir = makeFixture(['gateway/ai/self_repair/']);
    writeFile(dir, 'gateway/ai/self_repair/watcher.py'); // planted leak in dir
    const r = runGuard(dir);
    assert.strictEqual(r.code, 1);
    assert.match(r.out, /self_repair\//);
  });

  it('EXEMPTS license_core.py (transient compile input)', () => {
    const dir = makeFixture(['gateway/ai/license_core.py']);
    writeFile(dir, 'gateway/ai/license_core.py'); // present but exempt
    const r = runGuard(dir);
    assert.strictEqual(r.code, 0, r.out);
  });

  it('honours the emergency override', () => {
    const dir = makeFixture(['gateway/ai/social.py']);
    writeFile(dir, 'gateway/ai/social.py');
    const r = runGuard(dir, { DELIMIT_ALLOW_COMMITTED_LEAK: '1' });
    assert.strictEqual(r.code, 0, r.out);
    assert.match(r.out, /overriding/i);
  });
});
