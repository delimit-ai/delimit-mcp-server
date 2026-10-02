'use strict';

// Unit tests for scripts/claude-plugin-release-guard.js (Claude plugin release
// lane, docs/claude-plugin-release.md). Pure fixtures plus the repo's own
// files; no git, no network (the npm lookup is injected).

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const yaml = require('js-yaml');

const guard = require('../scripts/claude-plugin-release-guard.js');

const ROOT = path.join(__dirname, '..');
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), 'utf8');

const TABLE_HEAD = '| Version | Source commit | Tag | delimit-cli pin | Scan | Published at | Acceptance |\n|---|---|---|---|---|---|---|\n';
const row = (v) => `| ${v} | \`abc1234\` | \`delimit--v${v}\` | 4.20.1 | passed | Pending | Pending |\n`;
const releases = (...versions) => `# Releases\n\n${TABLE_HEAD}${versions.map(row).join('')}`;
const pluginJson = (version, name = 'delimit') => JSON.stringify({ name, version });
const marketJson = (source = './claude-plugin', name = 'delimit') =>
    JSON.stringify({ name, plugins: [{ name: 'delimit', source }] });
const mcpJson = (spec = 'delimit-cli@4.20.1') =>
    JSON.stringify({ mcpServers: { delimit: { command: 'npx', args: ['-y', spec, 'mcp', '--toolset', 'records'] } } });

function headFiles(over = {}) {
    return { plugin: pluginJson('1.0.4'), marketplace: marketJson(), mcp: mcpJson(), releases: releases('1.0.3', '1.0.4'), ...over };
}
const BASE = { plugin: pluginJson('1.0.4'), releases: releases('1.0.3', '1.0.4') };
const npmYes = () => true;
const npmNo = () => false;

test('semver parsing and comparison', () => {
    assert.deepEqual(guard.parseSemver('1.0.10'), [1, 0, 10]);
    for (const bad of ['1.0', 'v1.0.0', '1.0.0-beta', '^1.0.0', '01.0.0', '', undefined]) assert.equal(guard.parseSemver(bad), null, String(bad));
    assert.equal(guard.compareSemver('1.0.10', '1.0.9'), 1);
    assert.equal(guard.compareSemver('1.0.4', '1.0.4'), 0);
    assert.equal(guard.compareSemver('1.9.0', '2.0.0'), -1);
});

test('provenance table rows are read from any table line whose first cell is X.Y.Z', () => {
    const rows = guard.parseReleases(releases('1.0.0', '1.0.4'));
    assert.deepEqual(rows.map((r) => r.version), ['1.0.0', '1.0.4']);
    assert.deepEqual(guard.parseReleases('| `2.1.0` | x |').map((r) => r.version), ['2.1.0']);
});

test('plugin scope covers both plugin folders but not the provenance file', () => {
    assert.ok(guard.inPluginScope('claude-plugin/README.md'));
    assert.ok(guard.inPluginScope('.claude-plugin/marketplace.json'));
    assert.ok(!guard.inPluginScope('docs/claude-plugin-releases.md'));
    assert.ok(!guard.inPluginScope('lib/cli.js'));
    assert.ok(!guard.PROVENANCE.startsWith('claude-plugin/'), 'provenance must live outside the scanned plugin folder');
});

test('the repository as committed passes the offline invariants', () => {
    const res = guard.evaluate({
        head: {
            plugin: read(guard.PLUGIN_MANIFEST), marketplace: read(guard.MARKETPLACE),
            mcp: read(guard.MCP_CONFIG), releases: read(guard.PROVENANCE),
        },
    });
    assert.deepEqual(res.errors, []);
    const r = spawnSync(process.execPath, [path.join(ROOT, 'scripts', 'claude-plugin-release-guard.js')], { encoding: 'utf8' });
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.match(r.stdout, /PASS/);
});

test('a change outside the plugin needs no bump and no npm lookup', () => {
    let called = false;
    const res = guard.evaluate({ changedFiles: ['lib/cli.js', 'docs/claude-plugin-releases.md'], head: headFiles(), base: BASE, npmVersionExists: () => { called = true; return false; } });
    assert.deepEqual(res.errors, []);
    assert.equal(called, false);
});

test('(a) a plugin change without a version bump fails', () => {
    const res = guard.evaluate({ changedFiles: ['claude-plugin/README.md'], head: headFiles(), base: BASE, npmVersionExists: npmYes });
    assert.ok(res.errors.some((e) => /not greater than the base version 1\.0\.4/.test(e)), res.errors.join('\n'));
});

test('(a) a version already recorded at the base cannot be reused', () => {
    const base = { plugin: pluginJson('1.0.4'), releases: releases('1.0.4', '1.0.5') };
    const head = headFiles({ plugin: pluginJson('1.0.5'), releases: releases('1.0.4', '1.0.5') });
    const res = guard.evaluate({ changedFiles: ['claude-plugin/.claude-plugin/plugin.json'], head, base, npmVersionExists: npmYes });
    assert.ok(res.errors.some((e) => /latest recorded release 1\.0\.5/.test(e)), res.errors.join('\n'));
});

test('(b) a bumped version without a provenance row fails', () => {
    const head = headFiles({ plugin: pluginJson('1.0.5') });
    const res = guard.evaluate({ changedFiles: ['claude-plugin/.claude-plugin/plugin.json'], head, base: BASE, npmVersionExists: npmYes });
    assert.ok(res.errors.some((e) => /no row for plugin version 1\.0\.5/.test(e)), res.errors.join('\n'));
});

test('a bump with a row and an existing pin passes', () => {
    const head = headFiles({ plugin: pluginJson('1.0.5'), releases: releases('1.0.3', '1.0.4', '1.0.5') });
    let asked = null;
    const res = guard.evaluate({
        changedFiles: ['claude-plugin/skills/record/SKILL.md', 'claude-plugin/.claude-plugin/plugin.json', 'docs/claude-plugin-releases.md'],
        head, base: BASE, npmVersionExists: (pkg, v) => { asked = `${pkg}@${v}`; return true; },
    });
    assert.deepEqual(res.errors, []);
    assert.equal(asked, 'delimit-cli@4.20.1');
});

test('(c) a pin that is not on npm fails, and a lookup error fails closed', () => {
    const head = headFiles({ plugin: pluginJson('1.0.5'), releases: releases('1.0.3', '1.0.4', '1.0.5'), mcp: mcpJson('delimit-cli@9.9.9') });
    const args = { changedFiles: ['claude-plugin/.mcp.json'], head, base: BASE };
    assert.ok(guard.evaluate({ ...args, npmVersionExists: npmNo }).errors.some((e) => /9\.9\.9 does not exist on npm/.test(e)));
    const thrown = guard.evaluate({ ...args, npmVersionExists: () => { throw new Error('ETIMEDOUT'); } });
    assert.ok(thrown.errors.some((e) => /lookup .* failed: ETIMEDOUT/.test(e)), thrown.errors.join('\n'));
    const offline = guard.evaluate({ ...args, npmVersionExists: null });
    assert.deepEqual(offline.errors, []);
    assert.ok(offline.warnings.some((w) => /--offline/.test(w)));
});

test('(c) the pin must be one exact X.Y.Z version', () => {
    for (const spec of ['delimit-cli@^4.20.1', 'delimit-cli@latest', 'delimit-cli']) {
        const res = guard.evaluate({ head: headFiles({ mcp: mcpJson(spec) }) });
        assert.ok(res.errors.some((e) => /\.mcp\.json/.test(e)), `${spec}: ${res.errors.join('\n')}`);
    }
});

test('(d) manifests must be valid JSON named delimit, with the marketplace pointing at the plugin', () => {
    const cases = [
        [{ plugin: '{"name": "delimit",' }, /plugin\.json: invalid JSON/],
        [{ plugin: pluginJson('1.0.4', 'other') }, /plugin\.json: name must be "delimit"/],
        [{ plugin: pluginJson('1.0') }, /version must be X\.Y\.Z/],
        [{ marketplace: 'not json' }, /marketplace\.json: invalid JSON/],
        [{ marketplace: marketJson('./claude-plugin', 'other') }, /marketplace\.json: name must be "delimit"/],
        [{ marketplace: marketJson('./elsewhere') }, /source must be \.\/claude-plugin/],
        [{ mcp: null }, /\.mcp\.json: missing/],
    ];
    for (const [over, re] of cases) {
        const res = guard.evaluate({ head: headFiles(over) });
        assert.ok(res.errors.some((e) => re.test(e)), `${re}: ${res.errors.join('\n')}`);
    }
});

test('provenance rows must strictly increase and are append-only', () => {
    const dup = guard.evaluate({ head: headFiles({ releases: releases('1.0.4', '1.0.4') }) });
    assert.ok(dup.errors.some((e) => /strictly increase/.test(e)));
    const down = guard.evaluate({ head: headFiles({ releases: releases('1.0.4', '1.0.3') }) });
    assert.ok(down.errors.some((e) => /strictly increase/.test(e)));
    const removed = guard.evaluate({ changedFiles: ['docs/claude-plugin-releases.md'], head: headFiles({ releases: releases('1.0.4') }), base: BASE });
    assert.ok(removed.errors.some((e) => /row 1\.0\.3 was removed/.test(e)), removed.errors.join('\n'));
    const missing = guard.evaluate({ head: headFiles({ releases: null }) });
    assert.ok(missing.errors.some((e) => /claude-plugin-releases\.md: missing/.test(e)));
});

test('the CI workflow parses without duplicate keys and runs the guard against the PR base', () => {
    // js-yaml rejects duplicate mapping keys, so a duplicated key fails here.
    const ci = yaml.load(read('.github/workflows/ci.yml'));
    const job = ci.jobs['claude-plugin-release-guard'];
    assert.ok(job, 'claude-plugin-release-guard job missing');
    assert.match(job.if, /pull_request/);
    assert.equal(job.steps[0].with['fetch-depth'], 0);
    const run = job.steps.map((s) => s.run || '').join('\n');
    assert.match(run, /node scripts\/claude-plugin-release-guard\.js --base "\$BASE_SHA"/);
    assert.match(job.steps.find((s) => s.run).env.BASE_SHA, /pull_request\.base\.sha/);
});
