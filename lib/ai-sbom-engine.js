// lib/ai-sbom-engine.js
//
// LED-1018 Venture #6 MVP: `delimit ai-sbom` aggregation.
// Scans a directory of attestations, extracts the AI surface (models, prompts,
// tool calls, data classes), and emits a CycloneDX 1.6-shaped bill of materials
// with AI-specific fields.
//
// CycloneDX-AI schema reference: https://cyclonedx.org/capabilities/mlbom/
// Per the architecture doc, MVP aggregates what attestations already capture;
// explicit static-analysis code-walker lands in Phase 2.

const { loadAttestations } = require('./trust-page-engine');
const crypto = require('crypto');

const KNOWN_MODEL_PROVIDERS = [
    // Loose matching against wrapped_command strings. Extends easily.
    { pattern: /claude|anthropic/i, vendor: 'anthropic', family: 'claude' },
    { pattern: /openai|gpt-\d|o1|o3/i, vendor: 'openai', family: 'gpt' },
    { pattern: /gemini|vertex/i, vendor: 'google', family: 'gemini' },
    { pattern: /codex/i, vendor: 'openai', family: 'codex' },
    { pattern: /grok|xai/i, vendor: 'xai', family: 'grok' },
    { pattern: /llama|mistral/i, vendor: 'meta-or-mistral', family: 'open-weight' },
    { pattern: /cursor/i, vendor: 'cursor', family: 'cursor-agent' },
    { pattern: /aider/i, vendor: 'aider', family: 'aider-agent' },
    { pattern: /copilot/i, vendor: 'github', family: 'copilot' },
];

function detectModelFromCommand(cmd) {
    if (!cmd) return null;
    for (const m of KNOWN_MODEL_PROVIDERS) {
        if (m.pattern.test(cmd)) return { vendor: m.vendor, family: m.family };
    }
    return null;
}

function aggregateAISurface(attestations) {
    const models = new Map(); // key: vendor:family -> { count, first_seen, last_seen }
    const toolCallCounts = new Map(); // tool name -> count
    const totalAttestations = attestations.length;
    let earliest = null, latest = null;
    let totalGatesRun = 0, totalViolations = 0;

    for (const att of attestations) {
        const b = att.bundle || {};

        // Model detection — prefer explicit ai_surface.models_detected, fall back to command heuristic
        const explicitModels = (b.ai_surface?.models_detected) || [];
        for (const m of explicitModels) {
            const key = m.includes(':') ? m : `unknown:${m}`;
            const [vendor, family] = key.split(':', 2);
            const entry = models.get(key) || { vendor, family, count: 0, first_seen: null, last_seen: null };
            entry.count += 1;
            if (b.started_at) {
                if (!entry.first_seen || b.started_at < entry.first_seen) entry.first_seen = b.started_at;
                if (!entry.last_seen || b.started_at > entry.last_seen) entry.last_seen = b.started_at;
            }
            models.set(key, entry);
        }
        if (explicitModels.length === 0) {
            const inferred = detectModelFromCommand(b.wrapped_command || '');
            if (inferred) {
                const key = `${inferred.vendor}:${inferred.family}`;
                const entry = models.get(key) || { vendor: inferred.vendor, family: inferred.family, count: 0, first_seen: null, last_seen: null, source: 'inferred' };
                entry.count += 1;
                entry.source = 'inferred';
                if (b.started_at) {
                    if (!entry.first_seen || b.started_at < entry.first_seen) entry.first_seen = b.started_at;
                    if (!entry.last_seen || b.started_at > entry.last_seen) entry.last_seen = b.started_at;
                }
                models.set(key, entry);
            }
        }

        // Tool calls
        const tools = b.ai_surface?.tool_calls || [];
        for (const t of tools) toolCallCounts.set(t, (toolCallCounts.get(t) || 0) + 1);

        // Timestamps
        if (b.started_at) {
            if (!earliest || b.started_at < earliest) earliest = b.started_at;
            if (!latest || b.started_at > latest) latest = b.started_at;
        }

        // Governance counts
        totalGatesRun += (b.governance?.gates || []).length;
        totalViolations += (b.governance?.violations || []).length;
    }

    return {
        total_attestations: totalAttestations,
        total_gates_run: totalGatesRun,
        total_violations: totalViolations,
        models: Array.from(models.values()),
        tool_calls: Array.from(toolCallCounts.entries()).map(([name, count]) => ({ name, count })),
        earliest,
        latest,
    };
}

function renderCycloneDXAI(aggregate, { name = 'ai-sbom', version = '1.0.0' } = {}) {
    const serialNumber = 'urn:uuid:' + crypto.randomUUID();

    return {
        bomFormat: 'CycloneDX',
        specVersion: '1.6',
        serialNumber,
        version: 1,
        metadata: {
            timestamp: new Date().toISOString(),
            tools: [{ vendor: 'delimit', name: 'delimit-ai-sbom', version }],
            component: {
                'bom-ref': `pkg:${name}@${version}`,
                type: 'application',
                name,
                version,
            },
            properties: [
                { name: 'delimit:total_attestations', value: String(aggregate.total_attestations) },
                { name: 'delimit:total_gates_run', value: String(aggregate.total_gates_run) },
                { name: 'delimit:total_violations', value: String(aggregate.total_violations) },
                { name: 'delimit:earliest_attestation', value: aggregate.earliest || '' },
                { name: 'delimit:latest_attestation', value: aggregate.latest || '' },
                { name: 'delimit:tool_surface', value: JSON.stringify(aggregate.tool_calls) },
            ],
        },
        components: aggregate.models.map(m => ({
            'bom-ref': `model:${m.vendor}/${m.family}`,
            type: 'machine-learning-model',
            vendor: m.vendor,
            name: m.family,
            description: `AI model detected across ${m.count} attestations${m.source ? ` (${m.source} from command)` : ''}`,
            modelCard: {
                modelParameters: {
                    approach: { type: 'supervised' },
                },
                properties: [
                    { name: 'delimit:usage_count', value: String(m.count) },
                    { name: 'delimit:first_seen', value: m.first_seen || '' },
                    { name: 'delimit:last_seen', value: m.last_seen || '' },
                    ...(m.source ? [{ name: 'delimit:detection_source', value: m.source }] : []),
                ],
            },
        })),
    };
}

function buildAISBOM(attestationDir, opts = {}) {
    const attestations = loadAttestations(attestationDir);
    const aggregate = aggregateAISurface(attestations);
    const sbom = renderCycloneDXAI(aggregate, opts);
    return { sbom, aggregate, attestation_count: attestations.length };
}

module.exports = { buildAISBOM, aggregateAISurface, renderCycloneDXAI, detectModelFromCommand };
