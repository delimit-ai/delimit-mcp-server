#!/usr/bin/env node
/**
 * LED-1716: test runner wrapper that brackets the ENTIRE `node --test` run
 * with a package .git/config sentinel.
 *
 * Snapshots core.bare + user.email + user.name from the package checkout's
 * shared git config BEFORE the suite, runs the suite, then re-snapshots and
 * FAILS (non-zero exit) if any test mutated those fields. This is the
 * durable regression catch for the recurring config-pollution bug
 * (core.bare=true / user.email=test@delimit.test written into the real
 * checkout by a cwd-less git/git-config subprocess).
 *
 * Unlike an in-suite `after` hook (which only brackets its own file),
 * this wrapper guarantees the comparison spans every test file regardless
 * of `node --test` ordering. The in-suite sentinel (tests/_config-sentinel.test.js)
 * remains as a fast in-band signal; this wrapper is the authoritative gate.
 */

const { spawnSync } = require('child_process');
const path = require('path');
const { resolveSharedConfigPath, snapshotSentinel } = require('../tests/_config-sentinel-lib');

const REPO_ROOT = path.resolve(__dirname, '..');

// The full file list (kept in sync with the underlying `node --test` invocation).
const TEST_FILES = [
    'tests/_config-sentinel.test.js',
    'tests/setup-onboarding.test.js',
    'tests/setup-matrix.test.js',
    'tests/setup-no-clobber.test.js',
    'tests/config-export-import.test.js',
    'tests/cross-model-hooks.test.js',
    'tests/golden-path.test.js',
    'tests/v420-features.test.js',
    'tests/v43-wrap-engine.test.js',
    'tests/v43-trust-page-engine.test.js',
    'tests/v43-ai-sbom-engine.test.js',
    'tests/attest-mcp.test.js',
    'tests/delimit-home.test.js',
    'tests/postinstall-hardening.test.js',
    'tests/auth-signin.test.js',
    'tests/auth-signout.test.js',
    'tests/migration-2092-banner.test.js',
    'tests/control-browser.test.js',
];

const cfgPath = resolveSharedConfigPath();
const before = snapshotSentinel(cfgPath);

const res = spawnSync('node', ['--test', ...TEST_FILES], {
    cwd: REPO_ROOT,
    stdio: 'inherit',
});

let guardFailed = false;
if (cfgPath) {
    const after = snapshotSentinel(cfgPath);
    const mutated = [];
    if (before.bare !== after.bare) mutated.push(`core.bare: '${before.bare}' -> '${after.bare}'`);
    if (before.email !== after.email) mutated.push(`user.email: '${before.email}' -> '${after.email}'`);
    if (before.name !== after.name) mutated.push(`user.name: '${before.name}' -> '${after.name}'`);
    if (after.bare === 'true') mutated.push('core.bare is true (checkout is now BARE)');
    if (mutated.length) {
        guardFailed = true;
        console.error('\n\x1b[31m[LED-1716 CONFIG GUARD] FAIL — a test mutated the package .git/config:\x1b[0m');
        for (const m of mutated) console.error(`  - ${m}`);
        console.error(`  config: ${cfgPath}`);
        console.error('  A git/git-config subprocess ran without a hermetic cwd+env. Use tests/_git-hermetic.js.\n');
    } else {
        console.error(`\n[LED-1716 CONFIG GUARD] OK — package .git/config unmutated (core.bare=${after.bare}, user.email=${after.email}).`);
    }
} else {
    console.error('\n[LED-1716 CONFIG GUARD] inert — no package .git/config (running outside a checkout).');
}

const testExit = res.status === null ? 1 : res.status;
process.exit(guardFailed ? 1 : testExit);
