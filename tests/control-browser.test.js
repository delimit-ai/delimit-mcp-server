/**
 * LED-1709 Phase 1: tests for the `delimit control` CLI backend wiring.
 *
 * The interactive prompts (inquirer) are not driven here; instead we lock the
 * contract that the CLI consumes the SHARED control_plane backend via the
 * python3 shim (resolveBase + buildQueue/getItem/act) and that approve routes
 * through the existing inbox ack store — never a parallel approval store.
 *
 * Skips automatically if python3 isn't available on PATH.
 */

const { describe, it, before, after } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');

const cb = require('../lib/control-browser');

const HAVE_PY = spawnSync('python3', ['--version']).status === 0;
const BASE = cb.resolveBase();
// The shared backend must be reachable for the round-trip tests.
const HAVE_BACKEND = !!BASE && fs.existsSync(path.join(BASE, 'ai', 'control_plane.py'));

const ORIG_HOME = process.env.DELIMIT_HOME;
const ORIG_NS = process.env.DELIMIT_NAMESPACE_ROOT;
let tmp;

function seedHome(dir) {
    fs.mkdirSync(path.join(dir, 'attestations'), { recursive: true });
    fs.writeFileSync(
        path.join(dir, 'attestations', 'att_x.json'),
        JSON.stringify({
            id: 'att_x',
            bundle: {
                kind: 'merge_attestation', wrapped_command: 'pytest',
                completed_at: '2026-06-09T00:00:00Z',
                governance: { violations: [], advisory: false },
            },
        })
    );
    // one pending founder directive (approval-class)
    fs.writeFileSync(
        path.join(dir, 'inbox_routing.jsonl'),
        JSON.stringify({
            event: 'founder_directive_received', subject: 'Ship it',
            msg_id: '7', from: 'founder@x.com', timestamp: '2026-06-09T10:00:00+00:00',
        }) + '\n'
    );
}

describe('lib/control-browser: shared backend wiring', () => {
    before(() => {
        tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'ctl-test-'));
        seedHome(tmp);
        process.env.DELIMIT_HOME = tmp;
        delete process.env.DELIMIT_NAMESPACE_ROOT;
    });

    after(() => {
        if (ORIG_HOME === undefined) delete process.env.DELIMIT_HOME;
        else process.env.DELIMIT_HOME = ORIG_HOME;
        if (ORIG_NS === undefined) delete process.env.DELIMIT_NAMESPACE_ROOT;
        else process.env.DELIMIT_NAMESPACE_ROOT = ORIG_NS;
        try { fs.rmSync(tmp, { recursive: true, force: true }); } catch (e) {}
    });

    it('resolveBase finds the bundled gateway control_plane', () => {
        assert.ok(HAVE_BACKEND, 'expected ai/control_plane.py in the bundled gateway');
    });

    it('buildQueue returns normalized items from the shared backend', (t) => {
        if (!HAVE_PY || !HAVE_BACKEND) return t.skip('python3/backend unavailable');
        const q = cb.buildQueue(BASE, '', 100);
        assert.ok(Array.isArray(q));
        const classes = new Set(q.map(it => it.class));
        assert.ok(classes.has('attestation'));
        assert.ok(classes.has('approval'));
    });

    it('getItem returns the raw payload for an attestation', (t) => {
        if (!HAVE_PY || !HAVE_BACKEND) return t.skip('python3/backend unavailable');
        const item = cb.getItem(BASE, 'att_x');
        assert.ok(item);
        assert.strictEqual(item.class, 'attestation');
        assert.ok('raw' in item);
    });

    it('approve routes to the inbox ack store (no parallel store)', (t) => {
        if (!HAVE_PY || !HAVE_BACKEND) return t.skip('python3/backend unavailable');
        const q = cb.buildQueue(BASE, 'approval', 100);
        const dir = q.find(it => it.title === 'Ship it');
        assert.ok(dir, 'expected the pending approval item');

        const beforeFiles = new Set(fs.readdirSync(tmp));
        const res = cb.act(BASE, 'approve', dir.id, 'lgtm');
        assert.strictEqual(res.status, 'approved');

        // The ack landed in the EXISTING inbox_routing.jsonl — no new file.
        const afterFiles = new Set(fs.readdirSync(tmp));
        assert.deepStrictEqual([...afterFiles].sort(), [...beforeFiles].sort(),
            'approve must not create a parallel approval store');
        const routing = fs.readFileSync(path.join(tmp, 'inbox_routing.jsonl'), 'utf-8');
        assert.match(routing, /founder_directive_completed/);
        assert.match(routing, /"directive_subject": "Ship it"/);

        // And it drops out of the pending approval queue.
        const q2 = cb.buildQueue(BASE, 'approval', 100);
        assert.ok(!q2.some(it => it.title === 'Ship it'));
    });

    it('approve on a non-approval class is unsupported', (t) => {
        if (!HAVE_PY || !HAVE_BACKEND) return t.skip('python3/backend unavailable');
        const res = cb.act(BASE, 'approve', 'att_x', '');
        assert.strictEqual(res.status, 'unsupported');
    });
});
