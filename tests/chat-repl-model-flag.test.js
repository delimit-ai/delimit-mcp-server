const { describe, it, beforeEach } = require('node:test');
const assert = require('node:assert');

// Module under test — the Auto-Phoenix launcher's `--model <id>` per-launch
// override (getActiveChain). The flag must reorder the chain WITHOUT mutating
// models.json, must leave the default chain intact when absent, and must
// degrade gracefully (warn once, fall back) on an unknown id.
process.env.DELIMIT_WRAPPED = 'true';
const { DelimitChatREPL } = require('../lib/chat-repl');

// A controlled models fixture so the test does not depend on ~/.delimit/models.json.
const FIXTURE = {
    claude: { auth_mode: 'chat_login' },
    codex: { auth_mode: 'chat_login' },
    antigravity: { auth_mode: 'chat_login' },
    grok: { api_key: 'x' }, // api-only, no chat_login
    fallbacks: { default: ['claude', 'codex', 'antigravity'] },
};

function replWith(options) {
    const r = new DelimitChatREPL(options);
    r.modelsConfig = JSON.parse(JSON.stringify(FIXTURE));
    return r;
}

function silence(fn) {
    const orig = console.log;
    console.log = () => {};
    try { return fn(); } finally { console.log = orig; }
}

describe('chat --model <id> per-launch override', () => {
    it('uses the default chain unchanged when --model is absent', () => {
        const ids = replWith({}).getActiveChain().map(m => m.id);
        assert.deepStrictEqual(ids, ['claude', 'codex', 'antigravity']);
    });

    it('launches the requested model first, keeping the rest as fallback', () => {
        const ids = replWith({ model: 'codex' }).getActiveChain().map(m => m.id);
        assert.deepStrictEqual(ids, ['codex', 'claude', 'antigravity']);
    });

    it('does not duplicate the model if it is already in the chain', () => {
        const ids = replWith({ model: 'antigravity' }).getActiveChain().map(m => m.id);
        assert.deepStrictEqual(ids, ['antigravity', 'claude', 'codex']);
    });

    it('honors an explicitly-requested api-only model even without --api-fallback', () => {
        // grok is api-only and not in the default chain; --model must surface it.
        const ids = replWith({ model: 'grok' }).getActiveChain().map(m => m.id);
        assert.strictEqual(ids[0], 'grok');
    });

    it('warns once and falls back to the default chain on an unknown id', () => {
        const r = replWith({ model: 'nope' });
        const ids = silence(() => r.getActiveChain().map(m => m.id));
        assert.deepStrictEqual(ids, ['claude', 'codex', 'antigravity']);
        // launchModel is cleared after the warning so it never repeats.
        assert.strictEqual(r.launchModel, null);
    });

    it('never mutates fallbacks.default in the loaded config', () => {
        const r = replWith({ model: 'codex' });
        silence(() => r.getActiveChain());
        assert.deepStrictEqual(r.modelsConfig.fallbacks.default, ['claude', 'codex', 'antigravity']);
    });
});
