// lib/auth-signout.js
//
// LED-2106: convenience wrapper around `rm ~/.delimit/auth.json` that ONLY
// removes the OAuth-related keys written by `delimit signin` (LED-2100) and
// preserves the legacy bookkeeping keys (`configured`, `timestamp`, `tools`)
// written by `lib/auth-setup.js`. Wiping the whole file regresses any user
// who ran `delimit auth` for tool credential bookkeeping.
//
// Design notes
//   - We MERGE-DELETE: read the file, drop the OAuth keys, write the
//     remainder back. If the post-scrub object has zero keys, we delete
//     the file entirely (cleanest state — equivalent to never having run
//     `delimit signin` or `delimit auth`).
//   - File mode is 0600 (owner-only readable). Write is atomic (tmp +
//     rename) — same pattern as lib/auth-signin.js.
//   - If auth.json is missing, malformed, or has no OAuth keys at all,
//     this is a no-op (returns { changed: false }). The CLI prints
//     "Not signed in." and exits 0 in that case.
//
// OAuth keys removed:
//   - delimit_token
//   - access_token
//   - signed_in_at
//   - email
//
// Returns: { path, changed, deleted, email } so callers can render a
// consistent success message.

const fs = require('fs');
const path = require('path');
const { delimitHome } = require('./delimit-home');

const AUTH_FILE_BASENAME = 'auth.json';

// The OAuth-related keys that `delimit signin` writes. Sign-out removes
// EXACTLY these and nothing else. Adding to this list is a behavior
// change that needs coordinated review (the gateway resolver in
// ai/deliberation.py::_read_oauth_token reads `delimit_token` /
// `access_token`).
const OAUTH_KEYS = Object.freeze([
    'delimit_token',
    'access_token',
    'signed_in_at',
    'email',
]);

function authFilePath() {
    return path.join(delimitHome(), AUTH_FILE_BASENAME);
}

/**
 * Read the existing auth.json if present, returning a plain object. Returns
 * an empty object on any read/parse error (consistent with auth-signin.js).
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
 * Write `data` to auth.json atomically with mode 0600. Mirrors the pattern
 * used by lib/auth-signin.js::writeAuthToken.
 */
function writeAuthAtomic(filePath, data) {
    const tmpPath = filePath + '.tmp';
    fs.writeFileSync(tmpPath, JSON.stringify(data, null, 2), { mode: 0o600 });
    try {
        fs.chmodSync(tmpPath, 0o600);
    } catch {
        // Non-POSIX filesystems may reject chmod; the file is already gated
        // by writeFileSync mode, so this is best-effort.
    }
    fs.renameSync(tmpPath, filePath);
}

/**
 * Remove the OAuth keys from ~/.delimit/auth.json, preserving any other
 * keys written by `delimit auth` / `lib/auth-setup.js`. If no OAuth keys
 * are present, this is a no-op. If removing the OAuth keys leaves the
 * file empty, the file is deleted.
 *
 * @param {object} [args]
 * @param {string} [args.home]  Override DELIMIT_HOME for tests
 * @returns {{ path: string, changed: boolean, deleted: boolean, email: string }}
 */
function removeAuthToken(args) {
    const opts = args || {};
    const home = opts.home || delimitHome();
    const filePath = path.join(home, AUTH_FILE_BASENAME);

    if (!fs.existsSync(filePath)) {
        return { path: filePath, changed: false, deleted: false, email: '' };
    }

    const existing = readExistingAuth(filePath);
    if (Object.keys(existing).length === 0) {
        // Malformed / empty / array. Nothing meaningful to scrub.
        return { path: filePath, changed: false, deleted: false, email: '' };
    }

    // Detect whether any OAuth keys are actually present. If not, we treat
    // this as "not signed in" and return changed=false WITHOUT touching the
    // file (preserves mtime / inode / mode).
    const hasOAuthKey = OAUTH_KEYS.some((k) => Object.prototype.hasOwnProperty.call(existing, k));
    if (!hasOAuthKey) {
        return { path: filePath, changed: false, deleted: false, email: '' };
    }

    const previousEmail = (existing.email || '').toString().trim();

    // Build the scrubbed object: keep everything that is NOT an OAuth key.
    const scrubbed = {};
    for (const [k, v] of Object.entries(existing)) {
        if (!OAUTH_KEYS.includes(k)) {
            scrubbed[k] = v;
        }
    }

    if (Object.keys(scrubbed).length === 0) {
        // Nothing left worth keeping — delete the file entirely so the
        // next session sees the cleanest possible state.
        try {
            fs.unlinkSync(filePath);
        } catch {
            // If unlink fails for some reason, fall back to writing an
            // empty object (still valid JSON, still mode 0600).
            writeAuthAtomic(filePath, {});
            return {
                path: filePath,
                changed: true,
                deleted: false,
                email: previousEmail,
            };
        }
        return {
            path: filePath,
            changed: true,
            deleted: true,
            email: previousEmail,
        };
    }

    writeAuthAtomic(filePath, scrubbed);
    return {
        path: filePath,
        changed: true,
        deleted: false,
        email: previousEmail,
    };
}

module.exports = {
    authFilePath,
    readExistingAuth,
    removeAuthToken,
    AUTH_FILE_BASENAME,
    OAUTH_KEYS,
};
