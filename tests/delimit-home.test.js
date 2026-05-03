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
