/**
 * LED-1188: regression tests for lib/delimit-home.js — single source of
 * truth for resolving the Delimit private-state directory.
 *
 * Locks the contract that:
 *   - $DELIMIT_HOME wins when set
 *   - $DELIMIT_NAMESPACE_ROOT is honored as gateway-compat fallback
 *   - $DELIMIT_HOME beats $DELIMIT_NAMESPACE_ROOT when both are set
 *   - default falls back to <homedir>/.delimit
 *   - homeSubpath() composes correctly under the resolved root
 *   - resolution is re-evaluated on every call (no cached state)
 */

const { describe, it, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert');
const os = require('os');
const path = require('path');

const { delimitHome, homeSubpath } = require('../lib/delimit-home');

const ORIG_DELIMIT_HOME = process.env.DELIMIT_HOME;
const ORIG_NAMESPACE_ROOT = process.env.DELIMIT_NAMESPACE_ROOT;

function clearEnv() {
    delete process.env.DELIMIT_HOME;
    delete process.env.DELIMIT_NAMESPACE_ROOT;
}

function restoreEnv() {
    if (ORIG_DELIMIT_HOME === undefined) delete process.env.DELIMIT_HOME;
    else process.env.DELIMIT_HOME = ORIG_DELIMIT_HOME;
    if (ORIG_NAMESPACE_ROOT === undefined) delete process.env.DELIMIT_NAMESPACE_ROOT;
    else process.env.DELIMIT_NAMESPACE_ROOT = ORIG_NAMESPACE_ROOT;
}

describe('lib/delimit-home: env-var unification', () => {
    beforeEach(clearEnv);
    afterEach(restoreEnv);

    it('defaults to <homedir>/.delimit when no env vars are set', () => {
        assert.equal(delimitHome(), path.join(os.homedir(), '.delimit'));
    });

    it('honors DELIMIT_HOME when set', () => {
        process.env.DELIMIT_HOME = '/tmp/test-delimit-home-1188';
        assert.equal(delimitHome(), '/tmp/test-delimit-home-1188');
    });

    it('honors DELIMIT_NAMESPACE_ROOT (gateway-compat fallback)', () => {
        process.env.DELIMIT_NAMESPACE_ROOT = '/tmp/test-namespace-root-1188';
        assert.equal(delimitHome(), '/tmp/test-namespace-root-1188');
    });

    it('DELIMIT_HOME wins when both are set', () => {
        process.env.DELIMIT_HOME = '/tmp/test-primary';
        process.env.DELIMIT_NAMESPACE_ROOT = '/tmp/test-secondary';
        assert.equal(delimitHome(), '/tmp/test-primary');
    });

    it('treats whitespace-only env values as unset', () => {
        process.env.DELIMIT_HOME = '   ';
        assert.equal(delimitHome(), path.join(os.homedir(), '.delimit'));
    });

    it('re-evaluates on every call (no module-level caching)', () => {
        process.env.DELIMIT_HOME = '/tmp/first';
        const first = delimitHome();
        process.env.DELIMIT_HOME = '/tmp/second';
        const second = delimitHome();
        assert.notEqual(first, second);
        assert.equal(second, '/tmp/second');
    });
});

// ─────────────────────────────────────────────────────────────────────
// LED-5658: the ambient-DELIMIT_HOME trap.
//
// This machine's own shell profile (~/.bashrc -> ~/.delimit/env)
// unconditionally exports DELIMIT_HOME=<realHome>/.delimit into every new
// shell. Live repro: `export HOME=<scratch>` in a normal (profile-sourcing)
// shell and run `delimit remember` — it still wrote into the REAL
// ~/.delimit/memory, because DELIMIT_HOME (preferred over HOME) was
// already set by the profile and untouched by the HOME override. Only
// `env -i` (which drops the profile-sourced DELIMIT_HOME too) isolated it.
// These tests lock the fix: overriding $HOME is treated as an isolation
// signal, and an ambient DELIMIT_HOME that is exactly the default for the
// REAL (non-overridden) home is untrusted in that case and re-derived from
// the overridden $HOME instead.
// ─────────────────────────────────────────────────────────────────────
describe('lib/delimit-home: LED-5658 ambient-DELIMIT_HOME trap', () => {
    const ORIG_HOME = process.env.HOME;
    const realHome = os.userInfo().homedir;

    beforeEach(clearEnv);
    afterEach(() => {
        restoreEnv();
        if (ORIG_HOME === undefined) delete process.env.HOME;
        else process.env.HOME = ORIG_HOME;
    });

    it('HOME overridden + ambient DELIMIT_HOME (== default for the real home) is re-derived from the overridden HOME', () => {
        process.env.HOME = '/tmp/led5658-scratch-home';
        // Simulate the profile-sourced ambient value: exactly what a normal
        // (non-overridden) shell on this machine would have.
        process.env.DELIMIT_HOME = path.join(realHome, '.delimit');

        assert.equal(delimitHome(), path.join('/tmp/led5658-scratch-home', '.delimit'));
    });

    it('HOME overridden + a genuinely distinct DELIMIT_HOME is still honored (explicit intent, not the trap)', () => {
        process.env.HOME = '/tmp/led5658-scratch-home';
        process.env.DELIMIT_HOME = '/tmp/led5658-custom-namespace';

        assert.equal(delimitHome(), '/tmp/led5658-custom-namespace');
    });

    it('HOME NOT overridden + ambient DELIMIT_HOME: normal owner usage is unaffected', () => {
        // realHome === realHome: no override signal, so the ambient value
        // (matching every pre-existing DELIMIT_HOME-only test above) is
        // trusted exactly as before this fix.
        process.env.HOME = realHome;
        process.env.DELIMIT_HOME = path.join(realHome, '.delimit');

        assert.equal(delimitHome(), path.join(realHome, '.delimit'));
    });

    it('MUTATION: the pre-fix resolver would leak into the ambient (real) store despite the HOME override', () => {
        // Reproduce the exact pre-fix delimitHome() body: DELIMIT_HOME wins
        // whenever set, full stop — no HOME-override mismatch check.
        function oldDelimitHome() {
            const fromEnv = process.env.DELIMIT_HOME || process.env.DELIMIT_NAMESPACE_ROOT;
            if (fromEnv && fromEnv.trim()) {
                return fromEnv;
            }
            return path.join(os.homedir(), '.delimit');
        }

        process.env.HOME = '/tmp/led5658-scratch-home';
        process.env.DELIMIT_HOME = path.join(realHome, '.delimit');

        // The bug, reproduced: an isolation attempt (HOME overridden) still
        // resolves to the REAL owner store under the old logic.
        assert.equal(oldDelimitHome(), path.join(realHome, '.delimit'));
        assert.notEqual(
            oldDelimitHome(), path.join('/tmp/led5658-scratch-home', '.delimit'),
            'MUTATION test did not reproduce the pre-fix leak'
        );

        // The fixed resolver, same inputs, correctly isolates instead.
        assert.equal(delimitHome(), path.join('/tmp/led5658-scratch-home', '.delimit'));
    });
});

describe('lib/delimit-home: homeSubpath composition', () => {
    beforeEach(clearEnv);
    afterEach(restoreEnv);

    it('composes one segment under the resolved home', () => {
        process.env.DELIMIT_HOME = '/tmp/sub';
        assert.equal(homeSubpath('ledger'), '/tmp/sub/ledger');
    });

    it('composes multiple segments', () => {
        process.env.DELIMIT_HOME = '/tmp/sub';
        assert.equal(homeSubpath('ledger', 'delimit', 'operations.jsonl'),
            '/tmp/sub/ledger/delimit/operations.jsonl');
    });

    it('returns the home itself when no segments are passed', () => {
        process.env.DELIMIT_HOME = '/tmp/sub';
        assert.equal(homeSubpath(), '/tmp/sub');
    });
});
