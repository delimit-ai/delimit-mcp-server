'use strict';

// LED-1710 Phase 2 — actionable handoff remediation.
//
// The python validator (ai/handoff_preflight.preflight_check) is strictly
// READ-ONLY: it classifies cross-agent handoff corruption but never writes.
// This module is the explicit, user-invoked WRITER (`delimit handoff fix`). It
// applies ONLY the unambiguously-safe, deterministic repairs:
//
//   • not_bare      → `git config core.bare false`  — but ONLY when a real
//                     working tree exists (.git is a directory). A genuinely
//                     bare repo is left untouched.
//   • stale index.lock → remove the lock the validator already deemed stale.
//
// It NEVER touches identity (junk/empty user.email needs a human — we can't
// guess the right committer) and never runs without the user typing the
// command. No silent writes, no surprise mutation at session start.

const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');

let chalk;
try { chalk = require('chalk'); } catch (_) {
    const id = (s) => s;
    chalk = { green: id, yellow: id, red: id, gray: id, cyan: id, bold: id };
}

// Resolve the bundled gateway (next to this package) → installed server.
function preflightBases() {
    return [
        path.join(__dirname, '..', 'gateway'),
        path.join(os.homedir(), '.delimit', 'server'),
    ];
}

// Run the read-only validator via the same python-shim pattern as chat-repl.
function runPreflight(repoPath) {
    for (const base of preflightBases()) {
        if (!fs.existsSync(path.join(base, 'ai', 'handoff_preflight.py'))) continue;
        try {
            const pyCmd = 'import sys, json; sys.path.insert(0, ' + JSON.stringify(base) + '); '
                + 'from ai.handoff_preflight import preflight_check; '
                + 'print(json.dumps(preflight_check(' + JSON.stringify(repoPath || '') + ')))';
            const r = spawnSync('python3', ['-c', pyCmd], { encoding: 'utf-8' });
            if (r.status === 0 && r.stdout) return JSON.parse(r.stdout);
        } catch (_) { /* try next base */ }
    }
    return null;
}

function git(repoPath, args) {
    const r = spawnSync('git', ['-C', repoPath, ...args], { encoding: 'utf-8' });
    return { ok: r.status === 0, out: (r.stdout || '').trim(), err: (r.stderr || '').trim() };
}

function check(verdict, name) {
    return (verdict.checks || []).find((c) => c && c.name === name) || null;
}

// Print the current verdict (read-only).
function report(repoPath) {
    const repo = repoPath || process.cwd();
    if (!fs.existsSync(path.join(repo, '.git'))) {
        console.log(chalk.gray('  Not a git repo: ' + repo + ' — nothing to check.'));
        return 0;
    }
    const v = runPreflight(repo);
    if (!v) {
        console.log(chalk.gray('  Handoff validator unavailable (gateway not bundled/installed).'));
        return 0;
    }
    if (v.ok) {
        console.log(chalk.green('  ✓ ') + (v.summary || 'all handoff invariants hold'));
        return 0;
    }
    const bad = (v.checks || []).filter((c) => c && !c.ok);
    console.log(chalk.yellow('  ⚠ ' + bad.length + ' handoff issue(s) in ' + repo + ':'));
    for (const c of bad) {
        const mark = c.severity === 'critical' ? chalk.red('  ✗ ') : chalk.yellow('  • ');
        console.log(mark + chalk.bold(c.name) + ': ' + c.detail
            + (c.remediation ? chalk.gray('  (' + c.remediation + ')') : ''));
    }
    console.log(chalk.gray('  Run `delimit handoff fix` to auto-repair the safe ones.'));
    return bad.some((c) => c.severity === 'critical') ? 2 : 1;
}

// Apply the safe repairs. Returns {fixed:[], skipped:[]}.
function fix(repoPath) {
    const repo = repoPath || process.cwd();
    const fixed = [];
    const skipped = [];
    if (!fs.existsSync(path.join(repo, '.git'))) {
        console.log(chalk.gray('  Not a git repo: ' + repo + ' — nothing to fix.'));
        return { fixed, skipped };
    }
    const v = runPreflight(repo);
    if (!v) {
        console.log(chalk.gray('  Handoff validator unavailable — cannot determine what to fix.'));
        return { fixed, skipped };
    }

    // 1) not_bare — only when a real working tree exists (.git is a directory).
    const nb = check(v, 'not_bare');
    if (nb && !nb.ok) {
        const dotGit = path.join(repo, '.git');
        const hasWorktree = fs.existsSync(dotGit) && fs.statSync(dotGit).isDirectory();
        if (hasWorktree) {
            const r = git(repo, ['config', 'core.bare', 'false']);
            if (r.ok) fixed.push('core.bare → false (working tree present; bare flag was pollution)');
            else skipped.push('not_bare: git config failed — ' + (r.err || 'unknown'));
        } else {
            skipped.push('not_bare: no working tree (.git is not a directory) — looks genuinely bare, left untouched');
        }
    }

    // 2) stale index.lock — remove the lock the validator deemed stale.
    const sl = check(v, 'no_stale_index_lock');
    if (sl && !sl.ok) {
        const gd = git(repo, ['rev-parse', '--git-dir']);
        const gitDir = gd.ok ? path.resolve(repo, gd.out) : path.join(repo, '.git');
        const lock = path.join(gitDir, 'index.lock');
        try {
            if (fs.existsSync(lock)) {
                fs.unlinkSync(lock);
                fixed.push('removed stale index.lock');
            }
        } catch (e) {
            skipped.push('index.lock: could not remove — ' + (e && e.message ? e.message : e));
        }
    }

    // 3) identity — NEVER auto-fixed (needs a human to choose the committer).
    const gi = check(v, 'git_identity');
    if (gi && !gi.ok) {
        skipped.push('git_identity: needs a human — ' + (gi.remediation || 'set user.email/user.name manually'));
    }

    // Report.
    if (fixed.length) {
        console.log(chalk.green('  Fixed:'));
        for (const f of fixed) console.log(chalk.green('    ✓ ') + f);
    }
    if (skipped.length) {
        console.log(chalk.yellow('  Left for you:'));
        for (const s of skipped) console.log(chalk.yellow('    • ') + s);
    }
    if (!fixed.length && !skipped.length) {
        console.log(chalk.green('  ✓ Nothing to fix — handoff invariants already hold.'));
    } else {
        // Re-validate so the user sees the post-fix state.
        const after = runPreflight(repo);
        if (after) {
            console.log((after.ok ? chalk.green('  → ') : chalk.yellow('  → ')) + (after.summary || ''));
        }
    }
    return { fixed, skipped };
}

module.exports = { report, fix, runPreflight };
