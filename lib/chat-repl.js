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
                
                // Spawn the shim silently with -p "space". Cold-start + MCP load
                // + API round-trip can exceed a few seconds, so a timeout means
                // SLOW, not out of quota. The shim is a /bin/sh wrapper that exits
                // 143 (128+SIGTERM) when our timeout kills it — so a timeout shows
                // up as BOTH error=ETIMEDOUT AND status=143; treat both as healthy.
                const probeResult = spawnSync(shimPath, ['-p', 'space'], { env: probeEnv, timeout: 12000 });

                const killedByTimeout = (probeResult.error && probeResult.error.code === 'ETIMEDOUT')
                    || probeResult.signal === 'SIGTERM'
                    || probeResult.status === 143;
                const probeOut = ((probeResult.stdout || '') + (probeResult.stderr || '')).toString().toLowerCase();
                const quotaHit = /quota|rate.?limit|usage limit|limit reached|spend limit|exceeded/.test(probeOut);

                if (killedByTimeout) {
                    // Reachable but slow (cold start) — NOT a quota signal. Proceed.
                    console.log(chalk.yellow('slow — proceeding'));
                } else if (quotaHit) {
                    console.log(chalk.red('failed (out of quota/limit)'));
                    isHealthy = false;
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

            // Execute the model interactively. Ignore SIGINT in parent while child runs
            // to prevent Ctrl+C from killing the parent Node process.
            const sigintHandler = () => {};
            process.on('SIGINT', sigintHandler);
            const result = spawnSync(shimPath, args, { stdio: 'inherit', env });
            process.removeListener('SIGINT', sigintHandler);

            if (result.status === 0) {
                // Clean exit (user typed /exit)
                console.log(chalk.gray('\n  Session saved. Exiting Delimit OS.'));
                process.exit(0);
            } else if (result.signal === 'SIGINT') {
                console.log(chalk.yellow('\n  Session interrupted (Ctrl+C).'));
                try {
                    execSync('read -p "  Migrate to the next fallback model? (Y/n) " yn && [ "$yn" = "n" ] || [ "$yn" = "N" ]', {stdio: 'inherit'});
                    console.log(chalk.yellow(`  ⚠ ${activeModel.id} interrupted. Auto-Phoenix initiating seamless migration...`));
                    this.failedModels.add(activeModel.id);
                    
                    // Capture soul to preserve context before switching
                    try {
                        const pyCmd = `import sys; sys.path.insert(0, '/home/delimit/delimit-gateway'); from ai.session_phoenix import capture_soul; capture_soul(active_task='Auto-Phoenix migration from ${activeModel.id}')`;
                        execSync(`python3 -c "${pyCmd}"`, { stdio: 'ignore' });
                    } catch (e) {}

                    if (chain.length > 1) {
                        const nextModel = chain[1];
                        console.log(chalk.green(`  ✓ Soul captured. Rehydrating into ${chalk.bold(nextModel.id)}...`));
                    }
                } catch (e) {
                    console.log(chalk.gray('  Exiting Delimit OS.\n'));
                    process.exit(0);
                }
            } else {
                // The CLI crashed (e.g. 429 Quota Error or exit code != 0)
                console.log(chalk.red(`\n  Execution failed: Model CLI exited with status ${result.status}`));
                console.log(chalk.yellow(`  ⚠ ${activeModel.id} degraded. Auto-Phoenix initiating seamless migration...`));
                this.failedModels.add(activeModel.id);
                
                // Capture soul to preserve context before switching
                try {
                    const pyCmd = `import sys; sys.path.insert(0, '/home/delimit/delimit-gateway'); from ai.session_phoenix import capture_soul; capture_soul(active_task='Auto-Phoenix migration from ${activeModel.id}')`;
                    execSync(`python3 -c "${pyCmd}"`, { stdio: 'ignore' });
                } catch (e) {}

                if (chain.length > 1) {
                    const nextModel = chain[1];
                    console.log(chalk.green(`  ✓ Soul captured. Rehydrating into ${chalk.bold(nextModel.id)}...`));
                }
                // Loop continues to spawn the next model
            }
        }
    }
}

module.exports = { DelimitChatREPL };
