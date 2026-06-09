/**
 * LED-1716: shared logic for the package .git/config sentinel.
 *
 * Non-test module (no node:test side effects) so it can be imported by both
 * the in-suite sentinel (tests/_config-sentinel.test.js) and the standalone
 * test-runner wrapper (scripts/test-with-config-guard.js) without triggering
 * a redundant test execution.
 *
 * Snapshots the package checkout's SHARED git config sentinel fields
 * (core.bare, user.email, user.name). Reads the config file text directly —
 * never via a git subprocess — so taking a snapshot can't itself mutate
 * anything. Resolves the common git dir so it works from a normal checkout
 * or a linked worktree (which share one config).
 */

const fs = require('fs');
const path = require('path');
const os = require('os');
const { execSync } = require('child_process');

const REPO_ROOT = path.resolve(__dirname, '..');

function resolveSharedConfigPath(repoRoot = REPO_ROOT) {
    try {
        const env = {};
        for (const [k, v] of Object.entries(process.env)) {
            if (k.startsWith('GIT_')) continue;
            env[k] = v;
        }
        env.GIT_CONFIG_GLOBAL = os.devnull;
        env.GIT_CONFIG_SYSTEM = os.devnull;
        const commonDir = execSync('git rev-parse --git-common-dir', {
            cwd: repoRoot,
            env,
            encoding: 'utf-8',
            stdio: ['ignore', 'pipe', 'pipe'],
        }).toString().trim();
        const abs = path.isAbsolute(commonDir) ? commonDir : path.join(repoRoot, commonDir);
        const cfg = path.join(abs, 'config');
        return fs.existsSync(cfg) ? cfg : null;
    } catch {
        return null;
    }
}

function snapshotSentinel(configPath) {
    if (!configPath) return { present: false };
    const text = fs.readFileSync(configPath, 'utf-8');
    const pick = (re) => {
        const m = text.match(re);
        return m ? m[1].trim() : null;
    };
    return {
        present: true,
        bare: pick(/^\s*bare\s*=\s*(.+)$/m),
        email: pick(/^\s*email\s*=\s*(.+)$/m),
        name: pick(/^\s*name\s*=\s*(.+)$/m),
    };
}

module.exports = { resolveSharedConfigPath, snapshotSentinel, REPO_ROOT };
