/**
 * LED-5478: every version source in this repo must agree.
 *
 * WHY THIS EXISTS
 * ---------------
 * `npm version` moves package.json and package-lock.json. It does NOT move
 * gateway/VERSION (regenerated from package.json by scripts/sync-gateway.sh at
 * pack time) and it does NOT move server.json (the MCP Registry record, which
 * carries the version TWICE). Nothing kept the other two in lockstep, and the
 * publish workflow only discovers the drift after a tag has been pushed — at
 * which point the tag is burned, because moving a pushed tag is forbidden.
 *
 * That cost three tags in one afternoon:
 *   v4.19.3  failed to publish
 *   v4.19.4  failed: "Committed bundle is STALE" — gateway/VERSION still 4.19.3
 *   v4.19.5  failed: "server.json versions (4.19.2 / 4.19.2) do not match tag"
 *
 * The v4.19.5 failure is the instructive one: after v4.19.4 I fixed the single
 * source CI had complained about instead of auditing every source, so the next
 * stale one simply failed one step later. This test enumerates ALL of them, so
 * the drift surfaces locally — on `npm test`, before a tag exists — instead of
 * in the publish job.
 *
 * These tests DETECT drift. They do not prevent it — prevention is
 * scripts/sync-version-sources.js, wired to the npm `version` lifecycle, which
 * writes every source from package.json and stages them into the same commit
 * as the bump. Review of PR #230 twice made the same point: detection that
 * enumerates sources cannot cover one it does not know about, so the
 * enumeration is a backstop and the hook is the fix.
 *
 * These remain useful for a tree that drifted some other way — a hand-edited
 * version, a bad merge, a cherry-pick — where no `npm version` ever ran.
 *
 * If you add a new file carrying the package version, add it to BOTH
 * collectSources() here and scripts/sync-version-sources.js.
 */

const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');

const readJson = (rel) => JSON.parse(fs.readFileSync(path.join(ROOT, rel), 'utf8'));

function collectSources() {
  const pkg = readJson('package.json');
  const lock = readJson('package-lock.json');
  const srv = readJson('server.json');
  const gatewayVersion = fs
    .readFileSync(path.join(ROOT, 'gateway', 'VERSION'), 'utf8')
    .trim();

  return [
    { label: 'package.json .version', value: pkg.version },
    { label: 'package-lock.json .version', value: lock.version },
    { label: 'package-lock.json .packages[""].version', value: lock.packages[''].version },
    { label: 'gateway/VERSION', value: gatewayVersion },
    { label: 'gateway/ai/server.py _VERSION_FALLBACK', value: fs.readFileSync(path.join(ROOT, 'gateway/ai/server.py'), 'utf8').match(/^_VERSION_FALLBACK = ["']([^"']+)["']$/m)?.[1] },
    { label: 'server.json .version', value: srv.version },
    { label: 'server.json .packages[0].version', value: srv.packages[0].version },
  ];
}

test('every version source agrees with package.json', () => {
  const sources = collectSources();
  const expected = sources[0].value;

  assert.match(
    expected,
    /^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$/,
    `package.json version is not a plain semver string: ${expected}`,
  );

  const drifted = sources.filter((s) => s.value !== expected);
  assert.deepStrictEqual(
    drifted,
    [],
    'version sources disagree — a tag cut now would fail in the publish job ' +
      `and burn the version.\n  expected (package.json): ${expected}\n` +
      sources.map((s) => `  ${s.value === expected ? 'ok  ' : 'DRIFT'} ${s.label} = ${s.value}`).join('\n'),
  );
});

/**
 * A PARTIAL cross-check against publish.yml. Read the scope limit before
 * trusting it — an earlier version of this comment overclaimed and was
 * blocked in review of PR #230 for exactly that.
 *
 * WHAT IT COVERS: version gates written in publish.yml as inline
 * `require('./x.json')...version` expressions. If someone adds another one,
 * this fails locally and names it, instead of the drift surfacing after a tag
 * is pushed and spent.
 *
 * WHAT IT DOES NOT COVER, stated plainly because the gap is the whole point:
 *   - gateway/VERSION. Its gate is the "Assert committed bundle is in sync
 *     with gateway" step, a bundle-sync assertion, NOT a require() expression.
 *     This check is BLIND to it — so it would NOT have caught the v4.19.4
 *     failure. That one is covered by the hardcoded list above, and prevented
 *     by scripts/sync-version-sources.js.
 *   - any gate that moves into a called script, uses different quoting, reads
 *     via a variable, or lives in another job.
 *
 * So this is a supplement, not proof of completeness. Completeness is not
 * claimed anywhere in this file. Drift is PREVENTED on the bump path by
 * scripts/sync-version-sources.js (the npm `version` lifecycle hook); the
 * tests here are the backstop for a tree that drifted some other way.
 */
test('require()-style version gates in publish.yml are covered here (partial check)', () => {
  const wf = fs.readFileSync(
    path.join(ROOT, '.github', 'workflows', 'publish.yml'),
    'utf8',
  );

  // Every `require('./<file>')<path>.version` the workflow evaluates.
  const found = [...wf.matchAll(/require\(\s*'\.\/([^']+)'\s*\)((?:\.[A-Za-z_$][\w$]*|\[\s*\d+\s*\])*)/g)]
    .map(([, file, accessor]) => `${file} ${accessor || '(root)'}`)
    .filter((expr) => expr.includes('version'));

  assert.ok(
    found.length > 0,
    'extracted no version expressions from publish.yml — the regex has drifted ' +
      'from the workflow and this check is now vacuous, which is worse than absent',
  );

  const covered = new Set(collectSources().map((s) => s.label));
  const uncovered = found.filter((expr) => !covered.has(expr));

  assert.deepStrictEqual(
    uncovered,
    [],
    'publish.yml gates on a version source this test does not check.\n' +
      'That is the exact gap that burned v4.19.5: the workflow knew about\n' +
      'server.json and the local checks did not.\n' +
      `  gated by publish.yml : ${JSON.stringify(found)}\n` +
      `  covered here         : ${JSON.stringify([...covered])}\n` +
      'Add the missing source to collectSources().',
  );
});

test('the version lifecycle hook stays wired', () => {
  // Prevention only works if npm actually runs it. Silently dropping
  // scripts.version would restore the exact conditions that burned three tags,
  // while every other test in this file still passed on an already-aligned
  // tree. Pin the wiring, and pin that the script it names exists.
  const pkg = readJson('package.json');
  const hook = pkg.scripts && pkg.scripts.version;
  assert.ok(
    typeof hook === 'string' && hook.includes('sync-version-sources'),
    'package.json scripts.version must run scripts/sync-version-sources.js so ' +
      '`npm version` cannot leave gateway/VERSION or server.json behind ' +
      `(got ${JSON.stringify(hook)})`,
  );
  assert.ok(
    fs.existsSync(path.join(ROOT, 'scripts', 'sync-version-sources.js')),
    'scripts/sync-version-sources.js is referenced by the version lifecycle but missing',
  );
});

test('no version source is empty or undefined', () => {
  // A missing field reads as undefined and would make every comparison above
  // pass vacuously if the expected value were also undefined. Pin presence
  // separately rather than trusting the equality check to imply it.
  for (const { label, value } of collectSources()) {
    assert.ok(
      typeof value === 'string' && value.trim().length > 0,
      `${label} is missing or empty (got ${JSON.stringify(value)})`,
    );
  }
});
