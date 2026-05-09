// lib/auth-signin.js
//
// LED-2100: writes the delimit.ai OAuth bearer token to ~/.delimit/auth.json
// so the gateway hosted-deliberation tier (LED-2092) can authenticate from
// the CLI. The gateway reads `delimit_token` or `access_token` from this
// file (see ai/deliberation.py::_read_oauth_token).
//
// Design notes
//   - We MERGE into any existing auth.json rather than overwrite. The legacy
//     `lib/auth-setup.js` writes {configured, timestamp, tools} into the same
//     file for tool-credential bookkeeping; clobbering that would regress
//     existing users. New keys (delimit_token, access_token, signed_in_at,
//     email) live alongside the legacy keys.
//   - File mode is 0600 (owner-only readable). Directory is created at 0700
//     when missing.
//   - Token shape is intentionally lax: accept anything non-empty after
//     trimming. The gateway is the source of truth for token validity; the
//     CLI must not try to second-guess JWT structure (Supabase tokens vs
//     opaque tokens vs future formats).
//
// Returns: { path, email, signedInAt, merged } so callers can render a
// consistent success message.

const fs = require('fs');
const path = require('path');
const { delimitHome } = require('./delimit-home');

const AUTH_FILE_BASENAME = 'auth.json';

function authFilePath() {
    return path.join(delimitHome(), AUTH_FILE_BASENAME);
}

/**
 * Read the existing auth.json if present, returning a plain object. Returns
 * an empty object on any read/parse error (we do not want to corrupt unrelated
 * keys, but a malformed file should not block sign-in either — overwrite it).
 */
function readExistingAuth(filePath) {
    if (!fs.existsSync(filePath)) {
        return {};
    }
    try {
        const raw = fs.readFileSync(filePath, 'utf-8');
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
            return parsed;
        }
        return {};
    } catch {
        return {};
    }
}

/**
 * Persist a delimit.ai OAuth bearer token to ~/.delimit/auth.json with mode
 * 0600. Existing keys (set by auth-setup.js or other flows) are preserved.
 *
 * @param {object} args
 * @param {string} args.token   Bearer token returned by delimit.ai OAuth
 * @param {string} [args.email] Email address associated with the account
 * @param {string} [args.now]   Override clock for deterministic tests (ISO8601)
 * @param {string} [args.home]  Override DELIMIT_HOME for tests
 * @returns {{ path: string, email: string, signedInAt: string, merged: boolean }}
 */
function writeAuthToken(args) {
    const opts = args || {};
    const token = (opts.token || '').toString().trim();
    if (!token) {
        const err = new Error('Empty token; nothing written.');
        err.code = 'DELIMIT_SIGNIN_EMPTY_TOKEN';
        throw err;
    }

    const home = opts.home || delimitHome();
    if (!fs.existsSync(home)) {
        fs.mkdirSync(home, { recursive: true, mode: 0o700 });
    }
    const filePath = path.join(home, AUTH_FILE_BASENAME);

    const existing = readExistingAuth(filePath);
    const merged = Object.keys(existing).length > 0;
    const signedInAt = opts.now || new Date().toISOString();
    const email = (opts.email || '').toString().trim();

    const next = Object.assign({}, existing, {
        delimit_token: token,
        access_token: token,
        signed_in_at: signedInAt,
    });
    if (email) {
        next.email = email;
    }

    // Two-step write: write to a temp file with mode 0600, then rename. This
    // avoids a window where the file exists with default permissions before
    // chmod runs.
    const tmpPath = filePath + '.tmp';
    fs.writeFileSync(tmpPath, JSON.stringify(next, null, 2), { mode: 0o600 });
    // Some umasks may still strip group/other bits to match the requested
    // mode; explicitly chmod to be safe (no-op on most platforms but cheap).
    try {
        fs.chmodSync(tmpPath, 0o600);
    } catch {
        // Non-POSIX filesystems may reject chmod; the file is already gated
        // by writeFileSync mode, so this is best-effort.
    }
    fs.renameSync(tmpPath, filePath);

    return {
        path: filePath,
        email,
        signedInAt,
        merged,
    };
}

/**
 * Read the currently stored token, if any. Returns "" when missing or
 * malformed. Mirrors the gateway-side resolver (ai/deliberation.py::
 * _read_oauth_token) so callers can implement `delimit signin --status`.
 */
function readCurrentToken() {
    const filePath = authFilePath();
    const data = readExistingAuth(filePath);
    const token = (data.delimit_token || data.access_token || '').toString().trim();
    return token;
}

module.exports = {
    authFilePath,
    readExistingAuth,
    writeAuthToken,
    readCurrentToken,
    AUTH_FILE_BASENAME,
};
