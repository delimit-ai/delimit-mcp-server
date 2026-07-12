const { describe, it, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert');

// Module under test — the Auto-Phoenix session launcher. We exercise ONLY the
// Fable doc-33 launch pre-brief surface (getControlBrief / printLaunchPrebrief).
// The pre-brief is a best-effort, hard-bounded, non-blocking add: it must never
// throw to the caller and never delay/block the session launch.
process.env.DELIMIT_WRAPPED = 'true';
const { DelimitChatREPL } = require('../lib/chat-repl');

// Capture console.log output for a single call.
function capture(fn) {
    const orig = console.log;
    const lines = [];
    console.log = (...args) => { lines.push(args.join(' ')); };
    try {
        fn();
    } finally {
        console.log = orig;
    }
    return lines.join('\n');
}

describe('chat launch pre-brief (Fable doc-33)', () => {
    let repl;
    beforeEach(() => { repl = new DelimitChatREPL(); });

    it('renders the one-line brief with counts when the backend returns data', () => {
        repl.getControlBrief = () => ({ approvals: 39, agents: 25, p0s: 251 });
        const out = capture(() => repl.printLaunchPrebrief());
        assert.match(out, /39 approvals waiting/);
        assert.match(out, /25 agents running/);
        assert.match(out, /251 P0s open/);
        // approvals > 0 => surface the actionable hint at `delimit control`.
        assert.match(out, /delimit control/);
    });

    it('pluralizes correctly (singular at 1, plural otherwise, including 0)', () => {
        repl.getControlBrief = () => ({ approvals: 1, agents: 1, p0s: 1 });
        const one = capture(() => repl.printLaunchPrebrief());
        assert.match(one, /1 approval waiting/);
        assert.match(one, /1 agent running/);
        assert.match(one, /1 P0 open/);

        repl.getControlBrief = () => ({ approvals: 0, agents: 0, p0s: 0 });
        const zero = capture(() => repl.printLaunchPrebrief());
        assert.match(zero, /0 approvals waiting/);
        assert.match(zero, /0 agents running/);
        assert.match(zero, /0 P0s open/);
        // No approvals => no triage hint.
        assert.doesNotMatch(zero, /delimit control/);
    });

    it('prints NOTHING when the backend is unreachable (getControlBrief -> null)', () => {
        repl.getControlBrief = () => null;
        const out = capture(() => repl.printLaunchPrebrief());
        assert.strictEqual(out, '');
    });

    it('degrades silently and never throws when the backend call throws', () => {
        repl.getControlBrief = () => { throw new Error('backend boom'); };
        let out;
        assert.doesNotThrow(() => { out = capture(() => repl.printLaunchPrebrief()); });
        assert.strictEqual(out, '');
    });

    it('coerces non-numeric/partial payloads to 0 rather than NaN or a throw', () => {
        repl.getControlBrief = () => ({ approvals: 'x', agents: undefined });
        const out = capture(() => repl.printLaunchPrebrief());
        assert.doesNotMatch(out, /NaN/);
        assert.match(out, /0 approvals waiting/);
        assert.match(out, /0 agents running/);
        assert.match(out, /0 P0s open/);
    });

    it('getControlBrief never throws and returns null or a numeric-count object', () => {
        // Runs against the real candidate resolution. Whatever the environment,
        // the contract is: return null OR an object; never throw.
        let brief;
        assert.doesNotThrow(() => { brief = repl.getControlBrief(); });
        if (brief !== null) {
            assert.strictEqual(typeof brief, 'object');
            // Keys, when present, must be JSON-numbers (the python side emits ints).
            for (const k of ['approvals', 'agents', 'p0s']) {
                if (k in brief) assert.strictEqual(typeof brief[k], 'number');
            }
        }
    });
});
