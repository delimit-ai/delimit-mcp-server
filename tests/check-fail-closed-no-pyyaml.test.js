/**
 * LED-4423 (P0): `delimit check` must FAIL CLOSED, and the engine must work
 * on a python with no PyYAML (zero-config, no `setup`).
 *
 * Origin: a fresh Debian 12 container (system python3 without `yaml`,
 * `pip install pyyaml` refused under PEP 668) ran `delimit check` on a spec
 * with a REMOVED ENDPOINT. The helper died with ModuleNotFoundError, the
 * CLI swallowed the error, printed "+ openapi.yaml — clean" and PASSED.
 *
 * Covered here:
 *   (a) any helper error, or a result without `summary`, is reported as
 *       "could not analyse", counted as a violation, exit code 1, and the
 *       word "clean" never appears;
 *   (b) with a python that cannot `import yaml`, `check` on a removed
 *       endpoint reports BLOCKED / MAJOR with exit 1 and no Traceback;
 *   (c) `diff` / `lint` / `explain` on that same python still produce the
 *       breaking-change output;
 *   (d) `scan` on that python (with pip refusing) prints one clear line and
 *       still scores the spec; no Traceback;
 *   (e) temp JSON conversions are cleaned up.
 *
 * How the no-yaml python is simulated: a `python3` shim placed FIRST on PATH
 * execs the real interpreter with `-S` (no site-packages: PyYAML lives in
 * site/dist-packages, the stdlib does not). The shim is verified before use:
 * `import yaml` must fail and `import json` must succeed. HOME/DELIMIT_HOME
 * point at an empty tmp dir so no ~/.delimit/venv can be preferred.
 */

const { describe, it, before, after } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execSync, spawnSync } = require('child_process');
const { makeTmpGitRepo } = require('./_git-hermetic');

const REPO_ROOT = path.join(__dirname, '..');
const CLI = path.join(REPO_ROOT, 'bin', 'delimit-cli.js');
const API_ENGINE = path.join(REPO_ROOT, 'lib', 'api-engine.js');
const PETSTORE_V1 = path.join(REPO_ROOT, 'examples', 'petstore-v1.yaml');
const PETSTORE_V2 = path.join(REPO_ROOT, 'examples', 'petstore-v2.yaml');

function realPython() {
    try {
        return execSync('command -v python3', { encoding: 'utf-8', stdio: ['ignore', 'pipe', 'ignore'] }).trim();
    } catch {
        return null;
    }
}

/**
 * Build a bin dir whose `python3` runs the real interpreter with -S
 * (no site-packages => no PyYAML) and whose `pip3` refuses (PEP 668).
 */
function makeNoYamlPythonDir(real) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-noyaml-py-'));
    fs.writeFileSync(path.join(dir, 'python3'), `#!/bin/sh\nexec "${real}" -S "$@"\n`, { mode: 0o755 });
    fs.writeFileSync(path.join(dir, 'python'), `#!/bin/sh\nexec "${real}" -S "$@"\n`, { mode: 0o755 });
    const refuse = '#!/bin/sh\necho "error: externally-managed-environment" >&2\nexit 1\n';
    fs.writeFileSync(path.join(dir, 'pip3'), refuse, { mode: 0o755 });
    fs.writeFileSync(path.join(dir, 'pip'), refuse, { mode: 0o755 });
    return dir;
}

/** A python3 that always fails (simulates any helper crash). */
function makeBrokenPythonDir() {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-broken-py-'));
    fs.writeFileSync(path.join(dir, 'python3'), '#!/bin/sh\necho "boom: helper exploded" >&2\nexit 1\n', { mode: 0o755 });
    fs.writeFileSync(path.join(dir, 'python'), '#!/bin/sh\necho "boom: helper exploded" >&2\nexit 1\n', { mode: 0o755 });
    return dir;
}

/** Hermetic env for spawning the CLI: shim bin first on PATH, empty HOME. */
function cliEnv(baseEnv, binDir, home) {
    const env = { ...baseEnv };
    env.PATH = `${binDir}${path.delimiter}${baseEnv.PATH || process.env.PATH}`;
    env.HOME = home;
    env.DELIMIT_HOME = path.join(home, '.delimit');
    env.FORCE_COLOR = '0';
    env.NO_COLOR = '1';
    env.CI = '1';
    delete env.DELIMIT_GATEWAY_ROOT;
    delete env.NODE_OPTIONS;
    return env;
}

function runCli(args, { cwd, env }) {
    const r = spawnSync(process.execPath, [CLI, ...args], {
        cwd,
        env,
        encoding: 'utf-8',
        timeout: 60000,
        input: '',
    });
    return { status: r.status, out: `${r.stdout || ''}\n${r.stderr || ''}` };
}

/** Temp repo: api/openapi.yaml committed as petstore v1, working tree = v2 (removed endpoint). */
function makeRemovedEndpointRepo() {
    const repo = makeTmpGitRepo({ prefix: 'delimit-check-fc-', commit: true });
    fs.mkdirSync(path.join(repo.dir, 'api'), { recursive: true });
    fs.copyFileSync(PETSTORE_V1, path.join(repo.dir, 'api', 'openapi.yaml'));
    repo.run('git add api/openapi.yaml');
    repo.run('git commit -qm "petstore v1"');
    fs.copyFileSync(PETSTORE_V2, path.join(repo.dir, 'api', 'openapi.yaml'));
    return repo;
}

function countSpecTemps() {
    return fs.readdirSync(os.tmpdir()).filter(f => f.startsWith('delimit_spec_')).length;
}

const PY = realPython();

describe('LED-4423: no-PyYAML python shim is real', { skip: PY ? false : 'python3 not on PATH' }, () => {
    let binDir;
    before(() => { binDir = makeNoYamlPythonDir(PY); });
    after(() => { fs.rmSync(binDir, { recursive: true, force: true }); });

    it('shim python3 cannot import yaml but the stdlib works', () => {
        const env = { ...process.env, PATH: `${binDir}${path.delimiter}${process.env.PATH}` };
        const yamlProbe = spawnSync('python3', ['-c', 'import yaml'], { env, encoding: 'utf-8' });
        assert.notEqual(yamlProbe.status, 0, 'shim must NOT be able to import yaml (is PyYAML vendored into the stdlib here?)');
        assert.match(yamlProbe.stderr, /No module named 'yaml'/);
        const jsonProbe = spawnSync('python3', ['-c', 'import json, sys; print(json.dumps({"ok": True}))'], { env, encoding: 'utf-8' });
        assert.equal(jsonProbe.status, 0, `stdlib must work under the shim: ${jsonProbe.stderr}`);
        assert.equal(jsonProbe.stdout.trim(), '{"ok": true}');
    });

    it('api-engine.pythonHasYaml() reports false under the shim', () => {
        const savedPath = process.env.PATH;
        const savedHome = process.env.HOME;
        const savedDh = process.env.DELIMIT_HOME;
        const home = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-noyaml-home-'));
        try {
            process.env.PATH = `${binDir}${path.delimiter}${savedPath}`;
            process.env.HOME = home;
            process.env.DELIMIT_HOME = path.join(home, '.delimit');
            delete require.cache[require.resolve(API_ENGINE)];
            const engine = require(API_ENGINE);
            assert.equal(engine.pythonHasYaml(), false);
            // And the engine still diffs the specs (node-side YAML -> JSON).
            const r = engine.diff(PETSTORE_V1, PETSTORE_V2);
            assert.ok(r.breaking_changes >= 1, `expected breaking changes, got ${JSON.stringify(r).slice(0, 200)}`);
            assert.ok(r.changes.some(c => /removed/i.test(c.message) && /vaccinations/.test(c.message)));
        } finally {
            process.env.PATH = savedPath;
            process.env.HOME = savedHome;
            if (savedDh === undefined) delete process.env.DELIMIT_HOME; else process.env.DELIMIT_HOME = savedDh;
            delete require.cache[require.resolve(API_ENGINE)];
            fs.rmSync(home, { recursive: true, force: true });
        }
    });
});

describe('LED-4423 (a): delimit check fails CLOSED on helper errors', () => {
    let repo, brokenDir, home;
    before(() => {
        repo = makeRemovedEndpointRepo();
        brokenDir = makeBrokenPythonDir();
        home = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-fc-home-'));
    });
    after(() => {
        repo.cleanup();
        fs.rmSync(brokenDir, { recursive: true, force: true });
        fs.rmSync(home, { recursive: true, force: true });
    });

    it('python that always exits 1: "could not analyse", exit 1, never "clean"', () => {
        const env = cliEnv(repo.env, brokenDir, home);
        const { status, out } = runCli(['check'], { cwd: repo.dir, env });
        assert.equal(status, 1, `expected exit 1\n${out}`);
        assert.match(out, /could not analyse/);
        assert.match(out, /boom: helper exploded/);
        assert.match(out, /BLOCKED/);
        assert.match(out, /could not be analysed/);
        assert.doesNotMatch(out, /clean/);
        assert.doesNotMatch(out, /PASSED/);
    });

    it('--staged keeps the same fail-closed behaviour', () => {
        repo.run('git add api/openapi.yaml');
        const env = cliEnv(repo.env, brokenDir, home);
        const { status, out } = runCli(['check', '--staged'], { cwd: repo.dir, env });
        repo.run('git reset -q api/openapi.yaml');
        assert.equal(status, 1, out);
        assert.match(out, /could not analyse/);
        assert.doesNotMatch(out, /clean/);
    });

    it('stubbed api-engine that throws => could not analyse, exit 1', () => {
        const preload = path.join(home, 'stub-throw.js');
        fs.writeFileSync(preload, `
            const Module = require('module');
            const orig = Module._load;
            Module._load = function (request, parent, isMain) {
                if (/api-engine(\\.js)?$/.test(request)) {
                    return { lint() { throw new Error('stub engine failure\\nsecond line ignored'); }, zeroSpec() { throw new Error('x'); }, GATEWAY_ROOT: '' };
                }
                return orig.apply(this, arguments);
            };
        `);
        const env = cliEnv(repo.env, path.dirname(PY || '/usr/bin/python3'), home);
        env.NODE_OPTIONS = `--require ${preload}`;
        const { status, out } = runCli(['check'], { cwd: repo.dir, env });
        assert.equal(status, 1, out);
        assert.match(out, /could not analyse: stub engine failure/);
        assert.doesNotMatch(out, /second line ignored/);
        assert.doesNotMatch(out, /clean/);
    });

    it('stubbed api-engine returning a result WITHOUT summary => could not analyse, exit 1', () => {
        const preload = path.join(home, 'stub-nosummary.js');
        fs.writeFileSync(preload, `
            const Module = require('module');
            const orig = Module._load;
            Module._load = function (request, parent, isMain) {
                if (/api-engine(\\.js)?$/.test(request)) {
                    return { lint() { return { error: 'engine produced no summary' }; }, zeroSpec() { throw new Error('x'); }, GATEWAY_ROOT: '' };
                }
                return orig.apply(this, arguments);
            };
        `);
        const env = cliEnv(repo.env, path.dirname(PY || '/usr/bin/python3'), home);
        env.NODE_OPTIONS = `--require ${preload}`;
        const { status, out } = runCli(['check'], { cwd: repo.dir, env });
        assert.equal(status, 1, out);
        assert.match(out, /could not analyse: engine produced no summary/);
        assert.doesNotMatch(out, /clean/);
    });

    it('--record verdict is "blocked" when a spec could not be analysed', () => {
        const env = cliEnv(repo.env, brokenDir, home);
        const recPath = path.join(repo.dir, 'rec.json');
        const { status } = runCli(['check', '--record', recPath], { cwd: repo.dir, env });
        assert.equal(status, 1);
        const rec = JSON.parse(fs.readFileSync(recPath, 'utf-8'));
        assert.equal(rec.verdict, 'blocked');
        assert.equal(rec.findings.unanalysed_specs, 1);
        assert.ok(rec.findings.spec_violations.some(v => /could not analyse/.test(v.message)));
    });
});

describe('LED-4423 (b)(c)(d)(e): zero-config YAML on a python without PyYAML', { skip: PY ? false : 'python3 not on PATH' }, () => {
    let repo, binDir, home, tempsBefore;
    before(() => {
        repo = makeRemovedEndpointRepo();
        binDir = makeNoYamlPythonDir(PY);
        home = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-noyaml-home-'));
        tempsBefore = countSpecTemps();
    });
    after(() => {
        repo.cleanup();
        fs.rmSync(binDir, { recursive: true, force: true });
        fs.rmSync(home, { recursive: true, force: true });
    });

    it('(b) check on a removed endpoint reports BLOCKED / MAJOR, exit 1, no Traceback', () => {
        const env = cliEnv(repo.env, binDir, home);
        const { status, out } = runCli(['check'], { cwd: repo.dir, env });
        assert.equal(status, 1, `expected exit 1\n${out}`);
        assert.match(out, /BLOCKED/);
        assert.match(out, /Semver: MAJOR/);
        assert.match(out, /breaking change\(s\)/);
        assert.doesNotMatch(out, /Traceback/);
        assert.doesNotMatch(out, /No module named/);
        assert.doesNotMatch(out, /could not analyse/);
        assert.doesNotMatch(out, /clean/);
    });

    it('(b) check --staged on the removed endpoint is also BLOCKED', () => {
        repo.run('git add api/openapi.yaml');
        const env = cliEnv(repo.env, binDir, home);
        const { status, out } = runCli(['check', '--staged'], { cwd: repo.dir, env });
        repo.run('git reset -q api/openapi.yaml');
        assert.equal(status, 1, out);
        assert.match(out, /BLOCKED/);
        assert.match(out, /Semver: MAJOR/);
        assert.doesNotMatch(out, /Traceback/);
    });

    it('(c) diff shows the removed endpoint as BREAKING', () => {
        const env = cliEnv(repo.env, binDir, home);
        const { status, out } = runCli(['diff', PETSTORE_V1, PETSTORE_V2], { cwd: repo.dir, env });
        assert.equal(status, 0, out);
        assert.match(out, /\[BREAKING\] Endpoint removed: \/pets\/\{petId\}\/vaccinations/);
        assert.doesNotMatch(out, /Traceback/);
    });

    it('(c) lint (preset policy read from the gateway) fails with the endpoint-removal violation', () => {
        const env = cliEnv(repo.env, binDir, home);
        const { status, out } = runCli(['lint', PETSTORE_V1, PETSTORE_V2, '--policy', 'strict', '--json'], { cwd: repo.dir, env });
        assert.equal(status, 1, out);
        const json = JSON.parse(out.slice(out.indexOf('{'), out.lastIndexOf('}') + 1));
        assert.equal(json.decision, 'fail');
        assert.ok(json.violations.some(v => v.rule === 'no_endpoint_removal'), JSON.stringify(json.violations));
        assert.equal(json.semver && json.semver.bump, 'major');
    });

    it('(c) explain renders the removed endpoint', () => {
        const env = cliEnv(repo.env, binDir, home);
        const { status, out } = runCli(['explain', PETSTORE_V1, PETSTORE_V2], { cwd: repo.dir, env });
        assert.equal(status, 0, out);
        assert.match(out, /vaccinations/);
        assert.doesNotMatch(out, /Traceback/);
    });

    it('(d) scan on a spec file: one clear line about pyyaml, spec still scored, no Traceback', () => {
        const env = cliEnv(repo.env, binDir, home);
        const { status, out } = runCli(['scan', PETSTORE_V1], { cwd: repo.dir, env });
        assert.equal(status, 0, out);
        assert.match(out, /pyyaml could not be installed/);
        assert.match(out, /built-in YAML reader/);
        assert.match(out, /Spec Health Score/);
        assert.doesNotMatch(out, /Traceback/);
        assert.doesNotMatch(out, /No module named/);
    });

    it('(e) temp JSON conversions are cleaned up', () => {
        assert.ok(countSpecTemps() <= tempsBefore, 'delimit_spec_* temp files left behind in os.tmpdir()');
    });
});
