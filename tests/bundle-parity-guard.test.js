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
const FASTMCP_PIN = 'fastmcp==3.2.4';

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

describe('non-authoritative build artifacts', () => {
  it('does not ship the source-tree Pro checksum manifest', () => {
    const staleManifest = 'gateway/ai/checksums.sha256';
    const pkg = JSON.parse(
      fs.readFileSync(path.join(REPO_ROOT, 'package.json'), 'utf8')
    );
    const allowlist = fs.readFileSync(
      path.join(REPO_ROOT, 'bundle-allowlist.txt'),
      'utf8'
    );

    assert.ok(!pkg.files.includes(staleManifest));
    assert.ok(!allowlist.split(/\r?\n/).includes(staleManifest));
    assert.ok(!fs.existsSync(path.join(REPO_ROOT, staleManifest)));
  });

  function makeLicenseBuildFixture({
    failCompile = false,
    glibcVersion = '2.34',
    glibcSuffix = '',
    glibcAbi = '',
    readelfExit = 0,
    glibcSedExit = 0,
    glibcSortExit = 0,
  } = {}) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'license-build-fix-'));
    fs.mkdirSync(path.join(dir, 'scripts'), { recursive: true });
    fs.copyFileSync(
      path.join(REPO_ROOT, 'scripts', 'build-license-core.sh'),
      path.join(dir, 'scripts', 'build-license-core.sh')
    );
    writeFile(dir, 'gateway/ai/license_core.py', 'VALUE = 1\n');
    writeFile(
      dir,
      'gateway/ai/license_core.pyi',
      '# reviewed stub; generated output must never replace these bytes\nVALUE: int\n'
    );

    const fakePython = path.join(dir, 'fake-python');
    fs.writeFileSync(
      fakePython,
      `#!/bin/bash
set -eu
if [ "\${1:-}" = "-c" ]; then
  printf '3.10\\n'
  exit 0
fi
if [ "\${1:-}" = "-m" ] && [ "\${2:-}" = "nuitka" ] && [ "\${3:-}" = "--version" ]; then
  printf '2.5.9\\n'
  exit 0
fi
if [ "\${1:-}" = "-m" ] && [ "\${2:-}" = "nuitka" ]; then
  printf '# unreviewed generated stub\\n' > license_core.pyi
  mkdir -p license_core.build
  printf 'generated intermediate\\n' > license_core.c
  printf 'compiled test artifact\\n' > license_core.cpython-310-x86_64-linux-gnu.so
  exit ${failCompile ? 7 : 0}
fi
exit 2
`
    );
    fs.chmodSync(fakePython, 0o755);

    const fakeReadelf = path.join(dir, 'readelf');
    fs.writeFileSync(
      fakeReadelf,
      `#!/bin/bash
set -eu
if [ "\${1:-}" = "--version-info" ]; then
  printf 'Version needs section: Name: GLIBC_${glibcVersion}${glibcSuffix} Flags: none Version: 1\\n'
  if [ -n '${glibcAbi}' ]; then
    printf 'Version needs section: Name: GLIBC_ABI_${glibcAbi} Flags: none Version: 2\\n'
  fi
  exit ${readelfExit}
fi
exit 2
`
    );
    fs.chmodSync(fakeReadelf, 0o755);

    const fakeSed = path.join(dir, 'sed');
    fs.writeFileSync(
      fakeSed,
      `#!/bin/bash
set -eu
if [ '${glibcSedExit}' -ne 0 ]; then
  cat >/dev/null
  printf 'GLIBC_2.34\\n'
  exit ${glibcSedExit}
fi
exec /usr/bin/sed "$@"
`
    );
    fs.chmodSync(fakeSed, 0o755);

    const fakeSort = path.join(dir, 'sort');
    fs.writeFileSync(
      fakeSort,
      `#!/bin/bash
set -eu
if [ "\${1:-}" = "-Vu" ] && [ '${glibcSortExit}' -ne 0 ]; then
  exit ${glibcSortExit}
fi
exec /usr/bin/sort "$@"
`
    );
    fs.chmodSync(fakeSort, 0o755);
    return {
      dir,
      fakePython,
      env: { PYTHON: fakePython, PATH: `${dir}:${process.env.PATH}` },
    };
  }

  it('restores the reviewed license stub and removes compiler intermediates', () => {
    const { dir, env } = makeLicenseBuildFixture();
    const stub = path.join(dir, 'gateway', 'ai', 'license_core.pyi');
    const reviewed = fs.readFileSync(stub);

    const r = runScript(dir, 'build-license-core.sh', env);
    assert.strictEqual(r.code, 0, r.out);
    assert.deepStrictEqual(fs.readFileSync(stub), reviewed);
    assert.ok(!fs.existsSync(path.join(dir, 'gateway', 'ai', 'license_core.py')));
    assert.ok(!fs.existsSync(path.join(dir, 'gateway', 'ai', 'license_core.build')));
    assert.ok(!fs.existsSync(path.join(dir, 'gateway', 'ai', 'license_core.c')));
    assert.ok(
      fs.existsSync(
        path.join(
          dir,
          'gateway',
          'ai',
          'license_core.cpython-310-x86_64-linux-gnu.so'
        )
      )
    );
    assert.match(r.out, /restored reviewed license_core\.pyi byte-for-byte/i);
    assert.match(r.out, /GLIBC requirement 2\.34 <= 2\.35/i);
  });

  it('restores the reviewed stub and removes binary output after compile failure', () => {
    const { dir, env } = makeLicenseBuildFixture({ failCompile: true });
    const stub = path.join(dir, 'gateway', 'ai', 'license_core.pyi');
    const reviewed = fs.readFileSync(stub);

    const r = runScript(dir, 'build-license-core.sh', env);
    assert.notStrictEqual(r.code, 0, 'a compiler failure must fail the build');
    assert.deepStrictEqual(fs.readFileSync(stub), reviewed);
    assert.ok(fs.existsSync(path.join(dir, 'gateway', 'ai', 'license_core.py')));
    assert.ok(!fs.existsSync(path.join(dir, 'gateway', 'ai', 'license_core.build')));
    assert.ok(!fs.existsSync(path.join(dir, 'gateway', 'ai', 'license_core.c')));
    assert.ok(
      !fs.existsSync(
        path.join(
          dir,
          'gateway',
          'ai',
          'license_core.cpython-310-x86_64-linux-gnu.so'
        )
      )
    );
  });

  it('fails closed and removes binary output that requires newer glibc', () => {
    const { dir, env } = makeLicenseBuildFixture({ glibcVersion: '2.38' });
    const stub = path.join(dir, 'gateway', 'ai', 'license_core.pyi');
    const reviewed = fs.readFileSync(stub);

    const r = runScript(dir, 'build-license-core.sh', env);
    assert.notStrictEqual(
      r.code,
      0,
      'a too-new GLIBC requirement must fail the build'
    );
    assert.match(
      r.out,
      /requires GLIBC_2\.38; maximum supported is GLIBC_2\.35/i
    );
    assert.deepStrictEqual(fs.readFileSync(stub), reviewed);
    assert.ok(fs.existsSync(path.join(dir, 'gateway', 'ai', 'license_core.py')));
    assert.ok(
      !fs.existsSync(
        path.join(
          dir,
          'gateway',
          'ai',
          'license_core.cpython-310-x86_64-linux-gnu.so'
        )
      )
    );
  });

  it('fails closed when readelf returns partial output and a non-zero status', () => {
    const { dir, env } = makeLicenseBuildFixture({ readelfExit: 7 });

    const r = runScript(dir, 'build-license-core.sh', env);
    assert.notStrictEqual(
      r.code,
      0,
      'partial parseable output must not mask a readelf failure'
    );
    assert.match(r.out, /readelf could not inspect/i);
    assert.ok(fs.existsSync(path.join(dir, 'gateway', 'ai', 'license_core.py')));
    assert.ok(
      !fs.existsSync(
        path.join(
          dir,
          'gateway',
          'ai',
          'license_core.cpython-310-x86_64-linux-gnu.so'
        )
      )
    );
  });

  it('rejects non-numeric GLIBC ABI requirements unsupported by Ubuntu 22.04', () => {
    const { dir, env } = makeLicenseBuildFixture({ glibcAbi: 'DT_RELR' });

    const r = runScript(dir, 'build-license-core.sh', env);
    assert.notStrictEqual(
      r.code,
      0,
      'GLIBC_ABI_DT_RELR must not pass a numeric-only ceiling check'
    );
    assert.match(r.out, /unsupported GLIBC version tag/i);
    assert.match(r.out, /GLIBC_ABI_DT_RELR/);
    assert.ok(fs.existsSync(path.join(dir, 'gateway', 'ai', 'license_core.py')));
    assert.ok(
      !fs.existsSync(
        path.join(
          dir,
          'gateway',
          'ai',
          'license_core.cpython-310-x86_64-linux-gnu.so'
        )
      )
    );
  });

  it('rejects a non-numeric suffix after an otherwise-compatible GLIBC version', () => {
    const { dir, env } = makeLicenseBuildFixture({ glibcSuffix: '+CUSTOM' });

    const r = runScript(dir, 'build-license-core.sh', env);
    assert.notStrictEqual(
      r.code,
      0,
      'a complete non-numeric GLIBC tag must not be truncated to a numeric prefix'
    );
    assert.match(r.out, /unsupported GLIBC version tag/i);
    assert.match(r.out, /GLIBC_2\.34\+CUSTOM/);
    assert.ok(fs.existsSync(path.join(dir, 'gateway', 'ai', 'license_core.py')));
    assert.ok(
      !fs.existsSync(
        path.join(
          dir,
          'gateway',
          'ai',
          'license_core.cpython-310-x86_64-linux-gnu.so'
        )
      )
    );
  });

  it('fails closed when GLIBC requirement sorting fails', () => {
    const { dir, env } = makeLicenseBuildFixture({ glibcSortExit: 7 });

    const r = runScript(dir, 'build-license-core.sh', env);
    assert.notStrictEqual(
      r.code,
      0,
      'a GLIBC parser failure must not be treated as an empty requirement set'
    );
    assert.match(r.out, /could not sort GLIBC requirements/i);
    assert.ok(fs.existsSync(path.join(dir, 'gateway', 'ai', 'license_core.py')));
    assert.ok(
      !fs.existsSync(
        path.join(
          dir,
          'gateway',
          'ai',
          'license_core.cpython-310-x86_64-linux-gnu.so'
        )
      )
    );
  });

  it('fails closed when extraction emits partial output and exits one', () => {
    const { dir, env } = makeLicenseBuildFixture({ glibcSedExit: 1 });

    const r = runScript(dir, 'build-license-core.sh', env);
    assert.notStrictEqual(
      r.code,
      0,
      'partial parser output must not be mistaken for a valid GLIBC requirement'
    );
    assert.match(r.out, /could not parse GLIBC requirements/i);
    assert.ok(fs.existsSync(path.join(dir, 'gateway', 'ai', 'license_core.py')));
    assert.ok(
      !fs.existsSync(
        path.join(
          dir,
          'gateway',
          'ai',
          'license_core.cpython-310-x86_64-linux-gnu.so'
        )
      )
    );
  });
});

describe('native release runner compatibility', () => {
  it('pins every native build job to Ubuntu 22.04', () => {
    const workflow = fs.readFileSync(
      path.join(REPO_ROOT, '.github', 'workflows', 'publish.yml'),
      'utf8'
    );
    const nativeBuildJobs = ['validate', 'publish'];

    for (const [index, job] of nativeBuildJobs.entries()) {
      const start = workflow.indexOf(`  ${job}:`);
      const nextJob = nativeBuildJobs[index + 1];
      const end = nextJob
        ? workflow.indexOf(`  ${nextJob}:`, start + 1)
        : workflow.indexOf('  release:', start + 1);
      const body = workflow.slice(start, end);
      assert.ok(start >= 0 && end > start, `workflow job ${job} must exist`);
      assert.match(
        body,
        /runs-on: ubuntu-22\.04/,
        `${job} must build native artifacts on Ubuntu 22.04`
      );
      assert.doesNotMatch(
        body,
        /runs-on: ubuntu-latest/,
        `${job} must not use a floating Linux ABI`
      );
    }
  });
});

describe('FastMCP fresh-install security parity (LED-4530)', () => {
  it('pins the patched runtime in the shipped requirements', () => {
    const requirements = fs.readFileSync(
      path.join(REPO_ROOT, 'gateway', 'requirements.txt'),
      'utf8'
    );
    assert.ok(requirements.split(/\r?\n/).includes(FASTMCP_PIN));
    assert.doesNotMatch(requirements, /fastmcp==3\.1\.0/);
  });

  it('uses the identical pin in every setup and container fallback', () => {
    const dockerfile = fs.readFileSync(path.join(REPO_ROOT, 'Dockerfile'), 'utf8');
    const setup = fs.readFileSync(
      path.join(REPO_ROOT, 'bin', 'delimit-setup.js'),
      'utf8'
    );

    assert.ok(dockerfile.includes(FASTMCP_PIN));
    assert.strictEqual(
      (setup.match(/fastmcp==3\.2\.4/g) || []).length,
      3,
      'venv, global, and manual recovery paths must share the patched pin'
    );
    assert.doesNotMatch(dockerfile, /fastmcp==3\.1\.0/);
    assert.doesNotMatch(setup, /fastmcp==3\.1\.0/);
  });
});
