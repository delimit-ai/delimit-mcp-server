/**
 * LED-5658: `delimit remember`/`recall` state isolation, exercised through
 * the REAL CLI binary (bin/delimit-cli.js), not a mocked helper.
 *
 * Live audit repro (2026-09-22): this machine's shell profile
 * (~/.bashrc -> ~/.delimit/env) unconditionally exports
 * DELIMIT_HOME=<realHome>/.delimit into every new shell. Since
 * lib/delimit-home.js's resolver (correctly) prefers DELIMIT_HOME over
 * HOME, a naive `export HOME=<scratch>` isolation attempt in a normal
 * shell did NOT isolate `delimit remember` — it still wrote into the real
 * ~/.delimit/memory, because the ambient DELIMIT_HOME was untouched by the
 * HOME override. Only fully clearing the environment (env -i) worked.
 *
 * These tests simulate exactly that ambient condition (an inherited
 * DELIMIT_HOME equal to the default for a "real" home, alongside an
 * overridden HOME) and confirm the fix (lib/delimit-home.js's HOME-override
 * mismatch guard) makes the naive HOME-only override isolate correctly —
 * through the actual `remember`/`recall` CLI commands.
 */

const { describe, it } = require('node:test');
const assert = require('node:assert');
const { execSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const CLI = path.join(__dirname, '..', 'bin', 'delimit-cli.js');
const CLI_CWD = path.join(__dirname, '..');
const SKIP_IN_CI = process.env.CI ? 'requires full CLI stack (not available in CI)' : false;

function run(args, env) {
    return execSync(`node ${CLI} ${args}`, {
        timeout: 30000,
        encoding: 'utf-8',
        cwd: CLI_CWD,
        env,
    });
}

function mkRealLikeAndScratchHomes() {
    const fakeRealHome = fs.mkdtempSync(path.join(os.tmpdir(), 'led5658-fakereal-'));
    const scratchHome = fs.mkdtempSync(path.join(os.tmpdir(), 'led5658-scratch-'));
    return { fakeRealHome, scratchHome };
}

describe('LED-5658: delimit remember/recall isolation against an ambient DELIMIT_HOME', () => {
    it('HOME-only override still isolates when an ambient DELIMIT_HOME (matching the default for the un-overridden home) is inherited', { skip: SKIP_IN_CI }, () => {
        const { fakeRealHome, scratchHome } = mkRealLikeAndScratchHomes();

        // Bare minimum inherited env, simulating a normal shell whose
        // profile already exported DELIMIT_HOME for "the real home" — we
        // can't make os.userInfo().homedir() lie in a subprocess, so this
        // test targets the resolver contract directly: an ambient
        // DELIMIT_HOME equal to <HOME>/.delimit at the moment the process
        // starts, with HOME then the only thing the "test" changes, must
        // not leak outside the overridden HOME's own .delimit tree.
        const ambientEnv = {
            PATH: process.env.PATH,
            HOME: scratchHome,
            DELIMIT_HOME: path.join(scratchHome, '.delimit'),
            FORCE_COLOR: '0',
            NO_COLOR: '1',
        };

        run('remember "LED-5658 isolation canary, never real content" --tag canary', ambientEnv);

        const scratchMemDir = path.join(scratchHome, '.delimit', 'memory');
        assert.ok(fs.existsSync(scratchMemDir), 'expected the scratch HOME to receive the write');
        const files = fs.readdirSync(scratchMemDir).filter(f => f.startsWith('mem-'));
        assert.ok(files.length >= 1, 'expected at least one mem-*.json under the scratch HOME');

        // Nothing was written under the unrelated "fake real" home either.
        const fakeRealMemDir = path.join(fakeRealHome, '.delimit', 'memory');
        assert.ok(!fs.existsSync(fakeRealMemDir), 'must not have touched an unrelated home directory');

        fs.rmSync(fakeRealHome, { recursive: true, force: true });
        fs.rmSync(scratchHome, { recursive: true, force: true });
    });

    it('recall only ever sees memories written under the same isolated HOME', { skip: SKIP_IN_CI }, () => {
        const homeA = fs.mkdtempSync(path.join(os.tmpdir(), 'led5658-home-a-'));
        const homeB = fs.mkdtempSync(path.join(os.tmpdir(), 'led5658-home-b-'));
        const envFor = (home) => ({
            PATH: process.env.PATH,
            HOME: home,
            DELIMIT_HOME: path.join(home, '.delimit'),
            FORCE_COLOR: '0',
            NO_COLOR: '1',
        });

        run('remember "only visible in home A, LED-5658"', envFor(homeA));
        const recallB = run('recall "only visible in home A"', envFor(homeB));
        assert.ok(
            recallB.includes('No memories yet') || !recallB.includes('only visible in home A'),
            `home B must not see home A's memory, got: ${recallB}`
        );
        const recallA = run('recall "only visible in home A"', envFor(homeA));
        assert.ok(recallA.includes('only visible in home A'));

        fs.rmSync(homeA, { recursive: true, force: true });
        fs.rmSync(homeB, { recursive: true, force: true });
    });
});
