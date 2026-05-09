/**
 * LED-2100: tests for lib/auth-signin.js — writes the delimit.ai OAuth
 * bearer token to ~/.delimit/auth.json so the gateway hosted-deliberation
 * tier can authenticate from the CLI.
 *
 * Locks the contract that:
 *   - empty / whitespace-only tokens are rejected
 *   - the file is written at mode 0600 (owner-only readable)
 *   - both `delimit_token` and `access_token` are written (gateway
 *     resolver uses delimit_token first, access_token as fallback)
 *   - `signed_in_at` is ISO8601
 *   - `email` is recorded when supplied, omitted when empty
 *   - existing keys in auth.json (e.g. from `delimit auth`) are preserved
 *   - readCurrentToken() round-trips written tokens
 *   - malformed auth.json does not block sign-in
 */

const { describe, it, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const {
    authFilePath,
    readExistingAuth,
    writeAuthToken,
    readCurrentToken,
    AUTH_FILE_BASENAME,
} = require('../lib/auth-signin');

const ORIG_DELIMIT_HOME = process.env.DELIMIT_HOME;
const ORIG_NAMESPACE_ROOT = process.env.DELIMIT_NAMESPACE_ROOT;

let tmpHome;

function setupTmpHome() {
    tmpHome = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-signin-test-'));
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

describe('lib/auth-signin: token validation', () => {
    beforeEach(setupTmpHome);
    afterEach(teardownTmpHome);

    it('rejects an empty token', () => {
        assert.throws(
            () => writeAuthToken({ token: '' }),
            /Empty token/
        );
    });

    it('rejects a whitespace-only token', () => {
        assert.throws(
            () => writeAuthToken({ token: '   \n' }),
            /Empty token/
        );
    });

    it('throws an error with code DELIMIT_SIGNIN_EMPTY_TOKEN', () => {
        try {
            writeAuthToken({ token: '' });
            assert.fail('expected throw');
        } catch (err) {
            assert.equal(err.code, 'DELIMIT_SIGNIN_EMPTY_TOKEN');
        }
    });

    it('rejects a missing token argument', () => {
        assert.throws(
            () => writeAuthToken({}),
            /Empty token/
        );
    });
});

describe('lib/auth-signin: file output', () => {
    beforeEach(setupTmpHome);
    afterEach(teardownTmpHome);

    it('writes auth.json under DELIMIT_HOME', () => {
        const result = writeAuthToken({ token: 'abcdef0123456789' });
        assert.equal(result.path, path.join(tmpHome, AUTH_FILE_BASENAME));
        assert.ok(fs.existsSync(result.path), 'auth.json should exist');
    });

    it('writes both delimit_token and access_token with the same value', () => {
        const token = 'eyJhbGciOiJIUzI1NiJ9.payload.sig';
        const result = writeAuthToken({ token });
        const data = JSON.parse(fs.readFileSync(result.path, 'utf-8'));
        assert.equal(data.delimit_token, token);
        assert.equal(data.access_token, token);
    });

    it('writes signed_in_at as ISO8601 (when not overridden)', () => {
        const result = writeAuthToken({ token: 'token-xyz-abcdef' });
        const data = JSON.parse(fs.readFileSync(result.path, 'utf-8'));
        assert.match(data.signed_in_at, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/);
        // Round-trips through Date.parse
        assert.ok(!Number.isNaN(Date.parse(data.signed_in_at)));
    });

    it('honors the `now` override for deterministic tests', () => {
        const fixed = '2026-05-08T12:00:00.000Z';
        const result = writeAuthToken({ token: 'token-xyz-abcdef', now: fixed });
        const data = JSON.parse(fs.readFileSync(result.path, 'utf-8'));
        assert.equal(data.signed_in_at, fixed);
        assert.equal(result.signedInAt, fixed);
    });

    it('records email when supplied', () => {
        const result = writeAuthToken({ token: 'token-xyz-abcdef', email: 'user@example.com' });
        const data = JSON.parse(fs.readFileSync(result.path, 'utf-8'));
        assert.equal(data.email, 'user@example.com');
        assert.equal(result.email, 'user@example.com');
    });

    it('omits email when not supplied', () => {
        writeAuthToken({ token: 'token-xyz-abcdef' });
        const data = JSON.parse(fs.readFileSync(authFilePath(), 'utf-8'));
        assert.equal(data.email, undefined);
    });

    it('omits email when supplied as empty / whitespace', () => {
        writeAuthToken({ token: 'token-xyz-abcdef', email: '   ' });
        const data = JSON.parse(fs.readFileSync(authFilePath(), 'utf-8'));
        assert.equal(data.email, undefined);
    });

    it('trims whitespace from the token before writing', () => {
        const result = writeAuthToken({ token: '  padded-token-1234  ' });
        const data = JSON.parse(fs.readFileSync(result.path, 'utf-8'));
        assert.equal(data.delimit_token, 'padded-token-1234');
    });
});

describe('lib/auth-signin: file permissions', () => {
    beforeEach(setupTmpHome);
    afterEach(teardownTmpHome);

    it('creates auth.json with mode 0600', () => {
        const result = writeAuthToken({ token: 'token-xyz-abcdef' });
        const stat = fs.statSync(result.path);
        // Mask off the file-type bits, keep only the 0o7777 permission bits.
        const mode = stat.mode & 0o7777;
        assert.equal(mode, 0o600, `expected 0600, got 0o${mode.toString(8)}`);
    });

    it('creates DELIMIT_HOME at mode 0700 when missing', () => {
        // Remove the temp home dir so writeAuthToken has to create it.
        fs.rmSync(tmpHome, { recursive: true, force: true });
        assert.ok(!fs.existsSync(tmpHome));

        writeAuthToken({ token: 'token-xyz-abcdef' });
        assert.ok(fs.existsSync(tmpHome));
        const stat = fs.statSync(tmpHome);
        // Some CI filesystems strip mode bits; allow either 0700 (the
        // requested mode) or whatever the umask permits, but require at
        // minimum that group/other read bits are clear.
        const mode = stat.mode & 0o7777;
        assert.equal(mode & 0o077, 0, `dir mode should not allow group/other access; got 0o${mode.toString(8)}`);
    });
});

describe('lib/auth-signin: merge semantics', () => {
    beforeEach(setupTmpHome);
    afterEach(teardownTmpHome);

    it('preserves existing keys written by lib/auth-setup.js', () => {
        // Simulate what lib/auth-setup.js writes today.
        const legacy = {
            configured: true,
            timestamp: '2026-01-01T00:00:00.000Z',
            tools: ['github', 'claude'],
        };
        fs.writeFileSync(authFilePath(), JSON.stringify(legacy, null, 2));

        const result = writeAuthToken({ token: 'token-xyz-abcdef' });
        assert.equal(result.merged, true);

        const data = JSON.parse(fs.readFileSync(result.path, 'utf-8'));
        assert.equal(data.configured, true);
        assert.equal(data.timestamp, '2026-01-01T00:00:00.000Z');
        assert.deepEqual(data.tools, ['github', 'claude']);
        assert.equal(data.delimit_token, 'token-xyz-abcdef');
        assert.equal(data.access_token, 'token-xyz-abcdef');
    });

    it('reports merged=false when no auth.json existed', () => {
        const result = writeAuthToken({ token: 'token-xyz-abcdef' });
        assert.equal(result.merged, false);
    });

    it('overwrites a stale token on re-signin', () => {
        writeAuthToken({ token: 'old-token-1234567890' });
        writeAuthToken({ token: 'new-token-1234567890' });
        const data = JSON.parse(fs.readFileSync(authFilePath(), 'utf-8'));
        assert.equal(data.delimit_token, 'new-token-1234567890');
        assert.equal(data.access_token, 'new-token-1234567890');
    });

    it('treats a malformed auth.json as empty and overwrites it', () => {
        fs.writeFileSync(authFilePath(), '{ this is not valid json');
        const result = writeAuthToken({ token: 'token-xyz-abcdef' });
        // We treat malformed files as missing; merged should be false.
        assert.equal(result.merged, false);
        const data = JSON.parse(fs.readFileSync(result.path, 'utf-8'));
        assert.equal(data.delimit_token, 'token-xyz-abcdef');
    });

    it('treats a JSON array as empty (object-only contract)', () => {
        fs.writeFileSync(authFilePath(), '[]');
        const result = writeAuthToken({ token: 'token-xyz-abcdef' });
        assert.equal(result.merged, false);
    });
});

describe('lib/auth-signin: readCurrentToken', () => {
    beforeEach(setupTmpHome);
    afterEach(teardownTmpHome);

    it('returns "" when auth.json is missing', () => {
        assert.equal(readCurrentToken(), '');
    });

    it('returns "" when auth.json has neither token key', () => {
        fs.writeFileSync(authFilePath(), JSON.stringify({ configured: true }));
        assert.equal(readCurrentToken(), '');
    });

    it('round-trips delimit_token after writeAuthToken', () => {
        writeAuthToken({ token: 'round-trip-token-1234' });
        assert.equal(readCurrentToken(), 'round-trip-token-1234');
    });

    it('falls back to access_token when delimit_token is missing', () => {
        // Mirrors gateway-side resolver: prefer delimit_token, fall back to
        // access_token. This covers the case where some other tool (or a
        // future surface) wrote only access_token.
        fs.writeFileSync(authFilePath(), JSON.stringify({ access_token: 'fallback-token-9999' }));
        assert.equal(readCurrentToken(), 'fallback-token-9999');
    });

    it('prefers delimit_token over access_token when both are present', () => {
        fs.writeFileSync(authFilePath(), JSON.stringify({
            delimit_token: 'primary-token-1111',
            access_token: 'secondary-token-2222',
        }));
        assert.equal(readCurrentToken(), 'primary-token-1111');
    });

    it('returns "" when auth.json is malformed', () => {
        fs.writeFileSync(authFilePath(), '{ broken');
        assert.equal(readCurrentToken(), '');
    });
});

describe('lib/auth-signin: readExistingAuth', () => {
    beforeEach(setupTmpHome);
    afterEach(teardownTmpHome);

    it('returns {} when the file is missing', () => {
        assert.deepEqual(readExistingAuth(authFilePath()), {});
    });

    it('returns {} for malformed JSON', () => {
        fs.writeFileSync(authFilePath(), '{ not json');
        assert.deepEqual(readExistingAuth(authFilePath()), {});
    });

    it('returns the parsed object for valid JSON', () => {
        fs.writeFileSync(authFilePath(), JSON.stringify({ a: 1, b: 'two' }));
        assert.deepEqual(readExistingAuth(authFilePath()), { a: 1, b: 'two' });
    });

    it('returns {} for non-object JSON (array, primitive)', () => {
        fs.writeFileSync(authFilePath(), JSON.stringify(['a', 'b']));
        assert.deepEqual(readExistingAuth(authFilePath()), {});

        fs.writeFileSync(authFilePath(), JSON.stringify('just a string'));
        assert.deepEqual(readExistingAuth(authFilePath()), {});
    });
});

describe('lib/auth-signin: authFilePath', () => {
    beforeEach(setupTmpHome);
    afterEach(teardownTmpHome);

    it('returns <DELIMIT_HOME>/auth.json', () => {
        assert.equal(authFilePath(), path.join(tmpHome, AUTH_FILE_BASENAME));
    });

    it('re-resolves on every call (honors env mutation)', () => {
        const first = authFilePath();
        process.env.DELIMIT_HOME = path.join(tmpHome, 'alt');
        const second = authFilePath();
        assert.notEqual(first, second);
        assert.equal(second, path.join(tmpHome, 'alt', AUTH_FILE_BASENAME));
    });
});
