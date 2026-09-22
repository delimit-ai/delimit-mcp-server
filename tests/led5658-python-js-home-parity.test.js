/**
 * LED-5658 review follow-up: cross-language parity test.
 *
 * The ambient-DELIMIT_HOME mismatch guard was implemented independently in
 * two languages: lib/delimit-home.js (delimitHome()) for the npm CLI, and
 * ai/continuity.py (_resolve_delimit_home()/get_namespace_root()) for the
 * Python gateway — bundled into this repo at gateway/ai/continuity.py and
 * kept byte-identical to the canonical delimit-gateway repo's copy. This
 * test proves both implementations resolve the SAME environment to the
 * SAME store for the four cases the review named:
 *
 *   1. default                                    — no overrides at all
 *   2. HOME override + ambient default DELIMIT_HOME — the LED-5658 trap
 *   3. HOME override + a deliberate, distinct DELIMIT_HOME — honored as-is
 *   4. sudo-like: HOME equals the passwd home of a DIFFERENT real user
 *      (i.e. the OS-level home genuinely tracks HOME, not just an env
 *      var) — must NOT be treated as an override
 *
 * Every case uses _DELIMIT_TEST_ONLY_REAL_HOME (the identical test-only
 * hook added to both lib/delimit-home.js and ai/continuity.py) instead of
 * the actual OS passwd database, so this test is fully deterministic,
 * portable, and never depends on — or risks touching — whatever the real
 * account running it happens to be.
 */

const { describe, it } = require('node:test');
const assert = require('node:assert');
const path = require('path');
const { execFileSync } = require('child_process');

const REPO_ROOT = path.join(__dirname, '..');
const GATEWAY_DIR = path.join(REPO_ROOT, 'gateway');

// Clear the require cache entry so each case's env is picked up fresh —
// delimitHome() re-resolves per call anyway (no module-level caching), so
// this isn't strictly required, but keeps the test explicit about intent.
function jsDelimitHome(env) {
    const savedEnv = { ...process.env };
    try {
        for (const k of ['HOME', 'DELIMIT_HOME', 'DELIMIT_NAMESPACE_ROOT', '_DELIMIT_TEST_ONLY_REAL_HOME']) {
            delete process.env[k];
        }
        Object.assign(process.env, env);
        delete require.cache[require.resolve('../lib/delimit-home')];
        const { delimitHome } = require('../lib/delimit-home');
        return delimitHome();
    } finally {
        for (const k of ['HOME', 'DELIMIT_HOME', 'DELIMIT_NAMESPACE_ROOT', '_DELIMIT_TEST_ONLY_REAL_HOME']) {
            delete process.env[k];
        }
        Object.assign(process.env, savedEnv);
    }
}

function pyGetNamespaceRoot(env) {
    const fullEnv = { PATH: process.env.PATH, PYTHONPATH: GATEWAY_DIR, ...env };
    const out = execFileSync(
        'python3',
        ['-c', 'from ai.continuity import get_namespace_root; print(get_namespace_root())'],
        { encoding: 'utf-8', env: fullEnv, timeout: 15000 }
    );
    return out.trim();
}

describe('LED-5658: Python <-> JS DELIMIT_HOME resolution parity (4 cases)', () => {
    const REAL = '/tmp/led5658-parity-real-home';
    const SCRATCH = '/tmp/led5658-parity-scratch-home';
    const CUSTOM = '/tmp/led5658-parity-custom-namespace';
    const OTHER_USER_HOME = '/tmp/led5658-parity-otheruser-home';

    it('case 1: default — no overrides at all', () => {
        const env = { HOME: REAL, _DELIMIT_TEST_ONLY_REAL_HOME: REAL };
        const js = jsDelimitHome(env);
        const py = pyGetNamespaceRoot(env);
        const expected = path.join(REAL, '.delimit');
        assert.strictEqual(js, expected, `JS case 1: got ${js}`);
        assert.strictEqual(py, expected, `Python case 1: got ${py}`);
        assert.strictEqual(js, py, 'JS and Python must agree');
    });

    it('case 2: HOME override + ambient default DELIMIT_HOME (the LED-5658 trap) — both rewrite to the overridden HOME', () => {
        const env = {
            HOME: SCRATCH,
            DELIMIT_HOME: path.join(REAL, '.delimit'), // the untouched ambient default
            _DELIMIT_TEST_ONLY_REAL_HOME: REAL,
        };
        const js = jsDelimitHome(env);
        const py = pyGetNamespaceRoot(env);
        const expected = path.join(SCRATCH, '.delimit');
        assert.strictEqual(js, expected, `JS case 2: got ${js} (must isolate to SCRATCH, not leak to REAL)`);
        assert.strictEqual(py, expected, `Python case 2: got ${py} (must isolate to SCRATCH, not leak to REAL)`);
        assert.strictEqual(js, py, 'JS and Python must agree');
    });

    it('case 3: HOME override + a deliberate, distinct DELIMIT_HOME — both honor it as-is', () => {
        const env = {
            HOME: SCRATCH,
            DELIMIT_HOME: CUSTOM,
            _DELIMIT_TEST_ONLY_REAL_HOME: REAL,
        };
        const js = jsDelimitHome(env);
        const py = pyGetNamespaceRoot(env);
        assert.strictEqual(js, CUSTOM, `JS case 3: got ${js}`);
        assert.strictEqual(py, CUSTOM, `Python case 3: got ${py}`);
        assert.strictEqual(js, py, 'JS and Python must agree');
    });

    it('case 4: sudo-like — HOME equals the passwd home of a DIFFERENT real user — neither rewrites', () => {
        // The OS-level "real home" genuinely tracks HOME here (as it would
        // under a real su/sudo -u), so this must NOT look like an override.
        const env = {
            HOME: OTHER_USER_HOME,
            DELIMIT_HOME: path.join(OTHER_USER_HOME, '.delimit'),
            _DELIMIT_TEST_ONLY_REAL_HOME: OTHER_USER_HOME,
        };
        const js = jsDelimitHome(env);
        const py = pyGetNamespaceRoot(env);
        const expected = path.join(OTHER_USER_HOME, '.delimit');
        assert.strictEqual(js, expected, `JS case 4: got ${js}`);
        assert.strictEqual(py, expected, `Python case 4: got ${py}`);
        assert.strictEqual(js, py, 'JS and Python must agree');
    });
});
