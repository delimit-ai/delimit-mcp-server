/**
 * Tests for the clean-tree publish gate (DRAFT-D2 / STR-2169, Wave 1).
 *
 * Proves the gate blocks a dirty working tree and passes on a clean one,
 * plus the emergency override behaviour. Uses a stubbed `git status
 * --porcelain` string so the test is deterministic and does not touch the
 * real repo state.
 */

const { describe, it } = require('node:test');
const assert = require('node:assert');
const { evaluateCleanTree, OVERRIDE_ENV } = require('../scripts/clean-tree-guard');

describe('clean-tree publish gate', () => {
  it('PASSES on a clean tree (empty porcelain)', () => {
    const r = evaluateCleanTree({ porcelain: '', allowDirty: false });
    assert.strictEqual(r.blocked, false);
    assert.strictEqual(r.exitCode, 0);
    assert.strictEqual(r.override, false);
    assert.match(r.message, /clean/i);
  });

  it('PASSES on a whitespace-only porcelain (treated as clean)', () => {
    const r = evaluateCleanTree({ porcelain: '\n  \n', allowDirty: false });
    assert.strictEqual(r.blocked, false);
    assert.strictEqual(r.exitCode, 0);
  });

  it('BLOCKS on a dirty tree with modified + untracked files', () => {
    const porcelain = [
      ' M gateway/ai/server.js',
      '?? lib/new-thing.js',
    ].join('\n');
    const r = evaluateCleanTree({ porcelain, allowDirty: false });
    assert.strictEqual(r.blocked, true, 'dirty tree must block');
    assert.strictEqual(r.exitCode, 1);
    assert.match(r.message, /PUBLISH BLOCKED/);
    // The offending files must be surfaced in the message.
    assert.match(r.message, /gateway\/ai\/server\.js/);
    assert.match(r.message, /lib\/new-thing\.js/);
    // The count must be reported.
    assert.match(r.message, /2 change/);
  });

  it('BLOCKS on a single untracked file', () => {
    const r = evaluateCleanTree({ porcelain: '?? secret.js', allowDirty: false });
    assert.strictEqual(r.blocked, true);
    assert.strictEqual(r.exitCode, 1);
  });

  it('allows publish under the documented emergency override', () => {
    const porcelain = ' M gateway/ai/server.js';
    const r = evaluateCleanTree({ porcelain, allowDirty: true });
    assert.strictEqual(r.blocked, false, 'override must not block');
    assert.strictEqual(r.exitCode, 0);
    assert.strictEqual(r.override, true);
    // Override path must warn loudly and still list what ships.
    assert.match(r.message, /WARNING/);
    assert.match(r.message, /gateway\/ai\/server\.js/);
    assert.ok(r.message.includes(OVERRIDE_ENV));
  });

  it('override on a clean tree is a normal clean pass (no warning)', () => {
    const r = evaluateCleanTree({ porcelain: '', allowDirty: true });
    assert.strictEqual(r.blocked, false);
    assert.strictEqual(r.override, false);
    assert.doesNotMatch(r.message, /WARNING/);
  });
});
