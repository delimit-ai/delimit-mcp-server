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
 * Deliberately a TEST, not a version lifecycle hook: it ships nothing, changes
 * no release mechanism, and cannot itself write a wrong version. The lifecycle
 * hook that would keep them in sync automatically remains LED-5478.
 *
 * If you add a new file carrying the package version, add it here too.
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
 * The completeness problem, and why the list above is not enough on its own.
 *
 * Blocked in review of PR #230: a hardcoded enumeration cannot detect a source
 * it does not enumerate, and "I found them all" is exactly the belief that was
 * already wrong twice. Reproducing the two KNOWN failures demonstrates nothing
 * about unknown ones.
 *
 * So this test does not trust the list. It reads the publish workflow — the
 * thing that actually burns a tag when it disagrees — extracts every version
 * expression that its gate evaluates, and asserts each one is covered above.
 * If someone adds a new version check to publish.yml, this fails and names it,
 * instead of the drift being discovered after a tag is pushed and spent.
 *
 * The authority is the pipeline, not the author's memory of it.
 */
test('every version source the publish workflow gates on is covered here', () => {
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
