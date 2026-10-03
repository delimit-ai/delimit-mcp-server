/**
 * `delimit activate <key>` must grant Pro ONLY on a positive Lemon Squeezy
 * confirmation (2xx + valid:true).
 *
 * Regression pin for the 4.1.38..4.21.0 defect: axios threw on the 404 that
 * Lemon Squeezy returns for an unknown key, and a catch-all printed
 * "Activating locally (7-day grace)" and wrote a Pro license.json — so any
 * string of 10+ characters activated Pro while online.
 *
 * All network traffic goes to a local fake HTTP server; nothing contacts the
 * real Lemon Squeezy API. Every test runs under a throwaway HOME.
 */

const { describe, it, before, after } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');
const net = require('net');
const { execFile } = require('child_process');
const axios = require('axios');

const {
    LS_VALIDATE_URL,
    classifyValidateResponse,
    validateLicenseKey,
    activateLicense,
} = require('../lib/license-activation');

const REPO = path.resolve(__dirname, '..');
const CLI = path.join(REPO, 'bin', 'delimit-cli.js');
const PRELOAD = path.join(__dirname, 'fixtures', 'license-ls-redirect-preload.js');
const FAKE_KEY = 'DELIMIT-TEST-0000-0000-NOTAREALKEY';

// ── fake Lemon Squeezy ────────────────────────────────────────────────
function startFakeLs() {
    const state = { status: 200, body: '{}', requests: 0, lastBody: null };
    const server = http.createServer((req, res) => {
        let raw = '';
        req.on('data', (c) => { raw += c; });
        req.on('end', () => {
            state.requests += 1;
            try { state.lastBody = JSON.parse(raw); } catch { state.lastBody = raw; }
            res.writeHead(state.status, { 'Content-Type': 'application/json' });
            res.end(state.body);
        });
    });
    return new Promise((resolve) => {
        server.listen(0, '127.0.0.1', () => {
            const url = `http://127.0.0.1:${server.address().port}/v1/licenses/validate`;
            resolve({
                url,
                state,
                answer(status, payload) {
                    state.status = status;
                    state.body = typeof payload === 'string' ? payload : JSON.stringify(payload);
                },
                close: () => new Promise((r) => server.close(r)),
            });
        });
    });
}

function refusedPortUrl() {
    return new Promise((resolve) => {
        const s = net.createServer();
        s.listen(0, '127.0.0.1', () => {
            const { port } = s.address();
            s.close(() => resolve(`http://127.0.0.1:${port}/v1/licenses/validate`));
        });
    });
}

// Injected transport: real axios, URL redirected to the fake server.
function postTo(url) {
    return (u, data, config) => {
        assert.strictEqual(u, LS_VALIDATE_URL);
        return axios.post(url, data, config);
    };
}

function tmpHome() {
    return fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-license-test-'));
}

const VALIDATED = {
    key: 'DELIMIT-REAL-AAAA-BBBB',
    tier: 'pro',
    valid: true,
    activated_at: 1700000000,
    last_validated_at: 1700000000,
    validated_via: 'lemon_squeezy',
};

// ── 1. pure classifier ────────────────────────────────────────────────
describe('classifyValidateResponse', () => {
    it('2xx + valid:true -> valid, carries id/email', () => {
        const r = classifyValidateResponse(200, { valid: true, license_key: { id: 7 }, meta: { customer_email: 'a@b.c' } });
        assert.strictEqual(r.outcome, 'valid');
        assert.strictEqual(r.licenseId, 7);
        assert.strictEqual(r.customerEmail, 'a@b.c');
    });
    it('404 + valid:false (real LS answer for an unknown key) -> invalid', () => {
        const r = classifyValidateResponse(404, { valid: false, error: 'license_key not found.' });
        assert.strictEqual(r.outcome, 'invalid');
        assert.strictEqual(r.error, 'license_key not found.');
    });
    it('200 + valid:false (disabled/expired key) -> invalid', () => {
        assert.strictEqual(classifyValidateResponse(200, { valid: false }).outcome, 'invalid');
    });
    for (const status of [429, 500, 503]) {
        it(`${status} + valid:false -> unavailable`, () => {
            assert.strictEqual(classifyValidateResponse(status, { valid: false }).outcome, 'unavailable');
        });
    }
    it('valid:true on a non-2xx status is never valid', () => {
        assert.notStrictEqual(classifyValidateResponse(404, { valid: true }).outcome, 'valid');
        assert.notStrictEqual(classifyValidateResponse(500, { valid: true }).outcome, 'valid');
    });
    it('other 4xx -> rejected; 429 -> unavailable', () => {
        assert.strictEqual(classifyValidateResponse(400, { errors: [] }).outcome, 'rejected');
        assert.strictEqual(classifyValidateResponse(422, 'nope').outcome, 'rejected');
        assert.strictEqual(classifyValidateResponse(429, {}).outcome, 'unavailable');
    });
    it('5xx / non-JSON / 2xx without valid -> unavailable', () => {
        assert.strictEqual(classifyValidateResponse(500, '<html>').outcome, 'unavailable');
        assert.strictEqual(classifyValidateResponse(503, undefined).outcome, 'unavailable');
        assert.strictEqual(classifyValidateResponse(200, {}).outcome, 'unavailable');
        assert.strictEqual(classifyValidateResponse(200, { valid: 'true' }).outcome, 'unavailable');
    });
});

// ── 2. validate/activate over a real local HTTP round trip ────────────
describe('activateLicense (local fake server, injected transport)', () => {
    let fake;
    before(async () => { fake = await startFakeLs(); });
    after(async () => { await fake.close(); });

    it('sends only license_key to the validate endpoint', async () => {
        fake.answer(404, { valid: false, error: 'license_key not found.' });
        await validateLicenseKey(FAKE_KEY, { post: postTo(fake.url) });
        assert.deepStrictEqual(Object.keys(fake.state.lastBody), ['license_key']);
    });

    it('404 valid:false -> refused, nothing written', async () => {
        const home = tmpHome();
        const lp = path.join(home, '.delimit', 'license.json');
        fake.answer(404, { valid: false, error: 'license_key not found.' });
        const r = await activateLicense(FAKE_KEY, lp, { post: postTo(fake.url) });
        assert.strictEqual(r.activated, false);
        assert.strictEqual(r.result.outcome, 'invalid');
        assert.strictEqual(r.message, 'License invalid: license_key not found. Pro was not activated.');
        assert.ok(!fs.existsSync(lp));
    });

    for (const [status, payload, outcome] of [
        [400, { errors: [{ detail: 'bad' }] }, 'rejected'],
        ...[429, 500, 503].map(status => [status, { valid: false }, 'unavailable']),
        [500, '<html>oops</html>', 'unavailable'],
        [503, { message: 'down' }, 'unavailable'],
        [429, { message: 'slow down' }, 'unavailable'],
    ]) {
        it(`HTTP ${status} -> ${outcome}, refused, existing license untouched`, async () => {
            const home = tmpHome();
            const lp = path.join(home, '.delimit', 'license.json');
            fs.mkdirSync(path.dirname(lp), { recursive: true });
            fs.writeFileSync(lp, JSON.stringify(VALIDATED));
            fake.answer(status, payload);
            const r = await activateLicense(FAKE_KEY, lp, { post: postTo(fake.url) });
            assert.strictEqual(r.activated, false);
            assert.strictEqual(r.result.outcome, outcome);
            assert.match(r.message, new RegExp(`HTTP ${status}`));
            assert.deepStrictEqual(JSON.parse(fs.readFileSync(lp, 'utf8')), VALIDATED);
        });
    }

    it('connection refused -> unreachable, refused, existing license untouched', async () => {
        const home = tmpHome();
        const lp = path.join(home, '.delimit', 'license.json');
        fs.mkdirSync(path.dirname(lp), { recursive: true });
        fs.writeFileSync(lp, JSON.stringify(VALIDATED));
        const r = await activateLicense(FAKE_KEY, lp, { post: postTo(await refusedPortUrl()) });
        assert.strictEqual(r.activated, false);
        assert.strictEqual(r.result.outcome, 'unreachable');
        assert.match(r.message, /Could not reach the license server/);
        assert.doesNotMatch(r.message, /grace|Activating locally/);
        assert.deepStrictEqual(JSON.parse(fs.readFileSync(lp, 'utf8')), VALIDATED);
    });

    it('a transport that still throws on 404 is classified, not treated as offline', async () => {
        const post = async () => {
            const err = new Error('Request failed with status code 404');
            err.response = { status: 404, data: { valid: false, error: 'license_key not found.' } };
            throw err;
        };
        const r = await validateLicenseKey(FAKE_KEY, { post });
        assert.strictEqual(r.outcome, 'invalid');
    });

    it('200 valid:true -> writes a validated Pro license', async () => {
        const home = tmpHome();
        const lp = path.join(home, '.delimit', 'license.json');
        fake.answer(200, { valid: true, license_key: { id: 42 }, meta: { customer_email: 'buyer@example.com' } });
        const r = await activateLicense('DELIMIT-TEST-1234-5678', lp, { post: postTo(fake.url) });
        assert.strictEqual(r.activated, true);
        const stored = JSON.parse(fs.readFileSync(lp, 'utf8'));
        assert.strictEqual(stored.tier, 'pro');
        assert.strictEqual(stored.valid, true);
        assert.strictEqual(stored.license_id, 42);
        assert.strictEqual(stored.validated_via, 'lemon_squeezy');
        assert.ok(stored.last_validated_at > 0);
        assert.strictEqual(stored.last_validated_at, stored.activated_at);
    });
});

// ── 3. the real CLI command, end to end ───────────────────────────────
function runActivate(home, key, fakeUrl) {
    const env = { ...process.env, HOME: home, DELIMIT_HOME: path.join(home, '.delimit'), NO_COLOR: '1' };
    delete env.DELIMIT_NAMESPACE_ROOT;
    delete env._TEST_FAKE_LS_URL;
    if (fakeUrl) env._TEST_FAKE_LS_URL = fakeUrl;
    return new Promise((resolve) => {
        execFile(process.execPath, ['-r', PRELOAD, CLI, 'activate', key],
            { env, cwd: home, timeout: 60000 },
            (err, stdout, stderr) => resolve({ code: err ? (err.code ?? 1) : 0, out: `${stdout}\n${stderr}` }));
    });
}

describe('delimit activate (CLI, throwaway HOME, fake server)', () => {
    let fake;
    before(async () => { fake = await startFakeLs(); });
    after(async () => { await fake.close(); });

    it('unknown key answered 404 {valid:false} -> exit 1, no Pro, no license.json', async () => {
        const home = tmpHome();
        fake.answer(404, { valid: false, error: 'license_key not found.' });
        const r = await runActivate(home, FAKE_KEY, fake.url);
        assert.notStrictEqual(r.code, 0, r.out);
        assert.match(r.out, /License invalid/);
        assert.doesNotMatch(r.out, /Activating locally|7-day grace|activated successfully/);
        assert.ok(!fs.existsSync(path.join(home, '.delimit', 'license.json')));
    });

    it('server 500 -> exit 1, existing validated license unchanged', async () => {
        const home = tmpHome();
        const lp = path.join(home, '.delimit', 'license.json');
        fs.mkdirSync(path.dirname(lp), { recursive: true });
        fs.writeFileSync(lp, JSON.stringify(VALIDATED));
        fake.answer(500, '<html>oops</html>');
        const r = await runActivate(home, FAKE_KEY, fake.url);
        assert.notStrictEqual(r.code, 0, r.out);
        assert.match(r.out, /HTTP 500/);
        assert.deepStrictEqual(JSON.parse(fs.readFileSync(lp, 'utf8')), VALIDATED);
    });

    it('network failure -> exit 1, no Pro, no license.json', async () => {
        const home = tmpHome();
        const r = await runActivate(home, FAKE_KEY, null);
        assert.notStrictEqual(r.code, 0, r.out);
        assert.match(r.out, /Could not reach the license server/);
        assert.doesNotMatch(r.out, /Activating locally|7-day grace/);
        assert.ok(!fs.existsSync(path.join(home, '.delimit', 'license.json')));
    });

    it('200 {valid:true} -> exit 0 and a Pro license.json', async () => {
        const home = tmpHome();
        fake.answer(200, { valid: true, license_key: { id: 1 }, meta: {} });
        const r = await runActivate(home, 'DELIMIT-TEST-1234-5678', fake.url);
        assert.strictEqual(r.code, 0, r.out);
        assert.match(r.out, /activated successfully/);
        const stored = JSON.parse(fs.readFileSync(path.join(home, '.delimit', 'license.json'), 'utf8'));
        assert.strictEqual(stored.tier, 'pro');
        assert.strictEqual(stored.validated_via, 'lemon_squeezy');
    });
});
