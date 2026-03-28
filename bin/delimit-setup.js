#!/usr/bin/env node
/**
 * delimit setup — Install Delimit MCP governance tools into Claude Code.
 *
 * What it does:
 * 1. Creates ~/.delimit/ directory with the MCP server + core engine
 * 2. Adds "delimit" to Claude Code's MCP config (~/.mcp.json or .mcp.json)
 * 3. Installs default agents into ~/.claude/agents/
 * 4. Prints next steps
 */

const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');
const os = require('os');

const DELIMIT_HOME = path.join(os.homedir(), '.delimit');
const MCP_CONFIG = path.join(os.homedir(), '.mcp.json');
const CLAUDE_DIR = path.join(os.homedir(), '.claude');
const AGENTS_DIR = path.join(CLAUDE_DIR, 'agents');

// Colors
const green = (s) => `\x1b[32m${s}\x1b[0m`;
const yellow = (s) => `\x1b[33m${s}\x1b[0m`;
const blue = (s) => `\x1b[34m${s}\x1b[0m`;
const dim = (s) => `\x1b[2m${s}\x1b[0m`;
const bold = (s) => `\x1b[1m${s}\x1b[0m`;

function log(msg) { console.log(msg); }
function step(n, msg) { log(`\n${blue(`[${n}]`)} ${msg}`); }
function pause(ms = 150) { return new Promise(r => setTimeout(r, ms)); }
async function logp(msg, ms = 180) { console.log(msg); await pause(ms); }

function findGitDir(startDir) {
    let dir = startDir;
    while (dir !== path.dirname(dir)) {
        const gitPath = path.join(dir, '.git');
        if (fs.existsSync(gitPath)) {
            // Handle both regular .git dirs and worktree .git files
            const stat = fs.statSync(gitPath);
            if (stat.isDirectory()) return gitPath;
            // .git file (worktree) — read the gitdir path
            const content = fs.readFileSync(gitPath, 'utf-8').trim();
            const match = content.match(/^gitdir:\s*(.+)$/);
            if (match) return match[1];
        }
        dir = path.dirname(dir);
    }
    return null;
}

/**
 * Recursively find OpenAPI/Swagger spec files, ignoring node_modules.
 */
function findSpecFiles(dir, depth = 0) {
    if (depth > 5) return [];
    const results = [];
    try {
        const entries = fs.readdirSync(dir, { withFileTypes: true });
        for (const entry of entries) {
            if (entry.name === 'node_modules' || entry.name === '.git' || entry.name === 'vendor') continue;
            const fullPath = path.join(dir, entry.name);
            if (entry.isDirectory()) {
                results.push(...findSpecFiles(fullPath, depth + 1));
            } else if (/^(openapi|swagger)[^/]*\.(ya?ml|json)$/i.test(entry.name)) {
                results.push(path.relative(process.cwd(), fullPath));
            }
        }
    } catch {}
    return results;
}

async function main() {
    log('');
    log(blue('    ____  ________    ______  _____________'));
    log(blue('   / __ \\/ ____/ /   /  _/  |/  /  _/_  __/'));
    log(blue('  / / / / __/ / /    / // /|_/ // /  / /   '));
    log(blue(' / /_/ / /___/ /____/ // /  / // /  / /    '));
    log(blue('/_____/_____/_____/___/_/  /_/___/ /_/     '));
    log(dim('  Unify all AI coding assistants'));
    log('');

    // Step 1: Check prerequisites
    step(1, 'Checking prerequisites...');

    // Python 3.9+
    let python = null;
    for (const cmd of ['python3', 'python']) {
        try {
            const ver = execSync(`${cmd} --version 2>&1`, { encoding: 'utf-8' }).trim();
            const match = ver.match(/(\d+)\.(\d+)/);
            if (match && (parseInt(match[1]) >= 3 && parseInt(match[2]) >= 9)) {
                python = cmd;
                await logp(`  ${green('✓')} ${ver}`);
                break;
            }
        } catch {}
    }
    if (!python) {
        log(`  ${yellow('✗')} Python 3.9+ not found. Install Python first.`);
        process.exit(1);
    }

    // Check if Claude Code is available
    let hasClaude = false;
    try {
        execSync('claude --version 2>/dev/null', { encoding: 'utf-8' });
        hasClaude = true;
        await logp(`  ${green('✓')} Claude Code detected`);
    } catch {
        log(`  ${yellow('!')} Claude Code not detected — MCP config will still be created`);
    }

    // Show what we're about to do and ask for confirmation
    log('');
    log(`  ${blue('What Delimit will do:')}`);
    log(`    • Install MCP server to ${dim('~/.delimit/')}`);
    log(`    • Configure ${hasClaude ? 'Claude Code' : 'your AI assistant'} to use Delimit`);
    log(`    • Install governance agents + hooks`);
    log(`    • Set up CLAUDE.md instruction file`);
    log('');
    log(`  ${dim('Undo anytime:')} rm -rf ~/.delimit && delimit uninstall`);
    log('');

    const inquirerTop = (() => { try { return require('inquirer'); } catch { return null; } })();
    if (inquirerTop && process.stdin.isTTY) {
        try {
            const { proceed } = await inquirerTop.prompt([{
                type: 'confirm',
                name: 'proceed',
                message: 'Continue with setup? (Enter = Yes)',
                default: true,
            }]);
            if (!proceed) {
                log(`\n  ${dim('Setup cancelled. Nothing was changed.')}\n`);
                process.exit(0);
            }
        } catch {}
    }
    log('');

    // Step 2: Install Delimit MCP server
    step(2, 'Installing Delimit MCP server...');

    // Create ~/.delimit directory
    fs.mkdirSync(path.join(DELIMIT_HOME, 'server', 'core', 'zero_spec'), { recursive: true });
    fs.mkdirSync(path.join(DELIMIT_HOME, 'server', 'tasks'), { recursive: true });
    fs.mkdirSync(path.join(DELIMIT_HOME, 'deploys'), { recursive: true });
    fs.mkdirSync(path.join(DELIMIT_HOME, 'ledger'), { recursive: true });
    fs.mkdirSync(path.join(DELIMIT_HOME, 'evidence'), { recursive: true });

    // Copy the gateway core from our bundled copy
    const gatewaySource = path.join(__dirname, '..', 'gateway');
    if (fs.existsSync(gatewaySource)) {
        copyDir(gatewaySource, path.join(DELIMIT_HOME, 'server'));
        await logp(`  ${green('✓')} Core engine installed`);
    } else {
        // Fallback: try to clone from GitHub
        log(`  ${dim('  Downloading from GitHub...')}`);
        try {
            execSync(`git clone --depth 1 https://github.com/delimit-ai/delimit-gateway.git "${path.join(DELIMIT_HOME, 'server')}" 2>/dev/null`, { stdio: 'pipe' });
            await logp(`  ${green('✓')} Core engine cloned`);
        } catch {
            log(`  ${yellow('!')} Could not download. Clone manually: git clone https://github.com/delimit-ai/delimit-gateway.git ~/.delimit/server`);
        }
    }

    // Copy the MCP server file
    const serverSource = path.join(__dirname, '..', 'mcp-server.py');
    if (fs.existsSync(serverSource)) {
        fs.copyFileSync(serverSource, path.join(DELIMIT_HOME, 'server', 'mcp-server.py'));
    }

    // Download compiled Pro modules (platform-specific)
    const proDir = path.join(DELIMIT_HOME, 'server', 'ai');
    const pyVer = (() => { try { return execSync(`${python} -c "import sys; print(f'cp{sys.version_info.major}{sys.version_info.minor}')"`, { encoding: 'utf-8' }).trim(); } catch { return 'cp310'; } })();
    const arch = (() => { try { return execSync('uname -m', { encoding: 'utf-8' }).trim(); } catch { return 'x86_64'; } })();
    const osName = process.platform === 'darwin' ? 'macos' : 'linux';
    const artifact = `${osName}-${arch}-${pyVer}`;
    const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'package.json'), 'utf-8'));
    const proVersion = pkg.proModuleVersion || '3.8.2';
    const proUrl = `https://delimit.ai/releases/v${proVersion}/delimit-pro-${artifact}.tar.gz`;

    try {
        const proTarball = path.join(DELIMIT_HOME, 'pro.tar.gz');
        execSync(`curl -sL "${proUrl}" -o "${proTarball}" --fail`, { stdio: 'pipe', timeout: 30000 });
        execSync(`tar -xzf "${proTarball}" -C "${proDir}"`, { stdio: 'pipe' });
        fs.unlinkSync(proTarball);
        await logp(`  ${green('✓')} Pro modules installed (${artifact})`);
    } catch {
        log(`  ${dim('  Pro modules not available for ${artifact} — free tools work fine')}`);
    }

    // Install Python deps into isolated venv with pinned versions
    log(`  ${dim('  Installing Python dependencies...')}`);
    const venvDir = path.join(DELIMIT_HOME, 'venv');
    const reqFile = path.join(DELIMIT_HOME, 'server', 'requirements.txt');
    try {
        if (!fs.existsSync(venvDir)) {
            execSync(`${python} -m venv "${venvDir}"`, { stdio: 'pipe' });
        }
        const venvPython = path.join(venvDir, 'bin', 'python');
        const venvPythonWin = path.join(venvDir, 'Scripts', 'python.exe');
        const venvPy = fs.existsSync(venvPython) ? venvPython : venvPythonWin;
        if (fs.existsSync(reqFile)) {
            execSync(`"${venvPy}" -m pip install --quiet -r "${reqFile}" 2>/dev/null`, { stdio: 'pipe' });
        } else {
            execSync(`"${venvPy}" -m pip install --quiet fastmcp==3.1.0 pyyaml==6.0.3 pydantic==2.12.5 packaging==26.0 2>/dev/null`, { stdio: 'pipe' });
        }
        python = venvPy;  // Use venv python for MCP config
        await logp(`  ${green('✓')} Python dependencies installed (isolated venv)`);
    } catch {
        log(`  ${yellow('!')} venv install failed — trying global pip`);
        try {
            execSync(`${python} -m pip install --quiet fastmcp==3.1.0 pyyaml==6.0.3 pydantic==2.12.5 packaging==26.0 2>/dev/null`, { stdio: 'pipe' });
            await logp(`  ${green('✓')} Python dependencies installed (global)`);
        } catch {
            log(`  ${yellow('!')} pip install failed — run manually: pip install fastmcp pyyaml pydantic packaging`);
        }
    }

    // Step 3: Configure Claude Code MCP
    step(3, 'Configuring Claude Code MCP...');

    let mcpConfig = {};
    if (fs.existsSync(MCP_CONFIG)) {
        try {
            mcpConfig = JSON.parse(fs.readFileSync(MCP_CONFIG, 'utf-8'));
        } catch {}
    }
    if (!mcpConfig.mcpServers) mcpConfig.mcpServers = {};

    const serverPath = path.join(DELIMIT_HOME, 'server', 'ai', 'server.py');
    const serverPathAlt = path.join(DELIMIT_HOME, 'server', 'mcp-server.py');
    const actualServer = fs.existsSync(serverPath) ? serverPath : serverPathAlt;

    if (mcpConfig.mcpServers.delimit) {
        await logp(`  ${green('✓')} Delimit MCP already configured`);
    } else {
        mcpConfig.mcpServers.delimit = {
            type: 'stdio',
            command: python,
            args: [actualServer],
            cwd: path.join(DELIMIT_HOME, 'server'),
            env: {
                PYTHONPATH: path.join(DELIMIT_HOME, 'server')
            },
            description: 'Delimit — AI agent guardrails'
        };
        fs.writeFileSync(MCP_CONFIG, JSON.stringify(mcpConfig, null, 2));
        await logp(`  ${green('✓')} Added delimit to ${MCP_CONFIG}`);
    }

    // Step 3b: Configure Codex MCP (if installed)
    const CODEX_CONFIG = path.join(os.homedir(), '.codex', 'config.toml');
    if (fs.existsSync(CODEX_CONFIG)) {
        try {
            let toml = fs.readFileSync(CODEX_CONFIG, 'utf-8');
            if (toml.includes('[mcp_servers.delimit]')) {
                await logp(`  ${green('✓')} Delimit already in Codex config`);
            } else {
                const serverDir = path.join(DELIMIT_HOME, 'server');
                const codexEntry = `\n[mcp_servers.delimit]\ncommand = "${python}"\nargs = ["${actualServer}"]\ncwd = "${serverDir}"\n\n[mcp_servers.delimit.env]\nPYTHONPATH = "${serverDir}:${path.join(serverDir, 'ai')}"\n`;
                toml += codexEntry;
                fs.writeFileSync(CODEX_CONFIG, toml);
                await logp(`  ${green('✓')} Added delimit to Codex (${CODEX_CONFIG})`);
            }
        } catch (e) {
            log(`  ${yellow('!')} Could not configure Codex: ${e.message}`);
        }
    }

    // Step 3c: Configure Cursor MCP (if installed)
    const CURSOR_CONFIG = path.join(os.homedir(), '.cursor', 'mcp.json');
    if (fs.existsSync(path.join(os.homedir(), '.cursor'))) {
        try {
            let cursorConfig = {};
            if (fs.existsSync(CURSOR_CONFIG)) {
                cursorConfig = JSON.parse(fs.readFileSync(CURSOR_CONFIG, 'utf-8'));
            }
            if (!cursorConfig.mcpServers) cursorConfig.mcpServers = {};
            if (cursorConfig.mcpServers.delimit) {
                await logp(`  ${green('✓')} Delimit already in Cursor config`);
            } else {
                cursorConfig.mcpServers.delimit = {
                    command: python,
                    args: [actualServer],
                    cwd: path.join(DELIMIT_HOME, 'server'),
                    env: { PYTHONPATH: path.join(DELIMIT_HOME, 'server') }
                };
                fs.writeFileSync(CURSOR_CONFIG, JSON.stringify(cursorConfig, null, 2));
                await logp(`  ${green('✓')} Added delimit to Cursor (${CURSOR_CONFIG})`);
            }
        } catch (e) {
            log(`  ${yellow('!')} Could not configure Cursor: ${e.message}`);
        }
    }

    // Step 3d: Configure Gemini CLI (if installed)
    const GEMINI_DIR = path.join(os.homedir(), '.gemini');
    const GEMINI_CONFIG = path.join(GEMINI_DIR, 'settings.json');
    if (fs.existsSync(GEMINI_DIR)) {
        try {
            let geminiConfig = {};
            if (fs.existsSync(GEMINI_CONFIG)) {
                geminiConfig = JSON.parse(fs.readFileSync(GEMINI_CONFIG, 'utf-8'));
            }
            if (!geminiConfig.mcpServers) geminiConfig.mcpServers = {};
            if (geminiConfig.mcpServers.delimit) {
                await logp(`  ${green('✓')} Delimit already in Gemini CLI config`);
            } else {
                geminiConfig.mcpServers.delimit = {
                    command: python,
                    args: [actualServer],
                    cwd: path.join(DELIMIT_HOME, 'server'),
                    env: { PYTHONPATH: path.join(DELIMIT_HOME, 'server') }
                };
                fs.writeFileSync(GEMINI_CONFIG, JSON.stringify(geminiConfig, null, 2));
                await logp(`  ${green('✓')} Added delimit to Gemini CLI (${GEMINI_CONFIG})`);
            }
            // Add governance instructions
            if (!geminiConfig.customInstructions || !geminiConfig.customInstructions.includes('delimit_ledger_context')) {
                geminiConfig.customInstructions = 'Delimit MCP is installed. On every session start: (1) call delimit_ledger_context for open tasks, (2) call delimit_gov_health, (3) call delimit_inbox_daemon(action="start") to start email polling. If new project, call delimit_scan. Track all work via the ledger.';
                fs.writeFileSync(GEMINI_CONFIG, JSON.stringify(geminiConfig, null, 2));
            }
        } catch (e) {
            log(`  ${yellow('!')} Could not configure Gemini CLI: ${e.message}`);
        }
    }

    // Checkpoint: MCP is configured, now ask before modifying project files
    log('');
    log(`  ${green('✓')} MCP server installed and configured`);
    log('');
    log(`  ${blue('Next: project-level setup')}`);
    log(`    • Install governance agents (lint, engineering, governance)`);
    log(`    • Update CLAUDE.md with Delimit instructions`);
    log(`    • Optional: governance wrapping + hooks`);
    log('');

    const inquirerMid = (() => { try { return require('inquirer'); } catch { return null; } })();
    if (inquirerMid && process.stdin.isTTY) {
        try {
            const { proceed } = await inquirerMid.prompt([{
                type: 'confirm',
                name: 'proceed',
                message: 'Continue with project setup? (Enter = Yes)',
                default: true,
            }]);
            if (!proceed) {
                log(`\n  ${green('MCP is ready.')} Skipped project-level setup.`);
                log(`  ${dim('Run')} delimit setup ${dim('again to complete project setup.')}\n`);
                process.exit(0);
            }
        } catch {}
    }

    // Step 4: Install default agents
    step(4, 'Installing governance agents...');

    fs.mkdirSync(AGENTS_DIR, { recursive: true });

    const agents = {
        'lint.md': `---
name: lint
description: Lint OpenAPI specs for breaking changes and policy violations
tools:
  - Read
  - Grep
  - Glob
  - Bash
  - mcp__delimit__delimit_lint
  - mcp__delimit__delimit_diff
  - mcp__delimit__delimit_policy
  - mcp__delimit__delimit_impact
  - mcp__delimit__delimit_ledger
---

# Lint Agent

Run API governance checks. Use delimit_lint to compare specs, delimit_policy to check rules, delimit_impact for downstream analysis.
`,
        'engineering.md': `---
name: engineering
description: Build features, fix bugs, write tests, refactor code
tools:
  - Read
  - Glob
  - Grep
  - Bash
  - Edit
  - Write
  - mcp__delimit__delimit_lint
  - mcp__delimit__delimit_diff
  - mcp__delimit__delimit_test_generate
  - mcp__delimit__delimit_test_coverage
---

# Engineering Agent

Execute code directives. Use delimit_test_coverage to verify coverage targets. Use delimit_lint to check API compatibility after changes.
`,
        'governance.md': `---
name: governance
description: Run governance checks on the current project
tools:
  - Read
  - Grep
  - Glob
  - Bash
  - mcp__delimit__delimit_gov_health
  - mcp__delimit__delimit_gov_status
  - mcp__delimit__delimit_gov_policy
  - mcp__delimit__delimit_security_scan
  - mcp__delimit__delimit_security_audit
  - mcp__delimit__delimit_evidence_collect
  - mcp__delimit__delimit_repo_analyze
  - mcp__delimit__delimit_repo_config_validate
---

# Governance Agent

Run full governance compliance checks. Verify security, policy compliance, evidence collection, and repo health.
`
    };

    let installed = 0;
    for (const [filename, content] of Object.entries(agents)) {
        const agentPath = path.join(AGENTS_DIR, filename);
        if (!fs.existsSync(agentPath)) {
            fs.writeFileSync(agentPath, content);
            installed++;
        }
    }
    await logp(`  ${green('✓')} ${installed} agents installed (${Object.keys(agents).length - installed} already existed)`);

    // Step 4b: Install Git hooks if inside a git repository
    const gitDir = findGitDir(process.cwd());
    if (gitDir) {
        const gitHooksDir = path.join(gitDir, 'hooks');
        const srcHooksDir = path.join(__dirname, '..', 'hooks', 'git');
        if (fs.existsSync(srcHooksDir)) {
            fs.mkdirSync(gitHooksDir, { recursive: true });
            let hooksInstalled = 0;
            for (const hookFile of ['pre-commit', 'pre-push', 'commit-msg']) {
                const src = path.join(srcHooksDir, hookFile);
                const dest = path.join(gitHooksDir, hookFile);
                if (fs.existsSync(src)) {
                    // Only install if hook doesn't already exist or is a delimit hook
                    let shouldInstall = !fs.existsSync(dest);
                    if (!shouldInstall) {
                        const existing = fs.readFileSync(dest, 'utf-8');
                        shouldInstall = existing.includes('Delimit') || existing.includes('delimit');
                    }
                    if (shouldInstall) {
                        fs.copyFileSync(src, dest);
                        fs.chmodSync(dest, '755');
                        hooksInstalled++;
                    }
                }
            }
            if (hooksInstalled > 0) {
                await logp(`  ${green('✓')} ${hooksInstalled} Git hooks installed to ${gitHooksDir}`);
            } else {
                log(`  ${dim('  Git hooks already present (non-Delimit hooks preserved)')}`);
            }
        }
    } else {
        log(`  ${dim('  Not inside a git repo — git hooks will be installed on next delimit setup inside a repo')}`);
    }

    // Step 5: Create/update CLAUDE.md and platform instruction files
    step(5, 'Setting up AI instruction files...');

    const claudeMd = path.join(os.homedir(), 'CLAUDE.md');
    const claudeResult = upsertDelimitSection(claudeMd);
    if (claudeResult.action === 'created') {
        await logp(`  ${green('✓')} Created ${claudeMd} with governance triggers`);
    } else if (claudeResult.action === 'updated') {
        await logp(`  ${green('✓')} Updated Delimit section in ${claudeMd} (version changed)`);
    } else if (claudeResult.action === 'appended') {
        await logp(`  ${green('✓')} Appended Delimit section to ${claudeMd} (user content preserved)`);
    } else {
        log(`  ${dim('  CLAUDE.md already up to date')}`);
    }

    // Codex instructions
    const codexInstructions = path.join(os.homedir(), '.codex', 'instructions.md');
    if (fs.existsSync(path.join(os.homedir(), '.codex'))) {
        const codexResult = upsertDelimitSection(codexInstructions);
        if (codexResult.action !== 'unchanged') {
            log(`  ${green('✓')} ${codexResult.action === 'created' ? 'Created' : 'Updated'} ${codexInstructions}`);
        }
    }

    // Cursor rules (legacy .cursorrules + new .cursor/rules/ directory)
    const cursorRules = path.join(os.homedir(), '.cursorrules');
    if (fs.existsSync(path.join(os.homedir(), '.cursor'))) {
        const cursorResult = upsertDelimitSection(cursorRules);
        if (cursorResult.action !== 'unchanged') {
            log(`  ${green('✓')} ${cursorResult.action === 'created' ? 'Created' : 'Updated'} ${cursorRules}`);
        }
        // Also install to .cursor/rules/delimit.md (Cursor 0.45+)
        try {
            const cursorRulesDir = path.join(os.homedir(), '.cursor', 'rules');
            fs.mkdirSync(cursorRulesDir, { recursive: true });
            const cursorAdapter = require('../adapters/cursor-rules');
            cursorAdapter.installRules(pkg.version);
            log(`  ${green('✓')} Installed governance rules to .cursor/rules/delimit.md`);
        } catch (e) {
            log(`  ${dim('  Could not install .cursor/rules: ' + e.message)}`);
        }
    }

    // Silent: auto-detect API keys and configure models.json (no output)
    const modelsPath = path.join(DELIMIT_HOME, 'models.json');
    if (!fs.existsSync(modelsPath)) {
        const models = {};
        if (process.env.XAI_API_KEY) {
            models.grok = { name: 'Grok', api_url: 'https://api.x.ai/v1/chat/completions', model: 'grok-4-0709', env_key: 'XAI_API_KEY', enabled: true };
        }
        if (process.env.GOOGLE_APPLICATION_CREDENTIALS) {
            const project = process.env.GOOGLE_CLOUD_PROJECT || process.env.GCLOUD_PROJECT || '';
            models.gemini = { name: 'Gemini', api_url: `https://us-central1-aiplatform.googleapis.com/v1/projects/{project}/locations/us-central1/publishers/google/models/gemini-2.5-flash:generateContent`, model: 'gemini-2.5-flash', format: 'vertex_ai', enabled: true };
        }
        if (process.env.OPENAI_API_KEY) {
            models.openai = { name: 'OpenAI', api_url: 'https://api.openai.com/v1/chat/completions', model: 'gpt-4o', env_key: 'OPENAI_API_KEY', prefer_cli: true, enabled: true };
        }
        if (process.env.ANTHROPIC_API_KEY) {
            models.anthropic = { name: 'Claude', api_url: 'https://api.anthropic.com/v1/messages', model: 'claude-sonnet-4-5-20250514', env_key: 'ANTHROPIC_API_KEY', format: 'anthropic', enabled: true };
        }
        if (Object.keys(models).length > 0) {
            fs.writeFileSync(modelsPath, JSON.stringify(models, null, 2));
        }
    }

    // Step 6: Governance wrapping (shims)
    step(6, 'Governance wrapping...');
    log('');
    log(`  Delimit can wrap your AI assistants with a governance layer:`);
    log('');
    log(`  ${blue('    ____  ________    ______  _____________')}`);
    log(`  ${blue('   / __ \\\\/ ____/ /   /  _/  |/  /  _/_  __/')}`);
    log(`  ${blue('  / / / / __/ / /    / // /|_/ // /  / /   ')}`);
    log(`  ${blue(' / /_/ / /___/ /____/ // /  / // /  / /    ')}`);
    log(`  ${blue('/_____/_____/_____/___/_/  /_/___/ /_/     ')}`);
    log('');
    log(`  ${dim('[Delimit]')} Executing governance check...`);
    log(`  ${dim('[Delimit]')} Mode: advisory`);
    log(`  ${dim('[Delimit]')} ${green('✓ GOVERNANCE ACTIVE')}`);
    log('');
    log(`  ${dim('This shows before each AI session (<1 second).')}`);
    log(`  ${dim('Adds ~/.delimit/shims to your shell PATH.')}`);
    log(`  ${dim('Disable anytime: delimit shims disable')}`);
    log('');

    // Check if shims already installed
    const shimsDir = path.join(DELIMIT_HOME, 'shims');
    const shimsInstalled = fs.existsSync(shimsDir) && fs.readdirSync(shimsDir).length > 0;

    if (shimsInstalled) {
        log(`  ${green('✓')} Governance wrapping already enabled`);
    } else {
        // Default YES prompt — non-interactive mode auto-accepts
        const inquirer = (() => { try { return require('inquirer'); } catch { return null; } })();
        let enableShims = true;

        if (inquirer && process.stdin.isTTY) {
            try {
                const answer = await inquirer.prompt([{
                    type: 'confirm',
                    name: 'enable',
                    message: 'Enable governance wrapping? (press Enter for Yes)',
                    default: true,
                }]);
                enableShims = answer.enable;
            } catch {
                enableShims = true; // Default yes if prompt fails
            }
        }

        if (enableShims) {
            // Create shims
            fs.mkdirSync(shimsDir, { recursive: true });

            const shimTemplate = (toolName, displayName) => `#!/bin/sh
# Delimit Governance Shim for ${displayName}
PURPLE='\\033[35m'; MAGENTA='\\033[91m'; ORANGE='\\033[33m'; GREEN='\\033[32m'
WHITE='\\033[97m'; BOLD='\\033[1m'; DIM='\\033[2m'; RESET='\\033[0m'
if [ "$DELIMIT_WRAPPED" = "true" ] || [ ! -t 1 ]; then
    for c in /usr/bin/${toolName} /usr/local/bin/${toolName} $HOME/.local/bin/${toolName}; do
        [ -x "$c" ] && exec "$c" "$@"
    done
fi
DELIMIT_HOME="\${DELIMIT_HOME:-$HOME/.delimit}"
TOOL_COUNT="0"
if [ -f "$DELIMIT_HOME/server/ai/server.py" ]; then
  TOTAL=$(grep -c '@mcp.tool' "$DELIMIT_HOME/server/ai/server.py" 2>/dev/null || echo "0")
  OPS=$(grep -c '@_ops_pack_tool' "$DELIMIT_HOME/server/ai/server.py" 2>/dev/null || echo "0")
  INTERNAL=$(grep -c '@_internal_tool' "$DELIMIT_HOME/server/ai/server.py" 2>/dev/null || echo "0")
  TOOL_COUNT=$((TOTAL - OPS - INTERNAL))
fi
echo ""
printf "  \${PURPLE}\${BOLD}    ____  ________    ______  _____________\${RESET}\\n"
printf "  \${PURPLE}\${BOLD}   / __ \\\\/ ____/ /   /  _/  |/  /  _/_  __/\${RESET}\\n"
printf "  \${MAGENTA}\${BOLD}  / / / / __/ / /    / // /|_/ // /  / /   \${RESET}\\n"
printf "  \${MAGENTA}\${BOLD} / /_/ / /___/ /____/ // /  / // /  / /    \${RESET}\\n"
printf "  \${ORANGE}\${BOLD}/_____/_____/_____/___/_/  /_/___/ /_/     \${RESET}\\n"
printf "  \${DIM}delimit.ai\${RESET}\\n"
echo ""
printf "  \${PURPLE}\${BOLD}[Delimit]\${RESET} \${DIM}Executing governance check...\${RESET}\\n"
sleep 0.1
printf "  \${PURPLE}\${BOLD}[Delimit]\${RESET} \${ORANGE}Mode: advisory\${RESET}\\n"
printf "  \${PURPLE}\${BOLD}[Delimit]\${RESET} \${DIM}MCP server: \${WHITE}\${TOOL_COUNT} tools\${RESET}\\n"
printf "  \${MAGENTA}\${BOLD}[Delimit]\${RESET} \${MAGENTA}═══════════════════════════════════════════\${RESET}\\n"
printf "  \${MAGENTA}\${BOLD}[Delimit]\${RESET} \${PURPLE}<\${MAGENTA}/\${ORANGE}>\${RESET} \${BOLD}GOVERNANCE ACTIVE: ${displayName.toUpperCase()}\${RESET}\\n"
printf "  \${MAGENTA}\${BOLD}[Delimit]\${RESET} \${MAGENTA}═══════════════════════════════════════════\${RESET}\\n"
sleep 0.08
printf "  \${GREEN}\${BOLD}[Delimit]\${RESET} \${GREEN}✓ Allowed\${RESET}\\n"
echo ""
# Find real binary — check common paths then fallback to PATH search (excluding shim dir)
SELF="$(readlink -f "$0" 2>/dev/null || echo "$0")"
for c in /usr/bin/${toolName} /usr/local/bin/${toolName} "$HOME/.local/bin/${toolName}" "$(npm bin -g 2>/dev/null)/${toolName}"; do
    [ -x "$c" ] && [ "$(readlink -f "$c" 2>/dev/null)" != "$SELF" ] && exec "$c" "$@"
done
# Last resort: search PATH excluding shim directory
REAL=$(PATH=$(echo "$PATH" | tr ':' '\\n' | grep -v '.delimit/shims' | tr '\\n' ':') command -v ${toolName} 2>/dev/null)
[ -x "$REAL" ] && exec "$REAL" "$@"
echo "[Delimit] ${toolName} not found in PATH" >&2
echo "  Install: npm install -g @anthropic-ai/claude-code" >&2
exit 127
`;

            for (const [tool, display] of [['claude', 'Claude'], ['codex', 'Codex'], ['gemini', 'Gemini CLI']]) {
                const shimPath = path.join(shimsDir, tool);
                fs.writeFileSync(shimPath, shimTemplate(tool, display));
                fs.chmodSync(shimPath, '755');
            }

            // Add to PATH in shell rc files
            const pathLine = `export PATH="${shimsDir}:$PATH"  # Delimit governance wrapping`;
            for (const rc of ['.bashrc', '.zshrc']) {
                const rcPath = path.join(os.homedir(), rc);
                if (fs.existsSync(rcPath)) {
                    const content = fs.readFileSync(rcPath, 'utf-8');
                    if (!content.includes('.delimit/shims')) {
                        fs.appendFileSync(rcPath, `\n# Delimit governance wrapping\n${pathLine}\n`);
                    }
                }
            }

            log(`  ${green('✓')} Governance wrapping enabled`);
            log(`  ${dim('  Restart your terminal or run: source ~/.bashrc')}`);
        } else {
            log(`  ${dim('  Skipped. Enable later: delimit shims enable')}`);
        }
    }
    log('');

    // Step 7: Install cross-model governance hooks (LED-202)
    step(7, 'Installing AI assistant hooks...');

    try {
        const crossModelHooks = require('../lib/cross-model-hooks');
        const hookConfig = crossModelHooks.loadHookConfig();
        const detected = crossModelHooks.detectAITools();

        if (detected.length === 0) {
            log(`  ${dim('  No AI assistants detected -- hooks will be installed when tools are found')}`);
        } else {
            log(`  ${dim('  Detected: ' + detected.map(t => t.name).join(', '))}`);

            // Install hooks (auto-accept in non-interactive or prompt if TTY)
            let installHooks = true;
            const inq = (() => { try { return require('inquirer'); } catch { return null; } })();
            if (inq && process.stdin.isTTY) {
                try {
                    const answer = await inq.prompt([{
                        type: 'confirm',
                        name: 'install',
                        message: `Install governance hooks for ${detected.map(t => t.name).join(', ')}? (press Enter for Yes)`,
                        default: true,
                    }]);
                    installHooks = answer.install;
                } catch {
                    installHooks = true;
                }
            }

            if (installHooks) {
                for (const tool of detected) {
                    const result = crossModelHooks.installHooksForTool(tool, hookConfig);
                    if (result.changes.length > 0) {
                        log(`  ${green('✓')} ${tool.name}: ${result.changes.join(', ')}`);
                    } else {
                        log(`  ${dim('  ' + tool.name + ': hooks already installed')}`);
                    }
                }
            } else {
                log(`  ${dim('  Skipped. Install later: delimit hook install')}`);
            }
        }
    } catch (e) {
        log(`  ${dim('  Hook installation skipped: ' + e.message)}`);
    }
    log('');

    // Step 8: Local dashboard API server
    step(8, 'Local dashboard API...');

    const localServerPath = path.join(DELIMIT_HOME, 'server', 'ai', 'local_server.py');
    if (fs.existsSync(localServerPath)) {
        log(`  ${green('✓')} Local API server available on port 7823`);
        log(`  ${dim('  Start it:  python3 ' + localServerPath)}`);
        log(`  ${dim('  Dashboard connects to localhost:7823 automatically')}`);
    } else {
        log(`  ${dim('  Local API server not found — dashboard will use cloud sync')}`);
    }
    log('');

    // Step 9: Post-install config validation (LED-098)
    step(9, 'Validating config integrity...');

    let validationIssues = 0;
    const configFiles = [
        { path: MCP_CONFIG, name: 'Claude Code', format: 'json' },
        { path: CODEX_CONFIG, name: 'Codex', format: 'toml' },
        { path: CURSOR_CONFIG, name: 'Cursor', format: 'json' },
        { path: GEMINI_CONFIG, name: 'Gemini CLI', format: 'json' },
    ];

    for (const cfg of configFiles) {
        if (!fs.existsSync(cfg.path)) continue;
        try {
            const content = fs.readFileSync(cfg.path, 'utf-8');
            if (cfg.format === 'json') {
                const parsed = JSON.parse(content);
                const servers = parsed.mcpServers || {};
                const delimitEntry = servers.delimit;
                if (delimitEntry) {
                    // Validate the delimit entry points to our server
                    const cmd = delimitEntry.command || '';
                    const args = delimitEntry.args || [];
                    const serverArg = args[0] || '';

                    // Check command is python (not arbitrary binary)
                    if (!cmd.includes('python') && !cmd.includes('venv')) {
                        log(`  ${yellow('⚠')} ${cfg.name}: delimit command is not python: ${cmd}`);
                        validationIssues++;
                    }
                    // Check server arg points to our server file
                    if (serverArg && !serverArg.includes('delimit') && !serverArg.includes('server.py')) {
                        log(`  ${yellow('⚠')} ${cfg.name}: server path looks unexpected: ${serverArg}`);
                        validationIssues++;
                    }
                    // Check no unexpected MCP servers were added
                    const knownServers = new Set(['delimit', 'codex', 'gemini', 'gemini-vertexai', 'filesystem', 'brave-search', 'fetch', 'memory', 'puppeteer', 'github', 'slack', 'datadog']);
                    for (const serverName of Object.keys(servers)) {
                        if (!knownServers.has(serverName) && !serverName.includes('delimit')) {
                            // Not necessarily bad, just note it
                        }
                    }
                }
            } else if (cfg.format === 'toml') {
                // Basic TOML check — ensure delimit entry has correct structure
                if (content.includes('[mcp_servers.delimit]')) {
                    if (!content.match(/command\s*=\s*"[^"]*python[^"]*"/)) {
                        log(`  ${yellow('⚠')} ${cfg.name}: delimit command may not be python`);
                        validationIssues++;
                    }
                }
            }
            log(`  ${green('✓')} ${cfg.name} config valid`);
        } catch (e) {
            log(`  ${yellow('⚠')} ${cfg.name}: could not validate — ${e.message}`);
            validationIssues++;
        }
    }

    // Verify server file exists and is our code
    if (fs.existsSync(actualServer)) {
        const serverContent = fs.readFileSync(actualServer, 'utf-8').substring(0, 500);
        if (serverContent.includes('delimit') || serverContent.includes('Delimit')) {
            log(`  ${green('✓')} Server file verified`);
        } else {
            log(`  ${yellow('⚠')} Server file at ${actualServer} does not appear to be Delimit`);
            validationIssues++;
        }
    }

    // Check directory permissions
    try {
        const stat = fs.statSync(DELIMIT_HOME);
        const mode = (stat.mode & 0o777).toString(8);
        if (mode.endsWith('7') || mode.endsWith('6')) {
            log(`  ${yellow('⚠')} ~/.delimit/ is world-readable/writable (${mode}) — consider: chmod 700 ~/.delimit`);
            validationIssues++;
        } else {
            log(`  ${green('✓')} Directory permissions OK`);
        }
    } catch {}

    if (validationIssues === 0) {
        log(`  ${green('✓')} All config validations passed`);
    } else {
        log(`  ${yellow(`⚠ ${validationIssues} issue(s) found — review above`)}`);
    }
    log('');

    // Step 10: Auto-detect OpenAPI specs
    step(10, 'Scanning for API specs...');

    let detectedSpecs = [];
    try {
        const { minimatch } = (() => { try { return require('minimatch'); } catch { return { minimatch: null }; } })();
        // Simple recursive glob for spec files
        detectedSpecs = findSpecFiles(process.cwd());
    } catch {}

    if (detectedSpecs.length > 0) {
        log(`  ${green('✓')} Found ${detectedSpecs.length} API spec(s):`);
        detectedSpecs.forEach(s => log(`    ${s}`));
        log('');
        log(`  Try: ${bold(`npx delimit-cli lint ${detectedSpecs[0]}`)}`);
    } else {
        log(`  ${dim('  No OpenAPI/Swagger specs found in current directory')}`);
    }
    log('');

    // Step 11: Done
    step(11, 'Done!');
    log('');
    log(`  ${green('Delimit is installed.')} Your AI now has persistent memory and governance.`);
    log('');
    log('  Configured for:');
    const tools = ['Claude Code'];
    if (fs.existsSync(CODEX_CONFIG)) tools.push('Codex');
    if (fs.existsSync(path.join(os.homedir(), '.cursor'))) tools.push('Cursor');
    if (fs.existsSync(GEMINI_DIR)) tools.push('Gemini CLI');
    log(`  ${green('✓')} ${tools.join(', ')}`);

    log('');

    // "What's next" box
    log('  \u250c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2510');
    log(`  \u2502  ${bold('What\'s next:')}                           \u2502`);
    log('  \u2502                                         \u2502');
    log(`  \u2502  1. ${blue('npx delimit-cli lint')}                \u2502`);
    log(`  \u2502  2. ${blue('npx delimit-cli doctor')}              \u2502`);
    log(`  \u2502  3. Add the GitHub Action to your repo  \u2502`);
    log('  \u2502                                         \u2502');
    log(`  \u2502  Docs: ${dim('https://delimit.ai/docs')}          \u2502`);
    log(`  \u2502  Try:  ${dim('https://delimit.ai/try')}           \u2502`);
    log('  \u2514\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2518');
    log('');
    log(`  ${dim('Config:')} ${MCP_CONFIG}`);
    log(`  ${dim('Server:')} ${actualServer}`);
    log(`  ${dim('Agents:')} ${AGENTS_DIR}`);
    log('');
    log(`  ${bold('Keep Building.')}`);
    log('');
}

function getDelimitSection() {
    const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'package.json'), 'utf-8'));
    const version = pkg.version || '0.0.0';
    return `<!-- delimit:start v${version} -->
# Delimit

Unify all AI coding assistants with persistent context, governance, and multi-model debate.

## On every session start:
1. Call \`delimit_ledger_context\` to check for open tasks
2. Call \`delimit_gov_health\` to check governance status
3. Call \`delimit_inbox_daemon(action="start")\` to start email polling — ALL venture inboxes route through this. Owner approves/adjusts/denies via email reply, same as being logged in.
4. If returning user, summarize what's pending
5. If new project, call \`delimit_scan\` to discover what Delimit can do

## After editing code:
- After editing UI/CSS: call \`delimit_design_validate_responsive\`
- After editing API specs: call \`delimit_lint\`
- After editing tests: call \`delimit_test_smoke\`

## Before deploying:
1. Call \`delimit_security_audit\`
2. Call \`delimit_test_smoke\`
3. Call \`delimit_deploy_plan\`

## Before committing:
- Call \`delimit_repo_diagnose\` to check for common issues

## When starting a new project:
- Call \`delimit_scan\` to discover what Delimit can do
- Call \`delimit_init\` to set up governance

## Try these:
- "scan this project" -- discover what Delimit can do here
- "lint my API spec" -- catch breaking changes in your OpenAPI spec
- "add to ledger: [anything]" -- track tasks across sessions
- "what's on the ledger?" -- pick up where you left off
- "deliberate [question]" -- get multi-model AI consensus

## What Delimit does:
- **API governance** -- lint, diff, semver classification, migration guides
- **Persistent ledger** -- tasks survive across sessions
- **Security audit** -- dependency scanning, secret detection
- **Test verification** -- confirm tests actually ran, measure coverage
- **Multi-model deliberation** -- say "deliberate [question]" to get consensus from multiple AI models

## GitHub Action
Add breaking change detection to any repo:
\`\`\`yaml
- uses: delimit-ai/delimit-action@v1
  with:
    spec: api/openapi.yaml
\`\`\`

## Links
- Docs: https://delimit.ai/docs
- GitHub: https://github.com/delimit-ai/delimit-mcp-server
- Action: https://github.com/marketplace/actions/delimit-api-governance
<!-- delimit:end -->`;
}

function getClaudeMdContent() {
    return getDelimitSection() + '\n';
}

/**
 * Upsert the Delimit section in a file using <!-- delimit:start --> / <!-- delimit:end --> markers.
 * If markers exist, replaces only that region (preserving user content above/below).
 * If no markers exist but old Delimit content is detected, replaces the whole file.
 * If no Delimit content at all, appends the section.
 * Returns { action: 'created' | 'updated' | 'unchanged' | 'appended' }
 */
function upsertDelimitSection(filePath) {
    const newSection = getDelimitSection();
    const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'package.json'), 'utf-8'));
    const version = pkg.version || '0.0.0';

    if (!fs.existsSync(filePath)) {
        fs.writeFileSync(filePath, newSection + '\n');
        return { action: 'created' };
    }

    const existing = fs.readFileSync(filePath, 'utf-8');

    // Check if markers already exist
    const startMarkerRe = /<!-- delimit:start[^>]*-->/;
    const endMarker = '<!-- delimit:end -->';
    const hasStart = startMarkerRe.test(existing);
    const hasEnd = existing.includes(endMarker);

    if (hasStart && hasEnd) {
        // Extract current version from the marker
        const versionMatch = existing.match(/<!-- delimit:start v([^ ]+) -->/);
        const currentVersion = versionMatch ? versionMatch[1] : '';
        if (currentVersion === version) {
            return { action: 'unchanged' };
        }
        // Replace only the delimit section
        const before = existing.substring(0, existing.search(startMarkerRe));
        const after = existing.substring(existing.indexOf(endMarker) + endMarker.length);
        fs.writeFileSync(filePath, before + newSection + after);
        return { action: 'updated' };
    }

    // No markers — check for old Delimit content that should be replaced
    const isOldDelimit = existing.includes('# Delimit AI Guardrails') ||
        existing.includes('delimit_init') ||
        existing.includes('persistent memory, verified execution') ||
        (existing.includes('# Delimit') && existing.includes('delimit_ledger_context'));

    if (isOldDelimit) {
        fs.writeFileSync(filePath, newSection + '\n');
        return { action: 'updated' };
    }

    // File exists with user content but no Delimit section — append
    const separator = existing.endsWith('\n') ? '\n' : '\n\n';
    fs.writeFileSync(filePath, existing + separator + newSection + '\n');
    return { action: 'appended' };
}

function copyDir(src, dest) {
    fs.mkdirSync(dest, { recursive: true });
    for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
        const srcPath = path.join(src, entry.name);
        const destPath = path.join(dest, entry.name);
        if (entry.name === '__pycache__' || entry.name === 'node_modules' || entry.name === '.git') continue;
        if (entry.isDirectory()) {
            copyDir(srcPath, destPath);
        } else {
            fs.copyFileSync(srcPath, destPath);
        }
    }
}

main().catch(err => {
    console.error('Setup failed:', err.message);
    process.exit(1);
});
