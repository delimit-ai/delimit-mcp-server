// lib/wrap-engine.js
//
// LED-1048: `delimit wrap` — Surface 1 CLI-pipe extension
// LED-1052: Kill Switch + cross-model handoff extension
//
// Runs an arbitrary command (typically `claude -p` or `cursor` or `codex`),
// snapshots repo state before/after, runs governance gates on the diff,
// emits a signed attestation JSON + replay URL reference.
//
// Advisory-first: exit 0 unless --enforce is set AND gates fail.
// Cross-model-agnostic: the wrapped command is arbitrary, not bound to Claude.
//
// Kill Switch (LED-1052): --max-time <seconds> caps wall-clock; on SIGKILL
// the attestation emitted is typed as kind=liability_incident and includes
// a handoff_suggestion field pointing the user at an alternative producer.

const { spawn, spawnSync, execSync } = require('child_process');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const os = require('os');

// ----------------------------------------------------------------------------
// Git helpers — snapshot before/after a wrapped command
// ----------------------------------------------------------------------------

function safeExec(cmd, opts = {}) {
    try {
        return execSync(cmd, { encoding: 'utf-8', stdio: ['ignore', 'pipe', 'pipe'], ...opts }).trim();
    } catch {
        return null;
    }
}

function getRepoRoot(cwd) {
    return safeExec('git rev-parse --show-toplevel', { cwd }) || cwd;
}

function getCurrentHead(cwd) {
    return safeExec('git rev-parse HEAD', { cwd });
}

function getDirtyFiles(cwd) {
    // Don't use safeExec because .trim() mangles the leading-space porcelain format.
    try {
        const raw = execSync('git status --porcelain', { cwd, encoding: 'utf-8', stdio: ['ignore', 'pipe', 'pipe'] });
        // Porcelain format: XY<space><path>  (XY is always 2 chars, e.g. " M", "??", "MM")
        return raw.split('\n').filter(Boolean).map(l => l.slice(3));
    } catch {
        return [];
    }
}

function getUnifiedDiff(cwd, fromHead) {
    if (!fromHead) return '';
    // Diff against the snapshot: tracked changes + untracked files (as if added)
    return safeExec(`git diff ${fromHead} -- .`, { cwd }) || '';
}

// ----------------------------------------------------------------------------
// Governance gate composition — reuse existing CLI subcommands where possible
// ----------------------------------------------------------------------------

function detectOpenAPISpecChanges(changedFiles, cwd) {
    // Simple heuristic: files matching openapi*.yaml / openapi*.json / swagger.*
    const specs = changedFiles.filter(f => {
        const base = path.basename(f).toLowerCase();
        return /(^openapi|\.openapi|swagger)\.(ya?ml|json)$/.test(base) || base === 'openapi.yaml';
    });
    return specs;
}

function runDelimitCLI(args, cwd) {
    // Invoke the sibling CLI entry point directly to avoid PATH assumptions.
    const cliPath = path.join(__dirname, '..', 'bin', 'delimit-cli.js');
    try {
        const result = spawnSync('node', [cliPath, ...args, '--json'], {
            cwd,
            encoding: 'utf-8',
            timeout: 60000,
            stdio: ['ignore', 'pipe', 'pipe'],
        });
        const stdout = result.stdout || '';
        const stderr = result.stderr || '';
        let parsed = null;
        try {
            // Find last JSON object in stdout (cli may print banner before)
            const m = stdout.match(/\{[\s\S]*\}\s*$/);
            if (m) parsed = JSON.parse(m[0]);
        } catch { /* leave null */ }
        return { exit: result.status ?? 1, stdout, stderr, parsed };
    } catch (e) {
        return { exit: 1, stdout: '', stderr: String(e), parsed: null };
    }
}

function runTestSmoke(cwd) {
    // Minimal heuristic: if pytest is available and tests/ exists, run it.
    // If package.json has a test script, run `npm test`.
    // Time-bounded. Advisory. Never the block criterion alone.
    const results = [];
    const pkgPath = path.join(cwd, 'package.json');
    if (fs.existsSync(pkgPath)) {
        try {
            const pkg = JSON.parse(fs.readFileSync(pkgPath, 'utf-8'));
            if (pkg.scripts && pkg.scripts.test && pkg.scripts.test !== 'echo "Error: no test specified" && exit 1') {
                const r = spawnSync('npm', ['test', '--silent'], { cwd, encoding: 'utf-8', timeout: 120000, stdio: ['ignore', 'pipe', 'pipe'] });
                results.push({ runner: 'npm test', exit: r.status ?? 1, stdout: (r.stdout || '').slice(-2000), stderr: (r.stderr || '').slice(-1000) });
            }
        } catch { /* ignore */ }
    }
    // Only run pytest if there's a Python-specific signal.
    // A bare `tests/` directory is common in Node projects too and should NOT trigger pytest.
    // Require: pytest.ini, OR pyproject.toml that mentions pytest, OR setup.py, OR setup.cfg with [tool:pytest]/[pytest].
    let pythonProject = false;
    if (fs.existsSync(path.join(cwd, 'pytest.ini'))) pythonProject = true;
    if (!pythonProject && fs.existsSync(path.join(cwd, 'setup.py'))) pythonProject = true;
    if (!pythonProject && fs.existsSync(path.join(cwd, 'pyproject.toml'))) {
        try {
            const pp = fs.readFileSync(path.join(cwd, 'pyproject.toml'), 'utf-8');
            if (/\bpytest\b/.test(pp)) pythonProject = true;
        } catch { /* ignore */ }
    }
    if (!pythonProject && fs.existsSync(path.join(cwd, 'setup.cfg'))) {
        try {
            const sc = fs.readFileSync(path.join(cwd, 'setup.cfg'), 'utf-8');
            if (/\[(tool:)?pytest\]/.test(sc)) pythonProject = true;
        } catch { /* ignore */ }
    }
    if (pythonProject) {
        const r = spawnSync('python3', ['-m', 'pytest', '--tb=short', '-q'], { cwd, encoding: 'utf-8', timeout: 180000, stdio: ['ignore', 'pipe', 'pipe'] });
        results.push({ runner: 'pytest', exit: r.status ?? 1, stdout: (r.stdout || '').slice(-2000), stderr: (r.stderr || '').slice(-1000) });
    }
    return results;
}

// ----------------------------------------------------------------------------
// Attestation bundling + signing
// ----------------------------------------------------------------------------

// LED-1180: deterministic canonical JSON. Recursively sorts object keys
// at every depth. Earlier implementations passed the second argument of
// JSON.stringify as `Object.keys(bundle).sort()`, which JSON.stringify
// treats as a property ALLOWLIST (not a sort order), filtered to
// top-level keys. The result was that nested objects serialised as
// `{}` and the HMAC committed only to the top-level shape — meaning a
// bad actor could change `bundle.governance.violations` or any nested
// field without invalidating the signature. Fixed in v4.5.1 hotfix.
// Verifier must use the same canonicalize to match.
function canonicalize(value) {
    if (value === null || typeof value !== 'object') return JSON.stringify(value);
    if (Array.isArray(value)) {
        return '[' + value.map(canonicalize).join(',') + ']';
    }
    const keys = Object.keys(value).sort();
    return '{' + keys.map((k) => JSON.stringify(k) + ':' + canonicalize(value[k])).join(',') + '}';
}

function computeAttestationId(bundle) {
    const hash = crypto.createHash('sha256').update(canonicalize(bundle)).digest('hex');
    return 'att_' + hash.slice(0, 16);
}

function loadOrCreateHmacKey() {
    // Local HMAC key for attestation signing.
    // Cloud-sync + verifiable signature is a Pro/Premium feature (deferred MVP).
    const keyPath = path.join(os.homedir(), '.delimit', 'wrap-hmac.key');
    if (fs.existsSync(keyPath)) return fs.readFileSync(keyPath);
    const key = crypto.randomBytes(32);
    fs.mkdirSync(path.dirname(keyPath), { recursive: true });
    fs.writeFileSync(keyPath, key, { mode: 0o600 });
    return key;
}

function signAttestation(bundle) {
    const key = loadOrCreateHmacKey();
    return crypto.createHmac('sha256', key).update(canonicalize(bundle)).digest('hex');
}

// ----------------------------------------------------------------------------
// Quota enforcement — free tier 3 lifetime, Pro unlimited
// ----------------------------------------------------------------------------

function checkQuota() {
    const counterPath = path.join(os.homedir(), '.delimit', 'wrap-lifetime-count');
    const licensePath = path.join(os.homedir(), '.delimit', 'license.json');
    let tier = 'free';
    try {
        if (fs.existsSync(licensePath)) {
            const lic = JSON.parse(fs.readFileSync(licensePath, 'utf-8'));
            if (lic.valid && ['pro', 'premium', 'enterprise'].includes(lic.tier)) {
                tier = lic.tier;
            }
        }
    } catch { /* treat as free */ }
    if (tier !== 'free') return { ok: true, tier, count: null };
    let count = 0;
    try {
        if (fs.existsSync(counterPath)) count = parseInt(fs.readFileSync(counterPath, 'utf-8').trim(), 10) || 0;
    } catch { /* start at 0 */ }
    return { ok: count < 3, tier: 'free', count, limit: 3 };
}

function incrementQuota() {
    const counterPath = path.join(os.homedir(), '.delimit', 'wrap-lifetime-count');
    let count = 0;
    try {
        if (fs.existsSync(counterPath)) count = parseInt(fs.readFileSync(counterPath, 'utf-8').trim(), 10) || 0;
    } catch {}
    count += 1;
    fs.mkdirSync(path.dirname(counterPath), { recursive: true });
    fs.writeFileSync(counterPath, String(count));
    return count;
}

// ----------------------------------------------------------------------------
// Persistence — save attestation to local ledger
// ----------------------------------------------------------------------------

function saveAttestation(att) {
    const dir = path.join(os.homedir(), '.delimit', 'attestations');
    fs.mkdirSync(dir, { recursive: true });
    const file = path.join(dir, `${att.id}.json`);
    fs.writeFileSync(file, JSON.stringify(att, null, 2));
    return file;
}

function replayUrl(attId) {
    // Public replay surface is served by app.delimit.ai (reuses the trust-page
    // pattern from LED-1018). For MVP, just returns the URL; rendering + upload
    // is the Pro-tier feature and not part of this MVP.
    return `https://delimit.ai/att/${attId}`;
}

// ----------------------------------------------------------------------------
// Main wrap flow
// ----------------------------------------------------------------------------

// LED-1052: map a wrapped command's base binary to a handoff suggestion
// for the remaining producers. Advisory only — prints the command a user
// could run to resume with a different model.
function suggestHandoff(rawCmd) {
    const bin = (rawCmd && rawCmd[0]) || '';
    const base = path.basename(bin).toLowerCase();
    const prompt = extractPromptArg(rawCmd);
    const fallbacks = {
        claude: ['codex', 'gemini', 'cursor'],
        'claude-code': ['codex', 'gemini', 'cursor'],
        cursor: ['claude', 'codex', 'gemini'],
        'cursor-cli': ['claude', 'codex', 'gemini'],
        aider: ['claude', 'codex', 'gemini'],
        codex: ['claude', 'gemini', 'cursor'],
        gemini: ['claude', 'codex', 'cursor'],
    };
    const key = Object.keys(fallbacks).find(k => base.includes(k));
    if (!key) return null;
    const alt = fallbacks[key][0];
    const altPrompt = prompt || '<your-goal>';
    return {
        kill_source: key,
        handoff_target: alt,
        suggested_command: `delimit wrap -- ${alt} -p "${altPrompt}"`,
        alternates: fallbacks[key],
    };
}

function extractPromptArg(rawCmd) {
    // Best-effort scrape: -p <prompt> or --prompt <prompt>
    if (!rawCmd) return null;
    for (let i = 0; i < rawCmd.length - 1; i++) {
        if (rawCmd[i] === '-p' || rawCmd[i] === '--prompt') return rawCmd[i + 1];
    }
    return null;
}

// LED-1052: spawn a child with wall-clock timeout + SIGKILL on breach.
// Returns { status, killed_by_timeout, ms }.
function spawnWithKillSwitch(bin, args, spawnOpts, maxTimeSeconds) {
    return new Promise((resolve) => {
        const child = spawn(bin, args, { ...spawnOpts, stdio: 'inherit' });
        const started = Date.now();
        let killed = false;
        const timer = maxTimeSeconds && maxTimeSeconds > 0
            ? setTimeout(() => {
                killed = true;
                try { child.kill('SIGKILL'); } catch { /* ignore */ }
            }, maxTimeSeconds * 1000)
            : null;
        child.on('close', (code, signal) => {
            if (timer) clearTimeout(timer);
            resolve({
                status: (code !== null ? code : (signal === 'SIGKILL' ? 137 : 1)),
                killed_by_timeout: killed,
                signal,
                ms: Date.now() - started,
            });
        });
        child.on('error', (err) => {
            if (timer) clearTimeout(timer);
            resolve({ status: 1, killed_by_timeout: false, error: String(err), ms: Date.now() - started });
        });
    });
}

async function runWrap(rawCmd, options = {}) {
    const {
        enforce = false,
        deliberate = false,
        attest = true,
        cwd = process.cwd(),
        maxTimeSeconds = 0,  // LED-1052: 0 disables kill switch
    } = options;

    const repoRoot = getRepoRoot(cwd);
    const isGitRepo = !!safeExec('git rev-parse --is-inside-work-tree', { cwd: repoRoot });

    // Quota check (only if attestation will be emitted)
    let quotaInfo = null;
    if (attest) {
        quotaInfo = checkQuota();
        if (!quotaInfo.ok) {
            return {
                exit: 1,
                error: 'quota_exceeded',
                message: `Free tier: ${quotaInfo.count}/${quotaInfo.limit} lifetime attestations used. Upgrade to Pro ($10/mo) for unlimited — visit https://delimit.ai/pricing`,
                tier: quotaInfo.tier,
            };
        }
    }

    // Snapshot before
    const beforeHead = isGitRepo ? getCurrentHead(repoRoot) : null;
    const beforeDirty = isGitRepo ? getDirtyFiles(repoRoot) : [];
    const startedAt = new Date().toISOString();

    // Execute the wrapped command
    // The wrapped command runs in the user's shell so `claude -p "..."` / `cursor edit` / etc. work natively.
    // LED-1052: if maxTimeSeconds > 0, use async spawnWithKillSwitch; otherwise spawnSync for back-compat.
    let wrappedExit;
    let killedByTimeout = false;
    let killSignal = null;
    if (maxTimeSeconds > 0) {
        const res = await spawnWithKillSwitch(rawCmd[0], rawCmd.slice(1), { cwd: repoRoot, shell: false }, maxTimeSeconds);
        wrappedExit = res.status;
        killedByTimeout = res.killed_by_timeout;
        killSignal = res.signal || null;
    } else {
        const child = spawnSync(rawCmd[0], rawCmd.slice(1), { cwd: repoRoot, stdio: 'inherit', shell: false });
        wrappedExit = child.status ?? 1;
    }
    const completedAt = new Date().toISOString();

    // Snapshot after
    const afterHead = isGitRepo ? getCurrentHead(repoRoot) : null;
    const afterDirty = isGitRepo ? getDirtyFiles(repoRoot) : [];
    const changedFiles = isGitRepo
        ? Array.from(new Set([...beforeDirty, ...afterDirty]))
        : [];

    // Governance chain
    const governance = { gates: [], violations: [], advisory: !enforce };

    // 1) OpenAPI spec changes → delimit lint / diff
    const specChanges = detectOpenAPISpecChanges(changedFiles, repoRoot);
    if (specChanges.length > 0) {
        governance.gates.push({ name: 'openapi_detect', result: 'ran', specs: specChanges });
        // Try running delimit lint on each (zero-spec mode against baseline)
        for (const spec of specChanges) {
            const lintResult = runDelimitCLI(['lint'], path.dirname(path.join(repoRoot, spec)));
            governance.gates.push({
                name: 'delimit_lint',
                spec,
                exit: lintResult.exit,
                summary: (lintResult.stdout || '').slice(-500),
            });
            if (lintResult.exit !== 0) governance.violations.push(`lint failed on ${spec}`);
        }
    }

    // 2) Test smoke
    const testResults = runTestSmoke(repoRoot);
    for (const t of testResults) {
        governance.gates.push({ name: 'test_smoke', runner: t.runner, exit: t.exit });
        if (t.exit !== 0) governance.violations.push(`${t.runner} failed`);
    }
    if (testResults.length === 0) {
        governance.gates.push({ name: 'test_smoke', result: 'no_tests_detected' });
    }

    // 3) Multi-model deliberate (optional)
    if (deliberate) {
        governance.gates.push({ name: 'deliberate', result: 'deferred', note: 'use `delimit deliberate` standalone for multi-model verdict — v1 wrap emits local attestation only' });
    }

    // Build attestation bundle
    // LED-1052: if killed by timeout, attestation is typed as liability_incident
    const kind = killedByTimeout ? 'liability_incident' : 'merge_attestation';
    const handoffSuggestion = killedByTimeout ? suggestHandoff(rawCmd) : null;
    const bundle = {
        schema: 'delimit.attestation.v1',
        kind,
        wrapped_command: rawCmd.join(' '),
        repo_root: repoRoot,
        is_git_repo: isGitRepo,
        before_head: beforeHead,
        after_head: afterHead,
        started_at: startedAt,
        completed_at: completedAt,
        wrapped_exit: wrappedExit,
        changed_files: changedFiles,
        governance,
        delimit_wrap_version: '1.1.0',
        ...(killedByTimeout ? {
            kill_switch: {
                kind: 'timeout',
                max_time_seconds: maxTimeSeconds,
                signal: killSignal,
                handoff_suggestion: handoffSuggestion,
            },
        } : {}),
    };
    const attId = computeAttestationId(bundle);
    const signature = signAttestation(bundle);
    const attestation = {
        id: attId,
        bundle,
        signature,
        signature_alg: 'HMAC-SHA256',
    };

    let filePath = null;
    if (attest) {
        filePath = saveAttestation(attestation);
        if (quotaInfo && quotaInfo.tier === 'free') incrementQuota();
    }

    const hasViolations = governance.violations.length > 0;
    const shouldFail = enforce && hasViolations;

    return {
        exit: shouldFail ? 2 : wrappedExit,
        attestation_id: attId,
        attestation_path: filePath,
        replay_url: replayUrl(attId),
        kind,
        violations: governance.violations,
        gates: governance.gates,
        wrapped_exit: wrappedExit,
        advisory: !enforce,
        tier: quotaInfo ? quotaInfo.tier : null,
        // LED-1052: kill-switch metadata surfaced at the top level for CLI rendering
        ...(killedByTimeout ? {
            killed_by_timeout: true,
            handoff_suggestion: handoffSuggestion,
        } : {}),
    };
}

module.exports = {
    runWrap,
    canonicalize,
    computeAttestationId,
    signAttestation,
    checkQuota,
    replayUrl,
};
