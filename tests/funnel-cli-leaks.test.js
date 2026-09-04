const { describe, it, before, after } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');
const { spawnSync } = require('child_process');
const { makeTmpGitRepo } = require('./_git-hermetic');

/**
 * Fresh-user install test of delimit-cli 4.18.1 (2026-09-02) found five
 * install-funnel leaks. These tests pin the fixes:
 *
 *  1. `delimit wrap` printed a hosted replay URL that 404s on the free tier.
 *  2. `delimit scan` / `delimit init` prompted (and exit 130) when stdin was
 *     not a TTY or CI=1.
 *  3. `delimit doctor` printed a hardcoded "v4.20" banner.
 *  4. `delimit check` leaked raw git stderr in a repo with no commits.
 *  5. `delimit deliberate` / `delimit think` engine-resolution fast-follows
 *     from the PR #195 review.
 */

const CLI = path.join(__dirname, '..', 'bin', 'delimit-cli.js');
const CLI_SRC = fs.readFileSync(CLI, 'utf-8');
const PKG_VERSION = require('../package.json').version;
const PETSTORE = path.join(__dirname, '..', 'examples', 'petstore-v1.yaml');

const SKIP_IN_CI = process.env.CI ? 'requires full CLI stack (not available in CI)' : false;

// Run the CLI with stdin CLOSED (not a pipe, not a TTY) and CI=1: the exact
// shape of a scripted / container install.
function runCli(args, { cwd, home, env = {}, timeout = 60000 } = {}) {
    const delimitHome = path.join(home, '.delimit');
    const res = spawnSync('node', [CLI, ...args], {
        cwd,
        encoding: 'utf-8',
        timeout,
        stdio: ['ignore', 'pipe', 'pipe'],
        env: {
            ...process.env,
            HOME: home,
            DELIMIT_HOME: delimitHome,
            CI: '1',
            NO_COLOR: '1',
            ...env,
        },
    });
    return { status: res.status, stdout: res.stdout || '', stderr: res.stderr || '', out: (res.stdout || '') + (res.stderr || '') };
}

function handlerSource(commandLiteral) {
    const start = CLI_SRC.indexOf(`.command('${commandLiteral}')`);
    assert.ok(start > 0, `${commandLiteral} command not found`);
    return CLI_SRC.slice(start, CLI_SRC.indexOf(".command('", start + 10));
}

// ---------------------------------------------------------------------------
// Shared helper
// ---------------------------------------------------------------------------

describe('lib/interactive.isInteractive', () => {
    const { isInteractive } = require('../lib/interactive');
    const saved = {};
    const KEYS = ['CI', 'DELIMIT_NON_INTERACTIVE'];

    before(() => { for (const k of KEYS) { saved[k] = process.env[k]; delete process.env[k]; } });
    after(() => { for (const k of KEYS) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; } });

    it('is false when CI is set, whatever the TTY state', () => {
        process.env.CI = 'true';
        assert.equal(isInteractive(), false);
        delete process.env.CI;
    });

    it('is false when DELIMIT_NON_INTERACTIVE is set', () => {
        process.env.DELIMIT_NON_INTERACTIVE = '1';
        assert.equal(isInteractive(), false);
        delete process.env.DELIMIT_NON_INTERACTIVE;
    });

    it('is false when the caller passes yes (the --yes flag)', () => {
        assert.equal(isInteractive({ yes: true }), false);
    });

    it('is false when stdin is not a TTY (the node --test runner pipes stdin)', () => {
        if (process.stdin.isTTY && process.stdout.isTTY) return; // real terminal: nothing to assert
        assert.equal(isInteractive(), false);
    });
});

// ---------------------------------------------------------------------------
// 1. wrap: no dead replay link on the free tier
// ---------------------------------------------------------------------------

describe('delimit wrap: free-tier receipt is the local attestation, not a 404 URL', () => {
    let repo;
    let home;
    before(() => {
        repo = makeTmpGitRepo({ prefix: 'delimit-funnel-wrap-' });
        home = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-funnel-wrap-home-'));
    });
    after(() => { repo.cleanup(); try { fs.rmSync(home, { recursive: true, force: true }); } catch {} });

    it('runWrap reports replay_hosted=false and keeps replay_url for compatibility', async () => {
        const { runWrap, isReplayHosted } = require('../lib/wrap-engine');
        const origHome = process.env.HOME;
        process.env.HOME = home;
        try {
            const result = await runWrap(['echo', 'funnel-test'], { cwd: repo.dir });
            assert.equal(typeof result.replay_url, 'string', 'replay_url stays in the JSON contract');
            assert.equal(result.replay_hosted, false, 'nothing uploads today, so the URL must not be presented as resolving');
            assert.equal(isReplayHosted(), false);
        } finally {
            process.env.HOME = origHome;
        }
    });

    it('CLI prints the attestation path and a not-uploaded note, never the delimit.ai/att URL, on the free tier', { skip: SKIP_IN_CI }, () => {
        const r = runCli(['wrap', '--', 'echo', 'funnel-cli'], { cwd: repo.dir, home });
        assert.equal(r.status, 0, r.out);
        assert.match(r.stdout, /attestation:\s+\S+\.json/, 'local attestation path is the receipt');
        assert.doesNotMatch(r.stdout, /delimit\.ai\/att\//, 'hosted replay URL must not be printed (it 404s)');
        assert.match(r.stdout, /not available yet/, 'says hosted replay is not available yet, promising nothing to any tier');
        const replayLine = r.stdout.split('\n').find((l) => /^\s+replay:/.test(l)) || '';
        assert.doesNotMatch(replayLine, /—/, 'no em dashes in the new user-facing string');
    });

    it('--json still carries replay_url plus the replay_hosted flag', { skip: SKIP_IN_CI }, () => {
        const r = runCli(['wrap', '--json', '--', 'echo', 'funnel-json'], { cwd: repo.dir, home });
        assert.equal(r.status, 0, r.out);
        // The wrapped command's own stdout precedes the JSON document.
        const parsed = JSON.parse(r.stdout.slice(r.stdout.indexOf('{')));
        assert.match(parsed.replay_url, /^https:\/\/delimit\.ai\/att\/att_/);
        assert.equal(parsed.replay_hosted, false);
    });
});

// ---------------------------------------------------------------------------
// 2. scan / init: never prompt when non-interactive
// ---------------------------------------------------------------------------

describe('delimit init / scan: non-interactive (closed stdin + CI=1) uses defaults and exits 0', () => {
    let repo;
    let home;
    before(() => {
        repo = makeTmpGitRepo({ prefix: 'delimit-funnel-init-' });
        home = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-funnel-init-home-'));
        fs.copyFileSync(PETSTORE, path.join(repo.dir, 'openapi.yaml'));
    });
    after(() => { repo.cleanup(); try { fs.rmSync(home, { recursive: true, force: true }); } catch {} });

    it('init writes .delimit/policies.yml without --yes and never asks "Join the beta"', { skip: SKIP_IN_CI }, () => {
        const r = runCli(['init', '--preset', 'default'], { cwd: repo.dir, home });
        assert.equal(r.status, 0, r.out);
        assert.ok(fs.existsSync(path.join(repo.dir, '.delimit', 'policies.yml')), 'policies.yml must be written');
        assert.doesNotMatch(r.out, /Join the beta/, 'beta email prompt must be skipped silently');
        assert.doesNotMatch(r.out, /Compliance template/, 'compliance prompt must be skipped');
        assert.match(r.stdout, /Created \.delimit\/policies\.yml \(default\)/);
    });

    it('init with no preset flag also completes on the documented default', { skip: SKIP_IN_CI }, () => {
        const other = makeTmpGitRepo({ prefix: 'delimit-funnel-init2-' });
        try {
            fs.copyFileSync(PETSTORE, path.join(other.dir, 'openapi.yaml'));
            const r = runCli(['init'], { cwd: other.dir, home });
            assert.equal(r.status, 0, r.out);
            assert.ok(fs.existsSync(path.join(other.dir, '.delimit', 'policies.yml')));
            assert.match(r.stdout, /\(default\)/);
        } finally {
            other.cleanup();
        }
    });

    it('scan of a project directory exits 0 and does not open the "What next?" picker', { skip: SKIP_IN_CI }, () => {
        const r = runCli(['scan'], { cwd: repo.dir, home, timeout: 90000 });
        assert.equal(r.status, 0, `scan must not exit 130/non-zero on closed stdin:\n${r.out}`);
        assert.doesNotMatch(r.out, /What next\?/);
        assert.match(r.stdout, /Governance Readiness/);
    });

    it('scan of a spec file exits 0 and prints the next-step hint instead of prompting', { skip: SKIP_IN_CI }, () => {
        const r = runCli(['scan', 'openapi.yaml'], { cwd: repo.dir, home, timeout: 90000 });
        assert.equal(r.status, 0, r.out);
        assert.doesNotMatch(r.out, /What next\?/);
    });

    it('init honours DELIMIT_NON_INTERACTIVE even without CI', { skip: SKIP_IN_CI }, () => {
        const other = makeTmpGitRepo({ prefix: 'delimit-funnel-init3-' });
        try {
            const r = runCli(['init'], { cwd: other.dir, home, env: { CI: '', DELIMIT_NON_INTERACTIVE: '1' } });
            assert.equal(r.status, 0, r.out);
            assert.ok(fs.existsSync(path.join(other.dir, '.delimit', 'policies.yml')));
            assert.doesNotMatch(r.out, /Join the beta/);
        } finally {
            other.cleanup();
        }
    });

    it('setup and the demo/quickstart/try beta prompts route through the shared helper (source pin)', () => {
        const setupSrc = fs.readFileSync(path.join(__dirname, '..', 'bin', 'delimit-setup.js'), 'utf-8');
        assert.doesNotMatch(setupSrc, /process\.stdin\.isTTY/, 'setup must use isInteractive(), not a bare isTTY check');
        assert.match(setupSrc, /require\('\.\.\/lib\/interactive'\)/);
        const betaSites = CLI_SRC.split('Join the beta?').length - 1;
        assert.ok(betaSites >= 4, 'expected the four beta prompt sites to still exist');
        assert.equal((CLI_SRC.match(/if \(isInteractive\(\)\) try \{/g) || []).length, 3, 'demo/quickstart/try beta prompts gated');
    });
});

// ---------------------------------------------------------------------------
// 3. doctor: version comes from package.json
// ---------------------------------------------------------------------------

describe('delimit doctor: banner version is derived from package.json', () => {
    it('prints the real CLI version and no hardcoded v4.20', { skip: SKIP_IN_CI }, () => {
        const home = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-funnel-doctor-home-'));
        const repo = makeTmpGitRepo({ prefix: 'delimit-funnel-doctor-' });
        try {
            const r = runCli(['doctor'], { cwd: repo.dir, home, timeout: 90000 });
            assert.ok(r.stdout.includes(`Delimit Doctor v${PKG_VERSION}`), `expected v${PKG_VERSION} in:\n${r.out}`);
            if (PKG_VERSION !== '4.20') assert.doesNotMatch(r.stdout, /Doctor v4\.20\b/);
        } finally {
            repo.cleanup();
            try { fs.rmSync(home, { recursive: true, force: true }); } catch {}
        }
    });

    it('source no longer carries a literal doctor version', () => {
        assert.doesNotMatch(CLI_SRC, /Delimit Doctor v\d/);
        assert.match(CLI_SRC, /Delimit Doctor v\$\{CLI_VERSION\}/);
    });
});

// ---------------------------------------------------------------------------
// 4. check: no raw git stderr in a repo with no commits
// ---------------------------------------------------------------------------

describe('delimit check: repo with no commits', () => {
    it('prints one friendly line and never leaks "fatal: ambiguous argument"', { skip: SKIP_IN_CI }, () => {
        const repo = makeTmpGitRepo({ prefix: 'delimit-funnel-check-', commit: false });
        const home = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-funnel-check-home-'));
        try {
            fs.copyFileSync(PETSTORE, path.join(repo.dir, 'openapi.yaml'));
            const r = runCli(['check'], { cwd: repo.dir, home });
            assert.equal(r.status, 0, r.out);
            assert.doesNotMatch(r.out, /fatal:/, 'raw git stderr must not reach the user');
            assert.doesNotMatch(r.out, /ambiguous argument/);
            assert.match(r.stdout, /No commits yet/);
            assert.match(r.stdout, /openapi\.yaml/, 'working-tree spec is still examined');
        } finally {
            repo.cleanup();
            try { fs.rmSync(home, { recursive: true, force: true }); } catch {}
        }
    });

    it('still diffs against HEAD in a repo that has commits', { skip: SKIP_IN_CI }, () => {
        const repo = makeTmpGitRepo({ prefix: 'delimit-funnel-check2-' });
        const home = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-funnel-check2-home-'));
        try {
            fs.copyFileSync(PETSTORE, path.join(repo.dir, 'openapi.yaml'));
            repo.run('git add . && git commit -qm spec');
            const r = runCli(['check'], { cwd: repo.dir, home });
            assert.doesNotMatch(r.out, /fatal:/);
            assert.doesNotMatch(r.stdout, /No commits yet/);
        } finally {
            repo.cleanup();
            try { fs.rmSync(home, { recursive: true, force: true }); } catch {}
        }
    });
});

// ---------------------------------------------------------------------------
// 5. deliberate / think helpers (PR #195 review fast-follows)
// ---------------------------------------------------------------------------

describe('delimit deliberate / think: engine helper fast-follows', () => {
    it('resolver also looks for venv/Scripts/python.exe (the .pyd match implies Windows)', () => {
        const start = CLI_SRC.indexOf('function resolveDeliberationEngine(');
        const body = CLI_SRC.slice(start, start + 2500);
        assert.match(body, /'Scripts', 'python\.exe'/);
        assert.match(body, /'bin', 'python'/);
        assert.match(body, /venv:/, 'engine object records whether the venv interpreter was found');
    });

    it('PYTHONPATH is prepended to, not overwritten', () => {
        const start = CLI_SRC.indexOf('function withEnginePythonPath(');
        assert.ok(start > 0, 'withEnginePythonPath helper missing');
        const body = CLI_SRC.slice(start, start + 600);
        assert.match(body, /process\.env\.PYTHONPATH/);
        assert.match(body, /path\.delimiter/);
        const run = CLI_SRC.slice(CLI_SRC.indexOf('function runEngineDeliberation('), CLI_SRC.indexOf('function renderDeliberationOutcome('));
        assert.match(run, /PYTHONPATH: withEnginePythonPath\(engine\.serverDir\)/);
    });

    it('error tail hints at `delimit setup` when the PATH python fallback was used', () => {
        const run = CLI_SRC.slice(CLI_SRC.indexOf('function runEngineDeliberation('), CLI_SRC.indexOf('function renderDeliberationOutcome('));
        assert.match(run, /if \(!engine\.venv\)/);
        assert.match(run, /delimit setup/);
    });

    it('`delimit think` routes through the shared engine helpers', () => {
        const handler = handlerSource('think [question...]');
        assert.doesNotMatch(handler, /python3 -c/, 'no inline python3 shell call');
        assert.doesNotMatch(handler, /parsed\.synthesis|parsed\.confidence/, 'no keys the engine does not return');
        assert.match(handler, /resolveDeliberationEngine\(\)/);
        assert.match(handler, /runEngineDeliberation\(/);
        assert.match(handler, /renderDeliberationOutcome\(/);
    });

    it('think + deliberate run the venv interpreter with PYTHONPATH prepended (stub engine)', { skip: SKIP_IN_CI || process.platform === 'win32' }, () => {
        const py = spawnSync('python3', ['--version'], { encoding: 'utf-8' });
        if (py.status !== 0) return; // no python3 on this box: nothing to exercise
        const home = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-funnel-think-home-'));
        try {
            const dh = path.join(home, '.delimit');
            const aiDir = path.join(dh, 'server', 'ai');
            fs.mkdirSync(aiDir, { recursive: true });
            fs.writeFileSync(path.join(aiDir, '__init__.py'), '');
            fs.writeFileSync(path.join(aiDir, 'deliberation.py'), [
                'def deliberate(question, mode="dialogue", max_rounds=2, save_path=None):',
                '    return {"final_verdict": "STUB VERDICT", "rounds": [1], "models_used": ["stub"], "summary": "stub summary for " + question}',
                '',
            ].join('\n'));
            // Fake venv python: records PYTHONPATH then delegates to the real interpreter.
            const venvBin = path.join(dh, 'venv', 'bin');
            fs.mkdirSync(venvBin, { recursive: true });
            const recorder = path.join(home, 'pythonpath.txt');
            const fakePython = path.join(venvBin, 'python');
            fs.writeFileSync(fakePython, `#!/bin/sh\nprintf '%s' "$PYTHONPATH" > "${recorder}"\nexec python3 "$@"\n`);
            fs.chmodSync(fakePython, 0o755);

            const preexisting = '/opt/user/site-packages';
            const r = runCli(['think', 'Is the stub engine wired?'], { cwd: home, home, env: { PYTHONPATH: preexisting } });
            assert.equal(r.status, 0, r.out);
            assert.match(r.stdout, /Verdict: STUB VERDICT/, 'renders the engine\'s real verdict key');
            assert.match(r.stdout, /stub summary for Is the stub engine wired\?/);
            assert.ok(fs.existsSync(recorder), 'the venv interpreter (not PATH python3) must have been used');
            const recorded = fs.readFileSync(recorder, 'utf-8');
            const serverDir = path.join(dh, 'server');
            assert.equal(recorded, `${serverDir}${path.delimiter}${preexisting}`, 'engine dir prepended to the existing PYTHONPATH');

            fs.unlinkSync(recorder);
            const d = runCli(['deliberate', 'Is the stub engine wired?'], { cwd: home, home, env: { PYTHONPATH: preexisting } });
            assert.equal(d.status, 0, d.out);
            assert.match(d.stdout, /Verdict: STUB VERDICT/);
            assert.equal(fs.readFileSync(recorder, 'utf-8'), `${serverDir}${path.delimiter}${preexisting}`);
        } finally {
            try { fs.rmSync(home, { recursive: true, force: true }); } catch {}
        }
    });
});
