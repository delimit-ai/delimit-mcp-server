/**
 * LED-2106: tests for lib/auth-signout.js — removes the OAuth bearer
 * token written by `delimit signin` (LED-2100) without clobbering the
 * legacy bookkeeping keys written by `lib/auth-setup.js`.
 *
 * Locks the contract that:
 *   - signed-in scrub leaves bookkeeping keys (`configured`, `timestamp`,
 *     `tools`) intact
 *   - signed-in scrub removes EXACTLY the OAuth keys (delimit_token,
 *     access_token, signed_in_at, email) and nothing else
 *   - no-op when not signed in (file missing, malformed, or no OAuth keys)
 *   - the file is written atomically (tmp + rename) at mode 0600
 *   - the file is deleted entirely when scrubbing leaves it empty
 *   - the previously-signed-in email is returned to callers for display
 *   - readExistingAuth is consistent with auth-signin.js (object-only)
 */

const { describe, it, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const {
    authFilePath,
    readExistingAuth,
    removeAuthToken,
    AUTH_FILE_BASENAME,
    OAUTH_KEYS,
} = require('../lib/auth-signout');

// Reuse the writer from auth-signin so the round-trip test exercises the
// real signin path rather than a hand-built file.
const { writeAuthToken } = require('../lib/auth-signin');

const ORIG_DELIMIT_HOME = process.env.DELIMIT_HOME;
const ORIG_NAMESPACE_ROOT = process.env.DELIMIT_NAMESPACE_ROOT;

let tmpHome;

function setupTmpHome() {
    tmpHome = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-signout-test-'));
    process.env.DELIMIT_HOME = tmpHome;
    delete process.env.DELIMIT_NAMESPACE_ROOT;
}

function teardownTmpHome() {
    try {
        fs.rmSync(tmpHome, { recursive: true, force: true });
    } catch {}
    if (ORIG_DELIMIT_HOME === undefined) delete process.env.DELIMIT_HOME;
    else process.env.DELIMIT_HOME = ORIG_DELIMIT_HOME;
    if (ORIG_NAMESPACE_ROOT === undefined) delete process.env.DELIMIT_NAMESPACE_ROOT;
    else process.env.DELIMIT_NAMESPACE_ROOT = ORIG_NAMESPACE_ROOT;
}

describe('lib/auth-signout: OAUTH_KEYS contract', () => {
    it('exports the exact OAuth-related keys signin writes', () => {
        // If signin grows a new OAuth key (e.g. refresh_token), signout
        // MUST scrub it too — otherwise sign-out leaves stale state.
        // This test guards against drift between the two files.
        assert.deepEqual(
            [...OAUTH_KEYS].sort(),
            ['access_token', 'delimit_token', 'email', 'signed_in_at']
        );
    });

    it('OAUTH_KEYS is frozen (immutable contract)', () => {
        assert.ok(Object.isFrozen(OAUTH_KEYS));
    });
});

describe('lib/auth-signout: no-op cases', () => {
    beforeEach(setupTmpHome);
    afterEach(teardownTmpHome);

    it('returns changed=false when auth.json does not exist', () => {
        const result = removeAuthToken();
        assert.equal(result.changed, false);
        assert.equal(result.deleted, false);
        assert.equal(result.email, '');
    });

    it('returns changed=false when auth.json is malformed JSON', () => {
        fs.writeFileSync(authFilePath(), '{ this is not valid json');
        const result = removeAuthToken();
        assert.equal(result.changed, false);
        // Malformed file is left untouched (we do not delete user data
        // we cannot parse).
        assert.ok(fs.existsSync(authFilePath()));
    });

    it('returns changed=false when auth.json is a JSON array', () => {
        fs.writeFileSync(authFilePath(), JSON.stringify(['a', 'b']));
        const result = removeAuthToken();
        assert.equal(result.changed, false);
    });

    it('returns changed=false when auth.json has no OAuth keys', () => {
        // Simulate a user who only ran `delimit auth` (legacy bookkeeping)
        // but never `delimit signin` — there is nothing to sign out from.
        const legacy = {
            configured: true,
            timestamp: '2026-01-01T00:00:00.000Z',
            tools: ['github', 'claude'],
        };
        fs.writeFileSync(authFilePath(), JSON.stringify(legacy, null, 2));

        const result = removeAuthToken();
        assert.equal(result.changed, false);
        assert.equal(result.email, '');

        // File is left exactly as we found it.
        const after = JSON.parse(fs.readFileSync(authFilePath(), 'utf-8'));
        assert.deepEqual(after, legacy);
    });
});

describe('lib/auth-signout: scrub semantics', () => {
    beforeEach(setupTmpHome);
    afterEach(teardownTmpHome);

    it('removes ONLY the OAuth keys, preserving everything else', () => {
        // Hand-build a realistic merged auth.json: legacy bookkeeping +
        // OAuth keys written by signin.
        const legacy = {
            configured: true,
            timestamp: '2026-01-01T00:00:00.000Z',
            tools: ['github', 'claude'],
        };
        fs.writeFileSync(authFilePath(), JSON.stringify(legacy, null, 2));

        writeAuthToken({ token: 'token-xyz-abcdef', email: 'user@example.com' });

        // Sanity: all keys are now present.
        const before = JSON.parse(fs.readFileSync(authFilePath(), 'utf-8'));
        assert.equal(before.delimit_token, 'token-xyz-abcdef');
        assert.equal(before.access_token, 'token-xyz-abcdef');
        assert.equal(before.email, 'user@example.com');
        assert.ok(before.signed_in_at);
        assert.equal(before.configured, true);

        const result = removeAuthToken();
        assert.equal(result.changed, true);
        assert.equal(result.deleted, false);
        assert.equal(result.email, 'user@example.com');

        const after = JSON.parse(fs.readFileSync(authFilePath(), 'utf-8'));
        // OAuth keys are gone.
        assert.equal(after.delimit_token, undefined);
        assert.equal(after.access_token, undefined);
        assert.equal(after.signed_in_at, undefined);
        assert.equal(after.email, undefined);
        // Bookkeeping keys are intact.
        assert.equal(after.configured, true);
        assert.equal(after.timestamp, '2026-01-01T00:00:00.000Z');
        assert.deepEqual(after.tools, ['github', 'claude']);
    });

    it('returns the previously-signed-in email for display', () => {
        writeAuthToken({ token: 'token-xyz-abcdef', email: 'someone@example.com' });
        // Add a bookkeeping key so the file does not get deleted on scrub
        // (cleaner email-roundtrip assertion).
        const merged = JSON.parse(fs.readFileSync(authFilePath(), 'utf-8'));
        merged.configured = true;
        fs.writeFileSync(authFilePath(), JSON.stringify(merged, null, 2));

        const result = removeAuthToken();
        assert.equal(result.email, 'someone@example.com');
    });

    it('returns email="" when the previous sign-in had no email', () => {
        writeAuthToken({ token: 'token-xyz-abcdef' });
        const merged = JSON.parse(fs.readFileSync(authFilePath(), 'utf-8'));
        merged.configured = true;
        fs.writeFileSync(authFilePath(), JSON.stringify(merged, null, 2));

        const result = removeAuthToken();
        assert.equal(result.email, '');
    });

    it('removes only delimit_token / access_token / signed_in_at when no email was set', () => {
        writeAuthToken({ token: 'token-xyz-abcdef' }); // no email
        const merged = JSON.parse(fs.readFileSync(authFilePath(), 'utf-8'));
        merged.configured = true;
        merged.tools = ['github'];
        fs.writeFileSync(authFilePath(), JSON.stringify(merged, null, 2));

        removeAuthToken();
        const after = JSON.parse(fs.readFileSync(authFilePath(), 'utf-8'));
        assert.equal(after.delimit_token, undefined);
        assert.equal(after.access_token, undefined);
        assert.equal(after.signed_in_at, undefined);
        assert.equal(after.configured, true);
        assert.deepEqual(after.tools, ['github']);
    });

    it('handles a partial OAuth state (only access_token written)', () => {
        // Some other tool (or future surface) might write only one of the
        // two token keys. Sign-out should still scrub it.
        fs.writeFileSync(authFilePath(), JSON.stringify({
            configured: true,
            access_token: 'fallback-token-9999',
        }));
        const result = removeAuthToken();
        assert.equal(result.changed, true);
        const after = JSON.parse(fs.readFileSync(authFilePath(), 'utf-8'));
        assert.equal(after.access_token, undefined);
        assert.equal(after.configured, true);
    });
});

describe('lib/auth-signout: file deletion when empty', () => {
    beforeEach(setupTmpHome);
    afterEach(teardownTmpHome);

    it('deletes auth.json entirely when scrub leaves it empty', () => {
        // Pure signin file with no legacy bookkeeping keys. After scrub
        // there is nothing left worth keeping.
        writeAuthToken({ token: 'token-xyz-abcdef', email: 'user@example.com' });
        assert.ok(fs.existsSync(authFilePath()));

        const result = removeAuthToken();
        assert.equal(result.changed, true);
        assert.equal(result.deleted, true);
        assert.equal(result.email, 'user@example.com');
        assert.ok(!fs.existsSync(authFilePath()), 'auth.json should be deleted when empty after scrub');
    });

    it('does NOT delete auth.json when bookkeeping keys remain', () => {
        const legacy = { configured: true, tools: ['github'] };
        fs.writeFileSync(authFilePath(), JSON.stringify(legacy, null, 2));
        writeAuthToken({ token: 'token-xyz-abcdef' });

        const result = removeAuthToken();
        assert.equal(result.deleted, false);
        assert.ok(fs.existsSync(authFilePath()));
    });
});

describe('lib/auth-signout: file permissions', () => {
    beforeEach(setupTmpHome);
    afterEach(teardownTmpHome);

    it('preserves mode 0600 after scrub (file kept)', () => {
        // Set up signed-in + legacy bookkeeping so file survives scrub.
        const legacy = { configured: true, tools: ['github'] };
        fs.writeFileSync(authFilePath(), JSON.stringify(legacy, null, 2));
        writeAuthToken({ token: 'token-xyz-abcdef' });

        // Sanity: file is mode 0600 from signin.
        const beforeMode = fs.statSync(authFilePath()).mode & 0o7777;
        assert.equal(beforeMode, 0o600);

        removeAuthToken();
        const afterMode = fs.statSync(authFilePath()).mode & 0o7777;
        assert.equal(afterMode, 0o600, `expected 0600 after scrub, got 0o${afterMode.toString(8)}`);
    });
});

describe('lib/auth-signout: atomic write', () => {
    beforeEach(setupTmpHome);
    afterEach(teardownTmpHome);

    it('does not leave the .tmp file behind after a successful scrub', () => {
        const legacy = { configured: true };
        fs.writeFileSync(authFilePath(), JSON.stringify(legacy, null, 2));
        writeAuthToken({ token: 'token-xyz-abcdef' });

        removeAuthToken();
        const tmpPath = authFilePath() + '.tmp';
        assert.ok(!fs.existsSync(tmpPath), '.tmp file should be renamed away');
    });

    it('does not leave the .tmp file behind when the file is deleted', () => {
        writeAuthToken({ token: 'token-xyz-abcdef' });
        removeAuthToken();
        const tmpPath = authFilePath() + '.tmp';
        assert.ok(!fs.existsSync(tmpPath));
        assert.ok(!fs.existsSync(authFilePath()));
    });
});

describe('lib/auth-signout: idempotency', () => {
    beforeEach(setupTmpHome);
    afterEach(teardownTmpHome);

    it('second signout after the first is a no-op', () => {
        writeAuthToken({ token: 'token-xyz-abcdef', email: 'user@example.com' });

        const first = removeAuthToken();
        assert.equal(first.changed, true);

        const second = removeAuthToken();
        assert.equal(second.changed, false);
        assert.equal(second.deleted, false);
        assert.equal(second.email, '');
    });
});

describe('lib/auth-signout: authFilePath / readExistingAuth', () => {
    beforeEach(setupTmpHome);
    afterEach(teardownTmpHome);

    it('authFilePath returns <DELIMIT_HOME>/auth.json', () => {
        assert.equal(authFilePath(), path.join(tmpHome, AUTH_FILE_BASENAME));
    });

    it('readExistingAuth returns {} when the file is missing', () => {
        assert.deepEqual(readExistingAuth(authFilePath()), {});
    });

    it('readExistingAuth returns {} for malformed JSON', () => {
        fs.writeFileSync(authFilePath(), '{ not json');
        assert.deepEqual(readExistingAuth(authFilePath()), {});
    });

    it('readExistingAuth returns the parsed object for valid JSON', () => {
        fs.writeFileSync(authFilePath(), JSON.stringify({ a: 1, b: 'two' }));
        assert.deepEqual(readExistingAuth(authFilePath()), { a: 1, b: 'two' });
    });

    it('readExistingAuth returns {} for non-object JSON (array, primitive)', () => {
        fs.writeFileSync(authFilePath(), JSON.stringify(['a']));
        assert.deepEqual(readExistingAuth(authFilePath()), {});
    });
});
