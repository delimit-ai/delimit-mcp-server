'use strict';

// Tests for scripts/claude-plugin-release-guard.js (Claude plugin release
// lane, docs/claude-plugin-release.md). Pure fixtures, the repo's own files,
// and git-backed cases in throwaway repositories. No network: the npm lookup
// is injected, and the git-backed runs use --offline.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const yaml = require('js-yaml');

const guard = require('../scripts/claude-plugin-release-guard.js');

const ROOT = path.join(__dirname, '..');
const SCRIPT = path.join(ROOT, 'scripts', 'claude-plugin-release-guard.js');
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), 'utf8');

const TABLE_HEAD = '| Plugin | Version | Source commit | Tag | Pin | Scan | Published at | Acceptance |\n|---|---|---|---|---|---|---|---|\n';
const row = ([plugin, v]) => `| ${plugin} | ${v} | \`abc1234\` | \`${plugin}--v${v}\` | 4.20.1 | passed | Pending | Pending |\n`;
const releases = (...pairs) => `# Releases\n\n${TABLE_HEAD}${pairs.map((p) => row(Array.isArray(p) ? p : ['delimit', p])).join('')}`;
const pluginJson = (version, name = 'delimit') => JSON.stringify({ name, version });
const PANEL = { name: 'delimit-panel', source: './claude-plugin-panel', description: 'panel' };
const DELIMIT = { name: 'delimit', source: './claude-plugin', description: 'records' };
const marketJson = (entries = [DELIMIT], name = 'delimit') => JSON.stringify({ name, owner: { name: 'Delimit' }, plugins: entries });
const mcpJson = (spec = 'delimit-cli@4.20.1', server = 'delimit') =>
    JSON.stringify({ mcpServers: { [server]: { command: 'npx', args: ['-y', spec, 'mcp', '--toolset', 'records'] } } });

function side({ market = marketJson(), rel = releases('1.0.3', '1.0.4'), delimit = '1.0.4', mcp = mcpJson(), panel = null, panelMcp = mcpJson('delimit-cli@4.21.0', 'delimit-panel') } = {}) {
    const files = {
        'claude-plugin/.claude-plugin/plugin.json': delimit === null ? null : pluginJson(delimit),
        'claude-plugin/.mcp.json': mcp,
    };
    if (panel !== null) {
        files['claude-plugin-panel/.claude-plugin/plugin.json'] = pluginJson(panel, 'delimit-panel');
        files['claude-plugin-panel/.mcp.json'] = panelMcp;
    }
    return { marketplace: market, releases: rel, files };
}
const BASE = side();
const npmYes = () => true;
const npmNo = () => false;
const has = (res, re) => res.errors.some((e) => re.test(e));
const show = (res) => res.errors.join('\n');

test('semver parsing and comparison', () => {
    assert.deepEqual(guard.parseSemver('1.0.10'), [1, 0, 10]);
    for (const bad of ['1.0', 'v1.0.0', '1.0.0-beta', '^1.0.0', '01.0.0', '', undefined]) assert.equal(guard.parseSemver(bad), null, String(bad));
    assert.equal(guard.compareSemver('1.0.10', '1.0.9'), 1);
    assert.equal(guard.compareSemver('1.0.4', '1.0.4'), 0);
    assert.equal(guard.compareSemver('1.9.0', '2.0.0'), -1);
});

test('provenance rows carry the plugin name in the first cell and X.Y.Z in the second', () => {
    const rows = guard.parseReleases(releases('1.0.0', ['delimit-panel', '1.0.0'], '1.0.4'));
    assert.deepEqual(rows.map((r) => `${r.plugin} ${r.version}`), ['delimit 1.0.0', 'delimit-panel 1.0.0', 'delimit 1.0.4']);
    assert.deepEqual(guard.parseReleases('| `delimit` | `2.1.0` | x |').map((r) => r.version), ['2.1.0']);
    assert.deepEqual(guard.parseReleases('| 1.0.4 | x |'), [], 'a row without a plugin name is not a release row');
});

test('folders match by exact prefix plus a slash; sources must be relative folders', () => {
    assert.ok(guard.inFolder('claude-plugin/README.md', 'claude-plugin'));
    assert.ok(!guard.inFolder('claude-plugin-panel/README.md', 'claude-plugin'));
    assert.ok(!guard.inFolder('claude-plugin', 'claude-plugin'));
    assert.equal(guard.normalizeSource('./claude-plugin'), 'claude-plugin');
    assert.equal(guard.normalizeSource('./a/b'), 'a/b');
    for (const bad of ['.', './', '/abs', '../up', 'a/../b', './a/../b', 'https://x', { source: 'github' }, 7]) assert.equal(guard.normalizeSource(bad), null, JSON.stringify(bad));
    assert.ok(!guard.PROVENANCE.startsWith('claude-plugin'), 'provenance must live outside every plugin folder');
});

test('npx package specs are read from every server', () => {
    const specs = guard.npxSpecs(JSON.parse(mcpJson('@scope/pkg@1.2.3', 'x')));
    assert.deepEqual(specs, [{ server: 'x', spec: '@scope/pkg@1.2.3' }]);
    assert.deepEqual(guard.splitSpec('@scope/pkg@1.2.3'), { pkg: '@scope/pkg', version: '1.2.3' });
    assert.deepEqual(guard.splitSpec('delimit-cli'), { pkg: 'delimit-cli', version: null });
});

test('the repository as committed passes the offline invariants', () => {
    const r = spawnSync(process.execPath, [SCRIPT], { encoding: 'utf8' });
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.match(r.stdout, /delimit 1\.0\.4 \(claude-plugin\/\)/);
    assert.match(r.stdout, /PASS/);
    // Every listed plugin in the committed marketplace has a provenance row.
    const market = JSON.parse(read(guard.MARKETPLACE));
    const rows = guard.parseReleases(read(guard.PROVENANCE));
    for (const p of market.plugins) {
        const v = JSON.parse(read(`${guard.normalizeSource(p.source)}/.claude-plugin/plugin.json`)).version;
        assert.ok(rows.some((r) => r.plugin === p.name && r.version === v), `${p.name} ${v}`);
    }
});

test('a change outside every plugin folder needs no bump and no npm lookup', () => {
    let called = false;
    const res = guard.evaluate({ changedFiles: ['lib/cli.js', 'docs/claude-plugin-releases.md', 'claude-plugin-notes.md', 'docs/.gitattributes'], head: side(), base: BASE, npmVersionExists: () => { called = true; return false; } });
    assert.deepEqual(res.errors, []);
    assert.equal(called, false);
});

test('(a) a plugin change without a version bump fails', () => {
    const res = guard.evaluate({ changedFiles: ['claude-plugin/README.md'], head: side(), base: BASE, npmVersionExists: npmYes });
    assert.ok(has(res, /plugin delimit changed .* not greater than the base version 1\.0\.4/), show(res));
});

test('(a) a version already recorded at the base cannot be reused', () => {
    const base = side({ rel: releases('1.0.4', '1.0.5') });
    const head = side({ delimit: '1.0.5', rel: releases('1.0.4', '1.0.5') });
    const res = guard.evaluate({ changedFiles: ['claude-plugin/.claude-plugin/plugin.json'], head, base, npmVersionExists: npmYes });
    assert.ok(has(res, /latest recorded release 1\.0\.5/), show(res));
});

test('(b) a bumped version without a provenance row fails', () => {
    const head = side({ delimit: '1.0.5' });
    const res = guard.evaluate({ changedFiles: ['claude-plugin/.claude-plugin/plugin.json'], head, base: BASE, npmVersionExists: npmYes });
    assert.ok(has(res, /no row for plugin delimit version 1\.0\.5/), show(res));
});

test('a bump with a row and an existing pin passes', () => {
    const head = side({ delimit: '1.0.5', rel: releases('1.0.3', '1.0.4', '1.0.5') });
    let asked = null;
    const res = guard.evaluate({
        changedFiles: ['claude-plugin/skills/record/SKILL.md', 'claude-plugin/.claude-plugin/plugin.json', 'docs/claude-plugin-releases.md'],
        head, base: BASE, npmVersionExists: (pkg, v) => { asked = `${pkg}@${v}`; return true; },
    });
    assert.deepEqual(res.errors, []);
    assert.equal(asked, 'delimit-cli@4.20.1');
});

test('(c) a pin that is not on npm fails, and a lookup error fails closed', () => {
    const head = side({ delimit: '1.0.5', rel: releases('1.0.3', '1.0.4', '1.0.5'), mcp: mcpJson('delimit-cli@9.9.9') });
    const args = { changedFiles: ['claude-plugin/.mcp.json'], head, base: BASE };
    assert.ok(has(guard.evaluate({ ...args, npmVersionExists: npmNo }), /delimit-cli@9\.9\.9 does not exist on npm/));
    const thrown = guard.evaluate({ ...args, npmVersionExists: () => { throw new Error('ETIMEDOUT'); } });
    assert.ok(has(thrown, /lookup .* failed: ETIMEDOUT/), show(thrown));
    const offline = guard.evaluate({ ...args, npmVersionExists: null });
    assert.deepEqual(offline.errors, []);
    assert.ok(offline.warnings.some((w) => /--offline/.test(w)));
});

test('(c) every npx pin must be one exact X.Y.Z version', () => {
    for (const spec of ['delimit-cli@^4.20.1', 'delimit-cli@latest', 'delimit-cli']) {
        const res = guard.evaluate({ head: side({ mcp: mcpJson(spec) }) });
        assert.ok(has(res, /\.mcp\.json/), `${spec}: ${show(res)}`);
    }
});

test('(d) manifests must be valid JSON with matching names, and the listed plugin stays at ./claude-plugin', () => {
    const cases = [
        [{ market: 'not json' }, /marketplace\.json \(head\): invalid JSON/],
        [{ market: marketJson([DELIMIT], 'other') }, /marketplace\.json: name must be "delimit"/],
        [{ market: marketJson([{ ...DELIMIT, source: './elsewhere' }]) }, /source must be \.\/claude-plugin/],
        [{ market: marketJson([PANEL]) }, /no plugin entry named "delimit"/],
        [{ market: marketJson([DELIMIT, { ...PANEL, source: '../outside' }]) }, /not exactly "\.\/<folder>"/],
        [{ market: marketJson([DELIMIT, { ...PANEL, source: './claude-plugin' }]) }, /listed twice/],
        [{ delimit: null }, /plugin\.json: missing/],
        [{ mcp: null }, /\.mcp\.json: missing/],
    ];
    for (const [over, re] of cases) {
        const res = guard.evaluate({ head: side(over) });
        assert.ok(has(res, re), `${re}: ${show(res)}`);
    }
    const badName = guard.evaluate({ head: { ...side(), files: { ...side().files, 'claude-plugin/.claude-plugin/plugin.json': pluginJson('1.0.4', 'other') } } });
    assert.ok(has(badName, /name must be "delimit" \(its marketplace entry\)/), show(badName));
    const badVersion = guard.evaluate({ head: side({ delimit: '1.0' }) });
    assert.ok(has(badVersion, /version must be X\.Y\.Z/), show(badVersion));
});

test('provenance rows strictly increase per plugin and are append-only', () => {
    const dup = guard.evaluate({ head: side({ rel: releases('1.0.4', '1.0.4') }) });
    assert.ok(has(dup, /delimit versions must strictly increase/));
    const down = guard.evaluate({ head: side({ rel: releases('1.0.4', '1.0.3') }) });
    assert.ok(has(down, /strictly increase/));
    const interleaved = guard.evaluate({ head: side({ rel: releases('1.0.3', ['delimit-panel', '1.0.0'], '1.0.4') }) });
    assert.deepEqual(interleaved.errors, [], 'rows of different plugins do not have to be ordered against each other');
    const removed = guard.evaluate({ changedFiles: ['docs/claude-plugin-releases.md'], head: side({ rel: releases('1.0.4') }), base: BASE });
    assert.ok(has(removed, /row delimit 1\.0\.3 was removed/), show(removed));
    const missing = guard.evaluate({ head: side({ rel: null }) });
    assert.ok(has(missing, /claude-plugin-releases\.md: missing/));
});

// ---- sibling plugins (open PR #255 adds claude-plugin-governance/ and claude-plugin-panel/) ----

const SUITE_REL = releases('1.0.3', '1.0.4', ['delimit-panel', '1.0.0']);
const SUITE_BASE = side({ market: marketJson([DELIMIT, PANEL]), rel: SUITE_REL, panel: '1.0.0' });

test('a sibling folder claude-plugin-panel/ is guarded as its own plugin, not as claude-plugin/', () => {
    const head = side({ market: marketJson([DELIMIT, PANEL]), rel: SUITE_REL, panel: '1.0.0', panelMcp: mcpJson('delimit-cli@0.0.0-nope', 'delimit-panel') });
    const res = guard.evaluate({ changedFiles: ['claude-plugin-panel/.mcp.json'], head, base: SUITE_BASE, npmVersionExists: npmYes });
    assert.ok(has(res, /claude-plugin-panel\/\.mcp\.json: server "delimit-panel" must pin delimit-cli to an exact/), show(res));
    assert.ok(has(res, /plugin delimit-panel changed .* not greater than the base version 1\.0\.0/), show(res));
    assert.ok(!has(res, /plugin delimit changed/), 'the delimit plugin is not touched by a sibling change');
});

test('a sibling bump needs its own provenance row, and a missing npm pin fails for it', () => {
    const head = side({ market: marketJson([DELIMIT, PANEL]), rel: SUITE_REL, panel: '1.0.1', panelMcp: mcpJson('delimit-cli@4.99.99', 'delimit-panel') });
    const res = guard.evaluate({ changedFiles: ['claude-plugin-panel/skills/x/SKILL.md', 'claude-plugin-panel/.claude-plugin/plugin.json'], head, base: SUITE_BASE, npmVersionExists: (p, v) => v !== '4.99.99' });
    assert.ok(has(res, /no row for plugin delimit-panel version 1\.0\.1/), show(res));
    assert.ok(has(res, /claude-plugin-panel\/\.mcp\.json: delimit-cli@4\.99\.99 does not exist on npm/), show(res));
});

test('a marketplace edit that only concerns a sibling requires only the sibling bump', () => {
    const market = marketJson([DELIMIT, { ...PANEL, description: 'panel, reworded' }]);
    const noBump = guard.evaluate({ changedFiles: ['.claude-plugin/marketplace.json'], head: side({ market, rel: SUITE_REL, panel: '1.0.0' }), base: SUITE_BASE, npmVersionExists: npmYes });
    assert.ok(has(noBump, /plugin delimit-panel changed \(\.claude-plugin\/marketplace\.json \(entry "delimit-panel"\)\)/), show(noBump));
    assert.ok(!has(noBump, /plugin delimit changed/), show(noBump));
    const bumped = guard.evaluate({
        changedFiles: ['.claude-plugin/marketplace.json', 'claude-plugin-panel/.claude-plugin/plugin.json', 'docs/claude-plugin-releases.md'],
        head: side({ market, rel: releases('1.0.3', '1.0.4', ['delimit-panel', '1.0.0'], ['delimit-panel', '1.0.1']), panel: '1.0.1' }),
        base: SUITE_BASE, npmVersionExists: npmYes,
    });
    assert.deepEqual(bumped.errors, []);
});

test('a new sibling entry needs a provenance row; reordering entries changes nothing', () => {
    const added = guard.evaluate({ changedFiles: ['.claude-plugin/marketplace.json', 'claude-plugin-panel/.claude-plugin/plugin.json', 'claude-plugin-panel/.mcp.json'], head: side({ market: marketJson([DELIMIT, PANEL]), panel: '1.0.0' }), base: BASE, npmVersionExists: npmYes });
    assert.ok(has(added, /no row for plugin delimit-panel version 1\.0\.0/), show(added));
    assert.ok(!has(added, /plugin delimit changed/), show(added));
    const reordered = guard.evaluate({ changedFiles: ['.claude-plugin/marketplace.json'], head: side({ market: marketJson([PANEL, DELIMIT]), rel: SUITE_REL, panel: '1.0.0' }), base: SUITE_BASE, npmVersionExists: npmYes });
    assert.deepEqual(reordered.errors, []);
});

test('marketplace-wide edits touch every plugin; removing or renaming a listed plugin fails', () => {
    const wide = guard.evaluate({ changedFiles: ['.claude-plugin/marketplace.json'], head: side({ market: JSON.stringify({ name: 'delimit', owner: { name: 'Other' }, plugins: [DELIMIT, PANEL] }), rel: SUITE_REL, panel: '1.0.0' }), base: SUITE_BASE, npmVersionExists: npmYes });
    assert.ok(has(wide, /plugin delimit changed/) && has(wide, /plugin delimit-panel changed/), show(wide));
    const removed = guard.evaluate({ changedFiles: ['.claude-plugin/marketplace.json'], head: side({ rel: SUITE_REL }), base: SUITE_BASE, npmVersionExists: npmYes });
    assert.ok(has(removed, /"delimit-panel" \(claude-plugin-panel\/\) was removed/), show(removed));
    const renamed = guard.evaluate({ changedFiles: ['.claude-plugin/marketplace.json'], head: side({ market: marketJson([DELIMIT, { ...PANEL, name: 'delimit-council' }]), rel: SUITE_REL, panel: '1.0.0' }), base: SUITE_BASE, npmVersionExists: npmYes });
    assert.ok(has(renamed, /named "delimit-panel" at the base but "delimit-council"/), show(renamed));
});

// ---- git-backed: the real diff, through main() ----

function gitRepo(t) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'plugin-guard-'));
    t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    const env = { ...process.env, HOME: dir, GIT_CONFIG_NOSYSTEM: '1', GIT_CONFIG_GLOBAL: path.join(dir, 'no-gitconfig') };
    for (const k of ['GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY', 'GIT_COMMON_DIR']) delete env[k];
    const repo = path.join(dir, 'repo');
    fs.mkdirSync(repo);
    const g = (...args) => {
        const r = spawnSync('git', ['-c', 'user.name=t', '-c', 'user.email=t@example.invalid', '-c', 'commit.gpgsign=false', ...args], { cwd: repo, env, encoding: 'utf8' });
        assert.equal(r.status, 0, `git ${args.join(' ')}: ${r.stderr}`);
        return r.stdout;
    };
    const write = (rel, text) => { fs.mkdirSync(path.dirname(path.join(repo, rel)), { recursive: true }); fs.writeFileSync(path.join(repo, rel), text); };
    const run = () => spawnSync(process.execPath, [SCRIPT, '--root', repo, '--base', 'base', '--offline'], { cwd: repo, env, encoding: 'utf8' });
    g('init', '-q', '-b', 'main');
    // Rename and copy detection ON in config: --no-renames must still win.
    g('config', 'diff.renames', 'copies');
    return { repo, g, write, run, env };
}

function seedSuite({ write, g }) {
    write('.claude-plugin/marketplace.json', marketJson([DELIMIT, PANEL]));
    write('claude-plugin/.claude-plugin/plugin.json', pluginJson('1.0.4'));
    write('claude-plugin/.mcp.json', mcpJson());
    write('claude-plugin/PRIVACY.md', 'privacy\n'.repeat(20));
    write('claude-plugin-panel/.claude-plugin/plugin.json', pluginJson('1.0.0', 'delimit-panel'));
    write('claude-plugin-panel/.mcp.json', mcpJson('delimit-cli@4.21.0', 'delimit-panel'));
    write(guard.PROVENANCE, SUITE_REL);
    g('add', '-A');
    g('commit', '-q', '-m', 'base');
    g('tag', 'base');
}

test('git: moving a file OUT of the plugin folder is a plugin change (no rename bypass)', (t) => {
    const r = gitRepo(t);
    seedSuite(r);
    r.g('mv', 'claude-plugin/PRIVACY.md', 'docs/PRIVACY-moved.md');
    r.g('commit', '-q', '-m', 'move out');
    const out = r.run();
    assert.equal(out.status, 1, out.stdout + out.stderr);
    assert.match(out.stdout, /FAIL {2}plugin delimit changed \(claude-plugin\/PRIVACY\.md\)/);
    assert.doesNotMatch(out.stdout, /plugin delimit-panel changed/);
});

test('git: moving a file INTO a plugin folder is also a plugin change', (t) => {
    const r = gitRepo(t);
    seedSuite(r);
    r.write('docs/extra.md', 'x\n'.repeat(20));
    r.g('add', '-A');
    r.g('commit', '-q', '-m', 'extra');
    r.g('tag', '-f', 'base');
    r.g('mv', 'docs/extra.md', 'claude-plugin-panel/extra.md');
    r.g('commit', '-q', '-m', 'move in');
    const out = r.run();
    assert.equal(out.status, 1, out.stdout);
    assert.match(out.stdout, /plugin delimit-panel changed \(claude-plugin-panel\/extra\.md\)/);
});

test('git: a sibling claude-plugin-panel/ pin change without a bump fails; a proper sibling release passes', (t) => {
    const r = gitRepo(t);
    seedSuite(r);
    r.write('claude-plugin-panel/.mcp.json', mcpJson('delimit-cli@0.0.0-nope', 'delimit-panel'));
    r.g('commit', '-qam', 'bad pin');
    const bad = r.run();
    assert.equal(bad.status, 1, bad.stdout);
    assert.match(bad.stdout, /must pin delimit-cli to an exact X\.Y\.Z version, got "delimit-cli@0\.0\.0-nope"/);
    assert.match(bad.stdout, /plugin delimit-panel changed/);
    assert.doesNotMatch(bad.stdout, /plugin delimit changed/);

    r.write('claude-plugin-panel/.mcp.json', mcpJson('delimit-cli@4.21.1', 'delimit-panel'));
    r.write('claude-plugin-panel/.claude-plugin/plugin.json', pluginJson('1.0.1', 'delimit-panel'));
    r.write(guard.PROVENANCE, releases('1.0.3', '1.0.4', ['delimit-panel', '1.0.0'], ['delimit-panel', '1.0.1']));
    r.g('commit', '-qam', 'panel 1.0.1');
    const good = r.run();
    assert.equal(good.status, 0, good.stdout);
    assert.match(good.stdout, /delimit-panel 1\.0\.1 \(claude-plugin-panel\/\), delimit-cli@4\.21\.1, changed/);
    assert.match(good.stdout, /delimit 1\.0\.4 \(claude-plugin\/\), delimit-cli@4\.20\.1, unchanged/);
});

test('git: a marketplace edit that only concerns the sibling does not force a delimit bump', (t) => {
    const r = gitRepo(t);
    seedSuite(r);
    r.write('.claude-plugin/marketplace.json', marketJson([DELIMIT, { ...PANEL, description: 'reworded' }]));
    r.write('claude-plugin-panel/.claude-plugin/plugin.json', pluginJson('1.0.1', 'delimit-panel'));
    r.write(guard.PROVENANCE, releases('1.0.3', '1.0.4', ['delimit-panel', '1.0.0'], ['delimit-panel', '1.0.1']));
    r.g('commit', '-qam', 'panel description');
    const out = r.run();
    assert.equal(out.status, 0, out.stdout);
});

test('the CI workflow parses without duplicate keys and runs the guard against the PR base', () => {
    // js-yaml rejects duplicate mapping keys, so a duplicated key fails here.
    const ci = yaml.load(read('.github/workflows/ci.yml'));
    const job = ci.jobs['claude-plugin-release-guard'];
    assert.ok(job, 'claude-plugin-release-guard job missing');
    assert.match(job.if, /pull_request/);
    assert.equal(job.steps[0].with['fetch-depth'], 0);
    const run = job.steps.map((s) => s.run || '').join('\n');
    // The guard runs as it is on the base commit, never the PR's copy.
    assert.match(run, /git show "\$BASE_SHA":scripts\/claude-plugin-release-guard\.js > "\$RUNNER_TEMP\/claude-plugin-release-guard\.js"/);
    assert.match(run, /node "\$RUNNER_TEMP\/claude-plugin-release-guard\.js" --root "\$GITHUB_WORKSPACE" --base "\$BASE_SHA"/);
    assert.doesNotMatch(run, /node scripts\/claude-plugin-release-guard\.js/);
    assert.match(run, /guard changed: needs owner review/);
    assert.match(job.steps.find((s) => s.run).env.BASE_SHA, /pull_request\.base\.sha/);
});

// ---- verifier defects, 2026-10-02 (accident guard, see the runbook's "What this guard is and is not") ----

test('1: a PR that changes the guard, its test or its CI job needs owner review', () => {
    for (const f of guard.GUARD_FILES) {
        const res = guard.evaluate({ changedFiles: [f], head: side(), base: BASE, npmVersionExists: npmYes });
        assert.ok(has(res, new RegExp(`guard changed: needs owner review \\(${f.replace(/[./]/g, '\\$&')}\\)`)), show(res));
    }
    const ci = read(guard.WORKFLOW);
    assert.ok(guard.guardJobBlock(ci), 'job block found');
    const other = ci.replace('  bundle-guards:', '  bundle-guards:\n    # unrelated edit');
    const unrelated = guard.evaluate({ changedFiles: [guard.WORKFLOW], head: { ...side(), workflow: other }, base: { ...BASE, workflow: ci }, npmVersionExists: npmYes });
    assert.deepEqual(unrelated.errors, [], 'an edit to another job is not a guard change');
    const jobEdit = ci.replace(/--root "\$GITHUB_WORKSPACE" --base "\$BASE_SHA"/, '--offline || true');
    assert.notEqual(jobEdit, ci);
    const edited = guard.evaluate({ changedFiles: [guard.WORKFLOW], head: { ...side(), workflow: jobEdit }, base: { ...BASE, workflow: ci }, npmVersionExists: npmYes });
    assert.ok(has(edited, /guard changed: needs owner review \(\.github\/workflows\/ci\.yml job claude-plugin-release-guard\)/), show(edited));
    const dropped = guard.evaluate({ changedFiles: [guard.WORKFLOW], head: { ...side(), workflow: 'jobs: {}\n' }, base: { ...BASE, workflow: ci }, npmVersionExists: npmYes });
    assert.ok(has(dropped, /guard changed/), show(dropped));
});

test('1 (git): the base copy of the guard fails a PR that edits the guard to exit 0', (t) => {
    const r = gitRepo(t);
    r.write('scripts/claude-plugin-release-guard.js', fs.readFileSync(SCRIPT, 'utf8'));
    seedSuite(r);
    r.write('claude-plugin/README.md', 'changed\n');
    r.write('scripts/claude-plugin-release-guard.js', 'process.exitCode = 0;\n');
    r.g('add', '-A');
    r.g('commit', '-q', '-m', 'self edit');
    // What CI does: extract the guard from the base commit and run that copy.
    const baseCopy = path.join(path.dirname(r.repo), 'base-guard.js');
    fs.writeFileSync(baseCopy, r.g('show', 'base:scripts/claude-plugin-release-guard.js'));
    const out = spawnSync(process.execPath, [baseCopy, '--root', r.repo, '--base', 'base', '--offline'], { cwd: r.repo, env: r.env, encoding: 'utf8' });
    assert.equal(out.status, 1, out.stdout + out.stderr);
    assert.match(out.stdout, /guard changed: needs owner review \(scripts\/claude-plugin-release-guard\.js\)/);
    assert.match(out.stdout, /plugin delimit changed \(claude-plugin\/README\.md\)/);
});

test('2 (git): a gitlink hidden by .gitmodules ignore=all is seen and refused', (t) => {
    const r = gitRepo(t);
    seedSuite(r);
    const sha = r.g('rev-parse', 'HEAD').trim();
    r.write('.gitmodules', '[submodule "vendor"]\n\tpath = claude-plugin/vendor\n\turl = https://example.invalid/x.git\n\tignore = all\n');
    r.g('add', '.gitmodules');
    r.g('update-index', '--add', '--cacheinfo', `160000,${sha},claude-plugin/vendor`);
    r.g('commit', '-q', '-m', 'gitlink');
    const out = r.run();
    assert.equal(out.status, 1, out.stdout + out.stderr);
    assert.match(out.stdout, /claude-plugin\/vendor: submodule \(gitlink\) in plugin folder claude-plugin\/ is not allowed/);
    assert.match(out.stdout, /delimit 1\.0\.4 \(claude-plugin\/\), .*changed: .*claude-plugin\/vendor/);
});

test('2 (git): a symlink in a plugin folder is refused even with a proper bump', (t) => {
    const r = gitRepo(t);
    seedSuite(r);
    r.write('shared/evil/SKILL.md', 'x\n');
    fs.mkdirSync(path.join(r.repo, 'claude-plugin/skills'), { recursive: true });
    fs.symlinkSync('../../shared/evil', path.join(r.repo, 'claude-plugin/skills/evil'));
    r.write('claude-plugin/.claude-plugin/plugin.json', pluginJson('1.0.5'));
    r.write(guard.PROVENANCE, releases('1.0.3', '1.0.4', ['delimit-panel', '1.0.0'], '1.0.5'));
    r.g('add', '-A');
    r.g('commit', '-q', '-m', 'symlink');
    const out = r.run();
    assert.equal(out.status, 1, out.stdout + out.stderr);
    assert.match(out.stdout, /claude-plugin\/skills\/evil: symlink in plugin folder claude-plugin\/ is not allowed/);
});

test('3: sources match "./<folder>" exactly; pluginRoot and case variants fail closed', () => {
    for (const src of ['./claude-plugin ', ' ./claude-plugin', 'claude-plugin', './claude-plugin/', '.\\claude-plugin']) {
        assert.equal(guard.normalizeSource(src), null, JSON.stringify(src));
        const res = guard.evaluate({ head: side({ market: marketJson([DELIMIT, { ...PANEL, source: src }]) }) });
        assert.ok(has(res, /is not exactly "\.\/<folder>"/), `${JSON.stringify(src)}: ${show(res)}`);
    }
    const rooted = guard.evaluate({ head: side({ market: JSON.stringify({ name: 'delimit', metadata: { pluginRoot: './shadow' }, plugins: [DELIMIT] }) }) });
    assert.ok(has(rooted, /metadata\.pluginRoot must not be set/), show(rooted));
    const cased = guard.evaluate({ changedFiles: ['CLAUDE-PLUGIN/skills/evil/SKILL.md', 'Claude-Plugin'], head: side(), base: BASE, npmVersionExists: npmYes });
    assert.ok(has(cased, /CLAUDE-PLUGIN\/skills\/evil\/SKILL\.md: differs only in case from plugin folder claude-plugin\//), show(cased));
    assert.ok(has(cased, /^Claude-Plugin: differs only in case/), show(cased));
    const twice = guard.evaluate({ head: side({ market: marketJson([DELIMIT, { ...PANEL, source: './Claude-Plugin' }]) }) });
    assert.ok(has(twice, /listed twice/), show(twice));
});

test('4: .gitattributes or .gitmodules at the root or above a plugin folder touches every plugin', () => {
    for (const f of ['.gitattributes', '.gitmodules', '.GITATTRIBUTES']) {
        const res = guard.evaluate({ changedFiles: [f], head: side({ market: marketJson([DELIMIT, PANEL]), rel: SUITE_REL, panel: '1.0.0' }), base: SUITE_BASE, npmVersionExists: npmYes });
        assert.ok(has(res, /plugin delimit changed .*git attributes or submodules/), `${f}: ${show(res)}`);
        assert.ok(has(res, /plugin delimit-panel changed/), `${f}: ${show(res)}`);
    }
    assert.ok(guard.gitMetaAbove('a/.gitattributes', 'a/b'));
    assert.ok(!guard.gitMetaAbove('a/b/.gitattributes', 'a/b'), 'inside the folder it is a file of that plugin');
    assert.ok(!guard.gitMetaAbove('c/.gitattributes', 'a/b'));
    assert.ok(!guard.gitMetaAbove('docs/notes.md', 'claude-plugin'));
});

test('5 and 6: provenance time label and the guard scope statement', () => {
    const prov = read(guard.PROVENANCE);
    assert.match(prov, /per ledger note \(written 07:15 ET; icon fix committed 07:14 ET\)/);
    assert.doesNotMatch(prov, /07:30 ET/);
    const runbook = read('docs/claude-plugin-release.md');
    assert.match(runbook, /### What this guard is and is not/);
    for (const text of [runbook, read(guard.WORKFLOW)]) {
        for (const re of [/accident guard/i, /claude plugin tag/, /inline\s+(#\s*)?`?mcpServers/, /provenance\s+(#\s*)?row/, /stale-base race/, /launchers or registries/]) assert.match(text, re);
    }
});
