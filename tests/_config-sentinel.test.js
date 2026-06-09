/**
 * LED-1716: package .git/config sentinel guard (in-suite signal).
 *
 * Durable regression catch for the config-pollution bug: tests that run
 * `git config` / `git init` without a hermetic cwd+env have repeatedly
 * written `core.bare = true` and `user.email = test@delimit.test` into
 * THIS package checkout's real .git/config, corrupting it (bare repo =>
 * broken work-tree ops; junk commit identity).
 *
 * This file snapshots the package checkout's shared git config
 * (core.bare, user.email, user.name) as this suite starts, asserts the
 * baseline is clean, and asserts at the end of THIS suite that it did not
 * mutate. Because `node --test` scopes `after` to the file, the
 * authoritative whole-run bracket lives in scripts/test-with-config-guard.js
 * (the `npm test` entrypoint); this in-suite check is a fast in-band signal
 * and is listed FIRST in the file list so its baseline is captured early.
 *
 * Mirrors the gateway conftest .git/config sentinel pattern (cf9dfa1), in node.
 */

const { describe, it, before, after } = require('node:test');
const assert = require('node:assert');
const { resolveSharedConfigPath, snapshotSentinel } = require('./_config-sentinel-lib');

const CONFIG_PATH = resolveSharedConfigPath();
let baseline = null;

describe('LED-1716 package .git/config sentinel', () => {
    before(() => {
        baseline = snapshotSentinel(CONFIG_PATH);
    });

    it('captured a clean baseline of the package git config', () => {
        if (!CONFIG_PATH) {
            // Running outside a git checkout (e.g. extracted npm tarball) — nothing to guard.
            assert.ok(true, 'no package .git/config present; sentinel inert');
            return;
        }
        assert.ok(baseline.present, 'baseline snapshot must exist');
        assert.notEqual(
            baseline.bare,
            'true',
            `package checkout is already bare at suite start: ${CONFIG_PATH}`,
        );
        if (baseline.email) {
            assert.ok(
                !/^test@/.test(baseline.email),
                `package checkout already has a test identity at suite start: ${baseline.email}`,
            );
        }
    });

    after(() => {
        if (!CONFIG_PATH) return;
        const final = snapshotSentinel(CONFIG_PATH);
        assert.notEqual(
            final.bare,
            'true',
            `A test mutated the package .git/config: core.bare became 'true' (${CONFIG_PATH}). ` +
            `A git/git-config subprocess ran without a hermetic cwd+env. Use tests/_git-hermetic.js.`,
        );
        assert.strictEqual(
            final.email,
            baseline.email,
            `A test mutated the package .git/config user.email: '${baseline.email}' -> '${final.email}'. ` +
            `Use tests/_git-hermetic.js.`,
        );
        assert.strictEqual(
            final.name,
            baseline.name,
            `A test mutated the package .git/config user.name: '${baseline.name}' -> '${final.name}'. ` +
            `Use tests/_git-hermetic.js.`,
        );
    });
});
