const fs = require('fs');
const path = require('path');
const os = require('os');
const chalk = require('chalk');
const { spawnSync, execSync } = require('child_process');

// Banner version is read from package.json so it never goes stale per release.
const PKG_VERSION = (() => { try { return require('../package.json').version; } catch (e) { return ''; } })();

class DelimitChatREPL {
    constructor(options = {}) {
        this.apiFallbackEnabled = options.apiFallback !== undefined ? 
            !!options.apiFallback : 
            (process.env.DELIMIT_API_FALLBACK === 'true' || false);
        this.failedModels = new Set();
        this.modelsConfig = this.loadModels();
        this.routesConfig = this.loadRoutes();
        this.agentName = 'orchestrator'; // Default agent
    }

    loadModels() {
        const modelsPath = path.join(os.homedir(), '.delimit', 'models.json');
        if (fs.existsSync(modelsPath)) {
            try {
                return JSON.parse(fs.readFileSync(modelsPath, 'utf-8'));
            } catch (e) {
                console.error(chalk.red('Failed to parse models.json'));
            }
        }
        return { fallbacks: { default: [] } };
    }

    loadRoutes() {
        const routesPath = path.join(os.homedir(), '.delimit', 'routes.json');
        if (fs.existsSync(routesPath)) {
            try {
                return JSON.parse(fs.readFileSync(routesPath, 'utf-8'));
            } catch (e) {}
        }
        return {};
    }

    getActiveChain() {
        const chain = this.modelsConfig.fallbacks?.['default'] || [];
        const activeModels = [];
        
        for (const provider of chain) {
            if (this.failedModels.has(provider)) continue;
            const p = this.modelsConfig[provider];
            if (!p) continue;
            
            if (p.auth_mode === 'chat_login' || p.auth_mode === 'adc') {
                activeModels.push({ id: provider, type: 'subscription' });
            } else if (p.api_key && this.apiFallbackEnabled) {
                activeModels.push({ id: provider, type: 'api' });
            }
        }
        return activeModels;
    }

    // Capture the current session's soul before Auto-Phoenix switches models,
    // so the next agent can revive the context. Resolves the server path at
    // runtime (bundled gateway, then the installed server) instead of a
    // hardcoded dev path. Returns true only if capture actually succeeded.
    captureSoulForMigration(fromModel) {
        const candidates = [
            path.join(__dirname, '..', 'gateway'),
            path.join(os.homedir(), '.delimit', 'server'),
        ];
        for (const base of candidates) {
            if (!fs.existsSync(path.join(base, 'ai', 'session_phoenix.py'))) continue;
            try {
                const pyCmd = `import sys; sys.path.insert(0, ${JSON.stringify(base)}); `
                    + `from ai.session_phoenix import capture_soul; `
                    + `capture_soul(active_task=${JSON.stringify('Auto-Phoenix migration from ' + fromModel)})`;
                const r = spawnSync('python3', ['-c', pyCmd], { stdio: 'ignore' });
                if (r.status === 0) return true;
            } catch (e) { /* try next candidate */ }
        }
        return false;
    }

    // LED-1710: validate cross-agent handoff invariants (committer identity,
    // not-bare, no leaked GIT_*, no stale index.lock, context freshness) on the
    // given repo before entering an agent. Read-only; returns the verdict dict
    // {ok, checks, ...} or null if the validator is unavailable.
    preflightHandoff(repoPath) {
        const candidates = [
            path.join(__dirname, '..', 'gateway'),
            path.join(os.homedir(), '.delimit', 'server'),
        ];
        for (const base of candidates) {
            if (!fs.existsSync(path.join(base, 'ai', 'handoff_preflight.py'))) continue;
            try {
                const pyCmd = `import sys, json; sys.path.insert(0, ${JSON.stringify(base)}); `
                    + `from ai.handoff_preflight import preflight_check; `
                    + `print(json.dumps(preflight_check(${JSON.stringify(repoPath || '')})))`;
                const r = spawnSync('python3', ['-c', pyCmd], { encoding: 'utf-8' });
                if (r.status === 0 && r.stdout) return JSON.parse(r.stdout);
            } catch (e) { /* try next candidate */ }
        }
        return null;
    }

    start() {
                console.log(chalk.magenta.bold(`
  ██████╗ ███████╗██╗     ██╗███╗   ███╗██╗████████╗
  ██╔══██╗██╔════╝██║     ██║████╗ ████║██║╚══██╔══╝
  ██║  ██║█████╗  ██║     ██║██╔████╔██║██║   ██║   
  ██║  ██║██╔══╝  ██║     ██║██║╚██╔╝██║██║   ██║   
  ██████╔╝███████╗███████╗██║██║ ╚═╝ ██║██║   ██║   
  ╚═════╝ ╚══════╝╚══════╝╚═╝╚═╝     ╚═╝╚═╝   ╚═╝   
                                                    
   ██████╗██╗  ██╗ █████╗ ████████╗
  ██╔════╝██║  ██║██╔══██╗╚══██╔══╝
  ██║     ███████║███████║   ██║   
  ██║     ██╔══██║██╔══██║   ██║   
  ╚██████╗██║  ██║██║  ██║   ██║   
   ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝   ╚═╝   
   · v${PKG_VERSION}`));

        // LED-1710: scrub leaked GIT_* from our env so NO agent we spawn (or
        // python shim) inherits a misdirected git object-store / work-tree /
        // index across the handoff. Repo-independent, always safe.
        for (const v of ['GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY', 'GIT_COMMON_DIR', 'GIT_QUARANTINE_PATH']) {
            delete process.env[v];
        }

        while (true) {
            const chain = this.getActiveChain();
            if (chain.length === 0) {
                console.log(chalk.red('\n  Error: No active models available.'));
                if (!this.apiFallbackEnabled) {
                    console.log(chalk.yellow('  Auto-Phoenix stalled: All flat-rate subscription models have degraded.'));
                    // Ask user to enable API fallback synchronously
                    try {
                        execSync('read -p "  Enable API Fallback to continue using paid tokens? (y/N) " yn && [ "$yn" = "y" ] || [ "$yn" = "Y" ]', {stdio: 'inherit'});
                        this.apiFallbackEnabled = true;
                        console.log(chalk.green('  API Fallback enabled. Resuming...\n'));
                        continue;
                    } catch (e) {
                        console.log(chalk.gray('  API Fallback declined. Exiting.\n'));
                        process.exit(1);
                    }
                } else {
                    console.log(chalk.red('  Auto-Phoenix stalled: No remaining models in fallback chain.\n'));
                    process.exit(1);
                }
            }

            const activeModel = chain[0];
            const chainStr = chain.map(m => m.type === 'subscription' ? chalk.green(m.id) : chalk.yellow(m.id)).join(' -> ');
            console.log(`\n  [Agent: ${chalk.white(this.agentName)}] [API Fallback: ${this.apiFallbackEnabled ? chalk.green('ON') : chalk.gray('OFF')}]`);
            console.log(`  Active Routing: ${chainStr}`);
                        console.log(chalk.magenta.bold(`  [Delimit] `) + chalk.magenta(`═══════════════════════════════════════════`));
            console.log(chalk.magenta.bold(`  [Delimit] `) + chalk.magenta(`<`) + chalk.yellow(`/`) + chalk.magenta(`> `) + chalk.bold(`GOVERNANCE ACTIVE: ${activeModel.id.toUpperCase()}`));
            console.log(chalk.magenta.bold(`  [Delimit] `) + chalk.magenta(`═══════════════════════════════════════════\n`));

            const shimPath = path.join(os.homedir(), '.delimit', 'shims', activeModel.id);
            if (!fs.existsSync(shimPath)) {
                console.log(chalk.red(`  Error: Shim not found for ${activeModel.id} at ${shimPath}`));
                this.failedModels.add(activeModel.id);
                continue;
            }

            // Probe model quota/health before entering interactive session
            let isHealthy = true;
            if (activeModel.id === 'claude' || activeModel.id === 'gemini' || activeModel.id === 'gemini_consumer' || activeModel.id === 'antigravity') {
                process.stdout.write(`  Probing ${chalk.bold(activeModel.id)} quota... `);
                
                // Configure environment for the probe (identical to the run environment)
                const probeEnv = { ...process.env, DELIMIT_QUIET: 'true' };
                const p = this.modelsConfig[activeModel.id];
                if (activeModel.id === 'claude') {
                    if (p && p.auth_mode === 'chat_login') {
                        delete probeEnv.ANTHROPIC_API_KEY;
                    }
                } else if (activeModel.id === 'gemini' || activeModel.id === 'gemini_consumer' || activeModel.id === 'antigravity') {
                    if (!p || p.auth_mode === 'chat_login') {
                        delete probeEnv.GOOGLE_CLOUD_PROJECT;
                        delete probeEnv.GEMINI_USER_GCP_PROJECT;
                        delete probeEnv.GEMINI_CLI_USE_COMPUTE_ADC;
                        delete probeEnv.GOOGLE_APPLICATION_CREDENTIALS;
                    }
                }
                
                // A GENUINE subscription usage-cap exhaustion (the 5-hour rolling
                // plan cap or a hard quota). This is the ONLY signal that should
                // fall the model over to the next in the chain. Deliberately does
                // NOT include bare "rate limit"/429/"exceeded", which a transient
                // overload also emits and which was the false-quota source.
                const QUOTA_EXHAUSTED = /usage limit|limit reached|reached your .{0,20}\blimit\b|spend limit|plan limit|monthly limit|out of (?:credits|tokens)|\bquota\b/;
                // A TRANSIENT, self-healing rate-limit / overload (HTTP 429/503).
                // Retrying shortly usually clears it, so it must never be blamed
                // on quota — proceed (or retry) instead of falling back.
                const TRANSIENT_RATE_LIMIT = /\b429\b|\b503\b|rate.?limit|rate_limit|overloaded|service unavailable|temporarily unavailable|please try again|try again later/;

                // Spawn the shim silently with -p "space". Cold-start + MCP load
                // + API round-trip can exceed a few seconds, so a timeout means
                // SLOW, not out of quota. The shim is a /bin/sh wrapper that exits
                // 143 (128+SIGTERM) when our timeout kills it — so a timeout shows
                // up as BOTH error=ETIMEDOUT AND status=143; treat both as healthy.
                const runProbe = () => {
                    const r = spawnSync(shimPath, ['-p', 'space'], { env: probeEnv, timeout: 12000 });
                    const timedOut = (r.error && r.error.code === 'ETIMEDOUT')
                        || r.signal === 'SIGTERM'
                        || r.status === 143;
                    const out = ((r.stdout || '') + (r.stderr || '')).toString().toLowerCase();
                    return { r, timedOut, out };
                };

                let { r: probeResult, timedOut: killedByTimeout, out: probeOut } = runProbe();

                // If the first probe shows ONLY a transient rate-limit (no genuine
                // cap signal), back off briefly and re-probe once. A real plan-cap
                // is sticky and survives the retry; a transient 429 usually clears.
                if (!killedByTimeout && TRANSIENT_RATE_LIMIT.test(probeOut) && !QUOTA_EXHAUSTED.test(probeOut)) {
                    console.log(chalk.yellow('rate-limited — retrying once'));
                    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 3000);
                    process.stdout.write(`  Re-probing ${chalk.bold(activeModel.id)} quota... `);
                    ({ r: probeResult, timedOut: killedByTimeout, out: probeOut } = runProbe());
                }

                const quotaExhausted = QUOTA_EXHAUSTED.test(probeOut);
                const transientRateLimit = TRANSIENT_RATE_LIMIT.test(probeOut);

                if (killedByTimeout) {
                    // Reachable but slow (cold start) — NOT a quota signal. Proceed.
                    console.log(chalk.yellow('slow — proceeding'));
                } else if (quotaExhausted && !transientRateLimit) {
                    // Sticky plan-cap exhaustion — fall over to the next model.
                    console.log(chalk.red('failed (out of quota/limit)'));
                    isHealthy = false;
                } else if (transientRateLimit) {
                    // Still transient after the retry — the plan is NOT exhausted,
                    // the API is briefly busy. Proceed rather than falsely falling
                    // back; if it's truly down the interactive launch fails and the
                    // existing crash handler migrates.
                    console.log(chalk.yellow('transient rate-limit — proceeding (not a quota cap)'));
                } else if (probeResult.error || (probeResult.status !== null && probeResult.status !== 0)) {
                    // Some other error — don't falsely blame quota.
                    console.log(chalk.red('failed (probe error)'));
                    isHealthy = false;
                } else {
                    console.log(chalk.green('verified'));
                }
            }
            
            if (!isHealthy) {
                this.failedModels.add(activeModel.id);
                continue;
            }

            const args = [];
            if (activeModel.id === 'antigravity' || activeModel.id === 'agy') {
                 args.push('--dangerously-skip-permissions');
             }
             if (activeModel.id.startsWith('gemini')) {
                const p = this.modelsConfig[activeModel.id];
                if (p && p.model) {
                    let modelName = p.model;
                    if (modelName.endsWith('-latest') && this.routesConfig[activeModel.id]) {
                        const basePrefix = modelName.replace('-latest', '');
                        const concrete = this.routesConfig[activeModel.id].find(m => m.startsWith(basePrefix) && m !== modelName);
                        if (concrete) modelName = concrete;
                    }
                    args.unshift('-m', modelName);
                }
            }

            const env = { ...process.env };
            // Suppress the shim banner so it feels like a native session
            env.DELIMIT_QUIET = 'true';
            
            const p = this.modelsConfig[activeModel.id];
            
            // Fix 403 Google Cloud API Error by preventing accidental enterprise routing
            // If the model is using chat_login (consumer Google One plan), we MUST strip
            // any global GCP env vars that would accidentally trigger Code Assist Enterprise mode.
            if (activeModel.id === 'gemini' || activeModel.id === 'gemini_consumer' || activeModel.id === 'antigravity') {
                if (!p || p.auth_mode === 'chat_login') {
                    delete env.GOOGLE_CLOUD_PROJECT;
                    delete env.GEMINI_USER_GCP_PROJECT;
                    delete env.GEMINI_CLI_USE_COMPUTE_ADC;
                    delete env.GOOGLE_APPLICATION_CREDENTIALS;
                } else if (p && p.auth_mode === 'adc') {
                    if (p.project) {
                        env.GOOGLE_CLOUD_PROJECT = p.project;
                        env.GEMINI_USER_GCP_PROJECT = p.project;
                    }
                    if (p.credentials_path) {
                        env.GOOGLE_APPLICATION_CREDENTIALS = p.credentials_path;
                    }
                    env.GEMINI_CLI_USE_COMPUTE_ADC = 'true';
                }
            }

            // Force Claude Code to use chat login rather than API keys when auth_mode is chat_login
            if (activeModel.id === 'claude') {
                if (p && p.auth_mode === 'chat_login') {
                    delete env.ANTHROPIC_API_KEY;
                }
            }

            // LED-1710: handoff preflight on the user's working repo before
            // entering the agent — surfaces corrupted state (junk git identity,
            // bare repo, stale index lock, missing context) that would propagate
            // across the switch. Non-blocking: warn, never trap an interactive
            // session. Silent in non-repo cwds.
            try {
                const cwd = process.cwd();
                if (fs.existsSync(path.join(cwd, '.git'))) {
                    const pf = this.preflightHandoff(cwd);
                    if (pf && pf.ok === false) {
                        const bad = (pf.checks || []).filter(c => c && !c.ok);
                        console.log(chalk.yellow(`  ⚠ Handoff preflight: ${bad.length} issue(s) in ${cwd}`));
                        for (const c of bad) {
                            console.log(chalk.yellow(`      • ${c.name}: ${c.detail}`) + (c.remediation ? chalk.gray(`  (${c.remediation})`) : ''));
                        }
                        if (bad.some(c => c.severity !== 'critical' || c.name !== 'git_identity')) {
                            console.log(chalk.gray('      run `delimit handoff fix` to auto-repair the safe ones'));
                        }
                    }
                }
            } catch (e) { /* preflight is best-effort; never block the session */ }

            // Execute the model interactively. Ignore SIGINT in parent while child runs
            // to prevent Ctrl+C from killing the parent Node process.
            const sigintHandler = () => {};
            process.on('SIGINT', sigintHandler);
            const result = spawnSync(shimPath, args, { stdio: 'inherit', env });
            process.removeListener('SIGINT', sigintHandler);

            if (result.status === 0) {
                // Clean exit (user typed /exit)
                console.log(chalk.gray('\n  Session saved. Exiting Delimit.'));
                process.exit(0);
            } else if (result.signal === 'SIGINT') {
                console.log(chalk.yellow('\n  Session interrupted (Ctrl+C).'));
                try {
                    execSync('read -p "  Migrate to the next fallback model? (Y/n) " yn && [ "$yn" = "n" ] || [ "$yn" = "N" ]', {stdio: 'inherit'});
                    console.log(chalk.yellow(`  ⚠ ${activeModel.id} interrupted. Auto-Phoenix initiating seamless migration...`));
                    this.failedModels.add(activeModel.id);

                    // Capture context before switching, then advance the chain.
                    const captured = this.captureSoulForMigration(activeModel.id);
                    if (chain.length > 1) {
                        const nextModel = chain[1];
                        if (captured) {
                            console.log(chalk.green(`  ✓ Context saved — ${chalk.bold(nextModel.id)} will revive it on start.`));
                        } else {
                            console.log(chalk.yellow(`  ⚠ Switching to ${chalk.bold(nextModel.id)} (context save unavailable).`));
                        }
                    }
                } catch (e) {
                    console.log(chalk.gray('  Exiting Delimit.\n'));
                    process.exit(0);
                }
            } else {
                // The CLI crashed (e.g. 429 Quota Error or exit code != 0)
                console.log(chalk.red(`\n  Execution failed: Model CLI exited with status ${result.status}`));
                console.log(chalk.yellow(`  ⚠ ${activeModel.id} degraded. Auto-Phoenix initiating seamless migration...`));
                this.failedModels.add(activeModel.id);

                // Capture context before switching, then advance the chain.
                const captured = this.captureSoulForMigration(activeModel.id);
                if (chain.length > 1) {
                    const nextModel = chain[1];
                    if (captured) {
                        console.log(chalk.green(`  ✓ Context saved — ${chalk.bold(nextModel.id)} will revive it on start.`));
                    } else {
                        console.log(chalk.yellow(`  ⚠ Switching to ${chalk.bold(nextModel.id)} (context save unavailable).`));
                    }
                }
                // Loop continues to spawn the next model
            }
        }
    }
}

module.exports = { DelimitChatREPL };
