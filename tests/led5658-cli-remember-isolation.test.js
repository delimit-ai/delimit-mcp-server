/**
 * LED-5658: `delimit remember`/`recall` state isolation, exercised through
 * the REAL CLI binary (bin/delimit-cli.js), not a mocked helper.
 *
 * Live audit repro (2026-09-22): this machine's shell profile
 * (~/.bashrc -> ~/.delimit/env) unconditionally exports a concrete
 * DELIMIT_HOME into every new shell. Since lib/delimit-home.js's resolver
 * correctly prefers DELIMIT_HOME over HOME (LED-1188), a naive
 * `export HOME=<scratch>` isolation attempt in a normal shell did NOT
 * isolate `delimit remember` — it still wrote into the real
 * ~/.delimit/memory, because the ambient DELIMIT_HOME was left untouched
 * by the HOME override. Only fully clearing the environment (env -i,
 * which also drops the profile's DELIMIT_HOME) worked.
 *
 * Review follow-up (2026-09-22, LED-5658 no-consensus review): the first
 * version of this test pre-set DELIMIT_HOME to the SCRATCH path (i.e. the
 * value the fix was already going to produce), so it never actually
 * exercised the ambient-conflict scenario and would have passed on
 * pre-fix code too — not a real regression test. This version sets
 * DELIMIT_HOME to the TRUE ambient default (what the real home's own
 * profile would export) while HOME is overridden, and includes a
 * PRE-FIX MUTATION run proving the exact same scenario leaks under the
 * old resolver.
 *
 * Safety: neither the positive nor the mutation run ever risks the
 * actual machine's real home (e.g. /root on the box this was audited on).
 * _DELIMIT_TEST_ONLY_REAL_HOME — a narrowly-named, test-only hook added to
 * lib/delimit-home.js's _realHome() — substitutes a disposable tmp dir
 * for "the real home" the mismatch guard compares against, so even a
 * regression writes into that disposable dir, never into real production
 * state. The mutation run uses a throwaway copy of bin/+lib/ with only
 * lib/delimit-home.js swapped back to its pre-LED-5658 content (the
 * *only* file that carries the resolution logic — lib/continuity-resolver.js
 * and bin/delimit-cli.js's LED-5658 changes are pure re-resolution/wiring
 * around delimitHome()/homeSubpath(), so reverting just this one file
 * reproduces the full pre-fix chain end to end).
 */

const { describe, it, before, after } = require('node:test');
const assert = require('node:assert');
const { execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const REPO_ROOT = path.join(__dirname, '..');
const SKIP_IN_CI = process.env.CI ? 'requires full CLI stack (not available in CI)' : false;

function run(cliPath, argv, env) {
    return execFileSync('node', [cliPath, ...argv], {
        timeout: 30000,
        encoding: 'utf-8',
        env,
    });
}

// The exact pre-LED-5658 content of lib/delimit-home.js (hand-copied, not
// fetched from git history at test time, so this test never depends on
// origin/main being fetchable in a shallow CI checkout).
const PRE_FIX_DELIMIT_HOME_JS = `
const os = require('os');
const path = require('path');

function delimitHome() {
    const fromEnv = process.env.DELIMIT_HOME || process.env.DELIMIT_NAMESPACE_ROOT;
    if (fromEnv && fromEnv.trim()) {
        return fromEnv;
    }
    return path.join(os.homedir(), '.delimit');
}

function homeSubpath(...segments) {
    return path.join(delimitHome(), ...segments);
}

module.exports = { delimitHome, homeSubpath };
`;

function buildPackageCopy(label) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), `led5658-pkg-${label}-`));
    fs.cpSync(path.join(REPO_ROOT, 'bin'), path.join(dir, 'bin'), { recursive: true });
    fs.cpSync(path.join(REPO_ROOT, 'lib'), path.join(dir, 'lib'), { recursive: true });
    fs.cpSync(path.join(REPO_ROOT, 'package.json'), path.join(dir, 'package.json'));
    fs.symlinkSync(path.join(REPO_ROOT, 'node_modules'), path.join(dir, 'node_modules'));
    return dir;
}

describe('LED-5658: delimit remember isolation against the TRUE ambient DELIMIT_HOME default', () => {
    let fixedPkgDir, mutatedPkgDir;

    before(() => {
        fixedPkgDir = buildPackageCopy('fixed');
        mutatedPkgDir = buildPackageCopy('mutated');
        fs.writeFileSync(path.join(mutatedPkgDir, 'lib', 'delimit-home.js'), PRE_FIX_DELIMIT_HOME_JS);
    });

    after(() => {
        fs.rmSync(fixedPkgDir, { recursive: true, force: true });
        fs.rmSync(mutatedPkgDir, { recursive: true, force: true });
    });

    it('FIXED code: HOME override + DELIMIT_HOME = the ambient default for the real home (never pre-set to scratch) still isolates to the overridden HOME', { skip: SKIP_IN_CI }, () => {
        const fakeRealHome = fs.mkdtempSync(path.join(os.tmpdir(), 'led5658-fakereal-'));
        const scratchHome = fs.mkdtempSync(path.join(os.tmpdir(), 'led5658-scratch-'));
        const env = {
            PATH: process.env.PATH,
            HOME: scratchHome,
            // The AMBIENT DEFAULT for the (fake) real home — exactly what
            // this machine's shell profile actually exports on every new
            // shell. Deliberately NOT pre-set to match scratchHome.
            DELIMIT_HOME: path.join(fakeRealHome, '.delimit'),
            // Test-only: makes the mismatch guard compare against
            // fakeRealHome instead of the actual OS user's real home, so
            // this test never touches real production state either way.
            _DELIMIT_TEST_ONLY_REAL_HOME: fakeRealHome,
            FORCE_COLOR: '0',
            NO_COLOR: '1',
        };

        run(path.join(fixedPkgDir, 'bin', 'delimit-cli.js'), ['remember', 'LED-5658-cli-repro-canary'], env);

        const scratchMemDir = path.join(scratchHome, '.delimit', 'memory');
        const fakeRealMemDir = path.join(fakeRealHome, '.delimit', 'memory');
        assert.ok(fs.existsSync(scratchMemDir), 'fixed code must isolate to the overridden HOME');
        assert.ok(!fs.existsSync(fakeRealMemDir), 'fixed code must NOT write to the ambient DELIMIT_HOME default');

        fs.rmSync(fakeRealHome, { recursive: true, force: true });
        fs.rmSync(scratchHome, { recursive: true, force: true });
    });

    it('PRE-FIX code (mutation): the identical scenario leaks into the ambient default — proves this is a real regression test, not vacuous', { skip: SKIP_IN_CI }, () => {
        const fakeRealHome = fs.mkdtempSync(path.join(os.tmpdir(), 'led5658-fakereal-mut-'));
        const scratchHome = fs.mkdtempSync(path.join(os.tmpdir(), 'led5658-scratch-mut-'));
        const env = {
            PATH: process.env.PATH,
            HOME: scratchHome,
            DELIMIT_HOME: path.join(fakeRealHome, '.delimit'),
            // _DELIMIT_TEST_ONLY_REAL_HOME is irrelevant here: the pre-fix
            // delimit-home.js has no mismatch guard, no _realHome(), and
            // never reads this var — that IS the defect being reproduced.
            FORCE_COLOR: '0',
            NO_COLOR: '1',
        };

        run(path.join(mutatedPkgDir, 'bin', 'delimit-cli.js'), ['remember', 'LED-5658-cli-repro-canary'], env);

        const scratchMemDir = path.join(scratchHome, '.delimit', 'memory');
        const fakeRealMemDir = path.join(fakeRealHome, '.delimit', 'memory');
        assert.ok(
            !fs.existsSync(scratchMemDir),
            'MUTATION sanity: pre-fix code must NOT isolate to the overridden HOME'
        );
        assert.ok(
            fs.existsSync(fakeRealMemDir),
            'MUTATION sanity: pre-fix code must leak into the ambient DELIMIT_HOME default — ' +
            'this IS the LED-5658 defect, reproduced end-to-end through the real CLI binary'
        );

        fs.rmSync(fakeRealHome, { recursive: true, force: true });
        fs.rmSync(scratchHome, { recursive: true, force: true });
    });
});

describe('LED-5658: recall only ever sees memories written under the same isolated HOME', () => {
    it('two consistently-configured (HOME === DELIMIT_HOME default) homes never share state', { skip: SKIP_IN_CI }, () => {
        const homeA = fs.mkdtempSync(path.join(os.tmpdir(), 'led5658-home-a-'));
        const homeB = fs.mkdtempSync(path.join(os.tmpdir(), 'led5658-home-b-'));
        const envFor = (home) => ({
            PATH: process.env.PATH,
            HOME: home,
            DELIMIT_HOME: path.join(home, '.delimit'),
            FORCE_COLOR: '0',
            NO_COLOR: '1',
        });

        run(path.join(REPO_ROOT, 'bin', 'delimit-cli.js'), ['remember', 'only visible in home A, LED-5658'], envFor(homeA));
        const recallB = run(path.join(REPO_ROOT, 'bin', 'delimit-cli.js'), ['recall', 'only visible in home A'], envFor(homeB));
        assert.ok(
            recallB.includes('No memories yet') || !recallB.includes('only visible in home A'),
            `home B must not see home A's memory, got: ${recallB}`
        );
        const recallA = run(path.join(REPO_ROOT, 'bin', 'delimit-cli.js'), ['recall', 'only visible in home A'], envFor(homeA));
        assert.ok(recallA.includes('only visible in home A'));

        fs.rmSync(homeA, { recursive: true, force: true });
        fs.rmSync(homeB, { recursive: true, force: true });
    });
});
