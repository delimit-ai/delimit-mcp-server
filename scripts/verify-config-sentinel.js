#!/usr/bin/env node
/**
 * LED-1716: standalone proof that the config sentinel catches the known-bad
 * pollution pattern (a cwd-less / non-hermetic `git config core.bare true`).
 *
 * Runs the bad pattern against a THROWAWAY tmp repo (so it never touches the
 * real package checkout), then asserts the sentinel's snapshot diff flags it.
 * Exits 0 if the guard correctly detects the mutation, non-zero otherwise.
 *
 *   node scripts/verify-config-sentinel.js
 */

const fs = require('fs');
const os = require('os');
const path = require('path');
const { execSync } = require('child_process');
const { snapshotSentinel } = require('../tests/_config-sentinel-lib');

// Build a throwaway repo whose config we will deliberately pollute.
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sentinel-proof-'));
const env = {};
for (const [k, v] of Object.entries(process.env)) {
    if (!k.startsWith('GIT_')) env[k] = v;
}
env.GIT_CONFIG_GLOBAL = os.devnull;
env.GIT_CONFIG_SYSTEM = os.devnull;
execSync('git init -q', { cwd: dir, env, stdio: 'pipe' });
execSync('git config user.email "real@example.com"', { cwd: dir, env, stdio: 'pipe' });
execSync('git config user.name "Real Dev"', { cwd: dir, env, stdio: 'pipe' });

const cfg = path.join(dir, '.git', 'config');
const before = snapshotSentinel(cfg);

// --- THE BAD PATTERN a non-hermetic test would trigger ---
execSync('git config core.bare true', { cwd: dir, env, stdio: 'pipe' });
execSync('git config user.email "test@delimit.test"', { cwd: dir, env, stdio: 'pipe' });

const after = snapshotSentinel(cfg);

const caughtBare = after.bare === 'true' && before.bare !== after.bare;
const caughtEmail = before.email !== after.email && /^test@/.test(after.email || '');

fs.rmSync(dir, { recursive: true, force: true });

console.log('[sentinel proof] before:', JSON.stringify(before));
console.log('[sentinel proof] after: ', JSON.stringify(after));
console.log('[sentinel proof] core.bare mutation detected:', caughtBare);
console.log('[sentinel proof] test-identity mutation detected:', caughtEmail);

if (caughtBare && caughtEmail) {
    console.log('[sentinel proof] PASS — guard catches the known-bad pollution pattern.');
    process.exit(0);
}
console.error('[sentinel proof] FAIL — guard did NOT detect the mutation.');
process.exit(1);
