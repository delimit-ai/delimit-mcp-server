/**
 * LED-1075: regression tests for lib/wrap-engine.js — the v4.3 delimit wrap
 * subcommand that gates any AI-assisted CLI with a signed, replayable
 * attestation. Locks the contract for:
 *   - delimit.attestation.v1 schema (bundle shape, kind enum)
 *   - HMAC-SHA256 signature generation + verification
 *   - Kill switch (--max-time) emitting kind=liability_incident + handoff suggestion
 *   - Free-tier lifetime quota
 *
 * Run by:  npm test
 */

const { describe, it, before, after } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');
const crypto = require('crypto');
const { execSync } = require('child_process');

const {
    runWrap,
    computeAttestationId,
    signAttestation,
    checkQuota,
    replayUrl,
} = require('../lib/wrap-engine');

// ----- test sandbox ---------------------------------------------------------

const SANDBOX = path.join(os.tmpdir(), 'delimit-wrap-test-' + crypto.randomBytes(4).toString('hex'));
const ATT_HOME = path.join(SANDBOX, '.delimit-home');
const ORIG_HOME = os.homedir();

function setupSandboxRepo() {
    fs.mkdirSync(SANDBOX, { recursive: true });
    fs.mkdirSync(ATT_HOME, { recursive: true });
    // Use a tmp HOME so we don't touch the user's real ~/.delimit/attestations
    process.env.HOME = ATT_HOME;
    // Fresh git repo
    execSync('git init -q', { cwd: SANDBOX });
    execSync('git config user.email "test@delimit.test"', { cwd: SANDBOX });
    execSync('git config user.name "delimit-test"', { cwd: SANDBOX });
    fs.writeFileSync(path.join(SANDBOX, 'README.md'), '# sandbox\n');
    execSync('git add . && git commit -qm init', { cwd: SANDBOX });
}

function teardownSandbox() {
    process.env.HOME = ORIG_HOME;
    try { fs.rmSync(SANDBOX, { recursive: true, force: true }); } catch {}
}

// ----- attestation round-trip ----------------------------------------------

describe('v43 wrap: attestation round-trip', () => {
    before(setupSandboxRepo);
    after(teardownSandbox);

    it('produces a kind=merge_attestation bundle with all required v1 fields', async () => {
        const result = await runWrap(['echo', 'hello-from-test'], { cwd: SANDBOX });

        assert.ok(result.attestation_id, 'attestation_id must exist');
        assert.match(result.attestation_id, /^att_[a-f0-9]{16}$/, 'att_id must match att_[16hex] format');
        assert.equal(result.kind, 'merge_attestation');
        assert.equal(result.wrapped_exit, 0);
        assert.equal(result.advisory, true);
        assert.equal(typeof result.replay_url, 'string');
        assert.ok(result.replay_url.includes(result.attestation_id), 'replay URL must reference the att_id');

        // Persisted on disk with correct schema
        assert.ok(fs.existsSync(result.attestation_path), 'attestation file must exist');
        const att = JSON.parse(fs.readFileSync(result.attestation_path, 'utf-8'));
        assert.equal(att.id, result.attestation_id);
        assert.equal(att.bundle.schema, 'delimit.attestation.v1');
        assert.equal(att.bundle.kind, 'merge_attestation');
        assert.equal(att.bundle.wrapped_exit, 0);
        assert.equal(typeof att.bundle.started_at, 'string');
        assert.equal(typeof att.bundle.completed_at, 'string');
        assert.equal(att.signature_alg, 'HMAC-SHA256');
        assert.match(att.signature, /^[a-f0-9]{64}$/, 'signature must be 64 hex chars');
        assert.ok(Array.isArray(att.bundle.governance.gates));
    });

    it('signature verifies + LED-1180 nested-tamper regression', async () => {
        const result = await runWrap(['true'], { cwd: SANDBOX });
        const att = JSON.parse(fs.readFileSync(result.attestation_path, 'utf-8'));
        const key = fs.readFileSync(path.join(ATT_HOME, '.delimit', 'wrap-hmac.key'));
        const { canonicalize } = require('../lib/wrap-engine');

        // Recomputed HMAC equals stored signature
        const expected = crypto.createHmac('sha256', key).update(canonicalize(att.bundle)).digest('hex');
        assert.equal(expected, att.signature, 'recomputed HMAC must equal stored signature');

        // LED-1180 regression: tampering a NESTED field MUST change the signature.
        // Pre-fix canonicalize used JSON.stringify(bundle, Object.keys(bundle).sort()),
        // which treats the second arg as a property allowlist (only top-level keys),
        // so nested objects serialised as {} and the HMAC committed only to shape.
        const tampered = JSON.parse(JSON.stringify(att.bundle));
        if (!tampered.governance) tampered.governance = {};
        tampered.governance.violations = [{ injected: 'malicious-rule' }];
        const tamperedSig = crypto.createHmac('sha256', key).update(canonicalize(tampered)).digest('hex');
        assert.notEqual(
            tamperedSig,
            att.signature,
            'nested-field tamper MUST change the signature; if equal, canonicalize is silently dropping nested keys'
        );
    });

    it('detects changed files in the wrapped command output', async () => {
        const result = await runWrap(
            ['sh', '-c', 'echo "// added by test" > new-file.txt'],
            { cwd: SANDBOX }
        );
        const att = JSON.parse(fs.readFileSync(result.attestation_path, 'utf-8'));
        assert.ok(att.bundle.changed_files.length >= 1, 'at least one changed file expected');
        assert.ok(
            att.bundle.changed_files.some(f => f.includes('new-file.txt')),
            'changed_files must include new-file.txt'
        );
    });
});

// ----- kill switch ----------------------------------------------------------

describe('v43 wrap: kill switch (--max-time)', () => {
    before(setupSandboxRepo);
    after(teardownSandbox);

    it('SIGKILLs the wrapped command when wall-clock exceeds --max-time', async () => {
        const start = Date.now();
        const result = await runWrap(['sleep', '10'], {
            cwd: SANDBOX,
            maxTimeSeconds: 1,
        });
        const elapsed = Date.now() - start;

        assert.ok(elapsed < 3000, `should kill within ~1s, elapsed=${elapsed}ms`);
        assert.equal(result.wrapped_exit, 137, 'SIGKILL exit should be 137');
        assert.equal(result.kind, 'liability_incident');
        assert.equal(result.killed_by_timeout, true);
    });

    it('emits a handoff_suggestion when the killed command maps to a known producer', async () => {
        // Use node itself as a long-running stand-in for the producer CLI (always available in CI).
        // argv[0] must still match a known producer in suggestHandoff's table, so we use a wrapper
        // script named like a producer via a symlink trick below, OR we test suggestHandoff directly.
        // Simpler and portable: create a tiny shim executable named "claude" in the sandbox.
        const shim = path.join(SANDBOX, 'claude');
        fs.writeFileSync(shim, `#!/usr/bin/env node\nsetInterval(()=>{},1000);\n`);
        fs.chmodSync(shim, 0o755);

        const result = await runWrap([shim, '-p', 'this will not actually complete'], {
            cwd: SANDBOX,
            maxTimeSeconds: 1,
        });

        assert.equal(result.killed_by_timeout, true, 'the shim should run long enough to hit --max-time');
        const att = JSON.parse(fs.readFileSync(result.attestation_path, 'utf-8'));
        assert.equal(att.bundle.kind, 'liability_incident');
        assert.ok(result.handoff_suggestion, 'handoff_suggestion must be present on kill');
        assert.equal(result.handoff_suggestion.kill_source, 'claude');
        assert.ok(
            Array.isArray(result.handoff_suggestion.alternates) &&
            result.handoff_suggestion.alternates.length >= 2,
            'at least 2 alternate producers'
        );
        assert.ok(
            result.handoff_suggestion.suggested_command.startsWith('delimit wrap --'),
            'suggested_command must be a runnable delimit wrap invocation'
        );
    });
});

// ----- quota ----------------------------------------------------------------

describe('v43 wrap: free-tier quota', () => {
    before(setupSandboxRepo);
    after(teardownSandbox);

    it('reports quota status accurately for a fresh install', () => {
        // ATT_HOME is fresh, so counter is 0 and license file absent → free tier
        const q = checkQuota();
        assert.equal(q.tier, 'free');
        assert.equal(q.ok, true);
        assert.equal(q.limit, 3);
        assert.equal(q.count, 0);
    });

    it('returns error=quota_exceeded once 3 lifetime attestations have been emitted', async () => {
        // Simulate prior usage by writing count=3 directly
        const counterPath = path.join(ATT_HOME, '.delimit', 'wrap-lifetime-count');
        fs.mkdirSync(path.dirname(counterPath), { recursive: true });
        fs.writeFileSync(counterPath, '3');

        const result = await runWrap(['echo', 'should-be-blocked'], { cwd: SANDBOX });
        assert.equal(result.error, 'quota_exceeded');
        assert.ok(result.message.includes('Upgrade to Pro'), 'must surface upgrade path');
    });
});

// ----- deterministic helpers ------------------------------------------------

describe('v43 wrap: deterministic helpers', () => {
    it('computeAttestationId is stable for the same bundle', () => {
        const bundle = { schema: 'delimit.attestation.v1', a: 1, b: 'x' };
        const id1 = computeAttestationId(bundle);
        const id2 = computeAttestationId(bundle);
        assert.equal(id1, id2);
        assert.match(id1, /^att_[a-f0-9]{16}$/);
    });

    it('computeAttestationId differs when the bundle differs', () => {
        const a = computeAttestationId({ schema: 'delimit.attestation.v1', wrapped_command: 'echo a' });
        const b = computeAttestationId({ schema: 'delimit.attestation.v1', wrapped_command: 'echo b' });
        assert.notEqual(a, b);
    });

    it('replayUrl targets the delimit.ai canonical pattern', () => {
        const url = replayUrl('att_abcdef1234567890');
        assert.equal(url, 'https://delimit.ai/att/att_abcdef1234567890');
    });
});
