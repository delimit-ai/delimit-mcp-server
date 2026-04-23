/**
 * LED-1075: regression tests for lib/trust-page-engine.js — renders
 * attestations into a static HTML trust page + JSON Feed 1.1.
 *
 * Locks the contract for:
 *   - HTML rendering with attestation table rows
 *   - JSON Feed 1.1 structure
 *   - Signature verification (verified / unverifiable / signature_mismatch)
 *   - Empty-state handling
 */

const { describe, it, before, after } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');
const crypto = require('crypto');

const {
    renderTrustPage,
    loadAttestations,
    verifySignature,
    renderHTML,
    renderFeed,
} = require('../lib/trust-page-engine');

const TMP_ROOT = path.join(os.tmpdir(), 'delimit-trust-page-test-' + crypto.randomBytes(4).toString('hex'));
const ATT_DIR = path.join(TMP_ROOT, 'attestations');
const OUT_DIR = path.join(TMP_ROOT, 'out');
const FAKE_HOME = path.join(TMP_ROOT, 'home');
const ORIG_HOME = os.homedir();

// Mint an HMAC key we control + a few matching attestations
function mintHmacKey() {
    const keyPath = path.join(FAKE_HOME, '.delimit', 'wrap-hmac.key');
    fs.mkdirSync(path.dirname(keyPath), { recursive: true });
    const key = crypto.randomBytes(32);
    fs.writeFileSync(keyPath, key);
    return key;
}

function mintAttestation(id, wrappedCommand, key, mutate = (b) => b) {
    const bundle = mutate({
        schema: 'delimit.attestation.v1',
        kind: 'merge_attestation',
        wrapped_command: wrappedCommand,
        repo_root: TMP_ROOT,
        is_git_repo: false,
        before_head: null,
        after_head: null,
        started_at: new Date(Date.now() - Math.floor(Math.random() * 86400000)).toISOString(),
        completed_at: new Date().toISOString(),
        wrapped_exit: 0,
        changed_files: ['sample.js'],
        governance: { gates: [{ name: 'test_smoke', exit: 0 }], violations: [], advisory: true },
        delimit_wrap_version: '1.1.0',
    });
    const canonical = JSON.stringify(bundle, Object.keys(bundle).sort());
    const signature = crypto.createHmac('sha256', key).update(canonical).digest('hex');
    return { id, bundle, signature, signature_alg: 'HMAC-SHA256' };
}

describe('v43 trust-page: signature verification', () => {
    before(() => {
        fs.mkdirSync(ATT_DIR, { recursive: true });
        fs.mkdirSync(FAKE_HOME, { recursive: true });
        process.env.HOME = FAKE_HOME;
    });
    after(() => {
        process.env.HOME = ORIG_HOME;
        try { fs.rmSync(TMP_ROOT, { recursive: true, force: true }); } catch {}
    });

    it('returns "verified" for a signature matching the stored HMAC key', () => {
        const key = mintHmacKey();
        const att = mintAttestation('att_verify_test01', 'echo ok', key);
        const result = verifySignature(att, key);
        assert.equal(result, 'verified');
    });

    it('returns "signature_mismatch" when the signature is tampered', () => {
        const key = mintHmacKey();
        const att = mintAttestation('att_tampered_001', 'echo ok', key);
        att.signature = 'deadbeef'.repeat(8); // 64 hex chars, obviously wrong
        const result = verifySignature(att, key);
        assert.equal(result, 'signature_mismatch');
    });

    it('returns "unverifiable" when no HMAC key is available', () => {
        const key = mintHmacKey();
        const att = mintAttestation('att_novf_test001', 'echo ok', key);
        const result = verifySignature(att, null);
        assert.equal(result, 'unverifiable');
    });
});

describe('v43 trust-page: render', () => {
    before(() => {
        fs.mkdirSync(ATT_DIR, { recursive: true });
        fs.mkdirSync(FAKE_HOME, { recursive: true });
        process.env.HOME = FAKE_HOME;
        const key = mintHmacKey();
        // Seed 3 attestations with different commands
        for (let i = 0; i < 3; i++) {
            const att = mintAttestation(`att_render_test${String(i).padStart(3,'0')}`, `echo run ${i}`, key);
            fs.writeFileSync(path.join(ATT_DIR, `${att.id}.json`), JSON.stringify(att, null, 2));
        }
    });
    after(() => {
        process.env.HOME = ORIG_HOME;
        try { fs.rmSync(TMP_ROOT, { recursive: true, force: true }); } catch {}
    });

    it('loadAttestations reads every att_*.json file, in reverse chronological order', () => {
        const atts = loadAttestations(ATT_DIR);
        assert.equal(atts.length, 3);
        // Sorted reverse-chronological (newest started_at first)
        for (let i = 0; i < atts.length - 1; i++) {
            const a = atts[i].bundle.started_at;
            const b = atts[i + 1].bundle.started_at;
            assert.ok(a >= b, `att[${i}] (${a}) should be >= att[${i+1}] (${b})`);
        }
    });

    it('renderTrustPage writes index.html + feed.json containing all attestations', () => {
        const result = renderTrustPage(ATT_DIR, OUT_DIR, 'Test Trust Page');
        assert.equal(result.count, 3);
        assert.equal(result.feed_items, 3);
        assert.ok(result.html_bytes > 500, 'HTML should be non-trivial');

        const html = fs.readFileSync(path.join(OUT_DIR, 'index.html'), 'utf-8');
        assert.ok(html.includes('Test Trust Page'), 'HTML must contain title');
        assert.ok(html.includes('att_render_test000'), 'HTML must reference first att_id');
        assert.ok(html.includes('att_render_test001'), 'HTML must reference second att_id');
        assert.ok(html.includes('att_render_test002'), 'HTML must reference third att_id');
        assert.ok(html.includes('verified'), 'HTML must show verified signature badge');

        const feed = JSON.parse(fs.readFileSync(path.join(OUT_DIR, 'feed.json'), 'utf-8'));
        assert.equal(feed.version, 'https://jsonfeed.org/version/1.1');
        assert.equal(feed.items.length, 3);
        for (const item of feed.items) {
            assert.match(item.id, /^att_render_test/);
            assert.ok(item._delimit.signature, 'each feed item must carry signature metadata');
            assert.equal(item._delimit.signature_alg, 'HMAC-SHA256');
        }
    });
});

describe('v43 trust-page: empty-state', () => {
    it('renders a helpful empty message when the attestation directory is empty', () => {
        const emptyDir = path.join(TMP_ROOT, 'empty-' + crypto.randomBytes(3).toString('hex'));
        const outDir = path.join(TMP_ROOT, 'empty-out-' + crypto.randomBytes(3).toString('hex'));
        fs.mkdirSync(emptyDir, { recursive: true });
        const result = renderTrustPage(emptyDir, outDir, 'Empty');
        assert.equal(result.count, 0);
        const html = fs.readFileSync(path.join(outDir, 'index.html'), 'utf-8');
        assert.ok(
            html.includes('No attestations yet') || html.includes('delimit wrap'),
            'empty state must guide user to delimit wrap'
        );
    });
});

describe('v43 trust-page: feed/HTML primitive functions', () => {
    it('renderHTML handles a single attestation cleanly', () => {
        const key = Buffer.from('test-key-32-bytes---------------');
        const att = mintAttestation('att_single_test01', 'echo one', key);
        const html = renderHTML([att], 'Single Attestation');
        assert.ok(html.startsWith('<!doctype html>'));
        assert.ok(html.includes('att_single_test01'));
        assert.ok(html.includes('Single Attestation'));
    });

    it('renderFeed produces valid JSON Feed 1.1 items with _delimit extension', () => {
        const key = Buffer.from('test-key-32-bytes---------------');
        const att = mintAttestation('att_feed_test_001', 'echo feed', key);
        const feed = renderFeed([att], 'Feed Test');
        assert.equal(feed.version, 'https://jsonfeed.org/version/1.1');
        assert.equal(feed.items.length, 1);
        const item = feed.items[0];
        assert.equal(item.id, 'att_feed_test_001');
        assert.ok(item._delimit);
        assert.equal(item._delimit.attestation_id, 'att_feed_test_001');
    });
});
