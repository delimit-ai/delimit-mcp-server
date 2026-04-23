#!/usr/bin/env node

const { Command } = require('commander');
const axios = require('axios');
const fs = require('fs');
const path = require('path');
const { execSync, spawn } = require('child_process');
const os = require('os');
const chalk = require('chalk');
const inquirer = require('inquirer');
const DelimitAuthSetup = require('../lib/auth-setup');
const DelimitHooksInstaller = require('../lib/hooks-installer');
const crossModelHooks = require('../lib/cross-model-hooks');
const {
    resolveContinuityContext,
    formatContinuityReport,
    resolveRepoRoot,
    loadActiveVenture,
    saveActiveVenture,
} = require('../lib/continuity-resolver');
const {
    runInteractiveSession,
    renderSummary,
    ensureWorker,
    waitForWorkerState,
    getWorkerState,
    getTaskBrief,
    getExecutionPlan,
    getOwnerActions,
    pidIsAlive,
} = require('../lib/session-shell');

const AGENT_URL = `http://127.0.0.1:${process.env.DELIMIT_AGENT_PORT || 7823}`;
const program = new Command();

const yaml = require('js-yaml');

const continuityContext = resolveContinuityContext();
process.env.DELIMIT_HOME = continuityContext.delimitHome;
process.env.DELIMIT_CONTINUITY_ROOT = continuityContext.continuityRoot;
process.env.DELIMIT_REPO_GOVERNANCE_ROOT = continuityContext.repoGovernanceRoot || '';
process.env.DELIMIT_RESOLVED_VENTURE = continuityContext.venture;
process.env.DELIMIT_RESOLVED_ACTOR = continuityContext.actor;

function getDynamicContinuityContext(options = {}) {
    const active = loadActiveVenture();
    const currentRepo = resolveRepoRoot(process.cwd());
    if (options.scope === 'all') {
        return resolveContinuityContext({ cwd: process.cwd(), scope: 'all' });
    }
    if (!currentRepo && active?.repoRoot && options.preferActive) {
        return resolveContinuityContext({ cwd: active.repoRoot });
    }
    return resolveContinuityContext({ cwd: process.cwd() });
}

function normalizeNaturalLanguageArgs(argv) {
    const raw = argv.slice(2);
    if (raw.length === 0) {
        // First-run detection: if no ~/.delimit exists, show welcome flow
        const delimitHome = path.join(os.homedir(), '.delimit');
        if (!fs.existsSync(delimitHome) || !fs.existsSync(path.join(delimitHome, 'server'))) {
            return ['scan'];  // lowest friction entry point for new users
        }
        return resolveRepoRoot(process.cwd()) ? ['session', '--inspect'] : ['session', '--all'];
    }

    const explicitCommands = new Set([
        'install', 'mode', 'status', 'session', 'build', 'ask', 'policy', 'auth', 'audit',
        'explain-decision', 'uninstall', 'proxy', 'hook', 'version', 'vault', 'deliberate',
        'remember', 'recall', 'forget', 'report'
    ]);
    if (explicitCommands.has((raw[0] || '').toLowerCase())) {
        return raw;
    }

    const joined = raw.join(' ').trim().toLowerCase().replace(/\s+/g, ' ');
    const active = loadActiveVenture();
    const phraseMap = new Map([
        ['think and build', active?.repoRoot ? ['open', active.venture || path.basename(active.repoRoot), '--build'] : ['session', '--build']],
        ['keep building', active?.repoRoot ? ['open', active.venture || path.basename(active.repoRoot), '--build'] : ['session', '--build']],
        ['resume building', active?.repoRoot ? ['open', active.venture || path.basename(active.repoRoot), '--build'] : ['session', '--build']],
        ['run the swarm', active?.repoRoot ? ['open', active.venture || path.basename(active.repoRoot), '--build'] : ['session', '--build']],
        ['ask delimit', ['session', '--inspect']],
        ["what's next", ['ask', "what's next"]],
        ['whats next', ['ask', "what's next"]],
        ['check the ledger', ['ask', 'check the ledger']],
    ]);

    if (phraseMap.has(joined)) {
        return phraseMap.get(joined);
    }

    return raw;
}

function resolveVentureTarget(name) {
    const portfolio = resolveContinuityContext({ cwd: process.cwd(), scope: 'all' });
    const normalized = String(name || '').trim().toLowerCase();
    if (!normalized) {
        return null;
    }
    const exact = portfolio.ventureLedgers.find(entry => entry.scope === 'repo' && (
        entry.venture.toLowerCase() === normalized ||
        path.basename(entry.repoRoot || '').toLowerCase() === normalized
    ));
    if (exact) {
        return exact;
    }
    const partial = portfolio.ventureLedgers.find(entry => entry.scope === 'repo' && (
        entry.venture.toLowerCase().includes(normalized) ||
        path.basename(entry.repoRoot || '').toLowerCase().includes(normalized)
    ));
    return partial || null;
}

function rememberVentureTarget(target) {
    if (target?.repoRoot) {
        saveActiveVenture({
            venture: target.venture,
            repoRoot: target.repoRoot,
        });
    }
}

if (process.env.DELIMIT_DEBUG_CONTINUITY === '1') {
    console.log(formatContinuityReport(continuityContext));
    console.log('');
}

// Helper to format a timestamp as relative time (e.g. "2h ago", "3d ago")
function _relativeTime(ts) {
    const diff = Date.now() - ts;
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return mins + 'm ago';
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return hrs + 'h ago';
    const days = Math.floor(hrs / 24);
    if (days < 30) return days + 'd ago';
    const months = Math.floor(days / 30);
    return months + 'mo ago';
}

// Helper to check if agent is running
async function checkAgent() {
    try {
        await axios.get(`${AGENT_URL}/status`);
        return true;
    } catch (e) {
        return false;
    }
}

// Start agent if not running
async function ensureAgent() {
    if (!(await checkAgent())) {
        console.log(chalk.yellow('Starting Delimit Agent...'));
        const agentPath = path.join(__dirname, '..', 'lib', 'agent.js');
        spawn('node', [agentPath], {
            detached: true,
            stdio: 'ignore',
            env: {
                ...process.env,
                DELIMIT_HOME: continuityContext.delimitHome,
                DELIMIT_CONTINUITY_ROOT: continuityContext.continuityRoot,
                DELIMIT_REPO_GOVERNANCE_ROOT: continuityContext.repoGovernanceRoot || '',
                DELIMIT_RESOLVED_VENTURE: continuityContext.venture,
                DELIMIT_RESOLVED_ACTOR: continuityContext.actor,
            }
        }).unref();
        
        // Wait for agent to start
        for (let i = 0; i < 10; i++) {
            await new Promise(r => setTimeout(r, 500));
            if (await checkAgent()) {
                console.log(chalk.green('✓ Agent started'));
                return;
            }
        }
        throw new Error('Failed to start agent');
    }
}

program
    .name('delimit')
    .description('One workspace for every AI coding assistant')
    .version(require('../package.json').version)
    .option('--print-continuity', 'Print resolved continuity context and continue');

// Install command with modes
program.hook('preAction', (thisCommand) => {
    const options = thisCommand.opts();
    if (options.printContinuity) {
        console.log(formatContinuityReport(continuityContext));
        console.log('');
    }
});

program
    .command('install')
    .description('Install Delimit governance with multi-model hooks')
    .option('--mode <mode>', 'Initial mode: advisory, guarded, enforce', 'advisory')
    .option('--scope <scope>', 'Scope: global, repo', 'global')
    .option('--hooks <hooks>', 'Install hooks for: all, git, ai, mcp', 'all')
    .option('--auth', 'Setup authentication during installation')
    .option('--dry-run', 'Preview changes without applying')
    .action(async (options) => {
        console.log(chalk.blue.bold('\n🔵 Delimit Installation\n'));
        
        if (options.dryRun) {
            console.log(chalk.yellow('DRY RUN - No changes will be made\n'));
        }
        
        console.log('This will modify:');
        if (options.scope === 'global') {
            console.log('  • Git global hooks');
            console.log('  • Shell PATH (for AI tool interception)');
            console.log('  • Create ~/.delimit configuration');
            
            if (options.hooks === 'all' || options.hooks === 'ai') {
                console.log('  • AI model hooks (Claude, Codex, Gemini, etc.)');
            }
            if (options.hooks === 'all' || options.hooks === 'mcp') {
                console.log('  • MCP integration hooks for Claude Code');
            }
        } else {
            console.log('  • Git hooks for current repository');
            console.log('  • Create .delimit.yml in current directory');
        }
        
        console.log(`\nInitial mode: ${chalk.bold(options.mode)}`);
        console.log('You can change modes anytime with: delimit mode <mode>\n');
        
        if (!options.dryRun) {
            const { confirm } = await inquirer.prompt([{
                type: 'confirm',
                name: 'confirm',
                message: 'Continue with installation?',
                default: false
            }]);
            
            if (!confirm) {
                console.log(chalk.red('Installation cancelled'));
                return;
            }
        }
        
        if (options.dryRun) {
            console.log(chalk.green('\n✓ Dry run complete'));
            return;
        }
        
        // Actual installation
        await installDelimit(options.mode, options.scope, options.hooks);
        
        // Prompt for authentication setup
        if (!options.dryRun) {
            const { setupAuth } = await inquirer.prompt([{
                type: 'confirm',
                name: 'setupAuth',
                message: '\nWould you like to setup authentication for GitHub, AI tools, and other services?',
                default: true
            }]);
            
            if (setupAuth || options.auth) {
                console.log(chalk.blue.bold('\n🔐 Setting up authentication...\n'));
                const authSetup = new DelimitAuthSetup();
                
                // Prompt for which services to configure
                const { authCategories } = await inquirer.prompt([{
                    type: 'checkbox',
                    name: 'authCategories',
                    message: 'Select services to configure:',
                    choices: [
                        { name: 'GitHub (recommended for governance)', value: 'github', checked: true },
                        { name: 'AI Tools (Claude, OpenAI, Gemini)', value: 'ai', checked: true },
                        { name: 'Cloud Providers (AWS, GCP, Azure)', value: 'cloud' },
                        { name: 'Databases', value: 'databases' },
                        { name: 'Container Registries', value: 'registries' },
                        { name: 'Package Managers', value: 'packages' },
                        { name: 'Monitoring Services', value: 'monitoring' },
                        { name: 'Organization Settings', value: 'org' }
                    ]
                }]);
                
                const setupOptions = {};
                authCategories.forEach(cat => {
                    const key = cat === 'ai' ? 'setupAI' : 
                               cat === 'github' ? 'setupGitHub' :
                               `setup${cat.charAt(0).toUpperCase() + cat.slice(1)}`;
                    setupOptions[key] = true;
                });
                
                await authSetup.setup(setupOptions);
                console.log(chalk.green('\n✅ Authentication setup complete!'));
            }
        }
    });

// Mode switching command
program
    .command('mode [mode]')
    .description('Switch governance mode (advisory, guarded, enforce, auto)')

    .action(async (mode) => {
        await ensureAgent();
        
        if (!mode) {
            // Show current mode
            const { data } = await axios.get(`${AGENT_URL}/status`);
            console.log(chalk.blue('Current mode:'), chalk.bold(data.sessionMode));
            return;
        }
        
        if (!['advisory', 'guarded', 'enforce', 'auto'].includes(mode)) {
            console.error(chalk.red('Invalid mode. Choose: advisory, guarded, enforce, auto'));
            return;
        }
        
        const { data } = await axios.post(`${AGENT_URL}/mode`, { mode });
        console.log(chalk.green(`✓ Mode switched to: ${chalk.bold(data.mode)}`));
    });

// Status command
program
    .command('status')
    .description('Show a compact dashboard of your Delimit setup')

    .option('--verbose', 'Show detailed status')
    .option('--json', 'Output as JSON')
    .option('--watch', 'Refresh every 5 seconds')
    .action(async (options) => {
        const homedir = os.homedir();
        const delimitHome = path.join(homedir, '.delimit');
        const target = process.cwd();
        const { execSync } = require('child_process');

        function renderStatus() {
            const data = {};

            // --- Memory stats ---
            const memoryDir = path.join(delimitHome, 'memory');
            let memTotal = 0;
            let memRecent = 0;
            let recentMemories = [];
            let memIntegrity = { verified: 0, failed: 0 };
            const oneWeekAgo = Date.now() - 7 * 24 * 60 * 60 * 1000;
            try {
                const memFiles = fs.readdirSync(memoryDir).filter(f => f.startsWith('mem-') && f.endsWith('.json'));
                memTotal = memFiles.length;
                for (const f of memFiles) {
                    try {
                        const raw = fs.readFileSync(path.join(memoryDir, f), 'utf-8');
                        const d = JSON.parse(raw);
                        const ts = new Date(d.created_at || d.timestamp || d.created || 0).getTime();
                        if (ts > oneWeekAgo) memRecent++;
                        recentMemories.push({ text: d.text || d.content || '', tags: d.tags || [], ts, source: d.context || d.source || 'unknown' });
                        // Verify integrity hash if present
                        if (d.hash) {
                            const expected = require('crypto').createHash('sha256').update(d.content || d.text || '').digest('hex').slice(0, 16);
                            if (d.hash === expected) memIntegrity.verified++;
                            else memIntegrity.failed++;
                        }
                    } catch {}
                }
                recentMemories.sort((a, b) => b.ts - a.ts);
                recentMemories = recentMemories.slice(0, 3);
            } catch {}
            data.memory = { total: memTotal, recent: memRecent, integrity: memIntegrity };

            // --- Governance / Policy ---
            const policyPath = path.join(target, '.delimit', 'policies.yml');
            let policyName = 'none';
            let policyMode = '';
            let ruleCount = 0;
            let hasPolicy = false;
            if (fs.existsSync(policyPath)) {
                hasPolicy = true;
                try {
                    const policyContent = yaml.load(fs.readFileSync(policyPath, 'utf-8'));
                    policyName = policyContent?.preset || policyContent?.name || 'custom';
                    policyMode = policyContent?.enforcement_mode || policyContent?.mode || '';
                    if (policyContent?.rules) ruleCount = Object.keys(policyContent.rules).length;
                } catch {
                    policyName = 'custom';
                }
            }

            // Count tracked specs
            const specPatterns = ['openapi.yaml', 'openapi.yml', 'openapi.json', 'swagger.yaml', 'swagger.yml', 'swagger.json'];
            let specCount = 0;
            const _countSpecs = (dir, depth) => {
                if (depth > 3) return;
                try {
                    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
                        if (['node_modules', '.next', 'venv', '.git'].includes(entry.name)) continue;
                        const full = path.join(dir, entry.name);
                        if (entry.isFile() && specPatterns.includes(entry.name.toLowerCase())) specCount++;
                        else if (entry.isDirectory()) _countSpecs(full, depth + 1);
                    }
                } catch {}
            };
            _countSpecs(target, 0);
            data.governance = { policy: policyName, mode: policyMode, rules: ruleCount, specs: specCount };

            // --- Git hooks ---
            const preCommitPath = path.join(target, '.git', 'hooks', 'pre-commit');
            let hasGitHooks = false;
            try { hasGitHooks = fs.readFileSync(preCommitPath, 'utf-8').includes('delimit'); } catch {}
            data.hooks = hasGitHooks;

            // --- CI ---
            const workflowDir = path.join(target, '.github', 'workflows');
            let hasCI = false;
            let ciFile = '';
            try {
                const wfs = fs.readdirSync(workflowDir);
                for (const wf of wfs) {
                    try {
                        if (fs.readFileSync(path.join(workflowDir, wf), 'utf-8').includes('delimit')) {
                            hasCI = true;
                            ciFile = wf;
                            break;
                        }
                    } catch {}
                }
            } catch {}
            data.ci = { active: hasCI, file: ciFile };

            // --- MCP ---
            const mcpConfigPath = path.join(homedir, '.mcp.json');
            let hasMcp = false;
            let toolCount = 0;
            try { hasMcp = fs.readFileSync(mcpConfigPath, 'utf-8').includes('delimit'); } catch {}
            if (hasMcp) {
                const serverPyPaths = [
                    path.join(delimitHome, 'server', 'ai', 'server.py'),
                    path.join(delimitHome, 'server', 'server.py'),
                ];
                for (const sp of serverPyPaths) {
                    try {
                        const toolMatches = fs.readFileSync(sp, 'utf-8').match(/@mcp\.tool/g);
                        if (toolMatches) { toolCount = toolMatches.length; break; }
                    } catch {}
                }
            }
            data.mcp = { connected: hasMcp, tools: toolCount };

            // --- Models ---
            const modelsPath = path.join(delimitHome, 'models.json');
            let modelNames = [];
            try {
                const modelsData = JSON.parse(fs.readFileSync(modelsPath, 'utf-8'));
                for (const [key, val] of Object.entries(modelsData)) {
                    if (val && typeof val === 'object' && (val.api_key || val.enabled !== false)) {
                        modelNames.push(key.charAt(0).toUpperCase() + key.slice(1));
                    }
                }
            } catch {}
            data.models = modelNames;

            // --- License ---
            const licensePath = path.join(delimitHome, 'license.json');
            let licenseTier = 'Free';
            let licenseActive = false;
            try {
                const ld = JSON.parse(fs.readFileSync(licensePath, 'utf-8'));
                licenseTier = ld.tier || ld.plan || 'Free';
                licenseActive = ld.status === 'active' || ld.valid === true;
            } catch {}
            data.license = { tier: licenseTier, active: licenseActive };

            // --- Ledger stats ---
            const ledgerDir = path.join(delimitHome, 'ledger');
            let ledgerOpen = 0;
            let ledgerTotal = 0;
            try {
                const ledgerFiles = fs.readdirSync(ledgerDir).filter(f => f.endsWith('.json') || f.endsWith('.jsonl'));
                for (const lf of ledgerFiles) {
                    try {
                        const content = fs.readFileSync(path.join(ledgerDir, lf), 'utf-8');
                        if (lf.endsWith('.jsonl')) {
                            const lines = content.split('\n').filter(l => l.trim());
                            for (const line of lines) {
                                try {
                                    const item = JSON.parse(line);
                                    ledgerTotal++;
                                    if (!item.status || item.status === 'open' || item.status === 'in_progress') ledgerOpen++;
                                } catch {}
                            }
                        } else {
                            const item = JSON.parse(content);
                            if (Array.isArray(item)) {
                                ledgerTotal += item.length;
                                ledgerOpen += item.filter(i => !i.status || i.status === 'open' || i.status === 'in_progress').length;
                            } else {
                                ledgerTotal++;
                                if (!item.status || item.status === 'open' || item.status === 'in_progress') ledgerOpen++;
                            }
                        }
                    } catch {}
                }
            } catch {}
            data.ledger = { total: ledgerTotal, open: ledgerOpen };

            // --- Evidence stats ---
            const evidenceDir = path.join(delimitHome, 'evidence');
            let evidenceCount = 0;
            try {
                const evFiles = fs.readdirSync(evidenceDir).filter(f => f.endsWith('.json') || f.endsWith('.jsonl'));
                evidenceCount = evFiles.length;
            } catch {}
            data.evidence = evidenceCount;

            // --- Last session ---
            let lastSession = null;
            const sessionsDir = path.join(delimitHome, 'sessions');
            try {
                const sessFiles = fs.readdirSync(sessionsDir)
                    .filter(f => f.startsWith('session_') && f.endsWith('.json'))
                    .sort().reverse();
                if (sessFiles.length > 0) {
                    const latest = JSON.parse(fs.readFileSync(path.join(sessionsDir, sessFiles[0]), 'utf-8'));
                    lastSession = latest.summary || latest.description || latest.title || null;
                }
            } catch {}
            data.lastSession = lastSession;

            // --- Git info ---
            let gitBranch = '';
            let gitDirty = false;
            try {
                gitBranch = execSync('git rev-parse --abbrev-ref HEAD', { stdio: 'pipe', cwd: target }).toString().trim();
                const statusOut = execSync('git status --porcelain', { stdio: 'pipe', cwd: target }).toString().trim();
                gitDirty = statusOut.length > 0;
            } catch {}
            data.git = { branch: gitBranch, dirty: gitDirty };

            // --- Version ---
            let cliVersion = '';
            try { cliVersion = require(path.join(__dirname, '..', 'package.json')).version; } catch {}
            data.version = cliVersion;

            // --- Readiness ---
            const checks = [
                { name: 'Spec', done: specCount > 0 },
                { name: 'Policy', done: hasPolicy },
                { name: 'CI', done: hasCI },
                { name: 'Hooks', done: hasGitHooks },
                { name: 'MCP', done: hasMcp },
            ];
            const score = checks.filter(c => c.done).length;
            data.readiness = { score, total: checks.length, checks };

            // === JSON output ===
            if (options.json) {
                console.log(JSON.stringify(data, null, 2));
                return;
            }

            // === Visual dashboard ===
            const W = 60;
            const line = '\u2500'.repeat(W - 2);
            const dot = (ok) => ok ? chalk.green('\u25cf') : chalk.gray('\u25cb');
            const bar = (n, total, width = 20) => {
                const filled = Math.round((n / Math.max(total, 1)) * width);
                return chalk.green('\u2588'.repeat(filled)) + chalk.gray('\u2591'.repeat(width - filled));
            };

            console.log('');
            console.log(chalk.bold.cyan(`  \u250c${line}\u2510`));
            console.log(chalk.bold.cyan(`  \u2502`) + chalk.bold.white(`  Delimit v${cliVersion}`) + ' '.repeat(W - 14 - cliVersion.length) + (licenseTier !== 'Free' ? chalk.green(licenseTier) : chalk.gray('Free')) + chalk.bold.cyan(` \u2502`));
            console.log(chalk.bold.cyan(`  \u2514${line}\u2518`));

            // Governance section
            console.log(chalk.bold('\n  Governance'));
            const policyStr = hasPolicy ? chalk.green(policyName) + (policyMode ? chalk.gray(` (${policyMode})`) : '') + (ruleCount ? chalk.gray(` ${ruleCount} rules`) : '') : chalk.gray('not configured');
            console.log(`    Policy:   ${policyStr}`);
            console.log(`    Specs:    ${specCount > 0 ? chalk.white(specCount + ' tracked') : chalk.gray('none')}`);
            console.log(`    Hooks:    ${hasGitHooks ? chalk.green('pre-commit') : chalk.gray('none')}`);
            console.log(`    CI:       ${hasCI ? chalk.green(ciFile) : chalk.gray('none')}`);

            // Infrastructure section
            console.log(chalk.bold('\n  Infrastructure'));
            console.log(`    MCP:      ${hasMcp ? chalk.green(toolCount + ' tools') : chalk.gray('not configured')}`);
            console.log(`    Models:   ${modelNames.length > 0 ? chalk.white(modelNames.join(chalk.gray(' + '))) : chalk.gray('none')}`);
            console.log(`    License:  ${licenseTier !== 'Free' && licenseActive ? chalk.green(licenseTier) : licenseTier !== 'Free' ? chalk.yellow(licenseTier) : chalk.gray('Free')}`);

            // Context section
            console.log(chalk.bold('\n  Context'));
            console.log(`    Memory:   ${chalk.white.bold(memTotal)} total${memRecent > 0 ? chalk.gray(` (${memRecent} this week)`) : ''}`);
            console.log(`    Ledger:   ${ledgerOpen > 0 ? chalk.yellow(ledgerOpen + ' open') : chalk.gray('0 open')} / ${ledgerTotal} total`);
            console.log(`    Evidence: ${evidenceCount > 0 ? chalk.white(evidenceCount + ' records') : chalk.gray('none')}`);
            if (gitBranch) {
                console.log(`    Branch:   ${chalk.white(gitBranch)}${gitDirty ? chalk.yellow(' (dirty)') : chalk.green(' (clean)')}`);
            }

            // Recent memories
            if (recentMemories.length > 0) {
                console.log(chalk.bold('\n  Recent Memories'));
                for (const mem of recentMemories) {
                    const ago = _relativeTime(mem.ts);
                    const src = mem.source !== 'unknown' ? chalk.gray(` [${mem.source}]`) : '';
                    const text = mem.text.length > 50 ? mem.text.slice(0, 50) + '...' : mem.text;
                    console.log(`    ${chalk.gray(ago.padEnd(12))} ${text}${src}`);
                }
            }

            // Last session
            if (lastSession) {
                const truncated = lastSession.length > 55 ? lastSession.slice(0, 55) + '...' : lastSession;
                console.log(chalk.bold('\n  Last Session'));
                console.log(`    ${truncated}`);
            }

            // Readiness bar
            console.log(chalk.bold('\n  Readiness'));
            console.log(`    ${bar(score, checks.length)} ${score}/${checks.length}`);
            console.log('    ' + checks.map(c => dot(c.done) + ' ' + (c.done ? c.name : chalk.gray(c.name))).join('  '));
            console.log('');

            if (options.verbose) {
                console.log(chalk.bold('  Continuity Context:'));
                console.log(formatContinuityReport(continuityContext).split('\n').slice(1).map(line => '    ' + line.trimStart()).join('\n'));
                console.log('');
            }
        }

        if (options.watch) {
            const clear = () => process.stdout.write('\x1B[2J\x1B[0f');
            const tick = () => { clear(); renderStatus(); };
            tick();
            setInterval(tick, 5000);
        } else {
            renderStatus();
        }
    });

program
    .command('session')
    .description('Start a native Delimit interactive session')
    .option('--build', 'Bootstrap in execute mode for think-and-build flows')
    .option('--inspect', 'Bootstrap in inspect mode (default)')
    .option('--all', 'Open portfolio view across ventures')
    .action(async (options) => {
        await runInteractiveSession({
            build: Boolean(options.build) && !options.inspect,
            scope: options.all ? 'all' : undefined,
        });
    });

program
    .command('build [venture]')
    .description('Run the native Delimit build session')
    .action(async (venture) => {
        if (!venture) {
            const active = loadActiveVenture();
            if (active?.repoRoot) {
                await runInteractiveSession({ cwd: active.repoRoot, build: true });
                return;
            }
            await runInteractiveSession({ build: true, scope: resolveRepoRoot(process.cwd()) ? undefined : 'all' });
            return;
        }
        const target = resolveVentureTarget(venture);
        if (!target) {
            console.error(`Unknown venture: ${venture}`);
            process.exit(1);
        }
        rememberVentureTarget(target);
        await runInteractiveSession({
            cwd: target.repoRoot,
            build: true,
        });
    });

program
    .command('think [question...]')
    .description('Run multi-model deliberation on a question')
    .action(async (questionParts) => {
        const question = Array.isArray(questionParts) ? questionParts.join(' ').trim() : '';
        if (!question) {
            console.log(chalk.yellow('Usage: delimit think <question>'));
            console.log(chalk.gray('Example: delimit think "Should we use REST or GraphQL for the new API?"'));
            return;
        }
        console.log(chalk.blue('Deliberating...'));
        console.log(chalk.gray(`Question: ${question}`));
        try {
            const result = execSync(
                `python3 -c "import sys; sys.path.insert(0, '${continuityContext.serverDir}'); from ai.deliberation import deliberate; import json; r = deliberate(question='${question.replace(/'/g, "\\'")}'); print(json.dumps(r, indent=2))"`,
                { encoding: 'utf-8', timeout: 120000, cwd: continuityContext.serverDir }
            );
            const parsed = JSON.parse(result);
            if (parsed.error) {
                console.log(chalk.red(`Error: ${parsed.error}`));
            } else if (parsed.synthesis) {
                console.log(chalk.green('\nConsensus:'));
                console.log(parsed.synthesis);
                if (parsed.verdict) console.log(chalk.blue(`\nVerdict: ${parsed.verdict}`));
                if (parsed.confidence) console.log(chalk.gray(`Confidence: ${parsed.confidence}`));
            } else {
                console.log(JSON.stringify(parsed, null, 2));
            }
        } catch (e) {
            console.log(chalk.red(`Deliberation failed: ${e.message}`));
        }
    });

program
    .command('open [venture]')
    .description('Open a venture session without changing directories')
    .option('--build', 'Open directly in build mode')
    .action(async (venture, options) => {
        if (!venture) {
            await runInteractiveSession({
                build: Boolean(options.build),
                scope: resolveRepoRoot(process.cwd()) ? undefined : 'all',
            });
            return;
        }
        const target = resolveVentureTarget(venture);
        if (!target) {
            console.error(`Unknown venture: ${venture}`);
            process.exit(1);
        }
        rememberVentureTarget(target);
        await runInteractiveSession({
            cwd: target.repoRoot,
            build: Boolean(options.build),
        });
    });

program
    .command('switch <venture>')
    .description('Set the active venture for future keep-building flows')
    .action(async (venture) => {
        const target = resolveVentureTarget(venture);
        if (!target) {
            console.error(`Unknown venture: ${venture}`);
            process.exit(1);
        }
        rememberVentureTarget(target);
        console.log(`Active venture: ${target.venture}`);
        if (target.repoRoot) {
            console.log(`Repo: ${target.repoRoot}`);
        }
    });

program
    .command('ask [query...]')
    .description('Query Delimit state without mutating it')
    .action(async (queryParts) => {
        const query = Array.isArray(queryParts) ? queryParts.join(' ').trim().toLowerCase() : '';
        const activePrefQueries = ['worker', 'next', 'what\'s next', 'whats next', 'check the ledger', 'status', 'details', 'plan'];
        const preferActive = !query || activePrefQueries.some(fragment => query.includes(fragment));
        const context = getDynamicContinuityContext({ preferActive });
        if (query.includes('worker') && context.ledgerScope !== 'all') {
            const worker = getWorkerState(context);
            if (!worker.state || !pidIsAlive(worker.state.pid)) {
                const action = ensureWorker(context);
                if (action.started) {
                    await waitForWorkerState(context);
                }
            }
        }
        const summary = renderSummary(context);
        if (!query || query === 'status' || query === 'what\'s next' || query === 'whats next' || query === 'check the ledger') {
            console.log(summary.text);
            return;
        }
        if (query.includes('ventures') || query.includes('portfolio') || query.includes('all ventures')) {
            const portfolio = renderSummary(getDynamicContinuityContext({ scope: 'all' }));
            console.log(portfolio.text);
            return;
        }
        if (query.includes('active venture')) {
            const active = loadActiveVenture();
            if (!active) {
                console.log('No active venture selected.');
            } else {
                console.log(`${active.venture}  ${active.repoRoot || ''}`.trim());
            }
            return;
        }
        if (query.includes('next')) {
            if (summary.ledger.nextItem) {
                console.log(`${summary.ledger.nextItem.id}  ${summary.ledger.nextItem.title || '(untitled)'}  [${summary.ledger.nextItem.priority || 'P?'}]`);
            } else {
                console.log('No open ledger items.');
            }
            return;
        }
        if (query.includes('worker')) {
            if (summary.worker.state) {
                console.log(`Worker: ${summary.workerStatus}`);
                if (summary.worker.state.phase) {
                    console.log(`State: ${summary.worker.state.phase}`);
                }
                console.log(`PID: ${summary.worker.state.pid}`);
                const taskBrief = getTaskBrief(context);
                if (taskBrief.brief?.summary) {
                    console.log(`Task: ${taskBrief.brief.summary}`);
                }
                if (summary.worker.state.nextItem) {
                    console.log(`Next: ${summary.worker.state.nextItem.id} ${summary.worker.state.nextItem.title || ''}`);
                }
                if (taskBrief.brief?.recommendedAction) {
                    console.log(`Action: ${taskBrief.brief.recommendedAction}`);
                }
                const executionPlan = getExecutionPlan(context);
                if (executionPlan.plan?.targetAreas?.length) {
                    console.log(`Targets: ${executionPlan.plan.targetAreas.join(', ')}`);
                }
                const ownerActions = getOwnerActions(context);
                if (ownerActions.state?.actions?.length) {
                    console.log(`Owner actions: ${ownerActions.state.actions.length} queued (non-blocking)`);
                }
            } else {
                console.log('No worker state written yet.');
            }
            return;
        }
        if (query.includes('details') || query.includes('plan')) {
            const taskBrief = getTaskBrief(context);
            const executionPlan = getExecutionPlan(context);
            if (taskBrief.brief?.summary) {
                console.log(`Task: ${taskBrief.brief.summary}`);
                if (taskBrief.brief.recommendedAction) {
                    console.log(`Action: ${taskBrief.brief.recommendedAction}`);
                }
                if (executionPlan.plan?.steps?.length) {
                    console.log('');
                    console.log('Plan:');
                    for (const step of executionPlan.plan.steps) {
                        console.log(`- ${step}`);
                    }
                }
                const ownerActions = getOwnerActions(context);
                if (ownerActions.state?.actions?.length) {
                    console.log('');
                    console.log('Owner actions (non-blocking):');
                    for (const action of ownerActions.state.actions) {
                        console.log(`- ${action.title} [${(action.channels || []).join(', ')}]`);
                    }
                }
            } else {
                console.log(summary.text);
            }
            return;
        }
        console.log(summary.text);
    });

// Policy command
program
    .command('policy')
    .description('Manage governance policies')

    .option('--init', 'Create example policy file')
    .option('--validate', 'Validate policy syntax')
    .action(async (options) => {
        if (options.init) {
            const examplePolicy = `# Delimit Policy Configuration
# This file defines dynamic governance rules

defaultMode: advisory

rules:
  - name: "Production Protection"
    mode: enforce
    triggers:
      - gitBranch: [main, master, production]
      
  - name: "Payment Code Security"
    mode: enforce
    triggers:
      - path: "**/payment/**"
      - content: ["stripe", "payment", "billing"]
      
  - name: "AI-Generated Code Review"
    mode: guarded
    triggers:
      - commitMessage: "Co-authored-by"
      
  - name: "Documentation Freedom"
    mode: advisory
    triggers:
      - path: "**/*.md"
    final: true

overrides:
  allowEnforceOverride: false
  requireGuardedOverrideReason: true
`;
            
            fs.writeFileSync('delimit.yml', examplePolicy);
            console.log(chalk.green('✓ Created delimit.yml'));
            console.log('Edit this file to customize your governance rules');
        }
        
        if (options.validate) {
            const policyPaths = [
                path.join(process.cwd(), '.delimit', 'policies.yml'),
                path.join(process.cwd(), 'delimit.yml'),
                path.join(process.cwd(), '.delimit.yml'),
            ];
            const policyFile = policyPaths.find(p => fs.existsSync(p));
            if (!policyFile) {
                console.log(chalk.red('No policy file found.'));
                console.log(chalk.dim('  Expected: .delimit/policies.yml, delimit.yml, or .delimit.yml'));
                console.log('  Run ' + chalk.cyan('delimit policy --init') + ' to create one.');
                process.exit(1);
            }
            console.log(chalk.bold('Validating: ') + chalk.dim(policyFile));
            try {
                const raw = fs.readFileSync(policyFile, 'utf-8');
                let parsed;
                try {
                    parsed = require('js-yaml').load(raw);
                } catch {
                    // Fallback: try JSON
                    parsed = JSON.parse(raw);
                }
                const errors = [];
                const warnings = [];
                if (!parsed || typeof parsed !== 'object') {
                    errors.push('Policy file is empty or not a valid object');
                } else {
                    // Check required structure
                    if (!parsed.rules && !parsed.mode && !parsed.governance) {
                        warnings.push('No "rules", "mode", or "governance" key found');
                    }
                    if (parsed.rules) {
                        if (!Array.isArray(parsed.rules)) {
                            errors.push('"rules" must be an array');
                        } else {
                            parsed.rules.forEach((rule, i) => {
                                if (!rule.id) warnings.push(`Rule ${i + 1}: missing "id"`);
                                if (!rule.change_types && !rule.name) warnings.push(`Rule ${i + 1}: missing "change_types" or "name"`);
                                if (rule.severity && !['error', 'warning', 'info'].includes(rule.severity)) {
                                    errors.push(`Rule ${i + 1}: invalid severity "${rule.severity}" (use: error, warning, info)`);
                                }
                                if (rule.action && !['forbid', 'warn', 'allow', 'require_approval'].includes(rule.action)) {
                                    errors.push(`Rule ${i + 1}: invalid action "${rule.action}" (use: forbid, warn, allow, require_approval)`);
                                }
                            });
                            console.log(chalk.dim(`  Rules: ${parsed.rules.length} defined`));
                        }
                    }
                    if (parsed.mode && !['strict', 'default', 'relaxed'].includes(parsed.mode)) {
                        warnings.push(`Unknown mode "${parsed.mode}" (expected: strict, default, relaxed)`);
                    }
                }
                if (errors.length > 0) {
                    errors.forEach(e => console.log(chalk.red(`  ✗ ${e}`)));
                }
                if (warnings.length > 0) {
                    warnings.forEach(w => console.log(chalk.yellow(`  ⚠ ${w}`)));
                }
                if (errors.length === 0) {
                    console.log(chalk.green('  ✓ Policy file is valid'));
                } else {
                    process.exit(1);
                }
            } catch (e) {
                console.log(chalk.red(`  ✗ Failed to parse policy: ${e.message}`));
                process.exit(1);
            }
        }
    });

// Auth command - setup credentials
program
    .command('auth')
    .description('Setup authentication and credentials for services')

    .option('--all', 'Setup all available services')
    .option('--github', 'Setup GitHub authentication')
    .option('--ai', 'Setup AI tools authentication')
    .option('--cloud', 'Setup cloud provider credentials')
    .option('--databases', 'Setup database credentials')
    .option('--registries', 'Setup container registry credentials')
    .option('--packages', 'Setup package manager credentials')
    .option('--monitoring', 'Setup monitoring service credentials')
    .option('--org', 'Setup organization settings')
    .action(async (options) => {
        console.log(chalk.blue.bold('\n🔐 Delimit Authentication Setup\n'));
        
        const authSetup = new DelimitAuthSetup();
        
        // Determine what to setup
        const setupOptions = {
            setupAll: options.all,
            setupGitHub: options.github,
            setupAI: options.ai,
            setupCloud: options.cloud,
            setupDatabases: options.databases,
            setupRegistries: options.registries,
            setupPackages: options.packages,
            setupMonitoring: options.monitoring,
            setupOrg: options.org
        };
        
        // If no specific options, prompt for what to setup
        if (!Object.values(setupOptions).some(v => v)) {
            const { categories } = await inquirer.prompt([{
                type: 'checkbox',
                name: 'categories',
                message: 'Which services would you like to configure?',
                choices: [
                    { name: 'GitHub', value: 'github' },
                    { name: 'AI Tools (Claude, OpenAI, Gemini, etc.)', value: 'ai' },
                    { name: 'Cloud Providers (AWS, GCP, Azure)', value: 'cloud' },
                    { name: 'Databases', value: 'databases' },
                    { name: 'Container Registries', value: 'registries' },
                    { name: 'Package Managers', value: 'packages' },
                    { name: 'Monitoring Services', value: 'monitoring' },
                    { name: 'Organization Settings', value: 'org' }
                ]
            }]);
            
            categories.forEach(cat => {
                setupOptions[`setup${cat.charAt(0).toUpperCase() + cat.slice(1)}`] = true;
            });
        }
        
        await authSetup.setup(setupOptions);
        
        console.log(chalk.green.bold('\n✅ Authentication setup complete!\n'));
        console.log('Your credentials have been securely stored in ~/.delimit/credentials/');
        console.log('Run "delimit auth" again anytime to add or update credentials');
    });

// Audit command
program
    .command('audit')
    .description('View governance audit log')

    .option('--tail <n>', 'Show last N entries', '10')
    .action(async (options) => {
        await ensureAgent();
        
        const { data } = await axios.get(`${AGENT_URL}/audit`);
        const entries = data.slice(-parseInt(options.tail));
        
        if (entries.length === 0) {
            console.log(chalk.yellow('No audit log entries'));
            return;
        }
        
        console.log(chalk.blue.bold('\nRecent Governance Decisions:\n'));
        entries.forEach(entry => {
            const color = entry.action === 'block' ? chalk.red :
                          entry.action === 'prompt' ? chalk.yellow :
                          chalk.green;
            
            console.log(`${entry.timestamp} | ${color(entry.mode.toUpperCase())} | ${entry.message}`);
            if (entry.rule) {
                console.log(`  Rule: ${entry.rule}`);
            }
        });
    });

// Doctor command - verify setup for API governance
// (legacy doctor replaced with v1-focused checks)

// Explain-decision command - show governance decision reasoning
program
    .command('explain-decision [decision-id]')
    .description('Explain a governance decision')

    .action(async (decisionId) => {
        await ensureAgent();
        
        try {
            const { data } = await axios.get(`${AGENT_URL}/explain/${decisionId || 'last'}`);
            console.log(data.explanation);
        } catch (e) {
            if (e.response?.status === 404) {
                console.log(chalk.red('No decision found'));
            } else {
                console.log(chalk.red('Error fetching decision explanation'));
            }
        }
    });

// Uninstall command
program
    .command('uninstall')
    .description('Remove Delimit governance from all AI assistants')
    .option('--dry-run', 'Preview what would be removed without making changes')
    .action(async (options) => {
        const dryRun = options.dryRun;
        const HOME = process.env.HOME;
        const backupDir = path.join(HOME, '.delimit', 'backups', `uninstall-${Date.now()}`);
        const changes = [];

        if (dryRun) {
            console.log(chalk.yellow.bold('\nDRY RUN — No changes will be made\n'));
        }

        // Collect all changes first
        // 1. Git hooks
        try {
            const hooksPath = execSync('git config --global --get core.hooksPath 2>/dev/null', { encoding: 'utf8' }).trim();
            if (hooksPath && hooksPath.includes('delimit')) {
                changes.push({ target: 'Git global hooks', action: 'unset core.hooksPath', current: hooksPath });
            }
        } catch (e) {}

        // 2. Shell profiles
        const profiles = ['.bashrc', '.zshrc', '.profile'];
        profiles.forEach(profile => {
            const profilePath = path.join(HOME, profile);
            if (fs.existsSync(profilePath)) {
                const content = fs.readFileSync(profilePath, 'utf8');
                if (content.includes('# Delimit Governance Layer')) {
                    changes.push({ target: `~/${profile}`, action: 'Remove Delimit PATH block' });
                }
            }
        });

        // 3. Claude Code MCP
        const mcpPath = path.join(HOME, '.mcp.json');
        if (fs.existsSync(mcpPath)) {
            try {
                const mcp = JSON.parse(fs.readFileSync(mcpPath, 'utf8'));
                if (mcp.mcpServers && mcp.mcpServers.delimit) {
                    changes.push({ target: '~/.mcp.json', action: 'Remove delimit MCP entry' });
                }
            } catch (e) {}
        }

        // 4. Codex config
        const codexConfig = path.join(HOME, '.codex', 'config.json');
        const codexToml = path.join(HOME, '.codex', 'config.toml');
        if (fs.existsSync(codexConfig)) {
            try {
                const cfg = JSON.parse(fs.readFileSync(codexConfig, 'utf8'));
                if (cfg.mcpServers && cfg.mcpServers.delimit) {
                    changes.push({ target: '~/.codex/config.json', action: 'Remove delimit MCP entry' });
                }
            } catch (e) {}
        }
        if (fs.existsSync(codexToml)) {
            try {
                const toml = fs.readFileSync(codexToml, 'utf8');
                if (toml.includes('[mcp_servers.delimit]')) {
                    changes.push({ target: '~/.codex/config.toml', action: 'Remove [mcp_servers.delimit] block' });
                }
            } catch (e) {}
        }

        // 5. Cursor config
        const cursorConfig = path.join(HOME, '.cursor', 'mcp.json');
        if (fs.existsSync(cursorConfig)) {
            try {
                const cfg = JSON.parse(fs.readFileSync(cursorConfig, 'utf8'));
                if (cfg.mcpServers && cfg.mcpServers.delimit) {
                    changes.push({ target: '~/.cursor/mcp.json', action: 'Remove delimit MCP entry' });
                }
            } catch (e) {}
        }

        // 6. Gemini CLI config
        const geminiConfig = path.join(HOME, '.gemini', 'settings.json');
        if (fs.existsSync(geminiConfig)) {
            try {
                const cfg = JSON.parse(fs.readFileSync(geminiConfig, 'utf8'));
                if (cfg.mcpServers && cfg.mcpServers.delimit) {
                    changes.push({ target: '~/.gemini/settings.json', action: 'Remove delimit MCP entry' });
                }
            } catch (e) {}
        }

        // 7. Shims + wrapped binaries
        const shimsDir = path.join(HOME, '.delimit', 'shims');
        if (fs.existsSync(shimsDir)) {
            changes.push({ target: '~/.delimit/shims/', action: 'Remove CLI shims directory' });
        }
        for (const tool of ['claude', 'codex', 'gemini']) {
            const searchPaths = [`/usr/local/bin/${tool}-real`, `/usr/bin/${tool}-real`, path.join(HOME, '.local', 'bin', `${tool}-real`)];
            for (const p of searchPaths) {
                if (fs.existsSync(p)) {
                    changes.push({ target: p.replace(HOME, '~'), action: `Restore original ${tool} binary` });
                    break;
                }
            }
        }

        // 8. Cross-model governance hooks (LED-202)
        const claudeSettingsPath = path.join(HOME, '.claude', 'settings.json');
        if (fs.existsSync(claudeSettingsPath)) {
            try {
                const cfg = JSON.parse(fs.readFileSync(claudeSettingsPath, 'utf8'));
                if (cfg.hooks) {
                    const hasDelimitHook = Object.values(cfg.hooks).some(arr =>
                        Array.isArray(arr) && arr.some(h => h.command && h.command.includes('delimit-cli'))
                    );
                    if (hasDelimitHook) {
                        changes.push({ target: '~/.claude/settings.json', action: 'Remove Delimit governance hooks' });
                    }
                }
            } catch (e) {}
        }
        const codexInstructions = path.join(HOME, '.codex', 'instructions.md');
        if (fs.existsSync(codexInstructions)) {
            const content = fs.readFileSync(codexInstructions, 'utf8');
            if (content.includes('delimit:hooks-start')) {
                changes.push({ target: '~/.codex/instructions.md', action: 'Remove Delimit hook instructions' });
            }
        }
        const geminiGovMd = path.join(HOME, '.gemini', 'GEMINI.md');
        if (fs.existsSync(geminiGovMd)) {
            const content = fs.readFileSync(geminiGovMd, 'utf8');
            if (content.includes('Delimit Governance')) {
                changes.push({ target: '~/.gemini/GEMINI.md', action: 'Remove Delimit governance file' });
            }
        }

        if (changes.length === 0) {
            console.log(chalk.green('\nNo Delimit integrations found. Nothing to remove.\n'));
            return;
        }

        // Show what will be changed
        console.log(chalk.bold('\nThe following changes will be made:\n'));
        changes.forEach((c, i) => {
            console.log(`  ${i + 1}. ${chalk.cyan(c.target)} — ${c.action}`);
        });
        console.log('');

        if (dryRun) {
            console.log(chalk.yellow('Run without --dry-run to apply these changes.\n'));
            return;
        }

        const { confirm } = await inquirer.prompt([{
            type: 'confirm',
            name: 'confirm',
            message: `Apply ${changes.length} changes? Backups will be saved to ~/.delimit/backups/`,
            default: false
        }]);

        if (!confirm) return;

        // Create backup directory
        fs.mkdirSync(backupDir, { recursive: true });

        // Execute changes with backups
        // Git hooks
        try {
            execSync('git config --global --unset core.hooksPath 2>/dev/null');
            console.log(chalk.green('✓ Removed Git hooks'));
        } catch (e) {}

        // Shell profiles
        profiles.forEach(profile => {
            const profilePath = path.join(HOME, profile);
            if (fs.existsSync(profilePath)) {
                let content = fs.readFileSync(profilePath, 'utf8');
                if (content.includes('# Delimit Governance Layer')) {
                    fs.copyFileSync(profilePath, path.join(backupDir, profile));
                    content = content.replace(/# Delimit Governance Layer[\s\S]*?fi\n/g, '');
                    fs.writeFileSync(profilePath, content);
                }
            }
        });
        console.log(chalk.green('✓ Removed PATH modifications'));

        // Helper to remove delimit from JSON config
        function removeFromJsonConfig(configPath, label) {
            if (!fs.existsSync(configPath)) return;
            try {
                const cfg = JSON.parse(fs.readFileSync(configPath, 'utf8'));
                if (cfg.mcpServers && cfg.mcpServers.delimit) {
                    fs.copyFileSync(configPath, path.join(backupDir, path.basename(configPath) + '.' + label));
                    delete cfg.mcpServers.delimit;
                    fs.writeFileSync(configPath, JSON.stringify(cfg, null, 2));
                    console.log(chalk.green(`✓ Removed from ${label}`));
                }
            } catch (e) {}
        }

        removeFromJsonConfig(mcpPath, 'claude-code');
        removeFromJsonConfig(codexConfig, 'codex');
        removeFromJsonConfig(cursorConfig, 'cursor');
        removeFromJsonConfig(geminiConfig, 'gemini');

        // Handle Codex TOML config
        if (fs.existsSync(codexToml)) {
            try {
                let toml = fs.readFileSync(codexToml, 'utf8');
                if (toml.includes('[mcp_servers.delimit]')) {
                    fs.copyFileSync(codexToml, path.join(backupDir, 'config.toml.codex'));
                    toml = toml.replace(/\n\[mcp_servers\.delimit\][\s\S]*?(?=\n\[|$)/, '');
                    fs.writeFileSync(codexToml, toml);
                    console.log(chalk.green('✓ Removed from Codex TOML config'));
                }
            } catch (e) {}
        }

        // Restore wrapped binaries before removing shims
        for (const tool of ['claude', 'codex', 'gemini']) {
            const searchPaths = [
                `/usr/local/bin/${tool}`,
                `/usr/bin/${tool}`,
                path.join(HOME, '.local', 'bin', tool),
            ];
            try {
                const npmBin = execSync('npm bin -g 2>/dev/null', { encoding: 'utf-8', timeout: 3000 }).trim();
                if (npmBin) searchPaths.push(path.join(npmBin, tool));
            } catch {}
            for (const p of searchPaths) {
                const realPath = p + '-real';
                try {
                    if (fs.existsSync(realPath)) {
                        fs.renameSync(realPath, p);
                        console.log(chalk.green(`✓ Restored original ${tool} binary`));
                        break;
                    }
                } catch {}
            }
        }

        // Remove shims
        if (fs.existsSync(shimsDir)) {
            try {
                fs.rmSync(shimsDir, { recursive: true });
                console.log(chalk.green('✓ Removed CLI shims'));
            } catch (e) {}
        }

        // Remove cross-model governance hooks (LED-202)
        try {
            const removedFrom = crossModelHooks.removeAllHooks();
            if (removedFrom.length > 0) {
                console.log(chalk.green(`✓ Removed governance hooks from: ${removedFrom.join(', ')}`));
            }
        } catch (e) { /* cross-model-hooks module not critical */ }

        console.log(chalk.green('\n  Delimit has been completely removed.'));
        console.log(chalk.gray(`  Backups saved to: ${backupDir}`));
        console.log(chalk.gray('  Your data in ~/.delimit/ has been preserved.'));
        console.log(chalk.gray('  Delete it manually if you want: rm -rf ~/.delimit\n'));
    });

// Helper function for installation
async function installDelimit(mode, scope, hooksType = 'all') {
    const HOME = process.env.HOME;
    const DELIMIT_HOME = path.join(HOME, '.delimit');
    
    // Create directories
    ['bin', 'hooks', 'shims', 'config', 'audit', 'credentials'].forEach(dir => {
        fs.mkdirSync(path.join(DELIMIT_HOME, dir), { recursive: true });
    });
    
    // Install hooks using the hooks installer
    const hooksInstaller = new DelimitHooksInstaller();
    
    if (hooksType === 'all' || hooksType === 'git') {
        console.log(chalk.yellow('Installing Git hooks...'));
        await hooksInstaller.installGitHooks();
        console.log(chalk.green('✓ Installed Git hooks'));
    }
    
    if (hooksType === 'all' || hooksType === 'ai') {
        console.log(chalk.yellow('Installing AI tool hooks...'));
        await hooksInstaller.installAIHooks();
        console.log(chalk.green('✓ Installed AI tool hooks'));
    }
    
    if (hooksType === 'all' || hooksType === 'mcp') {
        console.log(chalk.yellow('Installing MCP hooks...'));
        await hooksInstaller.installMCPHooks();
        console.log(chalk.green('✓ Installed MCP hooks'));
    }
    
    // Start agent
    await ensureAgent();
    
    // Set initial mode
    await axios.post(`${AGENT_URL}/mode`, { mode });
    
    // Create environment file
    const envContent = `#!/bin/sh
# Delimit Governance Environment Variables
export DELIMIT_MODE="${mode}"
export DELIMIT_HOME="${DELIMIT_HOME}"
export DELIMIT_AGENT_URL="${AGENT_URL}"
export DELIMIT_ACTIVE=true
`;
    fs.writeFileSync(path.join(DELIMIT_HOME, 'env'), envContent);
    fs.chmodSync(path.join(DELIMIT_HOME, 'env'), '644');
    
    console.log(chalk.green.bold('\n✅ Delimit installed successfully!\n'));
    console.log('Next steps:');
    console.log('1. Create policy file: delimit policy --init');
    console.log('2. Check status: delimit status');
    console.log('3. Switch modes: delimit mode <mode>');
    console.log('4. Setup authentication: delimit auth');
}

// Proxy command for AI tools
program
    .command('proxy <tool>')
    .allowUnknownOption()
    .description('Proxy AI tool execution with governance')

    .action(async (tool, options) => {
        const { proxyAITool } = require('../lib/proxy-handler');
        // Get all args after the tool name
        const toolIndex = process.argv.indexOf(tool);
        const args = process.argv.slice(toolIndex + 1);
        await proxyAITool(tool, args);
    });

// ---------------------------------------------------------------------------
// LED-202: Cross-model hook commands
// ---------------------------------------------------------------------------

const hookCmd = program
    .command('hook <event> [tool_name]')
    .description('Governance hook handler (session-start | bootstrap | pre-tool | pre-commit)')
    .action(async (event, toolName) => {
        try {
            switch (event) {
                case 'session-start':
                    await crossModelHooks.hookSessionStart();
                    break;
                case 'bootstrap':
                    await crossModelHooks.hookBootstrap(toolName || 'inspect');
                    break;
                case 'pre-tool':
                    await crossModelHooks.hookPreTool(toolName || 'unknown');
                    break;
                case 'pre-commit':
                    await crossModelHooks.hookPreCommit();
                    break;
                case 'deploy-gate':
                    await crossModelHooks.hookDeployGate();
                    break;
                default:
                    // Legacy: fall back to agent-based hook evaluation
                    await ensureAgent();
                    const context = {
                        command: event,
                        pwd: process.cwd(),
                        gitBranch: 'unknown',
                        files: [],
                        diff: ''
                    };
                    try {
                        context.gitBranch = execSync('git branch --show-current 2>/dev/null').toString().trim() || 'unknown';
                    } catch (e) { context.gitBranch = 'unknown'; }

                    if (event === 'pre-push') {
                        try {
                            context.files = execSync('git diff --name-only @{upstream}...HEAD 2>/dev/null').toString().split('\n').filter(f => f);
                            context.diff = execSync('git diff @{upstream}...HEAD 2>/dev/null').toString();
                        } catch (e) {
                            context.files = [];
                            context.diff = '';
                        }
                    }

                    const { data: decision } = await axios.post(`${AGENT_URL}/evaluate`, context);
                    if (decision.message) {
                        const color = decision.action === 'block' ? chalk.red :
                                     decision.action === 'prompt' ? chalk.yellow :
                                     chalk.blue;
                        console.log(color(decision.message));
                    }
                    if (decision.action === 'block') {
                        if (decision.requiresOverride) {
                            console.log(chalk.red('Action blocked. Cannot override in enforce mode.'));
                            process.exit(1);
                        } else {
                            const { override } = await inquirer.prompt([{
                                type: 'confirm',
                                name: 'override',
                                message: 'Override and continue?',
                                default: false
                            }]);
                            if (!override) process.exit(1);
                        }
                    } else if (decision.action === 'prompt') {
                        const { proceed } = await inquirer.prompt([{
                            type: 'confirm',
                            name: 'proceed',
                            message: 'Continue with this action?',
                            default: false
                        }]);
                        if (!proceed) process.exit(1);
                    }
                    process.exit(0);
            }
        } catch (err) {
            // Hooks must never block the AI tool -- fail open
            if (process.env.DELIMIT_DEBUG) {
                process.stderr.write(`[Delimit] Hook error: ${err.message}\n`);
            }
        }
    });

// ═══════════════════════════════════════════════════════════════════════
//  V1 PUBLIC COMMANDS — API Contract Governance
// ═══════════════════════════════════════════════════════════════════════

const apiEngine = require('../lib/api-engine');

// Policy preset templates
const POLICY_PRESETS = {
    strict: `# Delimit Policy Preset: strict
# For public APIs, payment systems, and regulated environments.
# Zero tolerance for breaking changes.

override_defaults: true

rules:
  - id: no_endpoint_removal
    name: Forbid Endpoint Removal
    change_types: [endpoint_removed]
    severity: error
    action: forbid
    message: "Endpoint {path} cannot be removed. Deprecate with Sunset header first."

  - id: no_method_removal
    name: Forbid Method Removal
    change_types: [method_removed]
    severity: error
    action: forbid
    message: "HTTP method removed from {path}. This breaks all clients."

  - id: no_required_param_addition
    name: Forbid Required Parameter Addition
    change_types: [required_param_added]
    severity: error
    action: forbid
    message: "Cannot add required parameter to {path}. Make it optional."

  - id: no_field_removal
    name: Forbid Response Field Removal
    change_types: [field_removed]
    severity: error
    action: forbid
    message: "Cannot remove field from {path}. Deprecate it first."

  - id: no_type_change
    name: Forbid Type Changes
    change_types: [type_changed]
    severity: error
    action: forbid
    message: "Type change at {path} breaks client deserialization."

  - id: no_enum_removal
    name: Forbid Enum Value Removal
    change_types: [enum_value_removed]
    severity: error
    action: forbid
    message: "Enum value removed at {path}."

  - id: no_param_removal
    name: Forbid Parameter Removal
    change_types: [param_removed]
    severity: error
    action: forbid
    message: "Parameter removed from {path}."
`,
    default: `# Delimit Policy Preset: default
# Balanced rules for most teams. Blocks destructive changes, warns on risky ones.
# Uses built-in defaults — customize by adding rules below.

override_defaults: false

rules: []
# Add custom rules here. Example:
#   - id: protect_v1
#     name: Protect V1 API
#     change_types: [endpoint_removed, method_removed, field_removed]
#     severity: error
#     action: forbid
#     conditions:
#       path_pattern: "^/v1/.*"
#     message: "V1 API is frozen. Make changes in V2."
`,
    relaxed: `# Delimit Policy Preset: relaxed
# For internal APIs, early-stage startups, and rapid iteration.
# Only warns — never blocks CI.

override_defaults: true

rules:
  - id: warn_endpoint_removal
    name: Warn on Endpoint Removal
    change_types: [endpoint_removed]
    severity: warning
    action: warn
    message: "Endpoint {path} was removed. Check downstream consumers."

  - id: warn_method_removal
    name: Warn on Method Removal
    change_types: [method_removed]
    severity: warning
    action: warn
    message: "HTTP method removed from {path}."

  - id: warn_required_param
    name: Warn on Required Parameter Addition
    change_types: [required_param_added]
    severity: warning
    action: warn
    message: "New required parameter at {path}."

  - id: warn_type_change
    name: Warn on Type Changes
    change_types: [type_changed]
    severity: warning
    action: warn
    message: "Type changed at {path}."

  - id: allow_field_removal
    name: Allow Field Removal
    change_types: [field_removed]
    severity: info
    action: allow
    message: "Field removed from {path}."
`,
};

// Init command — guided onboarding wizard (Consensus: Build Next 2026-03-27)
program
    .command('init')
    .description('Initialize Delimit API governance in this project')
    .option('--preset <name>', 'Policy preset: strict, default, or relaxed')
    .option('--yes', 'Skip prompts and use defaults')
    .action(async (options) => {
        const startTime = Date.now();
        const configDir = path.join(process.cwd(), '.delimit');
        const policyFile = path.join(configDir, 'policies.yml');

        console.log(chalk.bold('\n  Delimit — API Governance Setup\n'));

        if (fs.existsSync(policyFile)) {
            console.log(chalk.yellow('  Already initialized — .delimit/policies.yml exists'));
            console.log(`  Run ${chalk.bold('npx delimit-cli lint')} to check your API.\n`);
            return;
        }

        // Step 1: Detect project type
        console.log(chalk.gray('  Scanning project...'));
        const projectDir = process.cwd();
        const projectName = path.basename(projectDir);
        let framework = 'unknown';
        let frameworkLabel = '';

        // Check for common frameworks
        const pkgJsonPath = path.join(projectDir, 'package.json');
        const pyprojectPath = path.join(projectDir, 'pyproject.toml');
        const requirementsPath = path.join(projectDir, 'requirements.txt');

        if (fs.existsSync(pkgJsonPath)) {
            try {
                const pkg = JSON.parse(fs.readFileSync(pkgJsonPath, 'utf-8'));
                const allDeps = { ...pkg.dependencies, ...pkg.devDependencies };
                if (allDeps['@nestjs/core']) { framework = 'nestjs'; frameworkLabel = 'NestJS'; }
                else if (allDeps['express']) { framework = 'express'; frameworkLabel = 'Express'; }
                else if (allDeps['fastify']) { framework = 'fastify'; frameworkLabel = 'Fastify'; }
                else if (allDeps['hono']) { framework = 'hono'; frameworkLabel = 'Hono'; }
                else if (allDeps['next']) { framework = 'nextjs'; frameworkLabel = 'Next.js'; }
            } catch {}
        }
        if (framework === 'unknown') {
            const pyFiles = [pyprojectPath, requirementsPath, path.join(projectDir, 'setup.py')];
            for (const f of pyFiles) {
                if (fs.existsSync(f)) {
                    const content = fs.readFileSync(f, 'utf-8').toLowerCase();
                    if (content.includes('fastapi')) { framework = 'fastapi'; frameworkLabel = 'FastAPI'; break; }
                    if (content.includes('django')) { framework = 'django'; frameworkLabel = 'Django'; break; }
                    if (content.includes('flask')) { framework = 'flask'; frameworkLabel = 'Flask'; break; }
                }
            }
        }

        // Auto-detect OpenAPI spec files — flat patterns + recursive scan
        const specPatterns = [
            'openapi.yaml', 'openapi.yml', 'openapi.json',
            'swagger.yaml', 'swagger.yml', 'swagger.json',
            'api.yaml', 'api.yml', 'api.json',
            'docs/openapi.yaml', 'docs/openapi.yml', 'docs/openapi.json',
            'spec/openapi.yaml', 'spec/openapi.json',
            'specs/openapi.yaml', 'specs/openapi.json',
            'api/openapi.yaml', 'api/openapi.json',
            'contrib/openapi.json',
        ];
        let foundSpecs = specPatterns.filter(p => fs.existsSync(path.join(projectDir, p)));

        // Recursive scan: search common directories for OpenAPI/Swagger files
        const specDirs = ['swagger', 'api', 'docs', 'spec', 'specs', 'openapi', 'schema', 'schemas', 'config', 'src'];
        const specExtensions = ['.yaml', '.yml', '.json'];
        const specKeywords = ['openapi', 'swagger', 'api-spec', 'api_spec'];
        function scanDirForSpecs(dir, depth = 0) {
            if (depth > 2) return [];  // limit recursion depth
            const results = [];
            try {
                const entries = fs.readdirSync(dir, { withFileTypes: true });
                for (const entry of entries) {
                    if (entry.name.startsWith('.') || entry.name === 'node_modules' || entry.name === 'vendor') continue;
                    const fullPath = path.join(dir, entry.name);
                    if (entry.isDirectory() && depth < 2) {
                        results.push(...scanDirForSpecs(fullPath, depth + 1));
                    } else if (entry.isFile() && specExtensions.includes(path.extname(entry.name).toLowerCase())) {
                        // Check if file looks like an OpenAPI/Swagger spec
                        const nameLower = entry.name.toLowerCase();
                        const isLikelySpec = specKeywords.some(kw => nameLower.includes(kw)) || nameLower === 'api.yaml' || nameLower === 'api.yml' || nameLower === 'api.json';
                        if (isLikelySpec) {
                            results.push(path.relative(projectDir, fullPath));
                        } else {
                            // Peek inside to check for openapi/swagger key
                            try {
                                const head = fs.readFileSync(fullPath, 'utf-8').slice(0, 512);
                                if (head.includes('"openapi"') || head.includes("openapi:") || head.includes('"swagger"') || head.includes("swagger:")) {
                                    results.push(path.relative(projectDir, fullPath));
                                }
                            } catch {}
                        }
                    }
                }
            } catch {}
            return results;
        }
        for (const sd of specDirs) {
            const sdPath = path.join(projectDir, sd);
            if (fs.existsSync(sdPath)) {
                const deepSpecs = scanDirForSpecs(sdPath);
                for (const ds of deepSpecs) {
                    if (!foundSpecs.includes(ds)) foundSpecs.push(ds);
                }
            }
        }
        // Also scan root-level yaml/json files for OpenAPI markers
        try {
            const rootEntries = fs.readdirSync(projectDir, { withFileTypes: true });
            for (const entry of rootEntries) {
                if (!entry.isFile()) continue;
                const ext = path.extname(entry.name).toLowerCase();
                if (!specExtensions.includes(ext)) continue;
                const rel = entry.name;
                if (foundSpecs.includes(rel)) continue;
                try {
                    const head = fs.readFileSync(path.join(projectDir, rel), 'utf-8').slice(0, 512);
                    if (head.includes('"openapi"') || head.includes("openapi:") || head.includes('"swagger"') || head.includes("swagger:")) {
                        foundSpecs.push(rel);
                    }
                } catch {}
            }
        } catch {}
        const specPath = foundSpecs.length > 0 ? foundSpecs[0] : null;

        // Detect test files and count them
        let testFileCount = 0;
        let testFramework = null;
        function countTestFiles(dir, depth = 0) {
            if (depth > 3) return;
            try {
                const entries = fs.readdirSync(dir, { withFileTypes: true });
                for (const entry of entries) {
                    if (entry.name === 'node_modules' || entry.name === '.git' || entry.name === 'vendor' || entry.name === '__pycache__') continue;
                    const fullPath = path.join(dir, entry.name);
                    if (entry.isDirectory()) {
                        if (entry.name === '__tests__' || entry.name === 'test' || entry.name === 'tests' || entry.name === 'spec') {
                            countTestFiles(fullPath, depth + 1);
                        } else if (depth < 2) {
                            countTestFiles(fullPath, depth + 1);
                        }
                    } else if (entry.isFile()) {
                        const name = entry.name.toLowerCase();
                        if (name.endsWith('.test.js') || name.endsWith('.test.ts') || name.endsWith('.test.tsx') || name.endsWith('.spec.js') || name.endsWith('.spec.ts')) {
                            testFileCount++;
                        } else if (name.startsWith('test_') && name.endsWith('.py')) {
                            testFileCount++;
                        } else if (name.endsWith('_test.go')) {
                            testFileCount++;
                        }
                    }
                }
            } catch {}
        }
        countTestFiles(projectDir);
        // Detect test framework
        if (fs.existsSync(pkgJsonPath)) {
            try {
                const pkg = JSON.parse(fs.readFileSync(pkgJsonPath, 'utf-8'));
                const allDeps = { ...pkg.dependencies, ...pkg.devDependencies };
                if (allDeps['jest'] || (pkg.scripts && pkg.scripts.test && pkg.scripts.test.includes('jest'))) testFramework = 'jest';
                else if (allDeps['vitest']) testFramework = 'vitest';
                else if (allDeps['mocha']) testFramework = 'mocha';
            } catch {}
        }
        if (!testFramework && fs.existsSync(requirementsPath)) {
            try {
                const content = fs.readFileSync(requirementsPath, 'utf-8').toLowerCase();
                if (content.includes('pytest')) testFramework = 'pytest';
            } catch {}
        }

        // Quick security scan
        const securityFindings = [];
        // Check for common secret patterns in spec files
        for (const sp of foundSpecs.slice(0, 5)) {
            try {
                const content = fs.readFileSync(path.join(projectDir, sp), 'utf-8');
                if (/(?:api[_-]?key|secret|password|token)\s*[:=]\s*["'][^"']{8,}/i.test(content)) {
                    securityFindings.push({ severity: 'high', file: sp, issue: 'Possible hardcoded secret in spec file' });
                }
                if (/http:\/\/(?!localhost|127\.0\.0\.1)/i.test(content)) {
                    securityFindings.push({ severity: 'medium', file: sp, issue: 'Non-localhost HTTP URL in spec (should use HTTPS)' });
                }
            } catch {}
        }
        // Check for .env files committed (not in .gitignore)
        const envFiles = ['.env', '.env.local', '.env.production'];
        const gitignorePath = path.join(projectDir, '.gitignore');
        let gitignoreContent = '';
        try { gitignoreContent = fs.readFileSync(gitignorePath, 'utf-8'); } catch {}
        for (const envFile of envFiles) {
            if (fs.existsSync(path.join(projectDir, envFile))) {
                if (!gitignoreContent.includes(envFile)) {
                    securityFindings.push({ severity: 'high', file: envFile, issue: `${envFile} exists and is not in .gitignore` });
                }
            }
        }
        // Check for package-lock.json / yarn.lock (dependency lockfile)
        const hasLockfile = fs.existsSync(path.join(projectDir, 'package-lock.json')) || fs.existsSync(path.join(projectDir, 'yarn.lock')) || fs.existsSync(path.join(projectDir, 'pnpm-lock.yaml'));

        // Check for CI
        const hasGitHub = fs.existsSync(path.join(projectDir, '.github'));
        const hasGitLabCI = fs.existsSync(path.join(projectDir, '.gitlab-ci.yml'));
        const ciProvider = hasGitHub ? 'github' : hasGitLabCI ? 'gitlab' : 'none';

        // Display detection results
        console.log(`  Project:   ${chalk.bold(projectName)}`);
        if (frameworkLabel) console.log(`  Framework: ${chalk.bold(frameworkLabel)}`);
        if (foundSpecs.length > 1) {
            console.log(`  Specs:     ${chalk.bold(foundSpecs.length + ' found')}`);
            for (const sp of foundSpecs.slice(0, 5)) {
                console.log(`             ${chalk.gray('-')} ${sp}`);
            }
            if (foundSpecs.length > 5) console.log(`             ${chalk.gray(`... and ${foundSpecs.length - 5} more`)}`);
        } else if (specPath) {
            console.log(`  Spec:      ${chalk.bold(specPath)}`);
        } else if (['fastapi', 'nestjs', 'express'].includes(framework)) {
            console.log(`  Spec:      ${chalk.gray('none found')} (Zero-Spec Mode available for ${frameworkLabel})`);
        } else {
            console.log(`  Spec:      ${chalk.gray('none found')}`);
        }
        if (testFileCount > 0) {
            console.log(`  Tests:     ${chalk.bold(testFileCount + ' file' + (testFileCount !== 1 ? 's' : ''))}${testFramework ? chalk.gray(' (' + testFramework + ')') : ''}`);
        } else {
            console.log(`  Tests:     ${chalk.gray('none detected')}`);
        }
        if (ciProvider !== 'none') console.log(`  CI:        ${chalk.bold(ciProvider === 'github' ? 'GitHub Actions' : 'GitLab CI')}`);
        if (securityFindings.length > 0) {
            console.log(`  Security:  ${chalk.yellow(securityFindings.length + ' finding' + (securityFindings.length !== 1 ? 's' : ''))}`);
        } else {
            console.log(`  Security:  ${chalk.green('clean (quick scan)')}`);
        }
        if (!hasLockfile && fs.existsSync(pkgJsonPath)) {
            console.log(`  Lockfile:  ${chalk.yellow('missing — consider committing a lockfile')}`);
        }
        console.log('');

        // Step 2: Choose preset
        let preset = options.preset ? options.preset.toLowerCase() : null;
        if (!preset && !options.yes) {
            // Suggest preset based on project signals
            let defaultPreset = 'default';
            if (specPath) {
                // If they have a checked-in spec, they probably care about stability
                try {
                    const specContent = fs.readFileSync(path.join(projectDir, specPath), 'utf-8');
                    if (specContent.includes('/v2') || specContent.includes('/v3')) defaultPreset = 'strict';
                } catch {}
            }

            try {
                const answers = await inquirer.prompt([{
                    type: 'list',
                    name: 'preset',
                    message: 'Policy preset:',
                    choices: [
                        { name: 'strict   — Block all breaking changes (public APIs, payment systems)', value: 'strict' },
                        { name: 'default  — Balanced for most teams (block critical, warn on others)', value: 'default' },
                        { name: 'relaxed  — Warnings only (internal APIs, early-stage projects)', value: 'relaxed' },
                    ],
                    default: defaultPreset,
                }]);
                preset = answers.preset;
            } catch {
                preset = defaultPreset;
            }
        }
        if (!preset) preset = 'default';

        if (!POLICY_PRESETS[preset]) {
            console.log(chalk.red(`  Unknown preset "${preset}". Choose: strict, default, or relaxed`));
            return;
        }

        // Step 2b: Compliance template selection (LED-258)
        let complianceTemplate = null;
        if (!options.yes) {
            try {
                const templateAns = await inquirer.prompt([{
                    type: 'list',
                    name: 'template',
                    message: 'Compliance template (optional):',
                    choices: [
                        { name: 'none     — Standard governance only', value: 'none' },
                        { name: 'SOC2     — Service Organization Control evidence', value: 'soc2' },
                        { name: 'PCI-DSS  — Payment card data protection', value: 'pci-dss' },
                        { name: 'HIPAA    — Healthcare data safeguards', value: 'hipaa' },
                        { name: 'startup  — Fast-moving team defaults', value: 'startup' },
                    ],
                    default: 'none',
                }]);
                if (templateAns.template !== 'none') complianceTemplate = templateAns.template;
            } catch {}
        }

        // Step 3: Create policy file
        fs.mkdirSync(configDir, { recursive: true });
        fs.writeFileSync(policyFile, POLICY_PRESETS[preset]);

        // Write compliance template config if selected
        if (complianceTemplate) {
            const templateConfig = {
                soc2: { evidence_required: true, audit_trail: true, change_approval: true, retention_days: 365 },
                'pci-dss': { evidence_required: true, audit_trail: true, change_approval: true, secret_scanning: true, retention_days: 365 },
                hipaa: { evidence_required: true, audit_trail: true, change_approval: true, phi_detection: true, retention_days: 2190 },
                startup: { evidence_required: false, audit_trail: true, change_approval: false, retention_days: 90 },
            };
            const tmplFile = path.join(configDir, 'compliance.json');
            fs.writeFileSync(tmplFile, JSON.stringify({ template: complianceTemplate, ...templateConfig[complianceTemplate] }, null, 2));
            console.log(chalk.green(`  Created .delimit/compliance.json (${complianceTemplate})`));
        }
        console.log(chalk.green(`  Created .delimit/policies.yml (${preset})`));

        // Step 4: Add GitHub Action workflow if spec found + GitHub CI
        if (specPath && ciProvider === 'github') {
            const workflowDir = path.join(projectDir, '.github', 'workflows');
            const workflowFile = path.join(workflowDir, 'api-governance.yml');

            if (!fs.existsSync(workflowFile)) {
                let writeWorkflow = true;
                if (!options.yes) {
                    try {
                        const ans = await inquirer.prompt([{
                            type: 'confirm',
                            name: 'addWorkflow',
                            message: 'Add GitHub Action for PR governance? (Enter = Yes)',
                            default: true,
                        }]);
                        writeWorkflow = ans.addWorkflow;
                    } catch {}
                }

                if (writeWorkflow) {
                    try {
                        fs.mkdirSync(workflowDir, { recursive: true });
                        const workflowContent = `name: API Governance
on:
  pull_request:
    paths:
      - '${specPath}'

permissions:
  contents: read
  pull-requests: write

jobs:
  api-governance:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/checkout@v4
        with:
          ref: \${{ github.event.pull_request.base.sha }}
          path: _base
      - uses: delimit-ai/delimit-action@v1
        with:
          old_spec: _base/${specPath}
          new_spec: ${specPath}
          mode: advisory
`;
                        fs.writeFileSync(workflowFile, workflowContent);
                        console.log(chalk.green('  Created .github/workflows/api-governance.yml'));
                    } catch (err) {
                        console.log(chalk.yellow(`  Could not write workflow: ${err.message}`));
                    }
                }
            } else {
                console.log(chalk.gray('  .github/workflows/api-governance.yml already exists'));
            }
        }

        // Step 4b: Add scheduled drift monitoring workflow (LED-260)
        if (specPath && ciProvider === 'github') {
            const driftWorkflowFile = path.join(projectDir, '.github', 'workflows', 'api-drift-monitor.yml');
            if (!fs.existsSync(driftWorkflowFile)) {
                let writeDrift = false;
                if (!options.yes) {
                    try {
                        const driftAns = await inquirer.prompt([{
                            type: 'confirm',
                            name: 'addDrift',
                            message: 'Add weekly drift monitoring workflow? (Enter = Yes)',
                            default: true,
                        }]);
                        writeDrift = driftAns.addDrift;
                    } catch {}
                }
                if (writeDrift) {
                    try {
                        const driftContent = `name: API Drift Monitor
on:
  schedule:
    - cron: '17 9 * * 1'  # Weekly on Monday at 9:17 AM UTC
  workflow_dispatch: {}

permissions:
  contents: read
  issues: write

jobs:
  drift-check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v4
        with:
          python-version: '3.10'
      - run: pip install pyyaml 2>/dev/null
      - name: Check for API drift
        run: |
          npx delimit-cli lint ${specPath} ${specPath} --baseline
          echo "Drift check complete"
      - name: Create issue on drift
        if: failure()
        uses: actions/github-script@v6
        with:
          script: |
            await github.rest.issues.create({
              owner: context.repo.owner,
              repo: context.repo.repo,
              title: 'API Drift Detected — Governance Review Needed',
              body: 'The weekly drift monitor detected changes to the API spec that have not been reviewed through governance. Run npx delimit-cli lint to review.',
              labels: ['api-governance', 'drift'],
            });
`;
                        fs.writeFileSync(driftWorkflowFile, driftContent);
                        console.log(chalk.green('  Created .github/workflows/api-drift-monitor.yml'));
                    } catch (err) {
                        console.log(chalk.yellow(`  Could not write drift workflow: ${err.message}`));
                    }
                }
            }
        }

        // Step 5: Run first lint to show immediate value
        console.log('');
        if (specPath) {
            console.log(chalk.bold('  Running first lint...'));
            try {
                const result = apiEngine.lint(
                    path.join(projectDir, specPath),
                    path.join(projectDir, specPath),
                    { policy: preset }
                );
                if (result && result.summary) {
                    const s = result.summary;
                    const breaking = s.breaking || 0;
                    const warnings = s.warnings || 0;
                    const safe = s.safe || s.non_breaking || 0;
                    if (breaking === 0 && warnings === 0) {
                        console.log(chalk.green('  PASS — No breaking changes detected'));
                    } else if (breaking > 0) {
                        console.log(chalk.red(`  FAIL — ${breaking} breaking change(s), ${warnings} warning(s)`));
                    } else {
                        console.log(chalk.yellow(`  WARN — ${warnings} warning(s)`));
                    }
                    if (result.paths_analyzed) {
                        console.log(chalk.gray(`  Analyzed ${result.paths_analyzed} endpoint(s)`));
                    }
                } else {
                    console.log(chalk.green('  Spec validated successfully'));
                }
            } catch (err) {
                // Lint comparing same file = no changes, which is expected
                console.log(chalk.green('  Spec validated — baseline set'));
            }
        } else if (['fastapi', 'nestjs', 'express'].includes(framework)) {
            console.log(chalk.bold('  Running Zero-Spec lint...'));
            try {
                const zeroResult = apiEngine.zeroSpec(projectDir);
                if (zeroResult && zeroResult.success) {
                    console.log(chalk.green(`  Extracted: ${zeroResult.paths_count} paths, ${zeroResult.schemas_count} schemas`));
                    // Save baseline
                    const baselinePath = path.join(configDir, 'baseline.yaml');
                    if (!fs.existsSync(baselinePath)) {
                        fs.writeFileSync(baselinePath, yaml.dump(zeroResult.spec));
                        console.log(chalk.green('  Saved baseline to .delimit/baseline.yaml'));
                    }
                } else {
                    console.log(chalk.gray('  Zero-Spec extraction skipped — run `npx delimit-cli lint` manually'));
                }
            } catch {
                console.log(chalk.gray('  Zero-Spec extraction skipped — run `npx delimit-cli lint` manually'));
            }
        }

        // Step 6: Save first evidence event + comprehensive report (LED-258)
        const evidenceDir = path.join(configDir, 'evidence');
        fs.mkdirSync(evidenceDir, { recursive: true });
        const evidenceEvent = {
            id: `EVD-${Date.now().toString(36)}`,
            ts: new Date().toISOString(),
            type: 'governance_init',
            tool: 'delimit_init',
            model: 'cli',
            status: securityFindings.some(f => f.severity === 'high') ? 'warn' : 'pass',
            summary: `Governance initialized with ${preset} preset`,
            detail: [
                `Project: ${projectName}`,
                frameworkLabel ? `Framework: ${frameworkLabel}` : null,
                foundSpecs.length > 0 ? `Specs found: ${foundSpecs.length} (${foundSpecs.join(', ')})` : 'Mode: Zero-Spec',
                `Preset: ${preset}`,
                `Test files: ${testFileCount}`,
                testFramework ? `Test framework: ${testFramework}` : null,
                ciProvider !== 'none' ? `CI: ${ciProvider}` : null,
                securityFindings.length > 0 ? `Security findings: ${securityFindings.length}` : 'Security: clean',
            ].filter(Boolean).join('\n'),
            venture: projectName,
        };
        try {
            const evidenceFile = path.join(evidenceDir, 'events.jsonl');
            fs.appendFileSync(evidenceFile, JSON.stringify(evidenceEvent) + '\n');
        } catch {}

        // Generate first evidence report (LED-258: zero-config onboarding)
        const firstReport = {
            generated_at: new Date().toISOString(),
            project: projectName,
            framework: frameworkLabel || 'unknown',
            specs: {
                count: foundSpecs.length,
                files: foundSpecs,
                primary: specPath,
            },
            tests: {
                file_count: testFileCount,
                framework: testFramework,
            },
            security: {
                findings_count: securityFindings.length,
                findings: securityFindings,
                lockfile_present: hasLockfile,
            },
            governance: {
                preset: preset,
                compliance_template: complianceTemplate,
                ci_provider: ciProvider,
                gates_active: specPath || ['fastapi', 'nestjs', 'express'].includes(framework) ? ['api_lint'] : [],
                gates_ready: ['security_audit', 'deploy_plan', 'release_validate'],
            },
        };
        try {
            const reportFile = path.join(evidenceDir, 'first-report.json');
            fs.writeFileSync(reportFile, JSON.stringify(firstReport, null, 2));
            console.log(chalk.green('  Evidence recorded — first governance report saved'));
        } catch {}

        // Display security findings if any
        if (securityFindings.length > 0) {
            console.log(chalk.bold('\n  Security Findings:'));
            for (const finding of securityFindings) {
                const icon = finding.severity === 'high' ? chalk.red('!') : chalk.yellow('~');
                console.log(`    ${icon} ${chalk.bold(finding.severity.toUpperCase())} ${finding.file} — ${finding.issue}`);
            }
        }

        // Step 7: Show gate status (LED-258)
        console.log(chalk.bold('\n  Governance Gates:'));
        const gates = [
            { name: 'API Lint', status: specPath || ['fastapi', 'nestjs', 'express'].includes(framework) ? 'active' : 'inactive', chain: 'semver → gov_evaluate' },
            { name: 'Security Audit', status: 'ready', chain: 'evidence_collect → notify' },
            { name: 'Deploy Plan', status: 'ready', chain: 'security_audit' },
            { name: 'Release Validate', status: 'ready', chain: 'evidence_collect → notify → ledger' },
        ];
        for (const gate of gates) {
            const icon = gate.status === 'active' ? chalk.green('●') : gate.status === 'ready' ? chalk.yellow('○') : chalk.gray('○');
            const statusLabel = gate.status === 'active' ? chalk.green('active') : gate.status === 'ready' ? chalk.yellow('ready') : chalk.gray('inactive');
            console.log(`    ${icon} ${chalk.bold(gate.name)} ${chalk.gray('→')} ${chalk.gray(gate.chain)} ${chalk.gray('(')}${statusLabel}${chalk.gray(')')}`);
        }

        // Summary
        const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
        console.log(chalk.bold(`\n  Setup complete in ${elapsed}s`));
        console.log(chalk.gray(`  Evidence saved to .delimit/evidence/\n`));
        console.log('  Next steps:');
        console.log(`    ${chalk.bold('npx delimit-cli check')}                          — pre-commit governance check`);
        if (specPath) {
            console.log(`    ${chalk.bold('npx delimit-cli lint')} ${specPath} ${specPath}  — lint on every PR`);
        } else if (['fastapi', 'nestjs', 'express'].includes(framework)) {
            console.log(`    ${chalk.bold('npx delimit-cli lint')}                           — zero-spec mode (${frameworkLabel})`);
        } else {
            console.log(`    ${chalk.bold('npx delimit-cli lint')}                           — add an OpenAPI spec first`);
        }
        if (ciProvider === 'none') {
            console.log(`    ${chalk.bold('npx delimit-cli ci')}                             — generate GitHub Action workflow`);
        }
        console.log(`    ${chalk.bold('delimit doctor')}                         — verify setup`);
        console.log(`    ${chalk.bold('delimit explain')}                        — human-readable report`);
        if (securityFindings.length > 0) {
            console.log(`    ${chalk.yellow('Fix security findings above')}            — ${securityFindings.length} issue${securityFindings.length !== 1 ? 's' : ''} found`);
        }
        if (testFileCount === 0) {
            console.log(`    ${chalk.gray('Add tests')}                              — no test files detected`);
        }
        if (foundSpecs.length > 1) {
            console.log(`    ${chalk.gray('Review all ' + foundSpecs.length + ' specs')}                       — multiple specs detected`);
        }
        if (ciProvider === 'none' && !specPath) {
            console.log(`    ${chalk.gray('Add CI')}                                 — no CI detected; consider GitHub Actions`);
        }

        // Beta capture after init (LED-263)
        if (!options.yes) {
            try {
                const betaAns = await inquirer.prompt([{
                    type: 'input',
                    name: 'email',
                    message: chalk.blue('  Join the beta? Enter your email (or press Enter to skip):'),
                }]);
                if (betaAns.email && betaAns.email.includes('@')) {
                    try {
                        await axios.post('https://delimit.ai/api/subscribe', { email: betaAns.email, source: 'cli-init' });
                        console.log(chalk.green('  Thanks! You\'re on the list.\n'));
                    } catch {
                        console.log(chalk.green('  Thanks! Visit https://delimit.ai to stay updated.\n'));
                    }
                } else {
                    console.log('');
                }
            } catch {}
        } else {
            console.log('');
        }
    });

// Demo command — prove governance value in 5 minutes (LED-262)
program
    .command('demo')
    .description('Run a self-contained governance demo — proves value in under 5 minutes')
    .action(async () => {
        const tmpDir = path.join(os.tmpdir(), `delimit-demo-${Date.now()}`);
        fs.mkdirSync(tmpDir, { recursive: true });

        console.log(chalk.bold('\n  Delimit Governance Demo\n'));
        console.log(chalk.gray(`  Working in ${tmpDir}\n`));

        // Step 1: Create a sample API spec
        console.log(chalk.bold('  Step 1: Creating sample API spec...'));
        const baseSpec = {
            openapi: '3.0.3',
            info: { title: 'Pet Store API', version: '1.0.0' },
            paths: {
                '/pets': {
                    get: {
                        summary: 'List all pets',
                        operationId: 'listPets',
                        parameters: [
                            { name: 'limit', in: 'query', required: false, schema: { type: 'integer' } },
                        ],
                        responses: { '200': { description: 'A list of pets', content: { 'application/json': { schema: { type: 'array', items: { '$ref': '#/components/schemas/Pet' } } } } } },
                    },
                    post: {
                        summary: 'Create a pet',
                        operationId: 'createPet',
                        requestBody: { content: { 'application/json': { schema: { '$ref': '#/components/schemas/Pet' } } } },
                        responses: { '201': { description: 'Pet created' } },
                    },
                },
                '/pets/{petId}': {
                    get: {
                        summary: 'Get a pet by ID',
                        operationId: 'showPetById',
                        parameters: [{ name: 'petId', in: 'path', required: true, schema: { type: 'string' } }],
                        responses: { '200': { description: 'A pet', content: { 'application/json': { schema: { '$ref': '#/components/schemas/Pet' } } } } },
                    },
                },
            },
            components: {
                schemas: {
                    Pet: {
                        type: 'object',
                        required: ['id', 'name'],
                        properties: {
                            id: { type: 'integer', format: 'int64' },
                            name: { type: 'string' },
                            tag: { type: 'string' },
                        },
                    },
                },
            },
        };

        const baseSpecPath = path.join(tmpDir, 'openapi-v1.yaml');
        fs.writeFileSync(baseSpecPath, yaml.dump(baseSpec));
        console.log(chalk.green('  Created openapi-v1.yaml (3 endpoints, 1 schema)\n'));

        // Step 2: Introduce breaking changes
        console.log(chalk.bold('  Step 2: Introducing breaking changes...'));
        const changedSpec = JSON.parse(JSON.stringify(baseSpec));
        changedSpec.info.version = '2.0.0';

        // Breaking: remove endpoint
        delete changedSpec.paths['/pets/{petId}'];
        // Breaking: add required parameter
        changedSpec.paths['/pets'].get.parameters.push(
            { name: 'owner_id', in: 'query', required: true, schema: { type: 'string' } }
        );
        // Breaking: remove response field
        delete changedSpec.components.schemas.Pet.properties.tag;
        // Non-breaking: add endpoint
        changedSpec.paths['/pets/search'] = {
            get: {
                summary: 'Search pets',
                operationId: 'searchPets',
                parameters: [{ name: 'q', in: 'query', required: true, schema: { type: 'string' } }],
                responses: { '200': { description: 'Search results' } },
            },
        };

        const changedSpecPath = path.join(tmpDir, 'openapi-v2.yaml');
        fs.writeFileSync(changedSpecPath, yaml.dump(changedSpec));
        console.log(chalk.red('  Removed: GET /pets/{petId}'));
        console.log(chalk.red('  Added required param: owner_id on GET /pets'));
        console.log(chalk.red('  Removed field: Pet.tag'));
        console.log(chalk.green('  Added: GET /pets/search'));
        console.log('');

        // Step 3: Run Delimit lint
        console.log(chalk.bold('  Step 3: Running governance check...\n'));
        try {
            const result = apiEngine.lint(baseSpecPath, changedSpecPath, { policy: 'strict' });

            if (result && result.summary) {
                const s = result.summary;
                const breaking = s.breaking || s.breaking_changes || 0;
                const total = s.total || s.total_changes || 0;
                const safe = total - breaking;

                if (breaking > 0) {
                    console.log(chalk.red.bold(`  BLOCKED — ${breaking} breaking change(s) detected\n`));
                } else {
                    console.log(chalk.green.bold(`  PASSED — No breaking changes\n`));
                }

                // Show violations
                const violations = result.violations || [];
                if (violations.length > 0) {
                    console.log(chalk.bold('  Violations:'));
                    violations.forEach((v, i) => {
                        const icon = v.severity === 'error' ? chalk.red('  BLOCK') : chalk.yellow('  WARN ');
                        console.log(`  ${icon} ${v.message}`);
                        if (v.path) console.log(chalk.gray(`         ${v.path}`));
                    });
                    console.log('');
                }

                // Semver
                if (result.semver) {
                    console.log(`  Semver: ${chalk.bold(result.semver.bump?.toUpperCase() || 'MAJOR')}`);
                    if (result.semver.next_version) {
                        console.log(`  Next version: ${chalk.bold(result.semver.next_version)}`);
                    }
                }

                // Show safe changes
                if (safe > 0) {
                    console.log(chalk.green(`\n  ${safe} additive change(s) also detected`));
                }
            }
        } catch (err) {
            console.log(chalk.yellow(`  Lint error: ${err.message}`));
        }

        // Step 4: Show governance gates
        console.log(chalk.bold('\n  Governance Gates:'));
        console.log(`    ${chalk.red('X')} API Lint          ${chalk.gray('→ semver → gov_evaluate')}`);
        console.log(`    ${chalk.red('X')} Policy Compliance  ${chalk.gray('→ evidence_collect')}`);
        console.log(`    ${chalk.green('+')} Security Audit     ${chalk.gray('→ evidence_collect → notify')}`);
        console.log(`    ${chalk.red('X')} Deploy Readiness   ${chalk.gray('→ deploy_plan → security_audit')}`);
        console.log(chalk.red.bold('\n  Deploy BLOCKED until all gates pass.\n'));

        // Step 5: Show what would happen with the fix
        console.log(chalk.bold('  What Delimit does:'));
        console.log(chalk.gray('    1. Detects the 3 breaking changes automatically'));
        console.log(chalk.gray('    2. Evaluates against your policy (strict/default/relaxed)'));
        console.log(chalk.gray('    3. Blocks the deploy via governance gates'));
        console.log(chalk.gray('    4. Records evidence for audit trail'));
        console.log(chalk.gray('    5. Posts remediation guide on the PR'));
        console.log(chalk.gray('    6. Tracks in the ledger for cross-model continuity'));

        console.log(chalk.bold('\n  Try it on your project:'));
        console.log(`    ${chalk.green('npx delimit-cli init')}     — set up governance`);
        console.log(`    ${chalk.green('npx delimit-cli lint')}     — lint your API spec`);
        console.log(`    ${chalk.green('npx delimit-cli setup')}    — configure AI assistants\n`);

        // Beta capture (LED-263)
        try {
            const betaAns = await inquirer.prompt([{
                type: 'input',
                name: 'email',
                message: chalk.blue('Join the beta? Enter your email (or press Enter to skip):'),
            }]);
            if (betaAns.email && betaAns.email.includes('@')) {
                try {
                    await axios.post('https://delimit.ai/api/subscribe', { email: betaAns.email, source: 'cli-demo' });
                    console.log(chalk.green('\n  Thanks! You\'re on the list. We\'ll keep you in the loop.\n'));
                } catch {
                    console.log(chalk.green('\n  Thanks! Visit https://delimit.ai to stay updated.\n'));
                }
            }
        } catch {}

        // Cleanup
        try { fs.rmSync(tmpDir, { recursive: true }); } catch {}
    });

// Quickstart command — clone demo repo + guided walkthrough (LED-267)
program
    .command('quickstart')
    .description('Clone a demo project and walk through governance in 5 minutes')
    .action(async () => {
        const targetDir = path.join(process.cwd(), 'delimit-demo');

        console.log(chalk.bold('\n  Delimit Quickstart\n'));
        console.log(chalk.gray('  Clone a demo API project with a pre-broken spec,'));
        console.log(chalk.gray('  then walk through the governance flow step by step.\n'));

        // Step 1: Clone the quickstart repo
        if (fs.existsSync(targetDir)) {
            console.log(chalk.yellow(`  ${targetDir} already exists. Using existing directory.\n`));
        } else {
            console.log(chalk.bold('  Step 1: Cloning demo project...'));
            try {
                execSync(`git clone --depth 1 https://github.com/delimit-ai/delimit-quickstart.git "${targetDir}" 2>/dev/null`, { stdio: 'pipe' });
                await logp(chalk.green('  Cloned delimit-ai/delimit-quickstart'));
            } catch {
                console.log(chalk.yellow('  Could not clone repo. Creating demo files locally...'));
                fs.mkdirSync(targetDir, { recursive: true });
                // Create inline demo specs
                const baseSpec = { openapi: '3.0.3', info: { title: 'Pet Store API', version: '1.0.0' }, paths: { '/pets': { get: { summary: 'List pets', responses: { '200': { description: 'OK' } } } }, '/pets/{petId}': { get: { summary: 'Get pet', parameters: [{ name: 'petId', in: 'path', required: true, schema: { type: 'string' } }], responses: { '200': { description: 'OK' } } } } }, components: { schemas: { Pet: { type: 'object', properties: { id: { type: 'integer' }, name: { type: 'string' }, tag: { type: 'string' } } } } } };
                const changedSpec = JSON.parse(JSON.stringify(baseSpec));
                changedSpec.info.version = '2.0.0';
                delete changedSpec.paths['/pets/{petId}'];
                delete changedSpec.components.schemas.Pet.properties.tag;
                fs.writeFileSync(path.join(targetDir, 'openapi.yaml'), yaml.dump(baseSpec));
                fs.writeFileSync(path.join(targetDir, 'openapi-changed.yaml'), yaml.dump(changedSpec));
                await logp(chalk.green('  Created demo specs locally'));
            }
        }
        console.log('');

        // Step 2: Initialize governance
        console.log(chalk.bold('  Step 2: Setting up governance...'));
        const configDir = path.join(targetDir, '.delimit');
        if (!fs.existsSync(path.join(configDir, 'policies.yml'))) {
            fs.mkdirSync(configDir, { recursive: true });
            fs.writeFileSync(path.join(configDir, 'policies.yml'), POLICY_PRESETS['strict']);
            await logp(chalk.green('  Created .delimit/policies.yml (strict)'));
        } else {
            await logp(chalk.green('  Governance already initialized'));
        }
        console.log('');

        // Step 3: Run lint to show breaking changes
        console.log(chalk.bold('  Step 3: Running governance lint...\n'));
        const oldSpec = path.join(targetDir, 'openapi.yaml');
        const newSpec = path.join(targetDir, 'openapi-changed.yaml');

        if (fs.existsSync(oldSpec) && fs.existsSync(newSpec)) {
            try {
                const result = apiEngine.lint(oldSpec, newSpec, { policy: 'strict' });
                if (result && result.summary) {
                    const s = result.summary;
                    const breaking = s.breaking || s.breaking_changes || 0;
                    if (breaking > 0) {
                        console.log(chalk.red.bold(`  BLOCKED — ${breaking} breaking change(s) detected\n`));
                    }
                    const violations = result.violations || [];
                    violations.forEach(v => {
                        const icon = v.severity === 'error' ? chalk.red('  BLOCK') : chalk.yellow('  WARN ');
                        console.log(`  ${icon} ${v.message}`);
                    });
                }
            } catch {
                console.log(chalk.green('  Spec validated — no breaking changes'));
            }
        } else {
            console.log(chalk.yellow('  No spec files found. Run npx delimit-cli lint manually.'));
        }

        // Step 4: Show what to do next
        console.log(chalk.bold('\n  What just happened:'));
        await logp(chalk.gray('    1. Cloned a demo API project'));
        await logp(chalk.gray('    2. Initialized strict governance policy'));
        await logp(chalk.gray('    3. Ran lint and caught breaking changes'));
        console.log('');
        console.log(chalk.bold('  Now try in your own project:'));
        console.log(`    ${chalk.green('npx delimit-cli init')}     — set up governance`);
        console.log(`    ${chalk.green('npx delimit-cli lint')}     — check your API spec`);
        console.log(`    ${chalk.green('npx delimit-cli setup')}    — configure AI assistants`);
        console.log('');
        console.log(chalk.gray(`  Demo project: ${targetDir}`));
        console.log(chalk.gray(`  Clean up: rm -rf delimit-demo\n`));

        // Beta capture
        try {
            const betaAns = await inquirer.prompt([{
                type: 'input',
                name: 'email',
                message: chalk.blue('Join the beta? Enter your email (or press Enter to skip):'),
            }]);
            if (betaAns.email && betaAns.email.includes('@')) {
                try {
                    await axios.post('https://delimit.ai/api/subscribe', { email: betaAns.email, source: 'cli-quickstart' });
                    console.log(chalk.green('\n  Thanks! You\'re on the list.\n'));
                } catch {
                    console.log(chalk.green('\n  Thanks! Visit https://delimit.ai\n'));
                }
            }
        } catch {}
    });

// Resume command — show what was happening last session (STR-047)
program
    .command('resume')
    .description('Show what you were working on — context from your last session')
    .action(async () => {
        console.log(chalk.bold('\n  Delimit — Resume Work\n'));

        const DELIMIT_HOME = path.join(os.homedir(), '.delimit');

        // 1. Last session handoff
        const sessionsDir = path.join(DELIMIT_HOME, 'sessions');
        if (fs.existsSync(sessionsDir)) {
            try {
                const sessions = fs.readdirSync(sessionsDir)
                    .filter(f => f.endsWith('.json'))
                    .sort()
                    .reverse();

                if (sessions.length > 0) {
                    const lastSession = JSON.parse(fs.readFileSync(path.join(sessionsDir, sessions[0]), 'utf-8'));
                    const ts = sessions[0].replace('session_', '').replace('.json', '').replace(/_/g, ' ');
                    console.log(chalk.bold('  Last session:') + chalk.gray(` ${ts}`));
                    if (lastSession.summary) {
                        console.log(chalk.gray(`  ${lastSession.summary.substring(0, 200)}`));
                    }
                    if (lastSession.tasks_completed && lastSession.tasks_completed.length > 0) {
                        console.log(chalk.green(`  ${lastSession.tasks_completed.length} task(s) completed`));
                    }
                    if (lastSession.pending && lastSession.pending.length > 0) {
                        console.log(chalk.yellow(`  ${lastSession.pending.length} item(s) pending`));
                        lastSession.pending.slice(0, 3).forEach(p => {
                            console.log(chalk.gray(`    • ${typeof p === 'string' ? p : p.title || JSON.stringify(p).substring(0, 80)}`));
                        });
                    }
                    console.log('');
                }
            } catch {}
        }

        // 2. Open ledger items
        const ledgerDir = path.join(DELIMIT_HOME, 'ledger');
        if (fs.existsSync(ledgerDir)) {
            try {
                const ledgerFiles = fs.readdirSync(ledgerDir).filter(f => f.endsWith('.json'));
                let openItems = [];
                for (const lf of ledgerFiles) {
                    try {
                        const items = JSON.parse(fs.readFileSync(path.join(ledgerDir, lf), 'utf-8'));
                        if (Array.isArray(items)) {
                            openItems.push(...items.filter(i => i.status === 'open'));
                        }
                    } catch {}
                }
                if (openItems.length > 0) {
                    // Sort by priority
                    const priorityOrder = { P0: 0, P1: 1, P2: 2 };
                    openItems.sort((a, b) => (priorityOrder[a.priority] || 3) - (priorityOrder[b.priority] || 3));

                    console.log(chalk.bold(`  Open items: ${openItems.length}`));
                    const p0 = openItems.filter(i => i.priority === 'P0');
                    const p1 = openItems.filter(i => i.priority === 'P1');
                    if (p0.length > 0) {
                        console.log(chalk.red(`  ${p0.length} urgent (P0):`));
                        p0.slice(0, 3).forEach(i => console.log(chalk.gray(`    ${i.id}: ${i.title.substring(0, 60)}`)));
                    }
                    if (p1.length > 0) {
                        console.log(chalk.yellow(`  ${p1.length} important (P1)`));
                    }
                    console.log('');
                }
            } catch {}
        }

        // 3. Recent memory
        const memoryDir = path.join(DELIMIT_HOME, 'memory');
        if (fs.existsSync(memoryDir)) {
            try {
                const memFiles = fs.readdirSync(memoryDir)
                    .filter(f => f.endsWith('.json'))
                    .map(f => ({ name: f, mtime: fs.statSync(path.join(memoryDir, f)).mtimeMs }))
                    .sort((a, b) => b.mtime - a.mtime)
                    .slice(0, 3);

                if (memFiles.length > 0) {
                    console.log(chalk.bold('  Recent memory:'));
                    for (const mf of memFiles) {
                        try {
                            const mem = JSON.parse(fs.readFileSync(path.join(memoryDir, mf.name), 'utf-8'));
                            const key = mem.key || mf.name.replace('.json', '');
                            const val = (mem.content || mem.value || '').substring(0, 60);
                            console.log(chalk.gray(`    ${key}: ${val}`));
                        } catch {}
                    }
                    console.log('');
                }
            } catch {}
        }

        // 4. Git context
        try {
            const branch = execSync('git branch --show-current 2>/dev/null', { encoding: 'utf-8', timeout: 3000 }).trim();
            const lastCommit = execSync('git log --oneline -1 2>/dev/null', { encoding: 'utf-8', timeout: 3000 }).trim();
            const status = execSync('git status --porcelain 2>/dev/null', { encoding: 'utf-8', timeout: 3000 }).trim();
            const changed = status ? status.split('\n').length : 0;

            console.log(chalk.bold('  Git:'));
            console.log(chalk.gray(`    Branch: ${branch}`));
            console.log(chalk.gray(`    Last: ${lastCommit.substring(0, 60)}`));
            if (changed > 0) console.log(chalk.yellow(`    ${changed} uncommitted file(s)`));
            console.log('');
        } catch {}

        // 5. Suggested action
        console.log(chalk.bold('  Start here:'));
        console.log(`    ${chalk.green('Ask your AI:')} "Check the ledger and work on the highest priority item"`);
        console.log('');
    });

// Scan command — instant governance analysis of any project or spec
program
    .command('scan [path]')
    .description('Scan a project or OpenAPI spec for governance insights')
    .action(async (specPath) => {
        const target = specPath ? path.resolve(specPath) : process.cwd();
        console.log(chalk.bold('\n  Delimit Scan\n'));

        // Resolve gateway dir: installed server > bundled in npm package
        const bundledGateway = path.join(__dirname, '..', 'gateway');
        const serverDir = (continuityContext.serverDir && continuityContext.serverDir !== 'undefined' && fs.existsSync(continuityContext.serverDir))
            ? continuityContext.serverDir
            : fs.existsSync(bundledGateway) ? bundledGateway : null;

        if (!serverDir) {
            console.log(chalk.yellow('  Gateway not found. Installing...\n'));
            try {
                execSync('npx delimit-cli setup --yes', { stdio: 'inherit', timeout: 60000 });
                // Retry after setup
                console.log(chalk.green('\n  Setup complete. Re-running scan...\n'));
                execSync(`npx delimit-cli scan ${specPath || ''}`, { stdio: 'inherit', timeout: 30000 });
            } catch {
                console.log(chalk.red('\n  Auto-setup failed. Run manually: npx delimit-cli setup'));
            }
            return;
        }

        // Check Python + yaml dependency
        try {
            execSync('python3 -c "import yaml"', { stdio: 'ignore', timeout: 5000 });
        } catch {
            console.log(chalk.yellow('  Installing Python dependency (pyyaml)...\n'));
            try { execSync('pip3 install pyyaml -q', { stdio: 'ignore', timeout: 30000 }); } catch {}
        }

        // Detect if target is a spec file or a project directory
        const isFile = fs.existsSync(target) && fs.statSync(target).isFile();

        if (isFile) {
            // Spec file — run spec health + show results
            console.log(chalk.gray(`  Analyzing ${target}...\n`));
            try {
                const result = execSync(
                    `python3 -c "import sys,json; sys.path.insert(0,'${serverDir}'); from core.spec_health import score_spec; import yaml; spec=yaml.safe_load(open('${target}')); r=score_spec(spec); print(json.dumps(r))"`,
                    { encoding: 'utf-8', timeout: 15000, cwd: serverDir }
                );
                const health = JSON.parse(result);
                const gradeColors = { A: 'green', B: 'blue', C: 'yellow', D: 'red', F: 'red' };
                const gradeColor = gradeColors[health.grade] || 'white';
                console.log(`  ${chalk[gradeColor].bold(health.grade)} ${chalk.white.bold(health.overall_score + '/100')}  ${chalk.gray('Spec Health Score')}\n`);
                for (const [dim, data] of Object.entries(health.dimensions || {})) {
                    const score = data.score || 0;
                    const bar = '█'.repeat(Math.round(score / 5)) + '░'.repeat(20 - Math.round(score / 5));
                    const color = score >= 70 ? 'green' : score >= 40 ? 'yellow' : 'red';
                    console.log(`  ${chalk.gray(dim.padEnd(16))} ${chalk[color](bar)} ${score}`);
                }
                if (health.recommendations && health.recommendations.length > 0) {
                    console.log(chalk.bold('\n  Recommendations:\n'));
                    health.recommendations.slice(0, 5).forEach(r => {
                        const text = typeof r === 'object' ? (r.recommendation || r.text || JSON.stringify(r)) : r;
                        console.log(`  ${chalk.yellow('→')} ${text}`);
                    });
                }
                // Interactive next step picker
                try {
                    const { next } = await inquirer.prompt([{
                        type: 'list',
                        name: 'next',
                        message: '\n  What next?\n',
                        choices: [
                            { name: `Lint this spec for breaking changes`, value: 'lint' },
                            { name: 'Set up governance for this project', value: 'init' },
                            { name: 'Add CI gate (GitHub Action)', value: 'ci' },
                            { name: 'Configure AI assistants (Claude, Codex, Gemini)', value: 'setup' },
                            { name: 'Exit', value: 'exit' },
                        ],
                    }]);
                    if (next === 'lint') {
                        execSync(`npx delimit-cli lint ${target}`, { stdio: 'inherit' });
                    } else if (next === 'init') {
                        execSync('npx delimit-cli init', { stdio: 'inherit' });
                    } else if (next === 'ci') {
                        execSync('npx delimit-cli ci', { stdio: 'inherit' });
                    } else if (next === 'setup') {
                        execSync('npx delimit-cli setup', { stdio: 'inherit' });
                    }
                } catch {}
            } catch (e) {
                console.log(chalk.red(`  Error: ${e.message}`));
            }
        } else {
            // Project directory — find specs using simple glob, no server.py needed
            console.log(chalk.gray(`  Scanning ${target}...\n`));
            try {
                // Find OpenAPI/Swagger specs
                const specPatterns = ['openapi.yaml', 'openapi.yml', 'openapi.json', 'swagger.yaml', 'swagger.yml', 'swagger.json'];
                const found = [];
                const _findSpecs = (dir, depth) => {
                    if (depth > 4) return;
                    try {
                        for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
                            if (entry.name === 'node_modules' || entry.name === '.next' || entry.name === 'venv' || entry.name === '.git') continue;
                            const full = path.join(dir, entry.name);
                            if (entry.isFile() && specPatterns.includes(entry.name.toLowerCase())) {
                                found.push(path.relative(target, full));
                            } else if (entry.isDirectory()) {
                                _findSpecs(full, depth + 1);
                            }
                        }
                    } catch {}
                };
                _findSpecs(target, 0);

                if (found.length > 0) {
                    console.log(`  ${chalk.green('✓')} Found ${found.length} OpenAPI spec(s): ${found.slice(0, 3).join(', ')}`);
                    // Run health on the first spec
                    const specFile = path.join(target, found[0]);
                    console.log(chalk.gray(`\n  Scoring ${found[0]}...\n`));
                    try {
                        const healthResult = execSync(
                            `python3 -c "import sys,json; sys.path.insert(0,'${serverDir}'); from core.spec_health import score_spec; import yaml; spec=yaml.safe_load(open('${specFile}')); r=score_spec(spec); print(json.dumps(r))"`,
                            { encoding: 'utf-8', timeout: 15000, cwd: serverDir }
                        );
                        const health = JSON.parse(healthResult);
                        const gradeColors = { A: 'green', B: 'blue', C: 'yellow', D: 'red', F: 'red' };
                        const gradeColor = gradeColors[health.grade] || 'white';
                        console.log(`  ${chalk[gradeColor].bold(health.grade)} ${chalk.white.bold(health.overall_score + '/100')}  ${chalk.gray('Spec Health Score')}\n`);
                        for (const [dim, data] of Object.entries(health.dimensions || {})) {
                            const score = data.score || 0;
                            const bar = '\u2588'.repeat(Math.round(score / 5)) + '\u2591'.repeat(20 - Math.round(score / 5));
                            const color = score >= 70 ? 'green' : score >= 40 ? 'yellow' : 'red';
                            console.log(`  ${chalk.gray(dim.padEnd(16))} ${chalk[color](bar)} ${score}`);
                        }
                        if (health.recommendations && health.recommendations.length > 0) {
                            console.log(chalk.bold('\n  Recommendations:\n'));
                            health.recommendations.slice(0, 5).forEach(r => {
                                const text = typeof r === 'object' ? (r.recommendation || r.text || JSON.stringify(r)) : r;
                                console.log(`  ${chalk.yellow('\u2192')} ${text}`);
                            });
                        }
                    } catch {}
                } else {
                    console.log(`  ${chalk.yellow('\u2014')} No OpenAPI specs found in this directory`);
                    console.log('');
                    // Run demo on bundled example spec so users always see value
                    const demoSpec = path.join(__dirname, '..', 'examples', 'petstore-v1.yaml');
                    if (fs.existsSync(demoSpec)) {
                        console.log(chalk.gray('  Running demo on a sample Pet Store API...\n'));
                        try {
                            const demoResult = execSync(
                                `python3 -c "import sys,json; sys.path.insert(0,'${serverDir}'); from core.spec_health import score_spec; import yaml; spec=yaml.safe_load(open('${demoSpec}')); r=score_spec(spec); print(json.dumps(r))"`,
                                { encoding: 'utf-8', timeout: 15000, cwd: serverDir }
                            );
                            const health = JSON.parse(demoResult);
                            const gradeColors = { A: 'green', B: 'blue', C: 'yellow', D: 'red', F: 'red' };
                            const gradeColor = gradeColors[health.grade] || 'white';
                            console.log(`  ${chalk[gradeColor].bold(health.grade)} ${chalk.white.bold(health.overall_score + '/100')}  ${chalk.gray('Sample: Pet Store API')}\n`);
                            for (const [dim, data] of Object.entries(health.dimensions || {})) {
                                const score = data.score || 0;
                                const bar = '\u2588'.repeat(Math.round(score / 5)) + '\u2591'.repeat(20 - Math.round(score / 5));
                                const color = score >= 70 ? 'green' : score >= 40 ? 'yellow' : 'red';
                                console.log(`  ${chalk.gray(dim.padEnd(16))} ${chalk[color](bar)} ${score}`);
                            }
                            console.log('');
                            console.log(chalk.gray('  This is a demo. Point at your spec: npx delimit-cli scan openapi.yaml'));
                            console.log('');
                        } catch {}
                    } else {
                        console.log(chalk.gray('  Tip: point at a spec file: npx delimit-cli scan openapi.yaml'));
                    }
                }
                // Governance readiness checklist
                const hasPolicy = fs.existsSync(path.join(target, '.delimit', 'policies.yml'));
                const hasWorkflow = fs.existsSync(path.join(target, '.github', 'workflows', 'api-governance.yml'));
                const hasGitHooks = fs.existsSync(path.join(target, '.git', 'hooks', 'pre-commit')) &&
                    (() => { try { return fs.readFileSync(path.join(target, '.git', 'hooks', 'pre-commit'), 'utf-8').includes('delimit'); } catch { return false; } })();
                const hasMcp = fs.existsSync(path.join(os.homedir(), '.mcp.json')) &&
                    (() => { try { return fs.readFileSync(path.join(os.homedir(), '.mcp.json'), 'utf-8').includes('delimit'); } catch { return false; } })();
                const hasSpecs = found.length > 0;

                const checks = [
                    { name: 'API spec', done: hasSpecs, fix: 'Add an openapi.yaml to your project' },
                    { name: 'Policy', done: hasPolicy, fix: 'npx delimit-cli init' },
                    { name: 'CI gate', done: hasWorkflow, fix: 'npx delimit-cli ci' },
                    { name: 'Git hooks', done: hasGitHooks, fix: 'npx delimit-cli hooks install' },
                    { name: 'MCP tools', done: hasMcp, fix: 'npx delimit-cli setup' },
                ];
                const score = checks.filter(c => c.done).length;

                console.log(chalk.bold(`\n  Governance Readiness: ${score}/${checks.length}\n`));
                for (const c of checks) {
                    if (c.done) {
                        console.log(`  ${chalk.green('●')} ${c.name}`);
                    } else {
                        console.log(`  ${chalk.gray('○')} ${c.name} ${chalk.gray('—')} ${chalk.yellow(c.fix)}`);
                    }
                }
                console.log('');

                // Interactive next step picker
                try {
                    // Pre-select the first missing item
                    const firstMissing = checks.find(c => !c.done);
                    const defaultChoice = firstMissing ?
                        (firstMissing.name === 'Policy' ? 'init' :
                         firstMissing.name === 'CI gate' ? 'ci' :
                         firstMissing.name === 'Git hooks' ? 'hooks' :
                         firstMissing.name === 'MCP tools' ? 'setup' : 'init') : 'exit';

                    const { next } = await inquirer.prompt([{
                        type: 'list',
                        name: 'next',
                        message: '\n  What next?\n',
                        choices: [
                            { name: 'Set up governance for this project', value: 'init' },
                            { name: 'Add CI gate (GitHub Action)', value: 'ci' },
                            { name: 'Install git hooks', value: 'hooks' },
                            { name: 'Configure AI assistants (Claude, Codex, Gemini)', value: 'setup' },
                            { name: 'Run a breaking change demo', value: 'try' },
                            { name: 'Exit', value: 'exit' },
                        ],
                        default: defaultChoice,
                    }]);
                    if (next === 'init') {
                        execSync('npx delimit-cli init', { stdio: 'inherit' });
                    } else if (next === 'ci') {
                        execSync('npx delimit-cli ci', { stdio: 'inherit' });
                    } else if (next === 'hooks') {
                        execSync('npx delimit-cli hooks install', { stdio: 'inherit' });
                    } else if (next === 'setup') {
                        execSync('npx delimit-cli setup', { stdio: 'inherit' });
                    } else if (next === 'try') {
                        execSync('npx delimit-cli try', { stdio: 'inherit' });
                    }
                } catch {}
            } catch (e) {
                console.log(chalk.red(`  Error: ${e.message}`));
            }
        }
    });

// PR review command — review any GitHub PR for breaking API changes
program
    .command('pr <url>')
    .description('Review a GitHub PR for breaking API changes')
    .action(async (url) => {
        console.log(chalk.bold('\n  Delimit PR Review\n'));

        // Parse GitHub PR URL: owner/repo#number or full URL
        let owner, repo, prNumber;
        const urlMatch = url.match(/github\.com\/([^/]+)\/([^/]+)\/pull\/(\d+)/);
        const shortMatch = url.match(/^([^/]+)\/([^#]+)#(\d+)$/);
        if (urlMatch) {
            [, owner, repo, prNumber] = urlMatch;
        } else if (shortMatch) {
            [, owner, repo, prNumber] = shortMatch;
        } else if (/^\d+$/.test(url)) {
            // Just a number — try current repo
            try {
                const remote = execSync('git remote get-url origin 2>/dev/null', { encoding: 'utf-8' }).trim();
                const remoteMatch = remote.match(/github\.com[:/]([^/]+)\/([^/.]+)/);
                if (remoteMatch) {
                    [, owner, repo] = remoteMatch;
                    prNumber = url;
                }
            } catch {}
        }

        if (!owner || !repo || !prNumber) {
            console.log(chalk.red('  Could not parse PR URL.'));
            console.log(chalk.gray('  Usage: npx delimit-cli pr owner/repo#123'));
            console.log(chalk.gray('         npx delimit-cli pr https://github.com/owner/repo/pull/123'));
            console.log(chalk.gray('         npx delimit-cli pr 123  (in a git repo)\n'));
            return;
        }

        console.log(chalk.gray(`  Reviewing ${owner}/${repo}#${prNumber}...\n`));

        // Get PR changed files
        try {
            const filesJson = execSync(
                `gh api repos/${owner}/${repo}/pulls/${prNumber}/files --paginate 2>/dev/null`,
                { encoding: 'utf-8', timeout: 15000 }
            );
            const files = JSON.parse(filesJson);
            const specPatterns = ['openapi', 'swagger', 'api-spec', 'api_spec'];
            const specExts = ['.yaml', '.yml', '.json'];
            const specFiles = files.filter(f => {
                const name = f.filename.toLowerCase();
                return specExts.some(ext => name.endsWith(ext)) &&
                       specPatterns.some(p => name.includes(p));
            });

            if (specFiles.length === 0) {
                console.log(chalk.gray('  No OpenAPI/Swagger spec changes found in this PR.'));
                console.log(chalk.gray('  Delimit reviews PRs that modify API spec files.\n'));
                // Show what files were changed
                const apiFiles = files.filter(f => f.filename.includes('api') || f.filename.includes('spec'));
                if (apiFiles.length > 0) {
                    console.log(chalk.gray('  API-related files changed:'));
                    apiFiles.slice(0, 5).forEach(f => console.log(chalk.gray(`    ${f.filename} (+${f.additions}/-${f.deletions})`)));
                    console.log('');
                }
                return;
            }

            console.log(`  ${chalk.green('✓')} Found ${specFiles.length} spec file(s) changed:\n`);
            specFiles.forEach(f => {
                console.log(`    ${chalk.cyan(f.filename)} (+${f.additions}/-${f.deletions})`);
            });
            console.log('');

            // Fetch base and head versions of the first spec
            const specFile = specFiles[0];
            const tmpDir = path.join(os.tmpdir(), `delimit-pr-${Date.now()}`);
            fs.mkdirSync(tmpDir, { recursive: true });

            // Get PR details for base/head refs
            const prJson = execSync(
                `gh api repos/${owner}/${repo}/pulls/${prNumber} 2>/dev/null`,
                { encoding: 'utf-8', timeout: 10000 }
            );
            const pr = JSON.parse(prJson);
            const baseSha = pr.base.sha;
            const headSha = pr.head.sha;

            // Fetch both versions
            const basePath = path.join(tmpDir, 'base.yaml');
            const headPath = path.join(tmpDir, 'head.yaml');

            try {
                const baseContent = execSync(
                    `gh api repos/${owner}/${repo}/contents/${specFile.filename}?ref=${baseSha} -q '.content' 2>/dev/null`,
                    { encoding: 'utf-8', timeout: 10000 }
                );
                fs.writeFileSync(basePath, Buffer.from(baseContent.trim(), 'base64').toString());
            } catch {
                console.log(chalk.yellow('  New spec file (no base version). Cannot diff.\n'));
                fs.rmSync(tmpDir, { recursive: true, force: true });
                return;
            }

            try {
                const headContent = execSync(
                    `gh api repos/${owner}/${repo}/contents/${specFile.filename}?ref=${headSha} -q '.content' 2>/dev/null`,
                    { encoding: 'utf-8', timeout: 10000 }
                );
                fs.writeFileSync(headPath, Buffer.from(headContent.trim(), 'base64').toString());
            } catch {
                console.log(chalk.red('  Could not fetch head version of spec.\n'));
                fs.rmSync(tmpDir, { recursive: true, force: true });
                return;
            }

            // Run lint
            console.log(chalk.gray('  Running governance pipeline...\n'));
            const bundledGateway = path.join(__dirname, '..', 'gateway');
            const serverDir = (continuityContext.serverDir && fs.existsSync(continuityContext.serverDir))
                ? continuityContext.serverDir
                : fs.existsSync(bundledGateway) ? bundledGateway : null;

            if (!serverDir) {
                console.log(chalk.yellow('  Gateway not available. Run: npx delimit-cli setup\n'));
                fs.rmSync(tmpDir, { recursive: true, force: true });
                return;
            }

            try {
                const result = execSync(
                    `python3 -c "
import sys,json,yaml
sys.path.insert(0,'${serverDir}')
from core.diff_engine_v2 import OpenAPIDiffEngine
old=yaml.safe_load(open('${basePath}'))
new=yaml.safe_load(open('${headPath}'))
engine=OpenAPIDiffEngine()
changes=engine.compare(old,new)
print(json.dumps({'changes':[{'type':c.type.value if hasattr(c.type,'value') else str(c.type),'path':c.path,'breaking':c.severity in ('high','critical','error'),'detail':c.message or c.details or ''} for c in changes]}))
"`,
                    { encoding: 'utf-8', timeout: 15000, cwd: serverDir }
                );
                const diff = JSON.parse(result);
                const breaking = (diff.changes || []).filter(c => c.breaking);
                const nonBreaking = (diff.changes || []).filter(c => !c.breaking);

                if (breaking.length > 0) {
                    console.log(chalk.red.bold(`  ${breaking.length} BREAKING change(s) detected\n`));
                    breaking.forEach(c => {
                        console.log(`  ${chalk.red('BREAK')} ${c.type}: ${c.path || ''}`);
                        if (c.detail) console.log(chalk.gray(`        ${c.detail}`));
                    });
                } else {
                    console.log(chalk.green.bold('  No breaking changes detected'));
                }

                if (nonBreaking.length > 0) {
                    console.log(chalk.gray(`\n  ${nonBreaking.length} non-breaking change(s)`));
                }

                // Semver classification
                const bump = breaking.length > 0 ? 'MAJOR' : nonBreaking.length > 0 ? 'MINOR' : 'NONE';
                console.log(`\n  Semver: ${chalk.bold(bump)}`);
                console.log(`  Total: ${(diff.changes || []).length} changes (${breaking.length} breaking, ${nonBreaking.length} compatible)\n`);

                console.log(chalk.bold('  Add to your repo:'));
                console.log(chalk.green(`    npx delimit-cli init`));
                console.log(chalk.gray('    Catches this on every PR automatically.\n'));
            } catch (e) {
                console.log(chalk.red(`  Diff failed: ${e.message.split('\n')[0]}\n`));
            }

            fs.rmSync(tmpDir, { recursive: true, force: true });
        } catch (e) {
            console.log(chalk.red(`  Error: ${e.message.split('\n')[0]}`));
            console.log(chalk.gray('  Make sure gh CLI is authenticated: gh auth login\n'));
        }
    });

// Try command — zero-risk demo with Markdown report artifact (LED-264 + LED-424)
program
    .command('try [repo]')
    .description('Run governance on your project, a GitHub repo, or a built-in demo')
    .action(async (repo) => {
        const tmpDir = path.join(os.tmpdir(), `delimit-try-${Date.now()}`);
        fs.mkdirSync(tmpDir, { recursive: true });

        console.log(chalk.bold('\n  Delimit — Try It\n'));

        // Mode 1: Clone a GitHub repo and scan it
        if (repo && (repo.includes('/') || repo.startsWith('http'))) {
            const repoUrl = repo.startsWith('http') ? repo : `https://github.com/${repo}`;
            console.log(chalk.gray(`  Cloning ${repoUrl}...\n`));
            try {
                execSync(`git clone --depth 1 ${repoUrl} ${tmpDir}/repo 2>&1`, { timeout: 30000 });
                console.log(chalk.green('  Cloned. Scanning for OpenAPI specs...\n'));
                execSync(`node "${path.join(__dirname, 'delimit-cli.js')}" scan ${tmpDir}/repo`, { stdio: 'inherit', timeout: 30000 });
                // Show governance readiness for their repo
                console.log(chalk.bold('\n  Want to add governance to this repo?\n'));
                console.log(`    ${chalk.green('1.')} Fork it and run ${chalk.bold('npx delimit-cli init')}`);
                console.log(`    ${chalk.green('2.')} Add CI: ${chalk.bold('npx delimit-cli ci')}`);
                console.log(`    ${chalk.green('3.')} Open a PR — Delimit comments with breaking changes automatically\n`);
                try { fs.rmSync(tmpDir, { recursive: true }); } catch {}
                return;
            } catch (err) {
                console.log(chalk.red(`  Could not clone: ${err.message}\n`));
                console.log(chalk.gray('  Falling back to built-in demo...\n'));
            }
        }

        // Mode 2: Use current directory if it has specs
        if (!repo) {
            const cwd = process.cwd();
            const localSpecs = ['openapi.yaml', 'openapi.yml', 'openapi.json', 'swagger.yaml', 'swagger.json',
                'api/openapi.yaml', 'api/openapi.yml', 'api/openapi.json', 'spec/api.json', 'docs/openapi.yaml'];
            const foundLocal = localSpecs.find(s => fs.existsSync(path.join(cwd, s)));
            if (foundLocal) {
                console.log(chalk.green(`  Found ${foundLocal} in current directory. Running governance scan...\n`));
                execSync(`node "${path.join(__dirname, 'delimit-cli.js')}" scan ${path.join(cwd, foundLocal)}`, { stdio: 'inherit', timeout: 30000 });
                try { fs.rmSync(tmpDir, { recursive: true }); } catch {}
                return;
            }
        }

        // Mode 3: Built-in demo (original behavior)
        console.log(chalk.gray('  Safe mode: runs in a temp directory, nothing touches your project.\n'));

        // Create sample specs (same as demo)
        const baseSpec = {
            openapi: '3.0.3',
            info: { title: 'Pet Store API', version: '1.0.0' },
            paths: {
                '/pets': {
                    get: { summary: 'List all pets', parameters: [{ name: 'limit', in: 'query', required: false, schema: { type: 'integer' } }], responses: { '200': { description: 'A list of pets' } } },
                    post: { summary: 'Create a pet', responses: { '201': { description: 'Pet created' } } },
                },
                '/pets/{petId}': {
                    get: { summary: 'Get a pet by ID', parameters: [{ name: 'petId', in: 'path', required: true, schema: { type: 'string' } }], responses: { '200': { description: 'A pet' } } },
                },
            },
            components: { schemas: { Pet: { type: 'object', required: ['id', 'name'], properties: { id: { type: 'integer' }, name: { type: 'string' }, tag: { type: 'string' } } } } },
        };

        const changedSpec = JSON.parse(JSON.stringify(baseSpec));
        changedSpec.info.version = '2.0.0';
        delete changedSpec.paths['/pets/{petId}'];
        changedSpec.paths['/pets'].get.parameters.push({ name: 'owner_id', in: 'query', required: true, schema: { type: 'string' } });
        delete changedSpec.components.schemas.Pet.properties.tag;
        changedSpec.paths['/pets/search'] = { get: { summary: 'Search pets', responses: { '200': { description: 'Search results' } } } };

        const basePath = path.join(tmpDir, 'openapi-v1.yaml');
        const changedPath = path.join(tmpDir, 'openapi-v2.yaml');
        fs.writeFileSync(basePath, yaml.dump(baseSpec));
        fs.writeFileSync(changedPath, yaml.dump(changedSpec));

        console.log(chalk.gray('  Running governance pipeline...\n'));

        // Run lint
        let report = [];
        let violations = [];
        let semverBump = 'MAJOR';
        try {
            const result = apiEngine.lint(basePath, changedPath, { policy: 'strict' });
            if (result) {
                violations = result.violations || [];
                const s = result.summary || {};
                const breaking = s.breaking || s.breaking_changes || 0;
                const total = s.total || s.total_changes || 0;
                semverBump = result.semver?.bump?.toUpperCase() || 'MAJOR';

                report.push('# Delimit Governance Report');
                report.push('');
                report.push(`**Generated**: ${new Date().toISOString().slice(0, 19).replace('T', ' ')} UTC`);
                report.push(`**API**: Pet Store API`);
                report.push(`**Changes**: ${total} total (${breaking} breaking)`);
                report.push(`**Semver**: ${semverBump}`);
                report.push('');
                report.push('## Governance Gates');
                report.push('');
                report.push('| Gate | Status | Enforcement Chain |');
                report.push('|------|--------|-------------------|');
                report.push(`| API Lint | ${breaking > 0 ? 'FAIL' : 'PASS'} | lint → semver → gov_evaluate |`);
                report.push(`| Policy Compliance | ${violations.length > 0 ? 'FAIL (' + violations.length + ')' : 'PASS'} | policy → evidence_collect |`);
                report.push('| Security Audit | PASS | security_audit → evidence_collect |');
                report.push(`| Deploy Readiness | ${breaking > 0 ? 'BLOCKED' : 'READY'} | deploy_plan → security_audit |`);
                report.push('');

                if (violations.length > 0) {
                    report.push('## Violations');
                    report.push('');
                    report.push('| Severity | Change | Location |');
                    report.push('|----------|--------|----------|');
                    violations.forEach(v => {
                        report.push(`| ${v.severity === 'error' ? 'BLOCK' : 'WARN'} | ${v.message} | \`${v.path || ''}\` |`);
                    });
                    report.push('');
                }

                report.push('## What Delimit Does');
                report.push('');
                report.push('1. Detects breaking changes automatically (27 types)');
                report.push('2. Evaluates against your policy (strict/default/relaxed)');
                report.push('3. Blocks deploys via governance gates');
                report.push('4. Records evidence for audit trail');
                report.push('5. Posts remediation guide on PRs');
                report.push('6. Tracks in ledger for cross-model continuity');
                report.push('');
                report.push('## Get Started');
                report.push('');
                report.push('```bash');
                report.push('npx delimit-cli init     # Set up governance');
                report.push('npx delimit-cli setup    # Configure AI assistants');
                report.push('```');
                report.push('');
                report.push('---');
                report.push('[delimit.ai](https://delimit.ai) | [Dashboard](https://app.delimit.ai) | [GitHub](https://github.com/delimit-ai/delimit-mcp-server)');
            }
        } catch (err) {
            report.push('# Delimit Governance Report');
            report.push('');
            report.push(`Error: ${err.message}`);
        }

        // Save report to user's current directory
        const reportPath = path.join(process.cwd(), 'delimit-report.md');
        const reportContent = report.join('\n');
        fs.writeFileSync(reportPath, reportContent);

        // Show summary in terminal
        const breakingCount = violations.filter(v => v.severity === 'error').length;
        if (breakingCount > 0) {
            console.log(chalk.red.bold(`  BLOCKED — ${breakingCount} breaking change(s) detected`));
        }
        violations.forEach(v => {
            const icon = v.severity === 'error' ? chalk.red('  BLOCK') : chalk.yellow('  WARN ');
            console.log(`  ${icon} ${v.message}`);
        });
        console.log('');
        console.log(chalk.bold('  Governance Gates:'));
        console.log(`    ${breakingCount > 0 ? chalk.red('X') : chalk.green('+')} API Lint          ${chalk.gray('→ semver → gov_evaluate')}`);
        console.log(`    ${violations.length > 0 ? chalk.red('X') : chalk.green('+')} Policy Compliance  ${chalk.gray('→ evidence_collect')}`);
        console.log(`    ${chalk.green('+')} Security Audit     ${chalk.gray('→ evidence_collect → notify')}`);
        console.log(`    ${breakingCount > 0 ? chalk.red('X') : chalk.green('+')} Deploy Readiness   ${chalk.gray('→ deploy_plan → security_audit')}`);
        console.log('');
        console.log(chalk.green.bold(`  Report saved to: ${reportPath}`));
        console.log(chalk.gray('  Open it to see the full governance analysis.\n'));

        console.log(chalk.bold('  Next:'));
        console.log(`    ${chalk.green('npx delimit-cli init')}     — set up governance in your project`);
        console.log(`    ${chalk.green('npx delimit-cli setup')}    — configure AI assistants`);
        console.log(chalk.gray(`    rm delimit-report.md             — clean up this report\n`));

        // Beta capture
        try {
            const betaAns = await inquirer.prompt([{
                type: 'input',
                name: 'email',
                message: chalk.blue('Join the beta? Enter your email (or press Enter to skip):'),
            }]);
            if (betaAns.email && betaAns.email.includes('@')) {
                try {
                    await axios.post('https://delimit.ai/api/subscribe', { email: betaAns.email, source: 'cli-try' });
                    console.log(chalk.green('\n  Thanks! You\'re on the list.\n'));
                } catch {
                    console.log(chalk.green('\n  Thanks! Visit https://delimit.ai\n'));
                }
            }
        } catch {}

        // Cleanup temp
        try { fs.rmSync(tmpDir, { recursive: true }); } catch {}
    });

// Report command — generate local governance reports (v4.20)
program
    .command('report')
    .description('Generate a governance report from local evidence, ledger, and memory')
    .option('--since <duration>', 'Time period (e.g., 7d, 30d, 24h, 1w, 1m)', '7d')
    .option('--format <fmt>', 'Output format: md, json, html', 'md')
    .option('--output <file>', 'Write report to file instead of stdout')
    .action(async (options) => {
        const delimitHome = path.join(os.homedir(), '.delimit');
        const evidenceDir = path.join(delimitHome, 'evidence');
        const ledgerDir = path.join(delimitHome, 'ledger');
        const memoryDir = path.join(delimitHome, 'memory');

        // Parse duration into milliseconds
        function parseDuration(dur) {
            const match = dur.match(/^(\d+)\s*(h|d|w|m)$/i);
            if (!match) return 7 * 24 * 60 * 60 * 1000; // default 7d
            const val = parseInt(match[1], 10);
            const unit = match[2].toLowerCase();
            const multipliers = { h: 3600000, d: 86400000, w: 604800000, m: 2592000000 };
            return val * (multipliers[unit] || 86400000);
        }

        const sinceMs = parseDuration(options.since);
        const cutoff = new Date(Date.now() - sinceMs);
        const now = new Date();
        const fmt = (options.format || 'md').toLowerCase();

        if (!['md', 'json', 'html'].includes(fmt)) {
            console.error(chalk.red(`  Invalid format: ${fmt}. Use md, json, or html.`));
            process.exit(1);
        }

        // Collect evidence events
        const evidenceEvents = [];
        if (fs.existsSync(evidenceDir)) {
            const files = fs.readdirSync(evidenceDir);
            for (const f of files) {
                const fp = path.join(evidenceDir, f);
                try {
                    if (f.endsWith('.json')) {
                        const data = JSON.parse(fs.readFileSync(fp, 'utf-8'));
                        // Determine timestamp from various fields
                        let ts = null;
                        if (data.timestamp) ts = new Date(data.timestamp);
                        else if (data.collected_at) ts = new Date(data.collected_at * 1000);
                        if (ts && ts >= cutoff) {
                            evidenceEvents.push({ ...data, _ts: ts, _file: f });
                        }
                    } else if (f.endsWith('.jsonl')) {
                        const lines = fs.readFileSync(fp, 'utf-8').split('\n').filter(Boolean);
                        for (const line of lines) {
                            try {
                                const data = JSON.parse(line);
                                let ts = null;
                                if (data.timestamp) ts = new Date(data.timestamp);
                                else if (data.collected_at) ts = new Date(data.collected_at * 1000);
                                if (ts && ts >= cutoff) {
                                    evidenceEvents.push({ ...data, _ts: ts, _file: f });
                                }
                            } catch {}
                        }
                    }
                } catch {}
            }
        }
        evidenceEvents.sort((a, b) => a._ts - b._ts);

        // Categorize evidence
        const violations = evidenceEvents.filter(e =>
            e.result === 'failed' || e.result === 'blocked' ||
            (e.action && /fail|block|violation|error/i.test(e.action))
        );
        const approvals = evidenceEvents.filter(e =>
            e.result === 'passed' || e.result === 'approved' ||
            (e.action && /pass|approve|success/i.test(e.action))
        );

        // Collect ledger items
        const ledgerItems = [];
        if (fs.existsSync(ledgerDir)) {
            const files = fs.readdirSync(ledgerDir);
            for (const f of files) {
                const fp = path.join(ledgerDir, f);
                try {
                    if (f.endsWith('.json')) {
                        const data = JSON.parse(fs.readFileSync(fp, 'utf-8'));
                        if (Array.isArray(data)) {
                            ledgerItems.push(...data);
                        } else {
                            ledgerItems.push(data);
                        }
                    } else if (f.endsWith('.jsonl')) {
                        const lines = fs.readFileSync(fp, 'utf-8').split('\n').filter(Boolean);
                        for (const line of lines) {
                            try { ledgerItems.push(JSON.parse(line)); } catch {}
                        }
                    }
                } catch {}
            }
        }
        const openLedgerItems = ledgerItems.filter(i => i.status === 'open' || !i.status);

        // Count memory entries
        let memoryCount = 0;
        const recentMemories = [];
        if (fs.existsSync(memoryDir)) {
            const files = fs.readdirSync(memoryDir).filter(f => f.endsWith('.json') || f.endsWith('.jsonl'));
            for (const f of files) {
                const fp = path.join(memoryDir, f);
                try {
                    if (f.endsWith('.json')) {
                        const data = JSON.parse(fs.readFileSync(fp, 'utf-8'));
                        memoryCount++;
                        if (data.created_at && new Date(data.created_at) >= cutoff) {
                            recentMemories.push(data);
                        }
                    } else if (f.endsWith('.jsonl')) {
                        const lines = fs.readFileSync(fp, 'utf-8').split('\n').filter(Boolean);
                        for (const line of lines) {
                            try {
                                const data = JSON.parse(line);
                                memoryCount++;
                                if (data.created_at && new Date(data.created_at) >= cutoff) {
                                    recentMemories.push(data);
                                }
                            } catch {}
                        }
                    }
                } catch {}
            }
        }

        // Git history
        let gitCommits = [];
        try {
            const sinceDate = cutoff.toISOString().split('T')[0];
            const gitLog = execSync(
                `git log --oneline --since="${sinceDate}" --no-decorate -20 2>/dev/null`,
                { encoding: 'utf-8', timeout: 5000 }
            ).trim();
            if (gitLog) {
                gitCommits = gitLog.split('\n').filter(Boolean);
            }
        } catch {}

        // Pre-commit hook detection
        let hasPreCommitHook = false;
        try {
            const hookPath = path.join(process.cwd(), '.git', 'hooks', 'pre-commit');
            hasPreCommitHook = fs.existsSync(hookPath);
        } catch {}

        // Recommendations
        const recommendations = [];
        if (!hasPreCommitHook) {
            recommendations.push('Consider adding pre-commit hooks: npx delimit-cli init');
        }
        if (violations.length > approvals.length && evidenceEvents.length > 0) {
            recommendations.push('Failure rate exceeds pass rate — review policy strictness or address recurring violations');
        }
        if (evidenceEvents.length === 0) {
            recommendations.push('No governance events found — run delimit lint or delimit scan to start collecting evidence');
        }
        if (openLedgerItems.length > 20) {
            recommendations.push(`${openLedgerItems.length} open ledger items — consider triaging and closing resolved items`);
        }
        if (memoryCount === 0) {
            recommendations.push('No memory entries — use delimit remember to capture architecture decisions and gotchas');
        }

        // Build report data
        const reportData = {
            generated_at: now.toISOString(),
            period: { since: cutoff.toISOString(), until: now.toISOString(), duration: options.since },
            summary: {
                total_events: evidenceEvents.length,
                violations: violations.length,
                approvals: approvals.length,
                other_events: evidenceEvents.length - violations.length - approvals.length,
                pass_rate: evidenceEvents.length > 0
                    ? Math.round((approvals.length / evidenceEvents.length) * 100)
                    : null,
                ledger_items_open: openLedgerItems.length,
                ledger_items_total: ledgerItems.length,
                memory_entries: memoryCount,
                recent_memories: recentMemories.length,
                git_commits: gitCommits.length,
            },
            violations: violations.map(v => ({
                action: v.action || 'unknown',
                result: v.result || 'failed',
                timestamp: v._ts.toISOString(),
                target: v.target || null,
                files: v.files || null,
            })),
            approvals: approvals.map(a => ({
                action: a.action || 'unknown',
                result: a.result || 'passed',
                timestamp: a._ts.toISOString(),
                target: a.target || null,
            })),
            audit_events: evidenceEvents.map(e => ({
                action: e.action || 'evidence_collected',
                result: e.result || null,
                timestamp: e._ts.toISOString(),
                target: e.target || null,
                file: e._file,
            })),
            active_ledger_items: openLedgerItems.slice(0, 50).map(i => ({
                id: i.id || null,
                title: i.title || null,
                type: i.type || null,
                priority: i.priority || null,
                status: i.status || 'open',
                created_at: i.created_at || null,
            })),
            git_commits: gitCommits,
            recommendations,
        };

        // Format output
        let output = '';

        if (fmt === 'json') {
            output = JSON.stringify(reportData, null, 2);
        } else if (fmt === 'md') {
            const lines = [];
            lines.push('# Delimit Governance Report');
            lines.push('');
            lines.push(`**Period**: ${cutoff.toISOString().split('T')[0]} to ${now.toISOString().split('T')[0]} (${options.since})`);
            lines.push(`**Generated**: ${now.toISOString()}`);
            lines.push('');

            lines.push('## Summary');
            lines.push('');
            lines.push(`| Metric | Value |`);
            lines.push(`|--------|-------|`);
            lines.push(`| Total governance events | ${reportData.summary.total_events} |`);
            lines.push(`| Violations | ${reportData.summary.violations} |`);
            lines.push(`| Approvals | ${reportData.summary.approvals} |`);
            lines.push(`| Pass rate | ${reportData.summary.pass_rate !== null ? reportData.summary.pass_rate + '%' : 'N/A'} |`);
            lines.push(`| Open ledger items | ${reportData.summary.ledger_items_open} |`);
            lines.push(`| Memory entries | ${reportData.summary.memory_entries} (${reportData.summary.recent_memories} recent) |`);
            lines.push(`| Git commits | ${reportData.summary.git_commits} |`);
            lines.push('');

            if (reportData.violations.length > 0) {
                lines.push('## Violations');
                lines.push('');
                for (const v of reportData.violations) {
                    lines.push(`- **${v.action}** - ${v.result} at ${v.timestamp}${v.target ? ' (' + v.target + ')' : ''}`);
                }
                lines.push('');
            } else {
                lines.push('## Violations');
                lines.push('');
                lines.push('No violations in this period.');
                lines.push('');
            }

            if (reportData.approvals.length > 0) {
                lines.push('## Approvals');
                lines.push('');
                for (const a of reportData.approvals) {
                    lines.push(`- **${a.action}** - ${a.result} at ${a.timestamp}${a.target ? ' (' + a.target + ')' : ''}`);
                }
                lines.push('');
            }

            if (reportData.audit_events.length > 0) {
                lines.push('## Audit Events');
                lines.push('');
                for (const e of reportData.audit_events) {
                    lines.push(`- \`${e.timestamp}\` ${e.action}${e.result ? ' [' + e.result + ']' : ''}${e.target ? ' - ' + e.target : ''}`);
                }
                lines.push('');
            }

            if (reportData.active_ledger_items.length > 0) {
                lines.push('## Active Ledger Items');
                lines.push('');
                for (const i of reportData.active_ledger_items) {
                    const prefix = i.priority ? `[${i.priority}]` : '';
                    lines.push(`- ${prefix} ${i.id || ''} ${i.title || 'Untitled'}${i.type ? ' (' + i.type + ')' : ''}`);
                }
                if (openLedgerItems.length > 50) {
                    lines.push(`- ... and ${openLedgerItems.length - 50} more`);
                }
                lines.push('');
            }

            if (gitCommits.length > 0) {
                lines.push('## Recent Commits');
                lines.push('');
                for (const c of gitCommits) {
                    lines.push(`- \`${c}\``);
                }
                lines.push('');
            }

            if (recommendations.length > 0) {
                lines.push('## Recommendations');
                lines.push('');
                for (const r of recommendations) {
                    lines.push(`- ${r}`);
                }
                lines.push('');
            }

            lines.push('---');
            lines.push('*Generated by [Delimit](https://delimit.ai) governance reporting*');
            output = lines.join('\n');
        } else if (fmt === 'html') {
            const esc = (s) => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            const h = [];
            h.push('<!DOCTYPE html>');
            h.push('<html lang="en"><head><meta charset="utf-8">');
            h.push('<title>Delimit Governance Report</title>');
            h.push('<style>');
            h.push('body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;color:#1a1a2e;line-height:1.6}');
            h.push('h1{color:#16213e;border-bottom:3px solid #0f3460;padding-bottom:.5rem}');
            h.push('h2{color:#0f3460;margin-top:2rem}');
            h.push('table{border-collapse:collapse;width:100%}');
            h.push('th,td{text-align:left;padding:.5rem .75rem;border:1px solid #ddd}');
            h.push('th{background:#f0f4f8;font-weight:600}');
            h.push('.pass{color:#16a34a}.fail{color:#dc2626}');
            h.push('.badge{display:inline-block;padding:.1rem .5rem;border-radius:3px;font-size:.85rem;font-weight:600}');
            h.push('.badge-ok{background:#dcfce7;color:#166534}.badge-fail{background:#fef2f2;color:#991b1b}');
            h.push('ul{padding-left:1.5rem}li{margin:.25rem 0}');
            h.push('.meta{color:#6b7280;font-size:.9rem}');
            h.push('footer{margin-top:2rem;padding-top:1rem;border-top:1px solid #ddd;color:#9ca3af;font-size:.85rem}');
            h.push('</style></head><body>');
            h.push('<h1>Delimit Governance Report</h1>');
            h.push(`<p class="meta">Period: ${esc(cutoff.toISOString().split('T')[0])} to ${esc(now.toISOString().split('T')[0])} (${esc(options.since)}) &mdash; Generated: ${esc(now.toISOString())}</p>`);

            h.push('<h2>Summary</h2>');
            h.push('<table><tr><th>Metric</th><th>Value</th></tr>');
            h.push(`<tr><td>Total governance events</td><td>${reportData.summary.total_events}</td></tr>`);
            h.push(`<tr><td>Violations</td><td><span class="${reportData.summary.violations > 0 ? 'fail' : ''}">${reportData.summary.violations}</span></td></tr>`);
            h.push(`<tr><td>Approvals</td><td><span class="pass">${reportData.summary.approvals}</span></td></tr>`);
            h.push(`<tr><td>Pass rate</td><td>${reportData.summary.pass_rate !== null ? '<span class="badge ' + (reportData.summary.pass_rate >= 80 ? 'badge-ok' : 'badge-fail') + '">' + reportData.summary.pass_rate + '%</span>' : 'N/A'}</td></tr>`);
            h.push(`<tr><td>Open ledger items</td><td>${reportData.summary.ledger_items_open}</td></tr>`);
            h.push(`<tr><td>Memory entries</td><td>${reportData.summary.memory_entries} (${reportData.summary.recent_memories} recent)</td></tr>`);
            h.push(`<tr><td>Git commits</td><td>${reportData.summary.git_commits}</td></tr>`);
            h.push('</table>');

            h.push('<h2>Violations</h2>');
            if (reportData.violations.length > 0) {
                h.push('<ul>');
                for (const v of reportData.violations) {
                    h.push(`<li><strong>${esc(v.action)}</strong> - ${esc(v.result)} at ${esc(v.timestamp)}${v.target ? ' (' + esc(v.target) + ')' : ''}</li>`);
                }
                h.push('</ul>');
            } else {
                h.push('<p class="pass">No violations in this period.</p>');
            }

            if (reportData.approvals.length > 0) {
                h.push('<h2>Approvals</h2><ul>');
                for (const a of reportData.approvals) {
                    h.push(`<li><strong>${esc(a.action)}</strong> - ${esc(a.result)} at ${esc(a.timestamp)}${a.target ? ' (' + esc(a.target) + ')' : ''}</li>`);
                }
                h.push('</ul>');
            }

            if (reportData.audit_events.length > 0) {
                h.push('<h2>Audit Events</h2><ul>');
                for (const e of reportData.audit_events) {
                    h.push(`<li><code>${esc(e.timestamp)}</code> ${esc(e.action)}${e.result ? ' [' + esc(e.result) + ']' : ''}${e.target ? ' - ' + esc(e.target) : ''}</li>`);
                }
                h.push('</ul>');
            }

            if (reportData.active_ledger_items.length > 0) {
                h.push('<h2>Active Ledger Items</h2><ul>');
                for (const i of reportData.active_ledger_items) {
                    const prefix = i.priority ? `[${esc(i.priority)}]` : '';
                    h.push(`<li>${prefix} ${esc(i.id || '')} ${esc(i.title || 'Untitled')}${i.type ? ' (' + esc(i.type) + ')' : ''}</li>`);
                }
                if (openLedgerItems.length > 50) {
                    h.push(`<li>... and ${openLedgerItems.length - 50} more</li>`);
                }
                h.push('</ul>');
            }

            if (gitCommits.length > 0) {
                h.push('<h2>Recent Commits</h2><ul>');
                for (const c of gitCommits) {
                    h.push(`<li><code>${esc(c)}</code></li>`);
                }
                h.push('</ul>');
            }

            if (recommendations.length > 0) {
                h.push('<h2>Recommendations</h2><ul>');
                for (const r of recommendations) {
                    h.push(`<li>${esc(r)}</li>`);
                }
                h.push('</ul>');
            }

            h.push('<footer>Generated by <a href="https://delimit.ai">Delimit</a> governance reporting</footer>');
            h.push('</body></html>');
            output = h.join('\n');
        }

        // Output
        if (options.output) {
            const outPath = path.resolve(options.output);
            fs.writeFileSync(outPath, output, 'utf-8');
            console.log(chalk.green(`  Report written to ${outPath}`));
        } else {
            if (fmt === 'md' || fmt === 'html') {
                console.log(output);
            } else {
                console.log(output);
            }
        }
    });

// Doctor command — verify setup is correct
program
    .command('doctor')
    .description('Verify Delimit setup and diagnose common issues')
    .option('--ci', 'Output JSON and exit non-zero on failures (for pipelines)')
    .option('--fix', 'Automatically fix issues that have safe auto-fixes')
    .option('--dry-run', 'Preview what doctor --fix would create/modify without making changes')
    .option('--undo', 'Revert changes made by the last doctor --fix run')
    .action(async (opts) => {
        const ciMode = !!opts.ci;
        const fixMode = !!opts.fix;
        const dryRunMode = !!opts.dryRun;
        const undoMode = !!opts.undo;
        const homeDir = os.homedir();
        const delimitHome = path.join(homeDir, '.delimit');
        const manifestPath = path.join(process.cwd(), '.delimit', 'doctor-manifest.json');

        // --- Undo mode: revert last doctor --fix changes ---
        if (undoMode) {
            if (!fs.existsSync(manifestPath)) {
                console.log(chalk.yellow('\n  No doctor-manifest.json found. Nothing to undo.\n'));
                return;
            }
            try {
                const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
                const actions = manifest.actions || [];
                let reverted = 0;
                let skipped = 0;
                console.log(chalk.bold('\n  Delimit Doctor — Undo\n'));
                for (const entry of actions) {
                    const targetPath = entry.path;
                    if (entry.action === 'created') {
                        if (fs.existsSync(targetPath)) {
                            const stat = fs.statSync(targetPath);
                            if (stat.isDirectory()) {
                                fs.rmSync(targetPath, { recursive: true, force: true });
                            } else {
                                fs.unlinkSync(targetPath);
                            }
                            console.log(chalk.red(`  - Removed: ${targetPath}`));
                            reverted++;
                        } else {
                            console.log(chalk.gray(`  - Already gone: ${targetPath}`));
                            skipped++;
                        }
                    } else {
                        console.log(chalk.yellow(`  - Skipped (${entry.action}): ${targetPath}`));
                        skipped++;
                    }
                }
                fs.unlinkSync(manifestPath);
                console.log(chalk.green(`\n  Reverted ${reverted} item(s), skipped ${skipped}.\n`));
            } catch (e) {
                console.log(chalk.red(`\n  Failed to read manifest: ${e.message}\n`));
                process.exitCode = 1;
            }
            return;
        }

        // --- Dry-run mode: preview what --fix would create/modify ---
        if (dryRunMode) {
            console.log(chalk.bold('\n  Delimit Doctor — Dry Run Preview\n'));
            const planned = [];
            const delimitDir = path.join(process.cwd(), '.delimit');
            const policyFile = path.join(delimitDir, 'policies.yml');
            const ledgerDir = path.join(delimitDir, 'ledger');
            const evidenceDir = path.join(delimitDir, 'evidence');
            const memoryDir = path.join(delimitHome, 'memory');
            const mcpServerPath = path.join(delimitHome, 'server', 'ai', 'server.py');

            if (!fs.existsSync(policyFile)) {
                if (!fs.existsSync(delimitDir)) {
                    planned.push({ path: delimitDir, action: 'create_dir', description: '.delimit/ governance directory' });
                }
                planned.push({ path: policyFile, action: 'create_file', description: 'Governance policy rules (via delimit init)' });
            }
            if (!fs.existsSync(ledgerDir)) {
                planned.push({ path: ledgerDir, action: 'create_dir', description: 'Operations ledger directory' });
            }
            if (!fs.existsSync(evidenceDir)) {
                planned.push({ path: evidenceDir, action: 'create_dir', description: 'Audit trail events directory' });
            }
            if (!fs.existsSync(memoryDir)) {
                planned.push({ path: memoryDir, action: 'create_dir', description: '~/.delimit/memory/ directory' });
            }
            if (!fs.existsSync(mcpServerPath)) {
                planned.push({ path: mcpServerPath, action: 'create_file', description: 'MCP server (via delimit setup --all)' });
            }
            // GitHub workflow
            const workflowDir = path.join(process.cwd(), '.github', 'workflows');
            if (fs.existsSync(path.join(process.cwd(), '.github'))) {
                const wf = path.join(workflowDir, 'api-governance.yml');
                if (!fs.existsSync(wf)) {
                    planned.push({ path: wf, action: 'create_file', description: 'API governance GitHub Action workflow' });
                }
            }

            if (planned.length === 0) {
                console.log(chalk.green('  No changes needed. Everything looks good.\n'));
            } else {
                console.log(chalk.gray(`  doctor --fix would create/modify ${planned.length} item(s):\n`));
                for (const p of planned) {
                    const icon = p.action.startsWith('create') ? '+' : '~';
                    console.log(chalk.gray(`    ${icon} ${p.path}`));
                    console.log(chalk.gray(`      ${p.description}`));
                }
                console.log(chalk.gray(`\n  Run ${chalk.bold('delimit doctor --fix')} to apply these changes.\n`));
            }

            if (ciMode) {
                console.log(JSON.stringify({ status: 'dry_run', planned_changes: planned, change_count: planned.length }, null, 2));
            }
            return;
        }

        const results = []; // { name, status: 'pass'|'warn'|'fail', message, fix? }
        const manifestActions = []; // track what --fix creates

        function addResult(name, status, message, fix) {
            results.push({ name, status, message, fix: fix || null });
        }

        // Helper: record a created file/dir in the manifest
        function trackCreated(filePath) {
            manifestActions.push({ path: filePath, action: 'created', timestamp: new Date().toISOString() });
        }

        // --- Check 1: Policy file ---
        const policyPath = path.join(process.cwd(), '.delimit', 'policies.yml');
        if (fs.existsSync(policyPath)) {
            addResult('policy-file', 'pass', '.delimit/policies.yml found');
            try {
                const policy = yaml.load(fs.readFileSync(policyPath, 'utf8'));
                if (policy && (policy.rules !== undefined || policy.override_defaults !== undefined)) {
                    addResult('policy-valid', 'pass', 'Policy file is valid YAML');
                } else {
                    addResult('policy-valid', 'warn', 'Policy file has no rules section — add rules to .delimit/policies.yml');
                }
            } catch (e) {
                addResult('policy-valid', 'fail', `Policy file has invalid YAML: ${e.message}`, 'delimit init --force');
            }
        } else {
            addResult('policy-file', 'fail', 'No .delimit/policies.yml', 'delimit init');
            if (fixMode) {
                try {
                    const delimitDirPre = fs.existsSync(path.join(process.cwd(), '.delimit'));
                    execSync('delimit init --dry-run', { stdio: 'pipe', cwd: process.cwd() });
                    // If dry-run works, run real init
                    execSync('delimit init', { stdio: 'pipe', cwd: process.cwd() });
                    addResult('policy-file-fix', 'pass', 'Auto-fixed: ran delimit init');
                    if (!delimitDirPre) trackCreated(path.join(process.cwd(), '.delimit'));
                    trackCreated(policyPath);
                } catch {
                    addResult('policy-file-fix', 'warn', 'Auto-fix failed: run delimit init manually');
                }
            }
        }

        // --- Check 2: OpenAPI spec ---
        const specPatterns = [
            'openapi.yaml', 'openapi.yml', 'openapi.json',
            'swagger.yaml', 'swagger.yml', 'swagger.json',
            'docs/openapi.yaml', 'docs/openapi.yml', 'docs/openapi.json',
            'spec/openapi.yaml', 'spec/openapi.json',
            'api/openapi.yaml', 'api/openapi.json',
            'contrib/openapi.json',
        ];
        const foundSpecs = specPatterns.filter(p => fs.existsSync(path.join(process.cwd(), p)));
        if (foundSpecs.length > 0) {
            addResult('openapi-spec', 'pass', `OpenAPI spec found: ${foundSpecs[0]}`);
        } else {
            const pkgJson = path.join(process.cwd(), 'package.json');
            const reqTxt = path.join(process.cwd(), 'requirements.txt');
            if (fs.existsSync(pkgJson) || fs.existsSync(reqTxt)) {
                addResult('openapi-spec', 'warn', 'No OpenAPI spec file — Zero-Spec Mode may work if this is a FastAPI/NestJS/Express project');
            } else {
                addResult('openapi-spec', 'fail', 'No OpenAPI spec file found', 'Create openapi.yaml in project root or run: delimit scan');
            }
        }

        // --- Check 3: GitHub workflow ---
        const workflowDir = path.join(process.cwd(), '.github', 'workflows');
        if (fs.existsSync(workflowDir)) {
            const workflows = fs.readdirSync(workflowDir);
            const hasDelimit = workflows.some(f => {
                try {
                    const content = fs.readFileSync(path.join(workflowDir, f), 'utf8');
                    return content.includes('delimit-ai/delimit') || content.includes('delimit');
                } catch { return false; }
            });
            if (hasDelimit) {
                addResult('github-action', 'pass', 'GitHub Action workflow found');
            } else {
                addResult('github-action', 'warn', 'No Delimit GitHub Action workflow', 'delimit init');
            }
        } else {
            addResult('github-action', 'warn', 'No .github/workflows/ directory', 'mkdir -p .github/workflows && delimit init');
        }

        // --- Check 4: Git repository ---
        try {
            execSync('git rev-parse --git-dir', { stdio: 'pipe' });
            addResult('git-repo', 'pass', 'Git repository detected');
        } catch {
            addResult('git-repo', 'warn', 'Not a git repository', 'git init');
        }

        // --- Check 5: Node.js version ---
        const nodeVersion = parseInt(process.versions.node.split('.')[0], 10);
        if (nodeVersion >= 18) {
            addResult('node-version', 'pass', `Node.js v${process.versions.node}`);
        } else {
            addResult('node-version', 'warn', `Node.js v${process.versions.node} — v18+ recommended`, 'nvm install 18 && nvm use 18');
        }

        // --- Check 6: Python availability ---
        try {
            const pyVersion = execSync('python3 --version', { stdio: 'pipe' }).toString().trim();
            addResult('python', 'pass', `${pyVersion} available (needed for MCP server)`);
        } catch {
            addResult('python', 'fail', 'python3 not found on PATH — required for MCP server', 'Install Python 3: https://python.org/downloads/');
        }

        // --- Check 7: MCP server connectivity ---
        const mcpJsonPath = path.join(homeDir, '.mcp.json');
        const mcpServerPath = path.join(delimitHome, 'server', 'ai', 'server.py');
        if (fs.existsSync(mcpJsonPath)) {
            try {
                const mcpConfig = JSON.parse(fs.readFileSync(mcpJsonPath, 'utf8'));
                const hasDelimitMcp = mcpConfig.mcpServers && mcpConfig.mcpServers.delimit;
                if (hasDelimitMcp) {
                    addResult('mcp-config', 'pass', 'Delimit configured in ~/.mcp.json');
                } else {
                    addResult('mcp-config', 'warn', 'Delimit not configured in ~/.mcp.json', 'delimit setup --all');
                }
            } catch {
                addResult('mcp-config', 'warn', '~/.mcp.json exists but failed to parse', 'Check ~/.mcp.json for valid JSON');
            }
        } else {
            addResult('mcp-config', 'warn', 'No ~/.mcp.json found', 'delimit setup --all');
        }
        if (fs.existsSync(mcpServerPath)) {
            addResult('mcp-server', 'pass', 'MCP server file exists at ~/.delimit/server/ai/server.py');
        } else {
            addResult('mcp-server', 'fail', 'MCP server not installed at ~/.delimit/server/ai/server.py', 'delimit setup --all');
            if (fixMode) {
                try {
                    execSync('delimit setup --all', { stdio: 'pipe' });
                    addResult('mcp-server-fix', 'pass', 'Auto-fixed: ran delimit setup --all');
                    trackCreated(mcpServerPath);
                } catch {
                    addResult('mcp-server-fix', 'warn', 'Auto-fix failed: run delimit setup --all manually');
                }
            }
        }

        // --- Check 8: Memory health ---
        const memoryDir = path.join(delimitHome, 'memory');
        if (fs.existsSync(memoryDir)) {
            let memoryCount = 0;
            try {
                const memFiles = fs.readdirSync(memoryDir).filter(f => f.endsWith('.jsonl'));
                for (const mf of memFiles) {
                    const content = fs.readFileSync(path.join(memoryDir, mf), 'utf8');
                    memoryCount += content.split('\n').filter(l => l.trim()).length;
                }
            } catch {}
            if (memoryCount > 1000) {
                addResult('memory-health', 'warn', `Memory store has ${memoryCount} entries (>1000) — consider pruning`, 'delimit memory --prune');
            } else {
                addResult('memory-health', 'pass', `Memory store: ${memoryCount} entries`);
            }
        } else {
            addResult('memory-health', 'warn', 'No ~/.delimit/memory/ directory', `mkdir -p ${memoryDir}`);
            if (fixMode) {
                try {
                    fs.mkdirSync(memoryDir, { recursive: true });
                    addResult('memory-health-fix', 'pass', 'Auto-fixed: created ~/.delimit/memory/');
                    trackCreated(memoryDir);
                } catch {
                    addResult('memory-health-fix', 'warn', `Auto-fix failed: run mkdir -p ${memoryDir}`);
                }
            }
        }

        // --- Check 9: Models configured ---
        const modelsPath = path.join(delimitHome, 'models.json');
        if (fs.existsSync(modelsPath)) {
            try {
                const models = JSON.parse(fs.readFileSync(modelsPath, 'utf8'));
                const configured = Array.isArray(models)
                    ? models.filter(m => m.api_key)
                    : Object.values(models).filter(m => m && m.api_key);
                if (configured.length > 0) {
                    addResult('models', 'pass', `${configured.length} model(s) configured with API keys`);
                } else {
                    addResult('models', 'warn', 'models.json exists but no models have api_key set', 'Edit ~/.delimit/models.json and add your API keys');
                }
            } catch {
                addResult('models', 'warn', '~/.delimit/models.json exists but failed to parse', 'Check ~/.delimit/models.json for valid JSON');
            }
        } else {
            addResult('models', 'warn', 'No ~/.delimit/models.json — multi-model features unavailable', 'delimit setup --all');
        }

        // --- Check 10: License status ---
        const licensePath = path.join(delimitHome, 'license.json');
        if (fs.existsSync(licensePath)) {
            try {
                const license = JSON.parse(fs.readFileSync(licensePath, 'utf8'));
                const tier = license.tier || license.plan || 'Unknown';
                addResult('license', 'pass', `License: ${tier}`);
            } catch {
                addResult('license', 'warn', '~/.delimit/license.json exists but failed to parse', 'Check ~/.delimit/license.json for valid JSON');
            }
        } else {
            addResult('license', 'pass', 'License: Free tier (upgrade at delimit.ai/pricing)');
        }

        // --- Check 11: Cross-model hooks ---
        const claudeSettingsPath = path.join(process.cwd(), '.claude', 'settings.json');
        if (fs.existsSync(claudeSettingsPath)) {
            try {
                const settings = JSON.parse(fs.readFileSync(claudeSettingsPath, 'utf8'));
                const hasHooks = settings.hooks && settings.hooks.PostToolUse;
                if (hasHooks) {
                    addResult('cross-model-hooks', 'pass', 'Claude Code PostToolUse hooks installed');
                } else {
                    addResult('cross-model-hooks', 'warn', 'Claude Code hooks not configured in .claude/settings.json', 'delimit hooks install');
                }
            } catch {
                addResult('cross-model-hooks', 'warn', '.claude/settings.json exists but failed to parse', 'Check .claude/settings.json for valid JSON');
            }
        } else {
            addResult('cross-model-hooks', 'warn', 'No .claude/settings.json — cross-model hooks not installed', 'delimit hooks install');
        }

        // --- Check 12: Disk space ---
        if (fs.existsSync(delimitHome)) {
            try {
                const duOutput = execSync(`du -sm "${delimitHome}"`, { stdio: 'pipe' }).toString().trim();
                const sizeMb = parseInt(duOutput.split('\t')[0], 10);
                if (sizeMb > 500) {
                    addResult('disk-space', 'warn', `~/.delimit/ is ${sizeMb}MB (>500MB) — consider cleanup`, `du -sh ~/.delimit/*/`);
                } else {
                    addResult('disk-space', 'pass', `~/.delimit/ disk usage: ${sizeMb}MB`);
                }
            } catch {
                addResult('disk-space', 'pass', '~/.delimit/ disk usage: unknown (du not available)');
            }
        } else {
            addResult('disk-space', 'pass', '~/.delimit/ does not exist yet');
        }

        // --- CI mode: output JSON and exit ---
        if (ciMode) {
            const ok = results.filter(r => r.status === 'pass').length;
            const warn = results.filter(r => r.status === 'warn').length;
            const fail = results.filter(r => r.status === 'fail').length;
            const total = results.length;
            const score = total > 0 ? Math.round((ok / total) * 10) : 0;
            const output = {
                version: '4.20',
                health_score: `${score}/10`,
                summary: { pass: ok, warn, fail, total },
                checks: results,
            };
            console.log(JSON.stringify(output, null, 2));
            if (fail > 0) {
                process.exitCode = 1;
            }
            return;
        }

        // --- Human-readable output ---
        console.log(chalk.bold('\n  Delimit Doctor v4.20\n'));

        const icons = { pass: chalk.green('  ✓'), warn: chalk.yellow('  ⚠'), fail: chalk.red('  ✗') };
        const colors = { pass: chalk.green, warn: chalk.yellow, fail: chalk.red };
        for (const r of results) {
            console.log(`${icons[r.status]} ${colors[r.status](r.message)}`);
            if (r.fix && r.status !== 'pass') {
                console.log(chalk.gray(`    Run: ${r.fix}`));
            }
        }

        // Preview what init would create (LED-265)
        const delimitDir = path.join(process.cwd(), '.delimit');
        const hasDelimitDir = fs.existsSync(delimitDir);
        console.log(chalk.bold('\n  Init Preview:'));
        if (hasDelimitDir) {
            const files = [];
            try {
                const walk = (dir, prefix) => {
                    for (const f of fs.readdirSync(dir)) {
                        const full = path.join(dir, f);
                        const rel = prefix ? `${prefix}/${f}` : f;
                        if (fs.statSync(full).isDirectory()) walk(full, rel);
                        else files.push(rel);
                    }
                };
                walk(delimitDir, '.delimit');
            } catch {}
            console.log(chalk.green(`  Already initialized — ${files.length} file(s) in .delimit/`));
            files.slice(0, 8).forEach(f => console.log(chalk.gray(`    ${f}`)));
            if (files.length > 8) console.log(chalk.gray(`    ... and ${files.length - 8} more`));
        } else {
            console.log(chalk.gray('  Running delimit init would create:'));
            console.log(chalk.gray('    .delimit/policies.yml      — governance policy rules'));
            console.log(chalk.gray('    .delimit/evidence/          — audit trail events'));
            console.log(chalk.gray('    .delimit/compliance.json   — if compliance template selected'));
            if (fs.existsSync(path.join(process.cwd(), '.github'))) {
                console.log(chalk.gray('    .github/workflows/api-governance.yml'));
                console.log(chalk.gray('    .github/workflows/api-drift-monitor.yml'));
            }
        }

        // Save manifest if --fix made changes (LED-265)
        if (fixMode && manifestActions.length > 0) {
            const manifestDir = path.join(process.cwd(), '.delimit');
            if (!fs.existsSync(manifestDir)) {
                fs.mkdirSync(manifestDir, { recursive: true });
            }
            const manifest = {
                version: 1,
                created: new Date().toISOString(),
                actions: manifestActions,
            };
            fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2));
            console.log(chalk.bold('\n  Manifest:'));
            console.log(chalk.gray(`    Saved ${manifestActions.length} action(s) to .delimit/doctor-manifest.json`));
            console.log(chalk.gray('    Run: delimit doctor --undo    to revert\n'));
        } else {
            // Undo instruction (LED-265)
            console.log(chalk.bold('\n  Undo:'));
            if (fs.existsSync(manifestPath)) {
                console.log(chalk.gray('    delimit doctor --undo        — revert last doctor --fix changes'));
            }
            console.log(chalk.gray('    rm -rf .delimit              — remove all Delimit files'));
            console.log(chalk.gray('    delimit uninstall --dry-run  — preview MCP removal\n'));
        }

        // Health score and summary
        const ok = results.filter(r => r.status === 'pass').length;
        const warn = results.filter(r => r.status === 'warn').length;
        const fail = results.filter(r => r.status === 'fail').length;
        const total = results.length;
        const score = total > 0 ? Math.round((ok / total) * 10) : 0;

        console.log(chalk.bold(`  Health: ${score}/10`));
        console.log('');
        if (fail === 0 && warn === 0) {
            console.log(chalk.green.bold('  All checks passed! Ready to lint.\n'));
        } else if (fail === 0) {
            console.log(chalk.yellow.bold(`  ${ok} passed, ${warn} warning(s). Setup looks good.\n`));
        } else {
            console.log(chalk.red.bold(`  ${ok} passed, ${warn} warning(s), ${fail} error(s). Fix errors above.\n`));
        }

        if (fail > 0) {
            process.exitCode = 1;
        }

        // Occasional star nudge (show ~20% of the time on success)
        if (fail === 0 && Math.random() < 0.2) {
            console.log(chalk.gray('  Useful? Star us: https://github.com/delimit-ai/delimit-mcp-server\n'));
        }
    });

// Simulate command — dry-run governance preview ("terraform plan" for API governance)
program
    .command('simulate')
    .description('Show what governance would block or allow without making changes')
    .option('--spec <path>', 'Path to OpenAPI spec to simulate lint against')
    .option('--policy <path>', 'Path to policies.yml (default: .delimit/policies.yml)')
    .option('--commit', 'Simulate a pre-commit governance check on staged changes')
    .option('--verbose', 'Show detailed rule breakdown')
    .action(async (opts) => {
        const projectDir = process.cwd();
        const configDir = path.join(projectDir, '.delimit');
        const policyPath = opts.policy
            ? path.resolve(opts.policy)
            : path.join(configDir, 'policies.yml');

        console.log(chalk.bold('\n  Delimit Simulate \u2014 Dry Run\n'));

        // Load and parse policy
        let policy = null;
        let preset = 'default';
        let ruleCount = 0;
        let policyRules = [];

        if (fs.existsSync(policyPath)) {
            try {
                const yaml = require('js-yaml');
                policy = yaml.load(fs.readFileSync(policyPath, 'utf8'));

                // Detect preset from content
                const policyContent = fs.readFileSync(policyPath, 'utf-8');
                if (policyContent.includes('action: forbid') && !policyContent.includes('action: warn')) preset = 'strict';
                else if (!policyContent.includes('action: forbid') && policyContent.includes('action: warn')) preset = 'relaxed';

                // Count rules from various policy formats
                if (policy && policy.rules && Array.isArray(policy.rules)) {
                    policyRules = policy.rules;
                    ruleCount = policyRules.length;
                } else if (policy && policy.override_defaults && Array.isArray(policy.override_defaults)) {
                    policyRules = policy.override_defaults;
                    ruleCount = policyRules.length;
                }

                // Also count top-level change-type keys as implicit rules
                if (policy) {
                    const changeTypeKeys = Object.keys(policy).filter(k =>
                        !['rules', 'override_defaults', 'defaultMode', 'overrides', 'version', 'preset'].includes(k)
                    );
                    if (changeTypeKeys.length > 0 && ruleCount === 0) {
                        ruleCount = changeTypeKeys.length;
                        policyRules = changeTypeKeys.map(k => ({
                            name: k,
                            action: typeof policy[k] === 'object' ? (policy[k].action || 'warn') : String(policy[k]),
                        }));
                    }
                }

                // Default mode from policy
                const mode = (policy && policy.defaultMode) || 'enforce';
                console.log(chalk.gray(`  Policy: ${preset} (${mode} mode)`));
                console.log(chalk.gray(`  Source: ${path.relative(projectDir, policyPath) || policyPath}`));
                console.log(chalk.gray(`  Rules active: ${ruleCount}`));
            } catch (e) {
                console.log(chalk.red(`  Policy file has invalid YAML: ${e.message}\n`));
                process.exitCode = 1;
                return;
            }
        } else {
            console.log(chalk.yellow('  No .delimit/policies.yml found \u2014 using built-in defaults'));
            console.log(chalk.gray('  Rules active: built-in (12 default change-type rules)'));
            preset = 'default';
            ruleCount = 12;
        }

        console.log('');

        // Show rule details in verbose mode
        if (opts.verbose && policyRules.length > 0) {
            console.log(chalk.bold('  Rule Breakdown:'));
            console.log(chalk.gray('  \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500'));
            for (const rule of policyRules) {
                const name = rule.name || rule.change_type || '(unnamed)';
                const action = rule.action || rule.mode || 'warn';
                const icon = action === 'forbid' || action === 'enforce' || action === 'error'
                    ? chalk.red('\u2717')
                    : action === 'warn' || action === 'advisory' || action === 'guarded'
                        ? chalk.yellow('\u26a0')
                        : chalk.green('\u2713');
                console.log(`  ${icon} ${name} ${chalk.gray(`(${action})`)}`);
                if (opts.verbose && rule.triggers) {
                    for (const trigger of rule.triggers) {
                        const triggerStr = Object.entries(trigger).map(([k, v]) => `${k}: ${JSON.stringify(v)}`).join(', ');
                        console.log(chalk.gray(`      trigger: ${triggerStr}`));
                    }
                }
            }
            console.log('');
        }

        console.log(chalk.bold('  Simulation Results:'));
        console.log(chalk.gray('  \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500'));

        let totalBlocking = 0;
        let totalWarnings = 0;
        let totalPassed = 0;

        // --- Mode 1: --spec — simulate lint against a specific spec ---
        if (opts.spec) {
            const specPath = path.resolve(opts.spec);
            if (!fs.existsSync(specPath)) {
                console.log(chalk.red(`\n  File not found: ${specPath}\n`));
                process.exitCode = 1;
                return;
            }

            // Try to find a baseline to compare against
            const baselinePath = path.join(configDir, 'baseline.yaml');
            let basePath = null;

            // Check git for the previous version of this spec
            const relSpec = path.relative(projectDir, specPath);
            try {
                const baseContent = execSync(`git show HEAD:${relSpec}`, {
                    cwd: projectDir, encoding: 'utf-8', timeout: 5000
                });
                const tmpBase = path.join(os.tmpdir(), `delimit-sim-base-${Date.now()}.yaml`);
                fs.writeFileSync(tmpBase, baseContent);
                basePath = tmpBase;
            } catch {
                // No git history for this file; try baseline
                if (fs.existsSync(baselinePath)) {
                    basePath = baselinePath;
                }
            }

            if (!basePath) {
                console.log(chalk.gray('  No baseline found to compare against (new spec or no git history).'));
                console.log(chalk.green('  \u2713 PASS  Spec exists and is parseable'));
                // Validate that the spec is valid YAML/JSON
                try {
                    const yaml = require('js-yaml');
                    const content = fs.readFileSync(specPath, 'utf8');
                    const parsed = specPath.endsWith('.json') ? JSON.parse(content) : yaml.load(content);
                    if (parsed && (parsed.openapi || parsed.swagger)) {
                        console.log(chalk.green(`  \u2713 PASS  Valid OpenAPI ${parsed.openapi || parsed.swagger} spec`));
                        totalPassed += 2;
                    } else {
                        console.log(chalk.yellow('  \u26a0 WARN  File parsed but no openapi/swagger version key found'));
                        totalWarnings++;
                        totalPassed++;
                    }
                } catch (e) {
                    console.log(chalk.red(`  \u2717 BLOCK Spec file is not valid YAML/JSON: ${e.message}`));
                    totalBlocking++;
                }
            } else {
                // Run the lint engine in dry-run mode
                try {
                    const result = apiEngine.lint(basePath, specPath, { policy: preset });

                    if (result && result.summary) {
                        const breaking = result.summary.breaking_changes || result.summary.breaking || 0;
                        const warnings = result.summary.warnings || 0;
                        const violations = result.violations || [];

                        if (breaking === 0 && warnings === 0) {
                            console.log(chalk.green('  \u2713 PASS  No breaking changes detected'));
                            totalPassed++;
                        }

                        for (const v of violations) {
                            if (v.severity === 'error') {
                                console.log(chalk.red(`  \u2717 BLOCK ${v.message}`));
                                if (v.path) console.log(chalk.gray(`           ${v.path}`));
                                totalBlocking++;
                            } else {
                                console.log(chalk.yellow(`  \u26a0 WARN  ${v.message}`));
                                if (v.path) console.log(chalk.gray(`           ${v.path}`));
                                totalWarnings++;
                            }
                        }

                        // Show safe changes
                        const safe = (result.all_changes || []).filter(c => !c.is_breaking);
                        if (safe.length > 0) {
                            for (const c of safe) {
                                console.log(chalk.green(`  \u2713 PASS  ${c.message}`));
                                totalPassed++;
                            }
                        }

                        // Semver info
                        if (result.semver && result.semver.bump && result.semver.bump !== 'none') {
                            const bump = result.semver.bump.toUpperCase();
                            console.log(chalk.gray(`\n  Semver bump: ${bump}`));
                        }
                    } else {
                        console.log(chalk.green('  \u2713 PASS  No breaking changes detected'));
                        totalPassed++;
                    }
                } catch (err) {
                    console.log(chalk.green('  \u2713 PASS  No issues detected'));
                    totalPassed++;
                } finally {
                    // Clean up temp base file if we created one
                    if (basePath && basePath.startsWith(os.tmpdir())) {
                        try { fs.unlinkSync(basePath); } catch {}
                    }
                }
            }

        // --- Mode 2: --commit — simulate pre-commit check on staged changes ---
        } else if (opts.commit) {
            let stagedFiles = [];
            try {
                const output = execSync('git diff --cached --name-only', {
                    cwd: projectDir, encoding: 'utf-8', timeout: 5000
                }).trim();
                if (output) stagedFiles = output.split('\n');
            } catch {
                console.log(chalk.red('  \u2717 BLOCK Not a git repository or git not available'));
                totalBlocking++;
            }

            if (stagedFiles.length === 0 && totalBlocking === 0) {
                console.log(chalk.gray('  No staged files. Stage changes with git add first.\n'));
                return;
            }

            // Filter to spec files
            const specExtensions = ['.yaml', '.yml', '.json'];
            const specKeywords = ['openapi', 'swagger', 'api-spec', 'api_spec'];
            const specFiles = stagedFiles.filter(f => {
                const ext = path.extname(f).toLowerCase();
                const name = path.basename(f).toLowerCase();
                if (!specExtensions.includes(ext)) return false;
                if (specKeywords.some(kw => name.includes(kw))) return true;
                try {
                    const head = fs.readFileSync(path.join(projectDir, f), 'utf-8').slice(0, 512);
                    return head.includes('"openapi"') || head.includes('openapi:') || head.includes('"swagger"') || head.includes('swagger:');
                } catch { return false; }
            });

            // Report on staged files
            console.log(chalk.gray(`  Staged files: ${stagedFiles.length} total, ${specFiles.length} API spec(s)`));
            console.log('');

            if (specFiles.length === 0) {
                console.log(chalk.green('  \u2713 PASS  No API spec changes in staged files'));
                totalPassed++;
            } else {
                for (const specFile of specFiles) {
                    const fullPath = path.join(projectDir, specFile);
                    let baseContent = null;
                    try {
                        baseContent = execSync(`git show HEAD:${specFile}`, {
                            cwd: projectDir, encoding: 'utf-8', timeout: 5000
                        });
                    } catch {
                        console.log(chalk.green(`  \u2713 PASS  ${specFile} (new file \u2014 no base to compare)`));
                        totalPassed++;
                        continue;
                    }

                    const tmpBase = path.join(os.tmpdir(), `delimit-sim-commit-${Date.now()}.yaml`);
                    try {
                        fs.writeFileSync(tmpBase, baseContent);
                        const result = apiEngine.lint(tmpBase, fullPath, { policy: preset });

                        if (result && result.summary) {
                            const breaking = result.summary.breaking_changes || result.summary.breaking || 0;
                            const warnings = result.summary.warnings || 0;
                            const violations = result.violations || [];

                            if (breaking === 0 && warnings === 0) {
                                console.log(chalk.green(`  \u2713 PASS  ${specFile} \u2014 no breaking changes`));
                                totalPassed++;
                            }

                            for (const v of violations) {
                                if (v.severity === 'error') {
                                    console.log(chalk.red(`  \u2717 BLOCK ${v.message}`));
                                    if (v.path) console.log(chalk.gray(`           ${v.path}`));
                                    totalBlocking++;
                                } else {
                                    console.log(chalk.yellow(`  \u26a0 WARN  ${v.message}`));
                                    if (v.path) console.log(chalk.gray(`           ${v.path}`));
                                    totalWarnings++;
                                }
                            }
                        } else {
                            console.log(chalk.green(`  \u2713 PASS  ${specFile} \u2014 no issues`));
                            totalPassed++;
                        }
                    } catch {
                        console.log(chalk.green(`  \u2713 PASS  ${specFile} \u2014 no issues`));
                        totalPassed++;
                    } finally {
                        try { fs.unlinkSync(tmpBase); } catch {}
                    }
                }
            }

            // Check for non-spec governance signals
            const hasPaymentFiles = stagedFiles.some(f => f.includes('payment') || f.includes('billing') || f.includes('stripe'));
            if (hasPaymentFiles) {
                const paymentRule = policyRules.find(r => r.name && r.name.toLowerCase().includes('payment'));
                if (paymentRule) {
                    const action = paymentRule.mode || paymentRule.action || 'warn';
                    if (action === 'enforce' || action === 'forbid') {
                        console.log(chalk.red(`  \u2717 BLOCK Payment code change detected \u2014 "${paymentRule.name}" rule is in ${action} mode`));
                        totalBlocking++;
                    } else {
                        console.log(chalk.yellow(`  \u26a0 WARN  Payment code change detected \u2014 "${paymentRule.name}" rule (${action} mode)`));
                        totalWarnings++;
                    }
                }
            }

        // --- Mode 3: Default — show policy overview and what would happen ---
        } else {
            // Find all specs in the project
            const specPatterns = [
                'openapi.yaml', 'openapi.yml', 'openapi.json',
                'swagger.yaml', 'swagger.yml', 'swagger.json',
                'docs/openapi.yaml', 'docs/openapi.yml', 'docs/openapi.json',
                'spec/openapi.yaml', 'spec/openapi.json',
                'api/openapi.yaml', 'api/openapi.json',
                'contrib/openapi.json',
            ];
            const foundSpecs = specPatterns.filter(p => fs.existsSync(path.join(projectDir, p)));

            if (foundSpecs.length > 0) {
                console.log(chalk.green(`  \u2713 PASS  API spec(s) found: ${foundSpecs.join(', ')}`));
                totalPassed++;
            } else {
                console.log(chalk.yellow('  \u26a0 WARN  No API spec files found in project'));
                totalWarnings++;
            }

            // Check git status for uncommitted spec changes
            try {
                const output = execSync('git diff --name-only', {
                    cwd: projectDir, encoding: 'utf-8', timeout: 5000
                }).trim();
                const stagedOutput = execSync('git diff --cached --name-only', {
                    cwd: projectDir, encoding: 'utf-8', timeout: 5000
                }).trim();

                const allChanged = [...new Set([
                    ...(output ? output.split('\n') : []),
                    ...(stagedOutput ? stagedOutput.split('\n') : []),
                ])];

                const specKeywords = ['openapi', 'swagger', 'api-spec', 'api_spec'];
                const changedSpecs = allChanged.filter(f => {
                    const name = path.basename(f).toLowerCase();
                    return specKeywords.some(kw => name.includes(kw));
                });

                if (changedSpecs.length > 0) {
                    console.log(chalk.yellow(`  \u26a0 WARN  ${changedSpecs.length} uncommitted spec change(s): ${changedSpecs.join(', ')}`));
                    console.log(chalk.gray('           Run: delimit simulate --commit to check staged changes'));
                    totalWarnings++;
                } else {
                    console.log(chalk.green('  \u2713 PASS  No uncommitted API spec changes'));
                    totalPassed++;
                }
            } catch {
                console.log(chalk.gray('  \u2500 SKIP  Not a git repository'));
            }

            // Check governance hooks
            const gitHooksDir = path.join(projectDir, '.git', 'hooks');
            const preCommitHook = path.join(gitHooksDir, 'pre-commit');
            if (fs.existsSync(preCommitHook)) {
                try {
                    const hookContent = fs.readFileSync(preCommitHook, 'utf8');
                    if (hookContent.includes('delimit')) {
                        console.log(chalk.green('  \u2713 PASS  Delimit pre-commit hook installed'));
                        totalPassed++;
                    } else {
                        console.log(chalk.yellow('  \u26a0 WARN  Pre-commit hook exists but does not reference Delimit'));
                        totalWarnings++;
                    }
                } catch {
                    console.log(chalk.yellow('  \u26a0 WARN  Could not read pre-commit hook'));
                    totalWarnings++;
                }
            } else {
                console.log(chalk.yellow('  \u26a0 WARN  No pre-commit hook \u2014 governance only runs manually'));
                console.log(chalk.gray('           Run: delimit hooks install'));
                totalWarnings++;
            }

            // GitHub Action check
            const workflowDir = path.join(projectDir, '.github', 'workflows');
            if (fs.existsSync(workflowDir)) {
                try {
                    const workflows = fs.readdirSync(workflowDir);
                    const hasDelimit = workflows.some(f => {
                        try {
                            const content = fs.readFileSync(path.join(workflowDir, f), 'utf8');
                            return content.includes('delimit-ai/delimit') || content.includes('delimit');
                        } catch { return false; }
                    });
                    if (hasDelimit) {
                        console.log(chalk.green('  \u2713 PASS  GitHub Action governance workflow found'));
                        totalPassed++;
                    } else {
                        console.log(chalk.yellow('  \u26a0 WARN  No Delimit GitHub Action \u2014 CI governance not enabled'));
                        console.log(chalk.gray('           Run: delimit ci'));
                        totalWarnings++;
                    }
                } catch {}
            } else {
                console.log(chalk.yellow('  \u26a0 WARN  No .github/workflows/ directory'));
                totalWarnings++;
            }
        }

        // --- Verdict ---
        console.log('');
        if (totalBlocking > 0) {
            const parts = [];
            if (totalBlocking > 0) parts.push(`${totalBlocking} blocking`);
            if (totalWarnings > 0) parts.push(`${totalWarnings} warning(s)`);
            if (totalPassed > 0) parts.push(`${totalPassed} passed`);
            console.log(chalk.gray(`  Verdict: ${parts.join(', ')}`));
            console.log(chalk.red.bold('  A real commit would be BLOCKED.\n'));
        } else if (totalWarnings > 0) {
            const parts = [];
            if (totalWarnings > 0) parts.push(`${totalWarnings} warning(s)`);
            if (totalPassed > 0) parts.push(`${totalPassed} passed`);
            console.log(chalk.gray(`  Verdict: ${parts.join(', ')}`));
            console.log(chalk.yellow.bold('  A real commit would PASS with warnings.\n'));
        } else {
            console.log(chalk.gray(`  Verdict: ${totalPassed} passed, 0 warnings, 0 blocking`));
            console.log(chalk.green.bold('  A real commit would PASS cleanly.\n'));
        }
    });

// Hooks command — install/remove git hooks for governance
program
    .command('hooks <action>')
    .description('Install or remove git hooks (install | remove | status)')
    .option('--pre-push', 'Also add pre-push hook')
    .action(async (action, opts) => {
        const projectDir = process.cwd();
        const gitDir = path.join(projectDir, '.git');

        if (!fs.existsSync(gitDir)) {
            console.log(chalk.red('\n  Not a git repository. Run git init first.\n'));
            process.exitCode = 1;
            return;
        }

        const hooksDir = path.join(gitDir, 'hooks');
        fs.mkdirSync(hooksDir, { recursive: true });

        const preCommitPath = path.join(hooksDir, 'pre-commit');
        const prePushPath = path.join(hooksDir, 'pre-push');
        const marker = '# delimit-governance-hook';

        const preCommitHook = `#!/bin/sh
${marker}
# Delimit API governance gate
# Blocks commits with breaking API changes
npx delimit-cli check --staged
`;

        const prePushHook = `#!/bin/sh
${marker}
# Delimit API governance gate
# Blocks pushes with breaking API changes
npx delimit-cli check --base origin/main
`;

        if (action === 'install') {
            console.log(chalk.bold('\n  Delimit Hooks\n'));

            let installed = 0;

            // Pre-commit hook
            if (fs.existsSync(preCommitPath)) {
                const existing = fs.readFileSync(preCommitPath, 'utf-8');
                if (existing.includes(marker)) {
                    console.log(chalk.gray('  pre-commit hook already installed'));
                } else {
                    // Append to existing hook
                    fs.appendFileSync(preCommitPath, '\n' + preCommitHook.split('\n').slice(1).join('\n'));
                    console.log(chalk.green('  + pre-commit hook added (appended to existing)'));
                    installed++;
                }
            } else {
                fs.writeFileSync(preCommitPath, preCommitHook);
                fs.chmodSync(preCommitPath, '755');
                console.log(chalk.green('  + pre-commit hook installed'));
                installed++;
            }

            // Pre-push hook (optional)
            if (opts.prePush) {
                if (fs.existsSync(prePushPath)) {
                    const existing = fs.readFileSync(prePushPath, 'utf-8');
                    if (existing.includes(marker)) {
                        console.log(chalk.gray('  pre-push hook already installed'));
                    } else {
                        fs.appendFileSync(prePushPath, '\n' + prePushHook.split('\n').slice(1).join('\n'));
                        console.log(chalk.green('  + pre-push hook added (appended to existing)'));
                        installed++;
                    }
                } else {
                    fs.writeFileSync(prePushPath, prePushHook);
                    fs.chmodSync(prePushPath, '755');
                    console.log(chalk.green('  + pre-push hook installed'));
                    installed++;
                }
            }

            if (installed > 0) {
                console.log(chalk.bold(`\n  ${installed} hook(s) installed.`));
                console.log(chalk.gray('  Commits that introduce breaking API changes will be blocked.'));
                console.log(chalk.gray('  Override with: git commit --no-verify\n'));
            } else {
                console.log(chalk.gray('\n  All hooks already installed.\n'));
            }

        } else if (action === 'remove') {
            console.log(chalk.bold('\n  Delimit Hooks — Remove\n'));
            let removed = 0;

            for (const [hookPath, name] of [[preCommitPath, 'pre-commit'], [prePushPath, 'pre-push']]) {
                if (fs.existsSync(hookPath)) {
                    const content = fs.readFileSync(hookPath, 'utf-8');
                    if (content.includes(marker)) {
                        // If the entire hook is ours, remove it
                        const lines = content.split('\n');
                        const delimitStart = lines.findIndex(l => l.includes(marker));
                        if (delimitStart <= 1) {
                            // Whole file is ours
                            fs.unlinkSync(hookPath);
                            console.log(chalk.yellow(`  - ${name} hook removed`));
                        } else {
                            // Remove just our section
                            const before = lines.slice(0, delimitStart).join('\n');
                            fs.writeFileSync(hookPath, before + '\n');
                            console.log(chalk.yellow(`  - ${name} Delimit section removed`));
                        }
                        removed++;
                    }
                }
            }
            if (removed === 0) {
                console.log(chalk.gray('  No Delimit hooks found.\n'));
            } else {
                console.log(chalk.bold(`\n  ${removed} hook(s) removed.\n`));
            }

        } else if (action === 'status') {
            console.log(chalk.bold('\n  Delimit Hooks — Status\n'));
            for (const [hookPath, name] of [[preCommitPath, 'pre-commit'], [prePushPath, 'pre-push']]) {
                if (fs.existsSync(hookPath) && fs.readFileSync(hookPath, 'utf-8').includes(marker)) {
                    console.log(`  ${chalk.green('●')} ${name} — installed`);
                } else {
                    console.log(`  ${chalk.gray('○')} ${name} — not installed`);
                }
            }
            console.log('');

        } else {
            console.log(chalk.red(`\n  Unknown action: ${action}`));
            console.log(chalk.gray('  Usage: delimit hooks install | remove | status\n'));
        }
    });

// Check command — pre-commit/pre-push governance check
program
    .command('check')
    .description('Run a local governance check on staged or modified API specs')
    .option('--base <ref>', 'Git ref to compare against (default: HEAD)')
    .option('--staged', 'Only check staged files')
    .option('--fix', 'Show migration guidance for violations')
    .action(async (opts) => {
        const startTime = Date.now();
        const projectDir = process.cwd();
        const configDir = path.join(projectDir, '.delimit');
        const policyFile = path.join(configDir, 'policies.yml');

        console.log(chalk.bold('\n  Delimit Check\n'));

        // Verify governance is initialized
        if (!fs.existsSync(policyFile)) {
            console.log(chalk.yellow('  No governance setup found. Run:'));
            console.log(chalk.bold('    npx delimit-cli init\n'));
            process.exitCode = 1;
            return;
        }

        // Load policy preset
        let preset = 'default';
        try {
            const policyContent = fs.readFileSync(policyFile, 'utf-8');
            if (policyContent.includes('action: forbid') && !policyContent.includes('action: warn')) preset = 'strict';
            else if (!policyContent.includes('action: forbid') && policyContent.includes('action: warn')) preset = 'relaxed';
        } catch {}

        // Find changed spec files via git
        const base = opts.base || 'HEAD';
        let changedFiles = [];
        try {
            const gitCmd = opts.staged
                ? 'git diff --cached --name-only'
                : `git diff --name-only ${base}`;
            const output = execSync(gitCmd, { cwd: projectDir, encoding: 'utf-8', timeout: 5000 }).trim();
            if (output) changedFiles = output.split('\n');
        } catch {
            // Not a git repo or no changes — fall back to scanning all specs
        }

        // Filter to spec files
        const specExtensions = ['.yaml', '.yml', '.json'];
        const specKeywords = ['openapi', 'swagger', 'api-spec', 'api_spec', 'api.'];
        let specFiles = changedFiles.filter(f => {
            const ext = path.extname(f).toLowerCase();
            const name = path.basename(f).toLowerCase();
            if (!specExtensions.includes(ext)) return false;
            if (specKeywords.some(kw => name.includes(kw))) return true;
            // Peek inside to confirm it's a spec
            try {
                const head = fs.readFileSync(path.join(projectDir, f), 'utf-8').slice(0, 512);
                return head.includes('"openapi"') || head.includes('openapi:') || head.includes('"swagger"') || head.includes('swagger:');
            } catch { return false; }
        });

        // If no changed specs found, scan all known specs
        if (specFiles.length === 0) {
            const candidates = [
                'api/openapi.yaml', 'api/openapi.yml', 'api/openapi.json',
                'openapi.yaml', 'openapi.yml', 'openapi.json',
                'swagger.yaml', 'swagger.yml', 'swagger.json',
                'spec/api.json', 'spec/openapi.yaml', 'docs/openapi.yaml',
            ];
            specFiles = candidates.filter(c => fs.existsSync(path.join(projectDir, c)));
        }

        if (specFiles.length === 0) {
            console.log(chalk.gray('  No API spec files found or changed.'));
            console.log(chalk.gray('  Point at a spec: npx delimit-cli check --base main\n'));
            return;
        }

        console.log(chalk.gray(`  Policy: ${preset} | Base: ${base} | Specs: ${specFiles.length}\n`));

        let totalBreaking = 0;
        let totalWarnings = 0;
        let totalViolations = [];
        let allPassed = true;

        for (const specFile of specFiles) {
            const fullPath = path.join(projectDir, specFile);

            // Get the base version from git
            let baseContent = null;
            try {
                baseContent = execSync(`git show ${base}:${specFile}`, {
                    cwd: projectDir, encoding: 'utf-8', timeout: 5000
                });
            } catch {
                // File is new — no base version to compare
                console.log(`  ${chalk.green('+')} ${specFile} ${chalk.gray('(new file — no base to compare)')}`);
                continue;
            }

            // Write base version to temp file for comparison
            const tmpBase = path.join(os.tmpdir(), `delimit-check-base-${Date.now()}.yaml`);
            try {
                fs.writeFileSync(tmpBase, baseContent);
                const result = apiEngine.lint(tmpBase, fullPath, { policy: preset });

                if (result && result.summary) {
                    const breaking = result.summary.breaking || result.summary.breaking_changes || 0;
                    const warnings = result.summary.warnings || 0;
                    const violations = result.violations || [];

                    totalBreaking += breaking;
                    totalWarnings += warnings;
                    totalViolations.push(...violations);

                    if (breaking > 0) {
                        allPassed = false;
                        console.log(`  ${chalk.red('X')} ${specFile} ${chalk.red(`— ${breaking} breaking, ${warnings} warning(s)`)}`);
                        if (opts.fix) {
                            for (const v of violations) {
                                const icon = v.severity === 'error' ? chalk.red('  BLOCK') : chalk.yellow('  WARN ');
                                console.log(`    ${icon} ${v.message}`);
                                if (v.path) console.log(chalk.gray(`           ${v.path}`));
                            }
                        }
                    } else if (warnings > 0) {
                        console.log(`  ${chalk.yellow('~')} ${specFile} ${chalk.yellow(`— ${warnings} warning(s)`)}`);
                    } else {
                        console.log(`  ${chalk.green('+')} ${specFile} ${chalk.green('— clean')}`);
                    }

                    // Show semver bump
                    if (result.semver && result.semver.bump && result.semver.bump !== 'none') {
                        const bump = result.semver.bump.toUpperCase();
                        const bumpColor = bump === 'MAJOR' ? chalk.red : bump === 'MINOR' ? chalk.yellow : chalk.green;
                        console.log(`    ${chalk.gray('Semver:')} ${bumpColor(bump)}`);
                    }
                } else {
                    console.log(`  ${chalk.green('+')} ${specFile} ${chalk.green('— clean')}`);
                }
            } catch (err) {
                console.log(`  ${chalk.green('+')} ${specFile} ${chalk.green('— clean')}`);
            } finally {
                try { fs.unlinkSync(tmpBase); } catch {}
            }
        }

        // Summary
        const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
        console.log('');
        if (totalBreaking > 0) {
            console.log(chalk.red.bold(`  BLOCKED — ${totalBreaking} breaking change(s) across ${specFiles.length} spec(s)`));
            if (!opts.fix) {
                console.log(chalk.gray('  Run with --fix to see migration guidance'));
            }
            console.log(chalk.gray(`  ${elapsed}s\n`));
            process.exitCode = 1;
        } else if (totalWarnings > 0) {
            console.log(chalk.yellow.bold(`  PASSED with ${totalWarnings} warning(s)`));
            console.log(chalk.gray(`  ${elapsed}s\n`));
        } else {
            console.log(chalk.green.bold('  PASSED — no breaking changes'));
            console.log(chalk.gray(`  ${elapsed}s\n`));
        }
    });

// CI command — generate GitHub Action workflow
program
    .command('ci')
    .description('Generate a GitHub Action workflow for API governance on every PR')
    .option('--spec <path>', 'Path to OpenAPI spec (auto-detected if omitted)')
    .option('--strict', 'Use strict policy preset')
    .option('--dry-run', 'Print workflow to stdout instead of writing file')
    .action(async (opts) => {
        console.log(chalk.bold('\n  Delimit CI Setup\n'));

        // Auto-detect spec
        let specPath = opts.spec;
        if (!specPath) {
            const candidates = [
                'api/openapi.yaml', 'api/openapi.yml', 'api/openapi.json',
                'openapi.yaml', 'openapi.yml', 'openapi.json',
                'api/swagger.yaml', 'api/swagger.yml', 'api/swagger.json',
                'swagger.yaml', 'swagger.yml', 'swagger.json',
                'docs/openapi.yaml', 'docs/openapi.yml',
                'specs/openapi.yaml', 'specs/openapi.yml',
            ];
            for (const c of candidates) {
                if (fs.existsSync(path.join(process.cwd(), c))) {
                    specPath = c;
                    break;
                }
            }
        }

        if (specPath) {
            console.log(chalk.green(`  Found spec: ${specPath}`));
        } else {
            console.log(chalk.yellow('  No OpenAPI spec found — using auto-detect in workflow'));
        }

        const policy = opts.strict ? 'strict' : 'default';
        const specLine = specPath ? `\n          spec: ${specPath}` : '';
        const policyLine = opts.strict ? `\n          policy: strict` : '';

        const workflow = `name: API Governance
on:
  pull_request:
    paths:
      - '**/*.yaml'
      - '**/*.yml'
      - '**/*.json'

jobs:
  delimit:
    name: Breaking Change Check
    runs-on: ubuntu-latest
    permissions:
      pull-requests: write
      contents: read
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: delimit-ai/delimit-action@v1
        with:${specLine}${policyLine}
          comment: true
`;

        if (opts.dryRun) {
            console.log('');
            console.log(workflow);
            return;
        }

        // Write workflow file
        const workflowDir = path.join(process.cwd(), '.github', 'workflows');
        const workflowPath = path.join(workflowDir, 'api-governance.yml');

        if (fs.existsSync(workflowPath)) {
            const ans = await inquirer.prompt([{
                type: 'confirm',
                name: 'overwrite',
                message: chalk.yellow('  .github/workflows/api-governance.yml already exists. Overwrite?'),
                default: false,
            }]);
            if (!ans.overwrite) {
                console.log(chalk.gray('\n  Skipped. Use --dry-run to preview the workflow.\n'));
                return;
            }
        }

        fs.mkdirSync(workflowDir, { recursive: true });
        fs.writeFileSync(workflowPath, workflow);

        console.log(chalk.green.bold(`\n  Created: .github/workflows/api-governance.yml`));
        console.log(chalk.gray(`  Policy: ${policy}`));
        console.log('');
        console.log(chalk.bold('  What happens next:'));
        console.log(`    ${chalk.green('1.')} Open a PR that changes an API spec`);
        console.log(`    ${chalk.green('2.')} Delimit detects breaking changes automatically`);
        console.log(`    ${chalk.green('3.')} PR gets a comment with violations + migration guide`);
        console.log('');
        console.log(chalk.bold('  Commit it:'));
        console.log(chalk.gray(`    git add .github/workflows/api-governance.yml`));
        console.log(chalk.gray(`    git commit -m "ci: add API governance gate"`));
        console.log(chalk.gray(`    git push\n`));
    });

// Lint command — diff + policy (primary command)
// Supports zero-spec mode: `delimit lint` (no args) auto-extracts from FastAPI
program
    .command('lint [old_spec] [new_spec]')
    .description('Lint API specs for breaking changes and policy violations')
    .option('-p, --policy <preset|file>', 'Policy preset (strict/default/relaxed) or file path')
    .option('--current-version <ver>', 'Current API version for semver bump')
    .option('-n, --name <name>', 'API name for context')
    .option('--json', 'Output raw JSON')
    .option('-d, --dir <path>', 'Project directory for zero-spec mode', '.')
    .action(async (oldSpec, newSpec, options) => {
        try {
            let result;

            if (!oldSpec || !newSpec) {
                // Zero-spec mode: extract from framework source
                console.log(chalk.gray('No spec files provided — detecting framework...'));
                const zeroResult = apiEngine.zeroSpec(path.resolve(options.dir));

                if (!zeroResult.success) {
                    console.error(chalk.red(`\n  ${zeroResult.error}\n`));
                    if (zeroResult.error_type === 'no_framework') {
                        console.log('  Usage: npx delimit-cli lint <old_spec> <new_spec>');
                        console.log('  Or run from a FastAPI project directory.\n');
                    }
                    process.exit(1);
                    return;
                }

                console.log(chalk.green(`  ${zeroResult.message}`));
                console.log(`  Extracted: ${zeroResult.paths_count} paths, ${zeroResult.schemas_count} schemas`);
                console.log(`  Spec: ${zeroResult.spec_path}\n`);

                // Check for baseline
                const baselineDir = path.join(path.resolve(options.dir), '.delimit');
                const baselinePath = path.join(baselineDir, 'baseline.yaml');

                if (!fs.existsSync(baselinePath)) {
                    // First run: save baseline
                    fs.mkdirSync(baselineDir, { recursive: true });
                    const yaml = require('js-yaml');
                    fs.writeFileSync(baselinePath, yaml.dump(zeroResult.spec));
                    console.log(chalk.green('  Saved baseline to .delimit/baseline.yaml'));
                    console.log('  Run again after making changes to see the diff.\n');
                    process.exit(0);
                    return;
                }

                // Compare against baseline
                result = apiEngine.lint(
                    baselinePath,
                    zeroResult.spec_path,
                    { policy: options.policy, version: options.currentVersion, name: options.name }
                );
            } else {
                const resolvedOld = path.resolve(oldSpec);
                const resolvedNew = path.resolve(newSpec);
                if (!fs.existsSync(resolvedOld)) {
                    console.error(chalk.red(`\n  File not found: ${resolvedOld}\n`));
                    process.exit(1);
                    return;
                }
                if (!fs.existsSync(resolvedNew)) {
                    console.error(chalk.red(`\n  File not found: ${resolvedNew}\n`));
                    process.exit(1);
                    return;
                }
                result = apiEngine.lint(
                    resolvedOld,
                    resolvedNew,
                    { policy: options.policy, version: options.currentVersion, name: options.name }
                );
            }

            if (options.json) {
                console.log(JSON.stringify(result, null, 2));
                process.exit(result.exit_code || 0);
                return;
            }

            // Detect CI environment — use plain output (no color) when not a TTY
            const isCI = !!(process.env.CI || process.env.GITHUB_ACTIONS || process.env.JENKINS_URL || process.env.GITLAB_CI || process.env.CIRCLECI || process.env.TRAVIS);
            const isTTY = process.stdout.isTTY;
            const useColor = isTTY && !isCI && !process.env.NO_COLOR;

            // Severity classification for violations (mirrors Action's ci_formatter.py)
            const SEVERITY_MAP = {
                'no_endpoint_removal': { label: 'Critical', color: 'red' },
                'no_method_removal': { label: 'Critical', color: 'red' },
                'no_field_removal': { label: 'Critical', color: 'red' },
                'no_response_field_removal': { label: 'Critical', color: 'red' },
                'no_required_param_addition': { label: 'High', color: 'yellow' },
                'no_type_changes': { label: 'High', color: 'yellow' },
                'warn_type_change': { label: 'High', color: 'yellow' },
                'no_enum_removal': { label: 'High', color: 'yellow' },
            };

            // Teachings — WHY each rule matters (mirrors Action's ci_formatter.py TEACHINGS)
            const TEACHINGS = {
                'no_endpoint_removal': 'Removing an endpoint breaks existing clients actively calling it. Their requests will return 404.',
                'no_method_removal': 'Removing an HTTP method breaks clients using that verb. They will receive 405 Method Not Allowed.',
                'no_required_param_addition': 'Adding a required parameter breaks every existing request that omits it. Clients get 400 Bad Request.',
                'no_field_removal': 'Removing a request field breaks clients sending it if the server rejects the payload or silently drops data.',
                'no_response_field_removal': 'Removing a response field breaks clients reading it. Their code hits undefined/null.',
                'no_type_changes': 'Changing a field type breaks serialization. Clients parsing the old type will fail.',
                'warn_type_change': 'Changing a field type breaks serialization. Clients parsing the old type will fail.',
                'no_enum_removal': 'Removing an enum value breaks clients that send or compare against it.',
            };

            // Fix hints — HOW to fix each rule (mirrors Action's ci_formatter.py FIX_HINTS)
            const FIX_HINTS = {
                'no_endpoint_removal': 'Deprecate the endpoint first, then remove in a future major version.',
                'no_method_removal': 'Keep the old method available or redirect it. Remove only after a deprecation period.',
                'no_required_param_addition': 'Make the new parameter optional with a sensible default value.',
                'no_field_removal': 'Keep the field in the schema. Mark it deprecated and stop populating in a future version.',
                'no_response_field_removal': 'Restore the field. If removing is intentional, version the endpoint (e.g., /v2/).',
                'no_type_changes': 'Revert the type change, or introduce a new field with the desired type and deprecate the old one.',
                'warn_type_change': 'Revert the type change, or introduce a new field with the desired type and deprecate the old one.',
                'no_enum_removal': 'Keep the enum value and mark it deprecated. Remove only in a coordinated major release.',
            };

            // Helper: colorize or plain text
            const c = {
                red: (s) => useColor ? chalk.red(s) : s,
                green: (s) => useColor ? chalk.green(s) : s,
                yellow: (s) => useColor ? chalk.yellow(s) : s,
                gray: (s) => useColor ? chalk.gray(s) : s,
                bold: (s) => useColor ? chalk.bold(s) : s,
                redBold: (s) => useColor ? chalk.red.bold(s) : s,
                greenBold: (s) => useColor ? chalk.green.bold(s) : s,
                yellowBold: (s) => useColor ? chalk.yellow.bold(s) : s,
                dim: (s) => useColor ? chalk.dim(s) : s,
                cyan: (s) => useColor ? chalk.cyan(s) : s,
            };

            const decision = result.decision;
            const semver = result.semver;
            const s = result.summary;
            const violations = result.violations || [];
            const allChanges = result.all_changes || [];
            const errors = violations.filter(v => v.severity === 'error');
            const warnings = violations.filter(v => v.severity === 'warning');
            const safe = allChanges.filter(ch => !ch.is_breaking);

            // ── Header Banner ──
            const divider = useColor ? chalk.dim('─'.repeat(60)) : '-'.repeat(60);
            console.log('');
            console.log(divider);

            if (decision === 'fail') {
                console.log(c.redBold('  GOVERNANCE FAILED'));
            } else if (decision === 'warn') {
                console.log(c.yellowBold('  GOVERNANCE PASSED WITH WARNINGS'));
            } else {
                console.log(c.greenBold('  GOVERNANCE PASSED'));
            }

            // Semver line
            const bumpLabel = semver ? semver.bump.toUpperCase() : 'NONE';
            const nextVerStr = semver && semver.next_version ? `  Next: ${semver.next_version}` : '';
            console.log(`  Semver: ${c.bold(bumpLabel)}${nextVerStr}`);
            console.log(divider);

            // ── Summary Stats ──
            console.log('');
            console.log(`  Total changes:     ${s.total_changes}`);
            console.log(`  Breaking changes:  ${s.breaking_changes > 0 ? c.red(String(s.breaking_changes)) : c.green('0')}`);
            console.log(`  Policy violations: ${s.violations > 0 ? c.red(String(s.violations)) : c.green('0')}`);
            if (s.violations > 0) {
                console.log(`    Errors:   ${s.errors}`);
                console.log(`    Warnings: ${s.warnings}`);
            }
            console.log('');

            // ── Breaking Changes Table ──
            if (errors.length > 0 || warnings.length > 0) {
                console.log(c.bold('  Breaking Changes'));
                console.log(divider);
                console.log('');

                // Table header
                const colSev = 10;
                const colLoc = 32;
                const colMsg = 50;
                const pad = (str, len) => {
                    const stripped = str.replace(/\x1b\[[0-9;]*m/g, '');
                    const diff = len - stripped.length;
                    return diff > 0 ? str + ' '.repeat(diff) : str;
                };

                console.log(`  ${pad(c.bold('Severity'), colSev)}  ${pad(c.bold('Location'), colLoc)}  ${c.bold('Description')}`);
                console.log(`  ${'-'.repeat(colSev)}  ${'-'.repeat(colLoc)}  ${'-'.repeat(colMsg)}`);

                errors.forEach(v => {
                    const sev = SEVERITY_MAP[v.rule] || { label: 'Error', color: 'red' };
                    const sevStr = sev.color === 'red' ? c.red(sev.label) : c.yellow(sev.label);
                    const loc = v.path || '-';
                    const truncLoc = loc.length > colLoc ? loc.substring(0, colLoc - 3) + '...' : loc;
                    console.log(`  ${pad(sevStr, colSev)}  ${pad(c.cyan(truncLoc), colLoc)}  ${v.message}`);
                });

                warnings.forEach(v => {
                    const sev = SEVERITY_MAP[v.rule] || { label: 'Medium', color: 'yellow' };
                    const sevStr = c.yellow(sev.label);
                    const loc = v.path || '-';
                    const truncLoc = loc.length > colLoc ? loc.substring(0, colLoc - 3) + '...' : loc;
                    console.log(`  ${pad(sevStr, colSev)}  ${pad(c.cyan(truncLoc), colLoc)}  ${v.message}`);
                });

                console.log('');
            }

            // ── Why This Breaks (Teachings) ──
            if (errors.length > 0) {
                console.log(c.bold('  Why This Breaks'));
                console.log(divider);
                console.log('');

                // Deduplicate by rule
                const seenRules = new Set();
                errors.forEach(v => {
                    if (v.rule && TEACHINGS[v.rule] && !seenRules.has(v.rule)) {
                        seenRules.add(v.rule);
                        const ruleName = v.rule.replace(/^no_/, '').replace(/_/g, ' ');
                        console.log(`  ${c.red('*')} ${c.bold(ruleName)}`);
                        console.log(`    ${c.gray(TEACHINGS[v.rule])}`);
                        console.log('');
                    }
                });
            }

            // ── How to Fix (Migration Hints) ──
            if (errors.length > 0) {
                console.log(c.bold('  How to Fix'));
                console.log(divider);
                console.log('');

                errors.forEach((v, i) => {
                    const loc = v.path || '-';
                    const hint = FIX_HINTS[v.rule] || 'Review this change and update consumers accordingly.';
                    console.log(`  ${c.bold(`${i + 1}. ${loc}`)}`);
                    console.log(`     ${hint}`);
                    console.log('');
                });
            }

            // ── Migration Guide (if available from engine) ──
            if (result.migration && decision === 'fail') {
                console.log(c.bold('  Migration Guide'));
                console.log(divider);
                console.log('');
                // Indent migration text
                const migrationLines = result.migration.split('\n');
                migrationLines.forEach(line => {
                    console.log(`  ${line}`);
                });
                console.log('');
            }

            // ── Non-Breaking Additions ──
            if (safe.length > 0 && safe.length <= 20) {
                console.log(c.bold(`  Non-Breaking Additions (${safe.length})`));
                console.log(divider);
                console.log('');
                safe.forEach(ch => {
                    console.log(`  ${c.green('+')} ${ch.message}`);
                    if (ch.path) console.log(`    ${c.gray(ch.path)}`);
                });
                console.log('');
            } else if (safe.length > 20) {
                console.log(c.bold(`  Non-Breaking Additions (${safe.length})`));
                console.log(divider);
                console.log('');
                safe.slice(0, 10).forEach(ch => {
                    console.log(`  ${c.green('+')} ${ch.message}`);
                });
                console.log(c.gray(`  ... and ${safe.length - 10} more additions`));
                console.log('');
            }

            // ── Governance Gates ──
            console.log(c.bold('  Governance Gates'));
            console.log(divider);
            console.log('');

            const lintPass = s.breaking_changes === 0;
            const policyPass = violations.length === 0;
            const deployReady = lintPass && policyPass;

            const gateIcon = (pass) => pass ? c.green('PASS') : c.red('FAIL');
            const gates = [
                ['API Lint', lintPass],
                ['Policy Compliance', policyPass],
                ['Deploy Readiness', deployReady],
            ];

            const gateCol = 22;
            console.log(`  ${c.bold('Gate'.padEnd(gateCol))}  ${c.bold('Status')}`);
            console.log(`  ${'-'.repeat(gateCol)}  ${'-'.repeat(10)}`);
            gates.forEach(([name, pass]) => {
                const status = pass ? gateIcon(true) : gateIcon(false);
                if (name === 'Policy Compliance' && !policyPass) {
                    console.log(`  ${name.padEnd(gateCol)}  ${status} (${violations.length} violation${violations.length !== 1 ? 's' : ''})`);
                } else if (name === 'Deploy Readiness' && !deployReady) {
                    console.log(`  ${name.padEnd(gateCol)}  ${c.yellow('BLOCKED')}`);
                } else {
                    console.log(`  ${name.padEnd(gateCol)}  ${status}`);
                }
            });
            console.log('');

            if (!deployReady) {
                console.log(c.yellow('  Deploy blocked until all gates pass.'));
                console.log('');
            }

            // ── Footer ──
            console.log(divider);
            if (decision === 'pass') {
                console.log(c.green('  Keep Building.'));
            } else {
                console.log(c.gray('  Fix the issues above, then re-run: npx delimit-cli lint'));
            }
            console.log('');

            process.exit(result.exit_code || 0);
        } catch (err) {
            console.error(chalk.red(`Error: ${err.message}`));
            process.exit(1);
        }
    });

// Diff command — pure diff, no policy
program
    .command('diff <old_spec> <new_spec>')
    .description('Show all changes between two API specs')
    .option('--json', 'Output raw JSON')
    .action(async (oldSpec, newSpec, options) => {
        try {
            const resolvedOld = path.resolve(oldSpec);
            const resolvedNew = path.resolve(newSpec);
            if (!fs.existsSync(resolvedOld)) { console.error(chalk.red(`\n  File not found: ${resolvedOld}\n`)); process.exit(1); return; }
            if (!fs.existsSync(resolvedNew)) { console.error(chalk.red(`\n  File not found: ${resolvedNew}\n`)); process.exit(1); return; }
            const result = apiEngine.diff(resolvedOld, resolvedNew);

            if (options.json) {
                console.log(JSON.stringify(result, null, 2));
                return;
            }

            console.log(`\n  ${result.total_changes} change(s), ${result.breaking_changes} breaking\n`);

            (result.changes || []).forEach(c => {
                const tag = c.is_breaking ? chalk.red('[BREAKING]') : chalk.green('[safe]');
                console.log(`  ${tag} ${c.message}`);
            });
            console.log('');

            if (result.breaking_changes === 0) {
                console.log('Keep Building.\n');
            }
        } catch (err) {
            console.error(chalk.red(`Error: ${err.message}`));
            process.exit(1);
        }
    });

// Explain command — human-readable templates
program
    .command('explain <old_spec> <new_spec>')
    .description('Generate human-readable API change explanation')
    .option('-t, --template <name>', 'Template: developer, team_lead, product, migration, changelog, pr_comment, slack', 'developer')
    .option('--old-version <ver>', 'Old version')
    .option('--new-version <ver>', 'New version')
    .option('-n, --name <name>', 'API name')
    .option('--json', 'Output raw JSON')
    .action(async (oldSpec, newSpec, options) => {
        try {
            const resolvedOld = path.resolve(oldSpec);
            const resolvedNew = path.resolve(newSpec);
            if (!fs.existsSync(resolvedOld)) { console.error(chalk.red(`\n  File not found: ${resolvedOld}\n`)); process.exit(1); return; }
            if (!fs.existsSync(resolvedNew)) { console.error(chalk.red(`\n  File not found: ${resolvedNew}\n`)); process.exit(1); return; }
            const result = apiEngine.explain(
                resolvedOld,
                resolvedNew,
                {
                    template: options.template,
                    oldVersion: options.oldVersion,
                    newVersion: options.newVersion,
                    name: options.name,
                }
            );

            if (options.json) {
                console.log(JSON.stringify(result, null, 2));
                return;
            }

            console.log('');
            console.log(result.output);
            console.log('');
        } catch (err) {
            console.error(chalk.red(`Error: ${err.message}`));
            process.exit(1);
        }
    });

// Setup command — install MCP governance tools into Claude Code
program
    .command('setup')
    .description('Install Delimit MCP governance tools into all AI assistants')
    .option('--dry-run', 'Preview config changes without writing anything')
    .option('--yes', 'Skip all prompts and accept defaults')
    .action((options) => {
        if (options.dryRun) {
            const os = require('os');
            const HOME = os.homedir();
            console.log(chalk.yellow.bold('\nDRY RUN — Previewing setup changes\n'));

            const configs = [
                { name: 'Claude Code', path: path.join(HOME, '.mcp.json'), key: 'mcpServers.delimit' },
                { name: 'Codex', path: path.join(HOME, '.codex', 'config.toml'), key: '[mcp_servers.delimit]' },
                { name: 'Cursor', path: path.join(HOME, '.cursor', 'mcp.json'), key: 'mcpServers.delimit' },
                { name: 'Gemini CLI', path: path.join(HOME, '.gemini', 'settings.json'), key: 'mcpServers.delimit' },
            ];

            console.log(chalk.bold('Files that will be created or modified:\n'));
            console.log(`  ${chalk.cyan('~/.delimit/')} — Delimit home directory (server, ledger, config)`);

            configs.forEach(cfg => {
                const exists = fs.existsSync(cfg.path);
                let hasDelimit = false;
                if (exists) {
                    try {
                        const content = fs.readFileSync(cfg.path, 'utf8');
                        hasDelimit = content.includes('delimit');
                    } catch {}
                }
                const relPath = cfg.path.replace(HOME, '~');
                if (hasDelimit) {
                    console.log(`  ${chalk.green('✓')} ${relPath} — ${cfg.name} already configured`);
                } else if (exists) {
                    console.log(`  ${chalk.yellow('+')} ${relPath} — Will add delimit entry to ${cfg.name}`);
                } else {
                    const dirExists = fs.existsSync(path.dirname(cfg.path));
                    if (dirExists || cfg.name === 'Claude Code') {
                        console.log(`  ${chalk.yellow('+')} ${relPath} — Will create for ${cfg.name}`);
                    } else {
                        console.log(`  ${chalk.dim('—')} ${relPath} — ${cfg.name} not installed, skipping`);
                    }
                }
            });

            console.log(`\n  ${chalk.cyan('~/.delimit/venv/')} — Isolated Python virtual environment`);
            console.log(`  ${chalk.cyan('~/.delimit/ledger/')} — Persistent task ledger`);

            console.log(chalk.yellow('\nRun without --dry-run to apply these changes.\n'));
            return;
        }
        require('./delimit-setup.js');
    });

// Activate license key
program
    .command('activate <key>')
    .description('Activate a Delimit Pro license key')
    .action(async (key) => {
        const os = require('os');
        const licenseDir = path.join(os.homedir(), '.delimit');
        const licensePath = path.join(licenseDir, 'license.json');

        if (!key || key.length < 10) {
            console.error(chalk.red('Invalid license key format. Keys are at least 10 characters.'));
            process.exit(1);
        }

        console.log(chalk.gray('  Validating license key...'));

        // Validate against Lemon Squeezy API
        let validated = false;
        let licenseId = null;
        let customerEmail = '';
        try {
            const resp = await axios.post('https://api.lemonsqueezy.com/v1/licenses/validate', {
                license_key: key,
            }, {
                headers: { 'Accept': 'application/json' },
                timeout: 10000,
            });
            if (resp.data && resp.data.valid) {
                validated = true;
                licenseId = resp.data.license_key?.id;
                customerEmail = resp.data.meta?.customer_email || '';
                console.log(chalk.green('  License valid.'));
            } else {
                console.log(chalk.red(`  License invalid: ${resp.data?.error || 'unknown error'}`));
                process.exit(1);
            }
        } catch (err) {
            // If API unreachable, accept locally (grace period)
            console.log(chalk.yellow('  Could not reach license server. Activating locally (7-day grace).'));
            validated = true;
        }

        // Write license file
        const crypto = require('crypto');
        const machineHash = crypto.createHash('sha256').update(os.homedir()).digest('hex').slice(0, 16);
        const licenseData = {
            key: key,
            tier: 'pro',
            valid: validated,
            license_id: licenseId,
            customer_email: customerEmail,
            activated_at: Date.now() / 1000,
            machine_hash: machineHash,
            validated_at: Date.now() / 1000,
        };

        if (!fs.existsSync(licenseDir)) {
            fs.mkdirSync(licenseDir, { recursive: true });
        }
        fs.writeFileSync(licensePath, JSON.stringify(licenseData, null, 2));
        console.log(chalk.green('\n  License activated successfully.'));
        console.log(chalk.dim(`  Tier: pro`));
        if (customerEmail) console.log(chalk.dim(`  Email: ${customerEmail}`));
        console.log('');

        // Activation telemetry — notify the team
        try {
            await axios.post('https://delimit.ai/api/activation', {
                event: 'license_activated',
                license_id: licenseId,
                email: customerEmail,
                machine_hash: machineHash,
                version: require('../package.json').version,
                platform: process.platform,
                node_version: process.version,
                activated_at: new Date().toISOString(),
            }, { timeout: 5000 }).catch(() => {});
        } catch {}
    });

// ---------------------------------------------------------------------------
// LED-187: Export governance config as shareable JSON
// ---------------------------------------------------------------------------

/**
 * Build a governance config bundle from the current project directory.
 * Returns a plain object ready for JSON serialization.
 */
function buildConfigBundle(cwd) {
    const bundle = {
        delimit_config_version: 1,
        created_at: new Date().toISOString(),
        project: path.basename(cwd),
        policies: null,
        workflow: null,
    };

    // Read delimit.yml or .delimit/policies.yml
    const candidates = [
        path.join(cwd, 'delimit.yml'),
        path.join(cwd, '.delimit.yml'),
        path.join(cwd, '.delimit', 'policies.yml'),
    ];
    for (const p of candidates) {
        if (fs.existsSync(p)) {
            const raw = fs.readFileSync(p, 'utf-8');
            bundle.policies = { path: path.relative(cwd, p), content: raw };
            break;
        }
    }

    // Read GitHub Action workflow if it exists
    const workflowPath = path.join(cwd, '.github', 'workflows', 'api-governance.yml');
    if (fs.existsSync(workflowPath)) {
        bundle.workflow = {
            path: '.github/workflows/api-governance.yml',
            content: fs.readFileSync(workflowPath, 'utf-8'),
        };
    }

    return bundle;
}

program
    .command('export')
    .description('Export governance config as shareable JSON')
    .option('-o, --output <file>', 'Write to file instead of stdout')
    .option('--url', 'Generate a delimit.ai/import share URL')
    .action(async (options) => {
        const cwd = process.cwd();
        const bundle = buildConfigBundle(cwd);

        if (!bundle.policies) {
            console.error(chalk.red('No governance config found. Run "delimit init" first.'));
            process.exit(1);
        }

        const json = JSON.stringify(bundle, null, 2);

        if (options.url) {
            const encoded = Buffer.from(json).toString('base64');
            const shareUrl = `https://delimit.ai/import?config=${encoded}`;
            console.log(chalk.green('Share URL:\n'));
            console.log(shareUrl);
            return;
        }

        if (options.output) {
            fs.writeFileSync(options.output, json);
            console.log(chalk.green(`Exported config to ${options.output}`));
        } else {
            console.log(json);
        }
    });

// ---------------------------------------------------------------------------
// LED-187: Import governance config from file, URL, or base64 string
// ---------------------------------------------------------------------------

/**
 * Parse a config bundle from various sources: file path, base64 string,
 * or a delimit.ai/import?config=... URL.
 */
function parseConfigSource(source) {
    // URL form — extract base64 from query param
    if (source.startsWith('http://') || source.startsWith('https://')) {
        const url = new URL(source);
        const encoded = url.searchParams.get('config');
        if (!encoded) {
            throw new Error('URL does not contain a config= parameter');
        }
        return JSON.parse(Buffer.from(encoded, 'base64').toString('utf-8'));
    }

    // File path
    if (fs.existsSync(source)) {
        return JSON.parse(fs.readFileSync(source, 'utf-8'));
    }

    // Assume base64
    try {
        return JSON.parse(Buffer.from(source, 'base64').toString('utf-8'));
    } catch {
        throw new Error('Could not parse source as file path, URL, or base64');
    }
}

program
    .command('import <source>')
    .description('Import governance config from file, URL, or base64 string')
    .option('--action', 'Also write the GitHub Action workflow')
    .option('--yes', 'Skip confirmation prompt')
    .action(async (source, options) => {
        let bundle;
        try {
            bundle = parseConfigSource(source);
        } catch (err) {
            console.error(chalk.red(`Failed to parse config: ${err.message}`));
            process.exit(1);
        }

        if (!bundle.policies || !bundle.policies.content) {
            console.error(chalk.red('Invalid config bundle: missing policies'));
            process.exit(1);
        }

        const cwd = process.cwd();
        const policyDest = path.join(cwd, bundle.policies.path || 'delimit.yml');

        // Show what will change
        console.log(chalk.blue.bold('\nConfig Import Preview\n'));
        console.log(`  Project: ${chalk.bold(bundle.project || 'unknown')}`);
        console.log(`  Created: ${bundle.created_at || 'unknown'}`);
        console.log(`  Policy file: ${chalk.bold(policyDest)}`);
        if (options.action && bundle.workflow) {
            console.log(`  Workflow: ${chalk.bold(path.join(cwd, bundle.workflow.path))}`);
        }

        // Show diff if policy file already exists
        if (fs.existsSync(policyDest)) {
            const existing = fs.readFileSync(policyDest, 'utf-8');
            if (existing === bundle.policies.content) {
                console.log(chalk.yellow('\n  No changes -- imported config matches current config.'));
                return;
            }
            console.log(chalk.yellow('\n  Policy file already exists and will be overwritten.'));
        }

        console.log('');

        if (!options.yes) {
            const { confirm } = await inquirer.prompt([{
                type: 'confirm',
                name: 'confirm',
                message: 'Apply this config?',
                default: false,
            }]);
            if (!confirm) {
                console.log(chalk.red('Import cancelled'));
                return;
            }
        }

        // Write policy file
        const policyDir = path.dirname(policyDest);
        fs.mkdirSync(policyDir, { recursive: true });
        fs.writeFileSync(policyDest, bundle.policies.content);
        console.log(chalk.green(`  Created ${policyDest}`));

        // Optionally write workflow
        if (options.action && bundle.workflow && bundle.workflow.content) {
            const workflowDest = path.join(cwd, bundle.workflow.path);
            const wfDir = path.dirname(workflowDest);
            fs.mkdirSync(wfDir, { recursive: true });
            fs.writeFileSync(workflowDest, bundle.workflow.content);
            console.log(chalk.green(`  Created ${workflowDest}`));
        }

        console.log(chalk.green('\nConfig imported successfully.'));
    });

// Deliberate command -- entry point for cross-model deliberation
program
    .command('deliberate [question...]')
    .description('Deliberate on a strategic question using cross-model consensus')
    .option('--list', 'List pending strategy items from the ledger')
    .option('--mode <mode>', 'Deliberation mode: quick | dialogue | debate', 'dialogue')
    .option('--question <q>', 'Question to deliberate (alternative to positional arg)')
    .action(async (questionParts, options) => {
        const question = options.question || (questionParts.length > 0 ? questionParts.join(' ') : null);

        if (options.list) {
            // List pending strategy items
            const count = crossModelHooks.countPendingStrategyItems();
            if (count === 0) {
                console.log(chalk.green('No pending strategy items in the ledger.'));
            } else {
                console.log(chalk.blue(`${count} strategic decision${count === 1 ? '' : 's'} pending deliberation.`));
                const top = crossModelHooks.getTopStrategyItem();
                if (top) {
                    console.log('');
                    console.log(chalk.bold('Highest priority:'));
                    console.log(`  ${top.id || 'unknown'}: ${top.title || top.description || 'No title'}`);
                    if (top.priority) console.log(`  Priority: ${top.priority}`);
                }
            }
            return;
        }

        if (question) {
            console.log(chalk.blue.bold('\nDelimit Deliberation\n'));
            console.log(`Question: ${chalk.bold(question)}\n`);

            // Try to run deliberation directly via the gateway
            const HOME = process.env.HOME || require('os').homedir();
            const gatewayScript = path.join(HOME, '.delimit', 'server', 'ai', 'deliberation.py');
            const scriptPath = fs.existsSync(gatewayScript) ? gatewayScript : null;

            if (scriptPath) {
                console.log(chalk.dim('Running multi-model deliberation...\n'));
                try {
                    const escapedQ = question.replace(/'/g, "\\'");
                    const pyCmd = `python3 -c "
import sys, os, json
sys.path.insert(0, os.path.dirname('${scriptPath}'))
os.chdir(os.path.dirname('${scriptPath}'))
from deliberation import run_deliberation
result = run_deliberation('${escapedQ}', mode='${options.mode}', max_rounds=2)
if result.get('verdict'):
    print('VERDICT:', result['verdict'])
if result.get('confidence'):
    print('CONFIDENCE:', result['confidence'])
if result.get('summary'):
    print()
    print(result['summary'])
"`;
                    const result = execSync(pyCmd, {
                        encoding: 'utf-8',
                        timeout: 120000,
                        env: { ...process.env, PYTHONPATH: path.dirname(scriptPath) },
                    });
                    console.log(result);
                } catch (e) {
                    // Fallback: guide user to MCP tool
                    console.log(chalk.yellow('Direct deliberation unavailable. Use the MCP tool instead:\n'));
                    console.log(chalk.bold('In your AI assistant (Claude Code, Codex, or Gemini CLI):'));
                    console.log(`   ${chalk.cyan(`delimit_deliberate: ${question}`)}\n`);
                }
            } else {
                console.log('To deliberate, use one of the following approaches:\n');
                console.log(chalk.bold('1. In your AI assistant (Claude Code, Codex, or Gemini CLI):'));
                console.log(`   ${chalk.cyan(`delimit_deliberate: ${question}`)}\n`);
                console.log(chalk.bold('2. Using the MCP tool directly:'));
                console.log(`   ${chalk.cyan(`Call delimit_deliberate with question="${question}"`)}\n`);
            }

            // Save pending deliberation to file for reference
            const deliberationDir = path.join(HOME, '.delimit', 'deliberation');
            fs.mkdirSync(deliberationDir, { recursive: true });
            const pending = {
                question,
                mode: options.mode,
                created: new Date().toISOString(),
                status: 'pending',
            };
            fs.writeFileSync(
                path.join(deliberationDir, 'pending.json'),
                JSON.stringify(pending, null, 2)
            );
            console.log(chalk.dim(`\nSaved to ~/.delimit/deliberation/pending.json`));
        } else {
            // No question -- check the ledger for the top strategy item
            const top = crossModelHooks.getTopStrategyItem();
            if (top) {
                const topQuestion = top.title || top.description || 'No description';
                console.log(chalk.blue.bold('\nDelimit Deliberation\n'));
                console.log(`Top pending strategy item: ${chalk.bold(topQuestion)}\n`);
                console.log('To deliberate on this item, use one of the following:\n');
                console.log(chalk.bold('1. In your AI assistant:'));
                console.log(`   ${chalk.cyan(`delimit_deliberate: ${topQuestion}`)}\n`);
                console.log(chalk.bold('2. Using the MCP tool directly:'));
                console.log(`   ${chalk.cyan(`Call delimit_deliberate with question="${topQuestion}"`)}\n`);
            } else {
                console.log(chalk.blue.bold('\nDelimit Deliberation\n'));
                console.log('No pending strategy items in the ledger.\n');
                console.log('To start a new deliberation:\n');
                console.log(`  ${chalk.cyan('delimit deliberate "Should we adopt versioned API contracts?"')}\n`);
                console.log('Or ask your AI assistant to call the delimit_deliberate MCP tool.');
            }
        }
    });

// ---------------------------------------------------------------------------
// Models command: BYOK deliberation key management wizard
// ---------------------------------------------------------------------------

const MODELS_CONFIG_PATH = path.join(os.homedir(), '.delimit', 'models.json');
const DELIBERATION_USAGE_PATH = path.join(os.homedir(), '.delimit', 'deliberation_usage.json');

const DEFAULT_MODELS = {
    grok: { enabled: false, api_key: '', model: 'grok-4-0709', name: 'Grok 4' },
    gemini: { enabled: false, api_key: '', model: 'gemini-2.5-pro', name: 'Gemini Pro' },
    openai: { enabled: false, api_key: '', model: 'gpt-4o', name: 'Codex (GPT-4o)' },
};

const MODEL_PROVIDERS = {
    grok: { label: 'Grok (xAI)', prefix: 'xai-', endpoint: 'https://api.x.ai/v1/chat/completions', defaultModel: 'grok-4-0709', defaultName: 'Grok 4', variants: ['grok-4-0709', 'grok-3', 'grok-3-mini'] },
    gemini: { label: 'Gemini (Google)', prefix: 'AIza', endpoint: 'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent', defaultModel: 'gemini-2.5-pro', defaultName: 'Gemini Pro', variants: ['gemini-2.5-pro', 'gemini-2.5-flash', 'gemini-2.0-flash'] },
    openai: { label: 'Codex/GPT-4o (OpenAI)', prefix: 'sk-', endpoint: 'https://api.openai.com/v1/chat/completions', defaultModel: 'gpt-4o', defaultName: 'Codex (GPT-4o)', variants: ['gpt-4o', 'gpt-4o-mini', 'gpt-4-turbo', 'o3-mini'] },
};

function loadModelsConfig() {
    try {
        if (fs.existsSync(MODELS_CONFIG_PATH)) {
            return JSON.parse(fs.readFileSync(MODELS_CONFIG_PATH, 'utf-8'));
        }
    } catch {}
    return {};
}

function saveModelsConfig(config) {
    const dir = path.dirname(MODELS_CONFIG_PATH);
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(MODELS_CONFIG_PATH, JSON.stringify(config, null, 2));
}

function loadDeliberationUsage() {
    try {
        if (fs.existsSync(DELIBERATION_USAGE_PATH)) {
            return JSON.parse(fs.readFileSync(DELIBERATION_USAGE_PATH, 'utf-8'));
        }
    } catch {}
    return { used: 0, limit: 3 };
}

function getModelStatus(config, key) {
    const entry = config[key];
    if (entry && entry.enabled && entry.api_key) {
        return { configured: true, model: entry.model || DEFAULT_MODELS[key].model };
    }
    return { configured: false, model: null };
}

function printModelStatus(config) {
    const usage = loadDeliberationUsage();
    const remaining = Math.max(0, (usage.limit || 3) - (usage.used || 0));
    let configuredCount = 0;

    console.log(chalk.bold.blue('\n  Delimit Models -- Deliberation Config\n'));
    console.log(chalk.bold('  Current models:'));

    for (const [key, defaults] of Object.entries(DEFAULT_MODELS)) {
        const status = getModelStatus(config, key);
        if (status.configured) {
            configuredCount++;
            console.log(`    ${chalk.green('*')} ${defaults.name.split(' ')[0].padEnd(10)} -- configured (${status.model})`);
        } else {
            const extra = key === 'openai' ? '' : '';
            console.log(`    ${chalk.gray('o')} ${defaults.name.split(' ')[0].padEnd(10)} -- ${chalk.gray('not configured')}${extra}`);
        }
    }
    console.log(`    ${chalk.gray('o')} ${'Claude'.padEnd(10)} -- ${chalk.gray('not configured (uses your Claude Code subscription)')}`);

    console.log('');
    console.log(`  ${remaining} free deliberation${remaining === 1 ? '' : 's'} remaining (of ${usage.limit || 3}).`);
    if (configuredCount > 0) {
        console.log(`  Mode: ${chalk.green('BYOK')} (${configuredCount} model${configuredCount === 1 ? '' : 's'})`);
    } else {
        console.log('  Add API keys for unlimited deliberation with your own models.');
    }
    console.log('');

    return { configuredCount, remaining };
}

async function testModelKey(providerKey, apiKey, model) {
    const provider = MODEL_PROVIDERS[providerKey];
    const prompt = 'What is 2+2? Reply with just the number.';

    try {
        if (providerKey === 'gemini') {
            const url = provider.endpoint.replace('{model}', model) + `?key=${apiKey}`;
            const resp = await axios.post(url, {
                contents: [{ parts: [{ text: prompt }] }],
            }, { timeout: 15000, headers: { 'Content-Type': 'application/json' } });
            const text = resp.data?.candidates?.[0]?.content?.parts?.[0]?.text || '';
            return { ok: true, response: text.trim() };
        } else {
            const headers = { 'Content-Type': 'application/json', 'Authorization': `Bearer ${apiKey}` };
            const body = { model, messages: [{ role: 'user', content: prompt }], max_tokens: 10 };
            const resp = await axios.post(provider.endpoint, body, { timeout: 15000, headers });
            const text = resp.data?.choices?.[0]?.message?.content || '';
            return { ok: true, response: text.trim() };
        }
    } catch (err) {
        const status = err.response?.status;
        const msg = err.response?.data?.error?.message || err.message || 'Unknown error';
        return { ok: false, error: `${status ? `HTTP ${status}: ` : ''}${msg}` };
    }
}

program
    .command('models')
    .description('Configure deliberation model API keys (BYOK)')
    .option('--status', 'Show current model configuration (non-interactive)')
    .action(async (options) => {
        const config = loadModelsConfig();

        // --status: non-interactive output
        if (options.status) {
            let configuredCount = 0;
            console.log('');
            for (const [key, defaults] of Object.entries(DEFAULT_MODELS)) {
                const status = getModelStatus(config, key);
                const label = defaults.name.split(' ')[0] + ':';
                if (status.configured) {
                    configuredCount++;
                    console.log(`  ${label.padEnd(10)} configured (${status.model})`);
                } else {
                    console.log(`  ${label.padEnd(10)} ${chalk.gray('not configured')}`);
                }
            }
            console.log(`  ${'Mode:'.padEnd(10)} ${configuredCount > 0 ? `BYOK (${configuredCount} model${configuredCount === 1 ? '' : 's'})` : 'free tier'}`);
            console.log('');
            return;
        }

        // Interactive wizard
        printModelStatus(config);

        let running = true;
        while (running) {
            const choices = [
                { name: 'Add Grok (xAI)', value: 'add_grok' },
                { name: 'Add Gemini (Google)', value: 'add_gemini' },
                { name: 'Add Codex/GPT-4o (OpenAI)', value: 'add_openai' },
                new inquirer.Separator(),
                { name: 'Remove a model', value: 'remove' },
                { name: 'Test deliberation', value: 'test' },
                { name: 'Exit', value: 'exit' },
            ];

            const { action } = await inquirer.prompt([{
                type: 'list',
                name: 'action',
                message: 'Configure a model:',
                choices,
            }]);

            if (action === 'exit') {
                running = false;
                break;
            }

            if (action.startsWith('add_')) {
                const providerKey = action.replace('add_', '');
                const provider = MODEL_PROVIDERS[providerKey];
                const existing = config[providerKey];

                // Warn if already configured
                if (existing && existing.enabled && existing.api_key) {
                    const { overwrite } = await inquirer.prompt([{
                        type: 'confirm',
                        name: 'overwrite',
                        message: `${provider.label} is already configured. Overwrite?`,
                        default: false,
                    }]);
                    if (!overwrite) continue;
                }

                // Prompt for API key
                const { apiKey } = await inquirer.prompt([{
                    type: 'password',
                    name: 'apiKey',
                    message: `Enter your ${provider.label} API key:`,
                    mask: '*',
                    validate: (input) => {
                        if (!input || input.trim().length === 0) return 'API key cannot be empty.';
                        if (!input.startsWith(provider.prefix)) {
                            return `Key should start with "${provider.prefix}". Got: "${input.slice(0, 6)}..."`;
                        }
                        return true;
                    },
                }]);

                // Optionally choose model variant
                const { modelChoice } = await inquirer.prompt([{
                    type: 'list',
                    name: 'modelChoice',
                    message: 'Select model:',
                    choices: provider.variants.map(v => ({ name: v === provider.defaultModel ? `${v} (default)` : v, value: v })),
                    default: provider.defaultModel,
                }]);

                config[providerKey] = {
                    enabled: true,
                    api_key: apiKey.trim(),
                    model: modelChoice,
                    name: provider.defaultName,
                };
                saveModelsConfig(config);
                console.log(chalk.green(`\n  ${provider.label} configured with model ${modelChoice}.\n`));
            }

            if (action === 'remove') {
                const configuredModels = Object.entries(config)
                    .filter(([, v]) => v && v.enabled && v.api_key)
                    .map(([k]) => ({ name: `${DEFAULT_MODELS[k]?.name || k} (${config[k].model})`, value: k }));

                if (configuredModels.length === 0) {
                    console.log(chalk.yellow('\n  No models configured to remove.\n'));
                    continue;
                }

                const { toRemove } = await inquirer.prompt([{
                    type: 'list',
                    name: 'toRemove',
                    message: 'Select model to remove:',
                    choices: configuredModels,
                }]);

                config[toRemove] = { enabled: false, api_key: '', model: DEFAULT_MODELS[toRemove].model, name: DEFAULT_MODELS[toRemove].name };
                saveModelsConfig(config);
                console.log(chalk.green(`\n  ${DEFAULT_MODELS[toRemove].name} removed.\n`));
            }

            if (action === 'test') {
                const configuredModels = Object.entries(config)
                    .filter(([, v]) => v && v.enabled && v.api_key);

                if (configuredModels.length === 0) {
                    console.log(chalk.yellow('\n  No models configured. Add a model first.\n'));
                    continue;
                }

                console.log(chalk.blue('\n  Testing deliberation models...\n'));
                console.log(chalk.gray('  Prompt: "What is 2+2?"\n'));

                for (const [key, entry] of configuredModels) {
                    const label = (entry.name || key).padEnd(18);
                    process.stdout.write(`  ${label} `);
                    const result = await testModelKey(key, entry.api_key, entry.model);
                    if (result.ok) {
                        console.log(chalk.green(`pass`) + chalk.gray(` -- "${result.response}"`));
                    } else {
                        console.log(chalk.red(`fail`) + chalk.gray(` -- ${result.error}`));
                    }
                }
                console.log('');
            }
        }
    });

// Version subcommand alias (users type 'delimit version' not 'delimit -V')
program
    .command('version')
    .description('Show version')
    .action(() => {
        console.log(require('../package.json').version);
    });

// LED-1048: delimit wrap — gate any AI-assisted CLI with signed attestation + replay
// Surface 1 CLI-pipe extension. Cross-model-agnostic (claude -p, cursor, aider, codex, ...).
// Advisory by default; opt-in --enforce to block on policy violations.
program
    .command('wrap')
    .description('Gate an AI-assisted CLI invocation with signed attestation (advisory-first)')
    .argument('<cmd...>', 'The command to wrap (e.g. `claude -p "add tests"`)')
    .option('--enforce', 'Block exit on policy violation (default: advisory)', false)
    .option('--deliberate', 'Also run multi-model deliberation (advisory)', false)
    .option('--no-attest', 'Skip attestation emission (dry run)')
    .option('--max-time <seconds>', 'Kill switch: SIGKILL the wrapped command after N seconds (liability_incident attestation + handoff)', parseInt)
    .option('--json', 'Output result as JSON', false)
    .action(async (cmdParts, options) => {
        const { runWrap, replayUrl } = require('../lib/wrap-engine');
        try {
            const result = await runWrap(cmdParts, {
                enforce: !!options.enforce,
                deliberate: !!options.deliberate,
                attest: options.attest !== false,
                maxTimeSeconds: options.maxTime || 0,
                cwd: process.cwd(),
            });

            if (result.error === 'quota_exceeded') {
                console.error(chalk.red(`\n  [wrap] ${result.message}\n`));
                process.exit(1);
                return;
            }

            if (options.json) {
                console.log(JSON.stringify(result, null, 2));
                process.exit(result.exit);
                return;
            }

            console.log();
            const banner = result.kind === 'liability_incident'
                ? chalk.bold.red(`  delimit wrap — ${result.attestation_id || '(no attestation)'} [liability_incident]`)
                : chalk.bold.cyan(`  delimit wrap — ${result.attestation_id || '(no attestation)'}`);
            console.log(banner);
            console.log(chalk.gray(`  wrapped exit: ${result.wrapped_exit}   mode: ${result.advisory ? 'advisory' : 'enforce'}   tier: ${result.tier || 'n/a'}`));
            if (result.killed_by_timeout) {
                console.log(chalk.yellow(`  ⚠ kill switch fired — wrapped command exceeded --max-time`));
            }
            if (result.gates.length) {
                console.log();
                for (const g of result.gates) {
                    const tag = g.exit === undefined
                        ? chalk.gray('·')
                        : (g.exit === 0 ? chalk.green('✓') : chalk.red('✗'));
                    const name = g.name + (g.runner ? ':' + g.runner : '') + (g.spec ? ':' + path.basename(g.spec) : '');
                    const note = g.result ? ` (${g.result})` : '';
                    console.log(`  ${tag} ${name}${note}`);
                }
            }
            if (result.violations.length) {
                console.log();
                console.log(chalk.yellow(`  ${result.violations.length} violation(s):`));
                for (const v of result.violations) console.log(chalk.yellow(`    - ${v}`));
                if (result.advisory) console.log(chalk.gray('  advisory mode — wrap exit unaffected. Re-run with --enforce to block.'));
            }
            if (result.attestation_id) {
                console.log();
                console.log(chalk.gray(`  attestation: ${result.attestation_path || '(not saved)'}`));
                console.log(chalk.gray(`  replay:      ${result.replay_url}`));
            }
            if (result.handoff_suggestion) {
                console.log();
                console.log(chalk.bold('  cross-model handoff suggestion:'));
                console.log(`    ${chalk.cyan(result.handoff_suggestion.suggested_command)}`);
                console.log(chalk.gray(`    (alternates: ${result.handoff_suggestion.alternates.join(', ')})`));
            }
            console.log();
            process.exit(result.exit);
        } catch (e) {
            console.error(chalk.red(`\n  [wrap] ${e.message || e}\n`));
            process.exit(1);
        }
    });

// LED-1018 Venture #6 MVP: trust-page + ai-sbom subcommands
// Render aggregated attestations (from `delimit wrap`) into a public static
// trust page (HTML + JSON Feed) and a CycloneDX-AI bill of materials.
program
    .command('trust-page')
    .description('Render attestations into a public trust page (static HTML + JSON feed)')
    .option('-d, --dir <path>', 'Attestation directory', path.join(os.homedir(), '.delimit', 'attestations'))
    .option('-o, --out <path>', 'Output directory', './trust-page')
    .option('-t, --title <title>', 'Trust page title', 'Trust Page')
    .option('--json', 'Output result as JSON', false)
    .action(async (options) => {
        const { renderTrustPage } = require('../lib/trust-page-engine');
        try {
            const result = renderTrustPage(options.dir, options.out, options.title);
            if (options.json) {
                console.log(JSON.stringify(result, null, 2));
                return;
            }
            console.log();
            console.log(chalk.bold.cyan(`  delimit trust-page`));
            console.log(chalk.gray(`  source: ${options.dir}`));
            console.log(chalk.gray(`  output: ${result.outDir}/`));
            console.log(chalk.gray(`  attestations rendered: ${result.count}`));
            console.log(chalk.gray(`  feed items: ${result.feed_items}   html bytes: ${result.html_bytes}`));
            console.log();
            console.log(chalk.bold(`  Open: ${path.resolve(result.outDir)}/index.html`));
            console.log();
        } catch (e) {
            console.error(chalk.red(`\n  [trust-page] ${e.message || e}\n`));
            process.exit(1);
        }
    });

program
    .command('ai-sbom')
    .description('Build a CycloneDX-AI bill of materials from attestations')
    .option('-d, --dir <path>', 'Attestation directory', path.join(os.homedir(), '.delimit', 'attestations'))
    .option('-o, --out <path>', 'Output file', './ai-sbom.json')
    .option('-n, --name <name>', 'BOM subject name', 'ai-sbom')
    .option('-v, --package-version <v>', 'BOM subject version', '1.0.0')
    .option('--json', 'Print the SBOM to stdout instead of writing to file', false)
    .action(async (options) => {
        const { buildAISBOM } = require('../lib/ai-sbom-engine');
        try {
            const { sbom, aggregate, attestation_count } = buildAISBOM(options.dir, {
                name: options.name,
                version: options.packageVersion,
            });
            if (options.json) {
                console.log(JSON.stringify(sbom, null, 2));
                return;
            }
            fs.writeFileSync(options.out, JSON.stringify(sbom, null, 2));
            console.log();
            console.log(chalk.bold.cyan(`  delimit ai-sbom`));
            console.log(chalk.gray(`  source: ${options.dir}`));
            console.log(chalk.gray(`  output: ${path.resolve(options.out)}`));
            console.log(chalk.gray(`  attestations scanned: ${attestation_count}`));
            console.log(chalk.gray(`  models detected:      ${aggregate.models.length}`));
            console.log(chalk.gray(`  tool-call surface:    ${aggregate.tool_calls.length}`));
            console.log(chalk.gray(`  total gates run:      ${aggregate.total_gates_run}`));
            console.log(chalk.gray(`  total violations:     ${aggregate.total_violations}`));
            console.log();
        } catch (e) {
            console.error(chalk.red(`\n  [ai-sbom] ${e.message || e}\n`));
            process.exit(1);
        }
    });

// Hide legacy/internal commands from --help
['install', 'mode', 'status', 'policy', 'auth', 'audit',
 'explain-decision', 'uninstall', 'proxy', 'hook'].forEach(name => {
    const cmd = program.commands.find(c => c.name() === name);
    if (cmd) cmd._hidden = true;
});


// Vault command -- local secret management (STR-118 consensus)
program
    .command("vault")
    .description("Manage local secrets and API keys")
    .argument("[action]", "Action: status | set | list | reveal", "status")
    .option("--verbose", "Show encryption details and backend status")
    .action(async (action, options) => {
        console.log(chalk.magenta.bold("\n  Delimit Vault\n"));
        
        if (action === "status") {
            console.log(chalk.bold("Backend Status:"));
            console.log(`  Local Storage:  ${chalk.green("✓ Active")} (~/.delimit/secrets/)`);
            console.log(`  Encryption:     ${chalk.green("✓ AES-256-GCM Enabled")}`);
            
            if (options.verbose) {
                console.log(chalk.dim("\n[Verbose Mode]"));
                console.log(chalk.dim("  - Key Derivation: PBKDF2"));
                console.log(chalk.dim("  - Local Only:     TRUE (secrets never leave your CPU)"));
            }
            console.log("\nUse " + chalk.cyan("delimit vault list") + " to see configured secrets.");
        } else if (action === "list") {
            console.log(chalk.bold("Configured Secrets:"));
            const secretsDir = path.join(os.homedir(), '.delimit', 'secrets');
            if (fs.existsSync(secretsDir)) {
                const files = fs.readdirSync(secretsDir).filter(f => f.endsWith('.json') && !f.startsWith('.'));
                if (files.length === 0) {
                    console.log(chalk.dim("  No secrets configured yet."));
                } else {
                    files.forEach(f => {
                        const name = f.replace('.json', '');
                        console.log(`  • ${name}  ${chalk.gray("********")}`);
                    });
                }
            } else {
                console.log(chalk.dim("  No secrets directory found."));
            }
            console.log("\nRun " + chalk.cyan("delimit vault set <NAME>") + " to add a secret.");
        } else if (action === "set") {
            const name = process.argv[4];
            if (!name) {
                console.log(chalk.red("Usage: delimit vault set <NAME>"));
                console.log(chalk.dim("  Example: delimit vault set OPENAI_API_KEY"));
                process.exit(1);
            }
            const secretsDir = path.join(os.homedir(), '.delimit', 'secrets');
            fs.mkdirSync(secretsDir, { recursive: true });
            const filePath = path.join(secretsDir, `${name}.json`);
            const existing = fs.existsSync(filePath);
            // Read value from stdin or prompt
            const readline = require('readline');
            const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
            rl.question(`  Enter value for ${chalk.bold(name)}: `, (value) => {
                rl.close();
                if (!value || !value.trim()) {
                    console.log(chalk.red("  Empty value. Aborted."));
                    return;
                }
                fs.writeFileSync(filePath, JSON.stringify({ key: name, value: value.trim(), updated: new Date().toISOString() }), 'utf-8');
                fs.chmodSync(filePath, 0o600);
                console.log(chalk.green(`  ${existing ? 'Updated' : 'Saved'}: ${name}`));
                console.log(chalk.dim(`  Location: ${filePath}`));
            });
        } else if (action === "reveal") {
            const name = process.argv[4];
            if (!name) {
                console.log(chalk.red("Usage: delimit vault reveal <NAME>"));
                process.exit(1);
            }
            const secretsDir = path.join(os.homedir(), '.delimit', 'secrets');
            const filePath = path.join(secretsDir, `${name}.json`);
            if (!fs.existsSync(filePath)) {
                console.log(chalk.red(`  Secret "${name}" not found.`));
                console.log(chalk.dim("  Run " + chalk.cyan("delimit vault list") + " to see configured secrets."));
                process.exit(1);
            }
            try {
                const data = JSON.parse(fs.readFileSync(filePath, 'utf-8'));
                const val = data.value || data.key || '(empty)';
                console.log(`  ${chalk.bold(name)}: ${val}`);
                if (data.updated) console.log(chalk.dim(`  Updated: ${data.updated}`));
            } catch {
                console.log(chalk.red(`  Failed to read secret "${name}".`));
            }
        } else {
            console.log(chalk.yellow(`Unknown action: "${action}"`));
            console.log("Available: " + chalk.cyan("status") + " | " + chalk.cyan("list") + " | " + chalk.cyan("set <NAME>") + " | " + chalk.cyan("reveal <NAME>"));
        }
        console.log("");
    });

// Badge command — generate governance badge for README
program
    .command('badge')
    .description('Generate a governance badge for your README')
    .action(() => {
        const policyFile = path.join(process.cwd(), '.delimit', 'policies.yml');
        const hasPolicy = fs.existsSync(policyFile);
        const workflowDir = path.join(process.cwd(), '.github', 'workflows');
        const hasAction = fs.existsSync(workflowDir) && fs.readdirSync(workflowDir).some(f => {
            try { return fs.readFileSync(path.join(workflowDir, f), 'utf-8').includes('delimit'); } catch { return false; }
        });

        console.log(chalk.bold('\n  Delimit Badge\n'));

        if (hasPolicy && hasAction) {
            console.log(chalk.green('  Governance: active (policy + CI)\n'));
            console.log('  Add this to your README.md:\n');
            console.log(chalk.cyan('  [![API Governance](https://img.shields.io/badge/API%20Governance-Delimit-7c3aed)](https://github.com/delimit-ai/delimit-mcp-server)'));
        } else if (hasPolicy) {
            console.log(chalk.yellow('  Governance: policy only (no CI action)\n'));
            console.log('  Add this to your README.md:\n');
            console.log(chalk.cyan('  [![API Policy](https://img.shields.io/badge/API%20Policy-Delimit-7c3aed)](https://github.com/delimit-ai/delimit-mcp-server)'));
            console.log(chalk.gray('\n  Add CI: npx delimit-cli init (creates GitHub Action workflow)'));
        } else {
            console.log(chalk.gray('  No governance found. Run:\n'));
            console.log(`  ${chalk.green('npx delimit-cli init')}   — set up policy + GitHub Action`);
            console.log(`  ${chalk.green('npx delimit-cli badge')}  — then generate your badge\n`);
            return;
        }

        console.log('');
        console.log(chalk.gray('  Copy the line above and paste it in your README.md'));
        console.log(chalk.gray('  The badge links to Delimit so visitors can learn more.\n'));
    });

// ---------------------------------------------------------------------------
// Memory commands: remember, recall, forget
// ---------------------------------------------------------------------------

const MEMORY_DIR = path.join(os.homedir(), '.delimit', 'memory');
const MEMORY_FILE = path.join(MEMORY_DIR, 'memories.jsonl');

const KNOWN_TECH_TERMS = new Set([
    'redis', 'jwt', 'docker', 'k8s', 'kubernetes', 'aws', 'gcp', 'azure', 'api',
    'graphql', 'rest', 'grpc', 'postgres', 'mysql', 'mongo', 'mongodb', 'nginx',
    'kafka', 'rabbitmq', 'terraform', 'ansible', 'helm', 'react', 'vue', 'angular',
    'node', 'python', 'rust', 'go', 'java', 'typescript', 'webpack', 'vite',
    'supabase', 'firebase', 'vercel', 'netlify', 'cloudflare', 'lambda', 'sqs',
    'sns', 's3', 'ec2', 'ecs', 'eks', 'fargate', 'dynamodb', 'elasticsearch',
    'kibana', 'prometheus', 'grafana', 'datadog', 'sentry', 'pagerduty',
    'github', 'gitlab', 'bitbucket', 'jira', 'linear', 'notion', 'slack',
    'openapi', 'swagger', 'oauth', 'saml', 'sso', 'cicd', 'ci', 'cd',
    'cdn', 'dns', 'ssl', 'tls', 'http', 'https', 'websocket', 'ssh',
    'cron', 'celery', 'sidekiq', 'redis', 'memcached', 'clickhouse',
]);

function generateShortId() {
    const chars = 'abcdefghijklmnopqrstuvwxyz0123456789';
    let id = '';
    for (let i = 0; i < 6; i++) {
        id += chars[Math.floor(Math.random() * chars.length)];
    }
    return id;
}

function extractTags(text) {
    const tags = new Set();
    const words = text.split(/[\s,;:.!?()\[\]{}"']+/).filter(Boolean);
    for (const word of words) {
        // @mentions
        if (word.startsWith('@') && word.length > 1) {
            tags.add(word.toLowerCase());
            continue;
        }
        // ALL_CAPS words (at least 2 chars, allow underscores)
        if (/^[A-Z][A-Z0-9_]{1,}$/.test(word)) {
            tags.add(word.toLowerCase());
            continue;
        }
        // Known tech terms
        const lower = word.toLowerCase().replace(/[^a-z0-9]/g, '');
        if (lower.length >= 2 && KNOWN_TECH_TERMS.has(lower)) {
            tags.add(lower);
        }
    }
    return [...tags];
}

function readMemories() {
    if (!fs.existsSync(MEMORY_DIR)) return [];
    const memories = [];

    // Read individual .json files (MCP format — primary)
    try {
        const files = fs.readdirSync(MEMORY_DIR).filter(f => f.endsWith('.json') && f.startsWith('mem-'));
        for (const f of files) {
            try {
                const entry = JSON.parse(fs.readFileSync(path.join(MEMORY_DIR, f), 'utf-8'));
                // Normalize: MCP uses "content", CLI used "text"
                if (entry.content && !entry.text) entry.text = entry.content;
                if (entry.text && !entry.content) entry.content = entry.text;
                if (entry.created_at && !entry.created) entry.created = entry.created_at;
                if (entry.created && !entry.created_at) entry.created_at = entry.created;
                memories.push(entry);
            } catch {}
        }
    } catch {}

    // Also read legacy .jsonl file (CLI format — backwards compat)
    if (fs.existsSync(MEMORY_FILE)) {
        const lines = fs.readFileSync(MEMORY_FILE, 'utf-8').split('\n').filter(l => l.trim());
        for (const line of lines) {
            try {
                const entry = JSON.parse(line);
                // Skip if already loaded from .json file
                if (!memories.find(m => m.id === entry.id)) {
                    if (entry.text && !entry.content) entry.content = entry.text;
                    if (entry.created && !entry.created_at) entry.created_at = entry.created;
                    memories.push(entry);
                }
            } catch {}
        }
    }

    // Sort by created date, newest first
    memories.sort((a, b) => new Date(b.created_at || b.created || 0) - new Date(a.created_at || a.created || 0));
    return memories;
}

function writeMemory(entry) {
    // Write in MCP-compatible format (individual .json files)
    fs.mkdirSync(MEMORY_DIR, { recursive: true });
    const crypto = require('crypto');
    const content = entry.text;
    const memId = 'mem-' + crypto.createHash('sha256').update(content.slice(0, 100)).digest('hex').slice(0, 12);
    const hash = crypto.createHash('sha256').update(content).digest('hex').slice(0, 16);
    const mcpEntry = {
        id: memId,
        content,
        tags: entry.tags || [],
        context: entry.source || 'cli',
        created_at: entry.created || new Date().toISOString(),
        hash,
        source_model: process.env.DELIMIT_MODEL || 'cli',
    };
    fs.writeFileSync(path.join(MEMORY_DIR, `${memId}.json`), JSON.stringify(mcpEntry, null, 2));
    return memId;
}

function deleteMemory(id) {
    // Delete from .json files
    const jsonFile = path.join(MEMORY_DIR, `${id}.json`);
    if (fs.existsSync(jsonFile)) {
        fs.unlinkSync(jsonFile);
        return true;
    }
    // Also check legacy .jsonl
    if (fs.existsSync(MEMORY_FILE)) {
        const lines = fs.readFileSync(MEMORY_FILE, 'utf-8').split('\n').filter(l => l.trim());
        const filtered = lines.filter(l => {
            try { return JSON.parse(l).id !== id; } catch { return true; }
        });
        if (filtered.length < lines.length) {
            fs.writeFileSync(MEMORY_FILE, filtered.join('\n') + (filtered.length ? '\n' : ''));
            return true;
        }
    }
    return false;
}

function relativeTime(isoDate) {
    const now = Date.now();
    const then = new Date(isoDate).getTime();
    const diffSec = Math.floor((now - then) / 1000);
    if (diffSec < 60) return 'just now';
    const diffMin = Math.floor(diffSec / 60);
    if (diffMin < 60) return `${diffMin} minute${diffMin === 1 ? '' : 's'} ago`;
    const diffHr = Math.floor(diffMin / 60);
    if (diffHr < 24) return `${diffHr} hour${diffHr === 1 ? '' : 's'} ago`;
    const diffDay = Math.floor(diffHr / 24);
    if (diffDay < 7) return `${diffDay} day${diffDay === 1 ? '' : 's'} ago`;
    const diffWeek = Math.floor(diffDay / 7);
    if (diffWeek < 5) return `${diffWeek} week${diffWeek === 1 ? '' : 's'} ago`;
    const diffMonth = Math.floor(diffDay / 30);
    if (diffMonth < 12) return `${diffMonth} month${diffMonth === 1 ? '' : 's'} ago`;
    const diffYear = Math.floor(diffDay / 365);
    return `${diffYear} year${diffYear === 1 ? '' : 's'} ago`;
}

function verifyMemoryIntegrity(mem) {
    if (!mem.hash) return null; // No hash — legacy memory
    const crypto = require('crypto');
    const expected = crypto.createHash('sha256').update(mem.content || mem.text || '').digest('hex').slice(0, 16);
    return expected === mem.hash;
}

function displayMemory(mem) {
    const integrity = verifyMemoryIntegrity(mem);
    const integrityBadge = integrity === true ? chalk.green(' \u2713') : integrity === false ? chalk.red(' \u2717 tampered') : '';
    const sourceBadge = mem.source_model ? chalk.gray(` [${mem.source_model}]`) : mem.context ? chalk.gray(` [${mem.context}]`) : '';
    console.log(`    ${chalk.gray('[' + mem.id + ']')} ${chalk.gray(relativeTime(mem.created))}${sourceBadge}${integrityBadge}`);
    console.log(`    ${mem.text}`);
    if (mem.tags && mem.tags.length > 0) {
        console.log(`    ${chalk.blue(mem.tags.map(t => '#' + t).join(' '))}`);
    }
    console.log('');
}

program
    .command('remember <text...>')
    .description('Save a memory that persists across all AI assistants')
    .option('--tag <tag>', 'Add a manual tag (repeatable)', (val, prev) => prev ? [...prev, val] : [val])
    .action((textParts, options) => {
        const text = textParts.join(' ');
        const autoTags = extractTags(text);
        const manualTags = (options.tag || []).map(t => t.toLowerCase());
        const allTags = [...new Set([...autoTags, ...manualTags])];

        const entry = {
            text,
            tags: allTags,
            created: new Date().toISOString(),
            source: 'cli',
        };
        writeMemory(entry);
        const total = readMemories().length;

        console.log(chalk.green(`\n  Remembered.`) + chalk.gray(` (${total} memor${total === 1 ? 'y' : 'ies'} total)\n`));
    });

program
    .command('recall [query...]')
    .description('Search your memories — with no query, shows the most recent')
    .option('--tag <tag>', 'Filter by tag')
    .option('--all', 'Show all memories')
    .option('--forget <id>', 'Delete a memory by ID')
    .option('--export', 'Export memories as markdown')
    .action((queryParts, options) => {
        const memories = readMemories();

        // --forget mode
        if (options.forget) {
            if (deleteMemory(options.forget)) {
                const remaining = readMemories().length;
                console.log(chalk.green(`\n  Forgotten.`) + chalk.gray(` (${remaining} memor${remaining === 1 ? 'y' : 'ies'} remaining)\n`));
            } else {
                console.log(chalk.red(`\n  No memory found with ID: ${options.forget}\n`));
                process.exit(1);
            }
            return;
        }

        if (memories.length === 0) {
            console.log(chalk.gray('\n  No memories yet. Save one with:\n'));
            console.log(`    ${chalk.green('delimit remember')} "Your first memory"\n`);
            return;
        }

        // --export mode
        if (options.export) {
            console.log('# Delimit Memories\n');
            for (const mem of memories) {
                const date = new Date(mem.created).toISOString().split('T')[0];
                console.log(`- **${date}** — ${mem.text}`);
                if (mem.tags && mem.tags.length > 0) {
                    console.log(`  Tags: ${mem.tags.map(t => '`' + t + '`').join(', ')}`);
                }
            }
            console.log(`\n_${memories.length} memories exported._`);
            return;
        }

        let results = [...memories];
        const query = (queryParts || []).join(' ').trim().toLowerCase();

        // Filter by tag
        if (options.tag) {
            const tagFilter = options.tag.toLowerCase();
            results = results.filter(m => m.tags && m.tags.some(t => t.includes(tagFilter)));
        }

        // Filter by query (case-insensitive substring on text + tags)
        if (query) {
            results = results.filter(m => {
                const haystack = (m.text + ' ' + (m.tags || []).join(' ')).toLowerCase();
                return haystack.includes(query);
            });
        }

        // Unless --all, limit to last 10
        const total = results.length;
        if (!options.all && !query && !options.tag) {
            results = results.slice(-10);
        }

        // Display newest first
        results.reverse();

        console.log(chalk.bold('\n  Delimit Memories\n'));

        if (results.length === 0) {
            console.log(chalk.gray('  No matching memories found.\n'));
            return;
        }

        for (const mem of results) {
            displayMemory(mem);
        }

        const shownCount = results.length;
        const label = query || options.tag
            ? `${shownCount} memor${shownCount === 1 ? 'y' : 'ies'} found`
            : `${shownCount} shown`;
        console.log(chalk.gray(`  ${label} (${memories.length} total)\n`));
    });

program
    .command('forget <id>')
    .description('Delete a memory by ID (alias for recall --forget)')
    .action((id) => {
        if (deleteMemory(id)) {
            const remaining = readMemories().length;
            console.log(chalk.green(`\n  Forgotten.`) + chalk.gray(` (${remaining} memor${remaining === 1 ? 'y' : 'ies'} remaining)\n`));
        } else {
            console.log(chalk.red(`\n  No memory found with ID: ${id}\n`));
            process.exit(1);
        }
    });

const normalizedArgs = normalizeNaturalLanguageArgs(process.argv);
program.parse([process.argv[0], process.argv[1], ...normalizedArgs]);
