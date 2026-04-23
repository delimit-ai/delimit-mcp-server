/**
 * LED-1075: regression tests for lib/ai-sbom-engine.js — aggregates
 * attestations into a CycloneDX 1.6 bill of materials with AI-specific fields.
 *
 * Locks the contract for:
 *   - CycloneDX 1.6 schema shape (bomFormat, specVersion, components[].type=machine-learning-model)
 *   - Model detection across wrapped_command strings (anthropic, openai, gemini, cursor, aider, codex, grok, copilot)
 *   - Aggregate metadata properties (total_attestations, total_gates_run, total_violations)
 *   - Empty-directory handling
 */

const { describe, it, before, after } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');
const crypto = require('crypto');

const {
    buildAISBOM,
    aggregateAISurface,
    renderCycloneDXAI,
    detectModelFromCommand,
} = require('../lib/ai-sbom-engine');

const TMP_ROOT = path.join(os.tmpdir(), 'delimit-ai-sbom-test-' + crypto.randomBytes(4).toString('hex'));
const ATT_DIR = path.join(TMP_ROOT, 'attestations');

function mintAttestation(id, wrappedCommand, { violations = 0, gates = 1, started_at = null } = {}) {
    return {
        id,
        bundle: {
            schema: 'delimit.attestation.v1',
            kind: 'merge_attestation',
            wrapped_command: wrappedCommand,
            started_at: started_at || new Date().toISOString(),
            completed_at: new Date().toISOString(),
            wrapped_exit: 0,
            changed_files: ['foo.js'],
            governance: {
                gates: Array.from({ length: gates }, (_, i) => ({ name: `gate_${i}`, exit: 0 })),
                violations: Array.from({ length: violations }, (_, i) => `violation_${i}`),
                advisory: true,
            },
            delimit_wrap_version: '1.1.0',
        },
        signature: 'x'.repeat(64),
        signature_alg: 'HMAC-SHA256',
    };
}

// ----- detectModelFromCommand ----------------------------------------------

describe('v43 ai-sbom: detectModelFromCommand', () => {
    it('detects anthropic/claude from claude -p', () => {
        const m = detectModelFromCommand('claude -p "add tests"');
        assert.deepEqual(m, { vendor: 'anthropic', family: 'claude' });
    });

    it('detects google/gemini from gemini invocation', () => {
        const m = detectModelFromCommand('gemini chat "something"');
        assert.deepEqual(m, { vendor: 'google', family: 'gemini' });
    });

    it('detects openai/gpt from gpt-4 reference', () => {
        const m = detectModelFromCommand('openai gpt-4 prompt');
        assert.deepEqual(m, { vendor: 'openai', family: 'gpt' });
    });

    it('detects cursor/cursor-agent from cursor edit', () => {
        const m = detectModelFromCommand('cursor edit "refactor auth"');
        assert.deepEqual(m, { vendor: 'cursor', family: 'cursor-agent' });
    });

    it('detects xai/grok from grok-mention', () => {
        const m = detectModelFromCommand('xai grok-4 "reason about this"');
        assert.deepEqual(m, { vendor: 'xai', family: 'grok' });
    });

    it('returns null for an unknown command', () => {
        const m = detectModelFromCommand('echo hello');
        assert.equal(m, null);
    });

    it('returns null for empty/null input', () => {
        assert.equal(detectModelFromCommand(''), null);
        assert.equal(detectModelFromCommand(null), null);
    });
});

// ----- aggregateAISurface --------------------------------------------------

describe('v43 ai-sbom: aggregateAISurface', () => {
    it('counts attestations, gates, and violations correctly', () => {
        const atts = [
            mintAttestation('att_a_001', 'claude -p a', { gates: 2, violations: 0 }),
            mintAttestation('att_a_002', 'cursor edit b', { gates: 3, violations: 1 }),
            mintAttestation('att_a_003', 'gemini c',     { gates: 1, violations: 2 }),
        ];
        const agg = aggregateAISurface(atts);
        assert.equal(agg.total_attestations, 3);
        assert.equal(agg.total_gates_run, 6);
        assert.equal(agg.total_violations, 3);
    });

    it('detects distinct model vendor/family pairs from wrapped_command strings', () => {
        const atts = [
            mintAttestation('att_m_001', 'claude -p "x"'),
            mintAttestation('att_m_002', 'claude -p "y"'),    // same vendor/family → should count, not duplicate
            mintAttestation('att_m_003', 'cursor edit "z"'),
            mintAttestation('att_m_004', 'gemini chat "q"'),
        ];
        const agg = aggregateAISurface(atts);
        const keys = agg.models.map(m => `${m.vendor}/${m.family}`).sort();
        assert.deepEqual(keys, ['anthropic/claude', 'cursor/cursor-agent', 'google/gemini']);

        const claude = agg.models.find(m => m.vendor === 'anthropic');
        assert.equal(claude.count, 2, 'anthropic/claude seen twice');
    });

    it('returns earliest and latest timestamps spanning the attestation set', () => {
        const atts = [
            mintAttestation('att_t_001', 'claude', { started_at: '2026-04-01T00:00:00.000Z' }),
            mintAttestation('att_t_002', 'claude', { started_at: '2026-04-23T00:00:00.000Z' }),
            mintAttestation('att_t_003', 'claude', { started_at: '2026-04-10T00:00:00.000Z' }),
        ];
        const agg = aggregateAISurface(atts);
        assert.equal(agg.earliest, '2026-04-01T00:00:00.000Z');
        assert.equal(agg.latest, '2026-04-23T00:00:00.000Z');
    });

    it('handles an empty set cleanly', () => {
        const agg = aggregateAISurface([]);
        assert.equal(agg.total_attestations, 0);
        assert.equal(agg.models.length, 0);
        assert.equal(agg.earliest, null);
        assert.equal(agg.latest, null);
    });
});

// ----- renderCycloneDXAI ---------------------------------------------------

describe('v43 ai-sbom: CycloneDX 1.6 schema shape', () => {
    it('produces a valid CycloneDX 1.6 document with machine-learning-model components', () => {
        const atts = [
            mintAttestation('att_s_001', 'claude -p a'),
            mintAttestation('att_s_002', 'cursor edit b'),
            mintAttestation('att_s_003', 'gemini c'),
        ];
        const agg = aggregateAISurface(atts);
        const sbom = renderCycloneDXAI(agg, { name: 'test-bom', version: '1.2.3' });

        assert.equal(sbom.bomFormat, 'CycloneDX');
        assert.equal(sbom.specVersion, '1.6');
        assert.match(sbom.serialNumber, /^urn:uuid:[0-9a-f-]+$/, 'serialNumber must be urn:uuid');
        assert.equal(sbom.metadata.tools[0].vendor, 'delimit');
        assert.equal(sbom.metadata.tools[0].name, 'delimit-ai-sbom');
        assert.equal(sbom.metadata.component.name, 'test-bom');
        assert.equal(sbom.metadata.component.version, '1.2.3');

        const props = Object.fromEntries(sbom.metadata.properties.map(p => [p.name, p.value]));
        assert.equal(props['delimit:total_attestations'], '3');

        assert.equal(sbom.components.length, 3);
        for (const c of sbom.components) {
            assert.equal(c.type, 'machine-learning-model');
            assert.ok(c.vendor);
            assert.ok(c.name);
            assert.ok(c['bom-ref'].startsWith('model:'));
            assert.ok(c.modelCard, 'each component must have a modelCard');
        }
    });

    it('buildAISBOM combines load + aggregate + render for a directory', () => {
        fs.mkdirSync(ATT_DIR, { recursive: true });
        const atts = [
            mintAttestation('att_b_001', 'claude -p a'),
            mintAttestation('att_b_002', 'aider --message b'),
        ];
        for (const a of atts) {
            fs.writeFileSync(path.join(ATT_DIR, `${a.id}.json`), JSON.stringify(a));
        }

        const { sbom, aggregate, attestation_count } = buildAISBOM(ATT_DIR, { name: 'dir-bom' });
        assert.equal(attestation_count, 2);
        assert.equal(aggregate.total_attestations, 2);
        assert.ok(aggregate.models.length >= 2, 'should detect >=2 producers (claude, aider)');
        assert.equal(sbom.bomFormat, 'CycloneDX');

        try { fs.rmSync(TMP_ROOT, { recursive: true, force: true }); } catch {}
    });
});
