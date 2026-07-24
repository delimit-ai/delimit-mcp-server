'use strict';

// LED-1962: `delimit chat` cross-agent continuity.
//
// Two fixes, guarded here at the source + wiring level (the interactive launch
// loop and the python-spawning helpers can't be unit-run in CI without a live
// gateway + polluting ~/.delimit/souls, so we assert the load-bearing shape and
// rely on the documented runtime verification for behavior):
//   Fix 1  reviveSoulForLaunch injects the revived CURRENT-project soul into
//          codex's launch prompt (codex has no session-start hook; claude is
//          excluded because its SessionStart hook already injects).
//   Fix 2  captureSoulForMigration salvages the dying session's REAL transcript
//          via reconcile_orphan instead of writing a thin "migration from X"
//          soul that both shadowed context and blocked the next reconcile.

const { test, describe } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const { DelimitChatREPL } = require('../lib/chat-repl');
const SRC = fs.readFileSync(path.join(__dirname, '..', 'lib', 'chat-repl.js'), 'utf-8');

describe('LED-1962 delimit chat cross-agent continuity', () => {
    test('exposes the continuity helpers on the REPL', () => {
        const repl = new DelimitChatREPL({});
        assert.strictEqual(typeof repl.reviveSoulForLaunch, 'function',
            'reviveSoulForLaunch is wired');
        assert.strictEqual(typeof repl.captureSoulForMigration, 'function',
            'captureSoulForMigration is wired');
    });

    // --- Fix 2: rich migration capture ---------------------------------------
    test('captureSoulForMigration salvages via reconcile_orphan, not a thin marker', () => {
        // The dying session's transcript is salvaged (rich) instead of writing a
        // thin "Auto-Phoenix migration from X" soul.
        assert.ok(/reconcile_orphan/.test(SRC),
            'migration capture calls reconcile_orphan');
        // The thin capture_soul(active_task="Auto-Phoenix migration…") is now only
        // a FALLBACK (guarded by the "nothing salvageable" branch), never the
        // default. Guard: it must be preceded by the reconcile decision.
        const migrationIdx = SRC.indexOf('Auto-Phoenix migration from');
        const reconcileIdx = SRC.indexOf('reconcile_orphan');
        assert.ok(reconcileIdx !== -1 && reconcileIdx < migrationIdx,
            'reconcile_orphan runs before the thin fallback');
        // The fresh-capture guard: never clobber a soul the model already wrote.
        assert.ok(/last_capture_present/.test(SRC),
            'respects the .last_capture guard (no clobber of a fresh rich soul)');
    });

    // --- Fix 1: codex soul injection -----------------------------------------
    test('injects the revived soul into codex launch, but NOT claude', () => {
        // Codex has no injection hook -> chat injects the soul as its prompt.
        assert.ok(/activeModel\.id === 'codex'/.test(SRC),
            'codex launch is special-cased for soul injection');
        assert.ok(/reviveSoulForLaunch\(\)/.test(SRC),
            'the codex path revives a soul to inject');
        assert.ok(/Auto-revived session context/.test(SRC),
            'the injected prompt is clearly labeled as revived context');
        // Claude must NOT be injected here (it revives via its SessionStart hook;
        // double-injection would just bloat context). There is no
        // `activeModel.id === 'claude'` soul-injection branch.
        assert.ok(!/activeModel\.id === 'claude'[\s\S]{0,120}reviveSoulForLaunch/.test(SRC),
            'claude is not double-injected');
    });

    test('reviveSoulForLaunch is CURRENT-project only (no cross-venture global scan)', () => {
        // `delimit chat` switches models in the same cwd, so the revive is scoped
        // to the current project (get_latest_soul with no arg = cwd). It must NOT
        // pull find_most_recent_soul_across_projects, which would risk injecting a
        // different venture's soul into this session.
        const fnStart = SRC.indexOf('reviveSoulForLaunch()');
        const fnBody = SRC.slice(fnStart, fnStart + 900);
        assert.ok(/get_latest_soul\(\)/.test(fnBody),
            'reviveSoulForLaunch uses the current-project soul');
        assert.ok(!/find_most_recent_soul_across_projects/.test(fnBody),
            'reviveSoulForLaunch does NOT do a cross-project global scan');
    });
});
