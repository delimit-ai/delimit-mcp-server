'use strict';
/**
 * License activation for `delimit activate <key>`.
 *
 * Pro is granted ONLY when Lemon Squeezy positively confirms the key
 * (2xx with `valid: true`). Every other outcome refuses activation, writes
 * nothing, and leaves an existing license.json untouched:
 *
 *   - 2xx / 4xx (except 429) + `valid: false`  -> invalid. Lemon Squeezy answers an
 *     unknown key with HTTP 404 {"valid": false, "error": "..."}; a disabled
 *     or expired key with HTTP 200 {"valid": false}.
 *   - another 4xx (not 429)         -> rejected.
 *   - 5xx / 429 / unparseable body  -> server unavailable (retry later).
 *   - no HTTP response at all       -> network failure (DNS, connect,
 *     timeout, TLS). Still no grant: a key that has never been confirmed is
 *     never Pro, so blocking api.lemonsqueezy.com cannot unlock it. This
 *     matches the MCP `delimit_activate` path (license_core.activate,
 *     LED-3809). An already validated license on disk keeps its own
 *     offline grace windows, enforced by the Python core at revalidation.
 *
 * History: before this module, the CLI used axios's default validateStatus
 * (2xx only). The 404 for an unknown key therefore threw, a catch-all
 * printed "Activating locally (7-day grace)" and wrote a Pro license, so any
 * string of 10+ characters activated Pro while online.
 *
 * There is deliberately no environment-variable override of the URL: that
 * would let a user point activation at a server that always says valid.
 */

const fs = require('fs');
const path = require('path');
const os = require('os');
const crypto = require('crypto');

const LS_VALIDATE_URL = 'https://api.lemonsqueezy.com/v1/licenses/validate';

/**
 * Classify an HTTP response from the Lemon Squeezy validate endpoint.
 * Pure; never throws.
 *
 * @param {number} status HTTP status code
 * @param {*} body parsed JSON body (or a string/undefined when unparseable)
 * @returns {{outcome: 'valid'|'invalid'|'rejected'|'unavailable',
 *            httpStatus: number, error: string, licenseId: (string|number|null),
 *            customerEmail: string}}
 */
function classifyValidateResponse(status, body) {
    const b = body && typeof body === 'object' && !Array.isArray(body) ? body : null;
    const lsError = b && typeof b.error === 'string' ? b.error : '';
    const base = { httpStatus: status, error: lsError, licenseId: null, customerEmail: '' };
    if (b && b.valid === false && ((status >= 200 && status < 300) ||
        (status >= 400 && status < 500 && status !== 429))) {
        return { ...base, outcome: 'invalid' };
    }
    if (b && b.valid === true && status >= 200 && status < 300) {
        return {
            ...base,
            outcome: 'valid',
            licenseId: (b.license_key && b.license_key.id) || null,
            customerEmail: (b.meta && b.meta.customer_email) || '',
        };
    }
    if (status >= 400 && status < 500 && status !== 429) {
        return { ...base, outcome: 'rejected' };
    }
    return { ...base, outcome: 'unavailable' };
}

/**
 * Validate a key against Lemon Squeezy.
 *
 * @param {string} key
 * @param {{post?: Function}} [opts] `post(url, body, config)` with axios
 *   semantics; defaults to axios.post. Injected by tests only.
 * @returns {Promise<object>} classification; outcome 'unreachable' when no
 *   HTTP response was received.
 */
async function validateLicenseKey(key, opts = {}) {
    const post = opts.post || require('axios').post;
    let resp;
    try {
        resp = await post(LS_VALIDATE_URL, { license_key: key }, {
            headers: { Accept: 'application/json' },
            timeout: 10000,
            // Every HTTP response is classified, never thrown: only a real
            // network failure reaches the catch below.
            validateStatus: () => true,
        });
    } catch (err) {
        if (err && err.response && typeof err.response.status === 'number') {
            // Defensive: a transport that still throws on non-2xx.
            return classifyValidateResponse(err.response.status, err.response.data);
        }
        return {
            outcome: 'unreachable', httpStatus: null, licenseId: null, customerEmail: '',
            error: (err && (err.code || err.message)) || 'network error',
        };
    }
    return classifyValidateResponse(resp && resp.status, resp && resp.data);
}

/** Human message for a non-valid outcome. Never includes the key. */
function refusalMessage(result) {
    switch (result.outcome) {
        case 'invalid':
            return `License invalid: ${(result.error || 'the license server rejected this key').replace(/\.$/, '')}. Pro was not activated.`;
        case 'rejected':
            return `The license server rejected this key (HTTP ${result.httpStatus}). Pro was not activated.`;
        case 'unavailable':
            return `The license server returned an error (HTTP ${result.httpStatus}). Pro was not activated; try again later.`;
        case 'unreachable':
        default:
            return 'Could not reach the license server. Pro was not activated; reconnect and run `delimit activate` again.';
    }
}

/** Build the license.json record for a CONFIRMED key. */
function buildLicenseRecord(key, result, now = Date.now() / 1000) {
    const machineHash = crypto.createHash('sha256').update(os.homedir()).digest('hex').slice(0, 16);
    return {
        key,
        tier: 'pro',
        valid: true,
        license_id: result.licenseId,
        customer_email: result.customerEmail,
        activated_at: now,
        machine_hash: machineHash,
        validated_at: now,
        last_validated_at: now,
        validated_via: 'lemon_squeezy',
    };
}

/**
 * Validate and, only on a confirmed key, write license.json.
 *
 * @returns {Promise<{activated: boolean, result: object, record?: object, message?: string}>}
 */
async function activateLicense(key, licensePath, opts = {}) {
    const result = await validateLicenseKey(key, opts);
    if (result.outcome !== 'valid') {
        return { activated: false, result, message: refusalMessage(result) };
    }
    const record = buildLicenseRecord(key, result);
    fs.mkdirSync(path.dirname(licensePath), { recursive: true });
    fs.writeFileSync(licensePath, JSON.stringify(record, null, 2));
    return { activated: true, result, record };
}

module.exports = {
    LS_VALIDATE_URL,
    classifyValidateResponse,
    validateLicenseKey,
    refusalMessage,
    buildLicenseRecord,
    activateLicense,
};
