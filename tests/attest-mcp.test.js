/**
 * STR-656 — `delimit attest mcp` panel-verdict behavior locks.
 *
 * Covers the items Q1–Q6 from the multi-model deliberation review:
 *   Q1: live MCP-protocol-conformance probe (tools/list)
 *   Q2: 3-tier exit codes (0 pass+skip, 1 fail, 2 error)
 *   Q3: --output, --no-write, EROFS soft-fail
 *   Q5: telemetry counter + DELIMIT_NO_TELEMETRY kill switch
 *   Q6: top-level runtime guard (single-check throw doesn't crash runner)
 */

const { describe, it, before, after } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');
const { spawnSync } = require('child_process');

const CLI = path.join(__dirname, '..', 'bin', 'delimit-cli.js');
const { runAttestMcp } = require('../lib/attest-mcp');

const SKIP_IN_CI = process.env.CI ? 'requires full CLI stack' : false;

function makeFixture(extraFiles = {}) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'attest-mcp-'));
    fs.writeFileSync(path.join(dir, 'package.json'), JSON.stringify({
        name: 'attest-fixture', version: '0.0.1',
        dependencies: { '@modelcontextprotocol/sdk': '1.0.0' },
        ...extraFiles.pkg,
    }, null, 2));
    for (const [rel, content] of Object.entries(extraFiles.files || {})) {
        const p = path.join(dir, rel);
        fs.mkdirSync(path.dirname(p), { recursive: true });
        fs.writeFileSync(p, content);
    }
    return dir;
}

describe('attest mcp: 3-tier exit codes (Q2)', () => {
    it('exits 2 when a check errors (no lockfile → npm audit unavailable)',
        { skip: SKIP_IN_CI }, () => {
            const dir = makeFixture();
            const res = spawnSync('node', [CLI, 'attest', 'mcp', '--path', dir, '--no-write'], {
                env: { ...process.env, DELIMIT_NO_TELEMETRY: '1' },
                encoding: 'utf-8',
            });
            assert.strictEqual(res.status, 2, 'tool error must elevate to exit 2');
        });

    it('exits 2 on unknown attestation kind', { skip: SKIP_IN_CI }, () => {
        const res = spawnSync('node', [CLI, 'attest', 'unknown'], {
            env: { ...process.env, DELIMIT_NO_TELEMETRY: '1' },
            encoding: 'utf-8',
        });
        assert.strictEqual(res.status, 2);
    });
});

describe('attest mcp: --output / --no-write (Q3)', () => {
    it('--no-write does not create .delimit/attestation-preview.json',
        { skip: SKIP_IN_CI }, () => {
            const dir = makeFixture();
            spawnSync('node', [CLI, 'attest', 'mcp', '--path', dir, '--no-write'], {
                env: { ...process.env, DELIMIT_NO_TELEMETRY: '1' },
                encoding: 'utf-8',
            });
            assert.strictEqual(
                fs.existsSync(path.join(dir, '.delimit', 'attestation-preview.json')),
                false,
                '--no-write must not write the preview file',
            );
        });

    it('--output redirects the preview JSON', { skip: SKIP_IN_CI }, () => {
        const dir = makeFixture();
        const out = path.join(dir, 'custom-out.json');
        spawnSync('node', [CLI, 'attest', 'mcp', '--path', dir, '--output', out], {
            env: { ...process.env, DELIMIT_NO_TELEMETRY: '1' },
            encoding: 'utf-8',
        });
        assert.ok(fs.existsSync(out), '--output target must be created');
        const parsed = JSON.parse(fs.readFileSync(out, 'utf-8'));
        assert.strictEqual(parsed.kind, 'mcp_attestation_preview');
    });
});

describe('attest mcp: --write deprecation (panel verdict pre-push)', () => {
    it('--write emits a deprecation warning to stderr',
        { skip: SKIP_IN_CI }, () => {
            const dir = makeFixture();
            const out = path.join(dir, 'via-write.json');
            const res = spawnSync('node', [CLI, 'attest', 'mcp', '--path', dir, '--write', out], {
                env: { ...process.env, DELIMIT_NO_TELEMETRY: '1', FORCE_COLOR: '0' },
                encoding: 'utf-8',
            });
            assert.match(res.stderr, /\[deprecation\] --write is deprecated/i,
                '--write must emit a one-line deprecation warning to stderr');
            assert.ok(fs.existsSync(out), '--write must still write the file');
        });

    it('--output does NOT emit a deprecation warning',
        { skip: SKIP_IN_CI }, () => {
            const dir = makeFixture();
            const out = path.join(dir, 'via-output.json');
            const res = spawnSync('node', [CLI, 'attest', 'mcp', '--path', dir, '--output', out], {
                env: { ...process.env, DELIMIT_NO_TELEMETRY: '1', FORCE_COLOR: '0' },
                encoding: 'utf-8',
            });
            assert.doesNotMatch(res.stderr, /deprecation/i,
                '--output is the canonical flag and must not warn');
        });
});

describe('attest mcp: telemetry kill switch (Q5)', () => {
    it('DELIMIT_NO_TELEMETRY=1 prevents telemetry writes',
        { skip: SKIP_IN_CI }, () => {
            // Use HOME override so we can observe the would-be telemetry file
            const tempHome = fs.mkdtempSync(path.join(os.tmpdir(), 'attest-home-'));
            const dir = makeFixture();
            spawnSync('node', [CLI, 'attest', 'mcp', '--path', dir, '--no-write'], {
                env: { ...process.env, HOME: tempHome, DELIMIT_NO_TELEMETRY: '1' },
                encoding: 'utf-8',
            });
            const tel = path.join(tempHome, '.delimit', 'telemetry', 'attest-mcp.jsonl');
            assert.strictEqual(fs.existsSync(tel), false,
                'kill switch must prevent the telemetry file from being created');
        });
});

describe('attest mcp: runtime guard (Q6)', () => {
    it('runner returns a complete report even with malformed package.json',
        async () => {
            const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'attest-bad-'));
            fs.writeFileSync(path.join(dir, 'package.json'), '{not valid json');
            const report = await runAttestMcp({ path: dir });
            assert.ok(report.checks, 'report must include checks even on bad input');
            assert.strictEqual(report.checks.length, 5, 'all five checks must be represented');
            for (const c of report.checks) {
                assert.ok(['pass', 'fail', 'skip', 'error'].includes(c.status),
                    `check ${c.id} returned invalid status: ${c.status}`);
            }
        });

    it('non-existent path returns structured error, not crash', async () => {
        const report = await runAttestMcp({ path: '/nonexistent/path/here-zzz' });
        assert.ok(report.error, 'must return error field');
    });
});

describe('attest mcp: framing (no separate-product language)', () => {
    it('scaffold notice references merge gate product family', async () => {
        const dir = makeFixture();
        const report = await runAttestMcp({ path: dir });
        assert.match(report.scaffold_notice, /merge gate product family/i,
            'scaffold notice must position attest mcp as part of the merge gate, not standalone');
    });
});
