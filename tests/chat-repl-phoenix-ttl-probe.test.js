const { describe, it, afterEach } = require('node:test');
const assert = require('node:assert');

// Module under test — LED-3432 (TTL-expiring failed-model tracker) and
// LED-3433 (deterministic quota probe: exit code + stderr signatures only,
// never the stdout reply text; codex probed too).
//
// Hermetic per tests/chat-repl-model-flag.test.js conventions: fixture-injected
// modelsConfig, no live CLIs — the spawn/probe layer (_runProbe) is mocked.
process.env.DELIMIT_WRAPPED = 'true';
const {
    DelimitChatREPL,
    FailedModelRegistry,
    resolvePhoenixFailTtlMs,
    DEFAULT_PHOENIX_FAIL_TTL_MS,
} = require('../lib/chat-repl');

// A controlled models fixture so tests do not depend on ~/.delimit/models.json.
const FIXTURE = {
    claude: { auth_mode: 'chat_login' },
    codex: { auth_mode: 'chat_login' },
    antigravity: { auth_mode: 'chat_login' },
    fallbacks: { default: ['claude', 'codex', 'antigravity'] },
};

function replWith(options) {
    const r = new DelimitChatREPL(options || {});
    r.modelsConfig = JSON.parse(JSON.stringify(FIXTURE));
    return r;
}

// Silence console.log AND process.stdout.write (the probe writes both).
function silence(fn) {
    const origLog = console.log;
    const origWrite = process.stdout.write;
    console.log = () => {};
    process.stdout.write = () => true;
    try { return fn(); } finally {
        console.log = origLog;
        process.stdout.write = origWrite;
    }
}

// A spawnSync-shaped probe result.
function probeResult(over) {
    return { status: 0, stdout: '', stderr: '', signal: null, error: undefined, ...over };
}

describe('LED-3432 failedModels TTL expiry', () => {
    it('excludes a failed model within the TTL and includes it again after expiry', () => {
        let clock = 0;
        const r = replWith({ failTtlMs: 1000, now: () => clock });
        r.failedModels.add('claude');

        // Within TTL: excluded, exactly like the old Set semantics.
        assert.deepStrictEqual(
            r.getActiveChain().map(m => m.id),
            ['codex', 'antigravity'],
        );
        clock = 999;
        assert.deepStrictEqual(
            r.getActiveChain().map(m => m.id),
            ['codex', 'antigravity'],
        );

        // At/after TTL: eligible again — the per-launch probe re-verifies
        // real health before any session handoff (that is the re-probe).
        clock = 1000;
        assert.deepStrictEqual(
            r.getActiveChain().map(m => m.id),
            ['claude', 'codex', 'antigravity'],
        );
    });

    it('a re-failure after expiry restarts the TTL window', () => {
        let clock = 0;
        const reg = new FailedModelRegistry({ ttlMs: 100, now: () => clock });
        reg.add('claude');
        clock = 100;
        assert.strictEqual(reg.has('claude'), false); // expired
        reg.add('claude'); // fails over again at t=100
        clock = 150;
        assert.strictEqual(reg.has('claude'), true); // new window active
        clock = 200;
        assert.strictEqual(reg.has('claude'), false);
    });
});

describe('LED-3432 DELIMIT_PHOENIX_FAIL_TTL_MS env override', () => {
    afterEach(() => { delete process.env.DELIMIT_PHOENIX_FAIL_TTL_MS; });

    it('respects a valid env override', () => {
        assert.strictEqual(resolvePhoenixFailTtlMs({ DELIMIT_PHOENIX_FAIL_TTL_MS: '5000' }), 5000);
        process.env.DELIMIT_PHOENIX_FAIL_TTL_MS = '7500';
        const reg = new FailedModelRegistry();
        assert.strictEqual(reg.ttlMs, 7500);
    });

    it('falls back to the default on garbage / non-positive / empty values', () => {
        for (const garbage of ['banana', '-5', '0', '', '   ', 'NaN', 'Infinity']) {
            assert.strictEqual(
                resolvePhoenixFailTtlMs({ DELIMIT_PHOENIX_FAIL_TTL_MS: garbage }),
                DEFAULT_PHOENIX_FAIL_TTL_MS,
                `garbage value ${JSON.stringify(garbage)} must fall back to default`,
            );
        }
        assert.strictEqual(resolvePhoenixFailTtlMs({}), DEFAULT_PHOENIX_FAIL_TTL_MS);
    });
});

describe('LED-3433 deterministic quota probe', () => {
    it('exit 0 with a reply that merely mentions "limit"/"quota" is HEALTHY (false-positive regression)', () => {
        const r = replWith({});
        r._runProbe = () => probeResult({
            status: 0,
            stdout: 'Rate limits and quota exhaustion: you have reached your usage limit is a common 429 error...',
        });
        // Pure classifier: stdout is NEVER consulted on a successful exit.
        assert.strictEqual(
            r.classifyProbeResult(probeResult({ status: 0, stdout: 'usage limit reached; quota exceeded' })).verdict,
            'healthy',
        );
        // End-to-end probe verdict.
        const healthy = silence(() => r.probeModelHealth({ id: 'claude' }, '/fake/shim'));
        assert.strictEqual(healthy, true);
    });

    it('nonzero exit + stderr quota signature => failover', () => {
        const r = replWith({});
        r._runProbe = () => probeResult({
            status: 1,
            stderr: 'Error: usage limit reached for your plan',
        });
        const healthy = silence(() => r.probeModelHealth({ id: 'claude' }, '/fake/shim'));
        assert.strictEqual(healthy, false);
        assert.strictEqual(
            r.classifyProbeResult(probeResult({ status: 1, stderr: 'you have hit your plan limit' })).verdict,
            'quota_exhausted',
        );
    });

    it('quota words on STDOUT with nonzero exit and clean stderr never classify as quota-dead', () => {
        const r = replWith({});
        const c = r.classifyProbeResult(probeResult({
            status: 1,
            stdout: 'quota usage limit reached', // must be ignored
            stderr: 'segmentation fault',
        }));
        assert.strictEqual(c.verdict, 'probe_error'); // fails over, but not blamed on quota
    });

    it('non-interactive TTY error (codex "stdin is not a terminal") is reachable, NOT a probe failure', () => {
        const r = replWith({});
        // Real codex probe output: it launches then refuses non-interactive stdin.
        assert.strictEqual(
            r.classifyProbeResult(probeResult({ status: 1, stderr: 'Error: stdin is not a terminal' })).verdict,
            'noninteractive',
        );
        assert.strictEqual(
            r.classifyProbeResult(probeResult({ status: 1, stderr: 'raw mode is not supported: not a tty' })).verdict,
            'noninteractive',
        );
        // And probeModelHealth PROCEEDS (returns true) rather than failing over.
        assert.strictEqual(
            silence(() => r.probeModelHealthFromResult
                ? r.probeModelHealthFromResult({ status: 1, stderr: 'stdin is not a terminal' })
                : (r._runProbe = () => probeResult({ status: 1, stderr: 'stdin is not a terminal' }),
                   r.probeModelHealth({ id: 'codex' }, '/fake/shim'))),
            true,
        );
    });

    it('timeout (ETIMEDOUT / SIGTERM / 143) stays healthy-but-slow', () => {
        const r = replWith({});
        assert.strictEqual(r.classifyProbeResult(probeResult({ status: 143 })).verdict, 'slow');
        assert.strictEqual(r.classifyProbeResult(probeResult({ status: null, signal: 'SIGTERM' })).verdict, 'slow');
        assert.strictEqual(r.classifyProbeResult(probeResult({ status: null, error: { code: 'ETIMEDOUT' } })).verdict, 'slow');
    });

    it('transient rate-limit on stderr retries once then proceeds (fail-open)', () => {
        const r = replWith({});
        let calls = 0;
        r._probeBackoff = () => {}; // no real sleep in tests
        r._runProbe = () => { calls += 1; return probeResult({ status: 1, stderr: '429 too many requests' }); };
        const healthy = silence(() => r.probeModelHealth({ id: 'claude' }, '/fake/shim'));
        assert.strictEqual(calls, 2); // initial probe + one retry
        assert.strictEqual(healthy, true); // transient is never blamed on quota
    });

    it('codex is now on the probed launch path and the probe hook is invoked for it', () => {
        const r = replWith({});
        // start() gates the probe on shouldProbe(activeModel.id) — codex is in.
        assert.strictEqual(r.shouldProbe('codex'), true);
        let probedShim = null;
        let calls = 0;
        r._runProbe = (shimPath) => { calls += 1; probedShim = shimPath; return probeResult({ status: 0, stdout: 'ok' }); };
        const healthy = silence(() => r.probeModelHealth({ id: 'codex' }, '/fake/shims/codex'));
        assert.strictEqual(calls, 1);
        assert.strictEqual(probedShim, '/fake/shims/codex');
        assert.strictEqual(healthy, true);
    });
});
