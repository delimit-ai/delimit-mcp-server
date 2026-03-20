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

async function main() {
    log('');
    log(bold('  Delimit Setup'));
    log(dim('  AI agent guardrails for developers'));
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
                log(`  ${green('✓')} ${ver}`);
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
        log(`  ${green('✓')} Claude Code detected`);
    } catch {
        log(`  ${yellow('!')} Claude Code not detected — MCP config will still be created`);
    }

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
        log(`  ${green('✓')} Core engine installed`);
    } else {
        // Fallback: try to clone from GitHub
        log(`  ${dim('  Downloading from GitHub...')}`);
        try {
            execSync(`git clone --depth 1 https://github.com/delimit-ai/delimit-gateway.git "${path.join(DELIMIT_HOME, 'server')}" 2>/dev/null`, { stdio: 'pipe' });
            log(`  ${green('✓')} Core engine cloned`);
        } catch {
            log(`  ${yellow('!')} Could not download. Clone manually: git clone https://github.com/delimit-ai/delimit-gateway.git ~/.delimit/server`);
        }
    }

    // Copy the MCP server file
    const serverSource = path.join(__dirname, '..', 'mcp-server.py');
    if (fs.existsSync(serverSource)) {
        fs.copyFileSync(serverSource, path.join(DELIMIT_HOME, 'server', 'mcp-server.py'));
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
        log(`  ${green('✓')} Python dependencies installed (isolated venv)`);
    } catch {
        log(`  ${yellow('!')} venv install failed — trying global pip`);
        try {
            execSync(`${python} -m pip install --quiet fastmcp==3.1.0 pyyaml==6.0.3 pydantic==2.12.5 packaging==26.0 2>/dev/null`, { stdio: 'pipe' });
            log(`  ${green('✓')} Python dependencies installed (global)`);
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
        log(`  ${green('✓')} Delimit MCP already configured`);
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
        log(`  ${green('✓')} Added delimit to ${MCP_CONFIG}`);
    }

    // Step 3b: Configure Codex MCP (if installed)
    const CODEX_CONFIG = path.join(os.homedir(), '.codex', 'config.toml');
    if (fs.existsSync(CODEX_CONFIG)) {
        try {
            let toml = fs.readFileSync(CODEX_CONFIG, 'utf-8');
            if (toml.includes('[mcp_servers.delimit]')) {
                log(`  ${green('✓')} Delimit already in Codex config`);
            } else {
                const serverDir = path.join(DELIMIT_HOME, 'server');
                const codexEntry = `\n[mcp_servers.delimit]\ncommand = "${python}"\nargs = ["${actualServer}"]\ncwd = "${serverDir}"\n\n[mcp_servers.delimit.env]\nPYTHONPATH = "${serverDir}:${path.join(serverDir, 'ai')}"\n`;
                toml += codexEntry;
                fs.writeFileSync(CODEX_CONFIG, toml);
                log(`  ${green('✓')} Added delimit to Codex (${CODEX_CONFIG})`);
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
                log(`  ${green('✓')} Delimit already in Cursor config`);
            } else {
                cursorConfig.mcpServers.delimit = {
                    command: python,
                    args: [actualServer],
                    cwd: path.join(DELIMIT_HOME, 'server'),
                    env: { PYTHONPATH: path.join(DELIMIT_HOME, 'server') }
                };
                fs.writeFileSync(CURSOR_CONFIG, JSON.stringify(cursorConfig, null, 2));
                log(`  ${green('✓')} Added delimit to Cursor (${CURSOR_CONFIG})`);
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
                log(`  ${green('✓')} Delimit already in Gemini CLI config`);
            } else {
                geminiConfig.mcpServers.delimit = {
                    command: python,
                    args: [actualServer],
                    cwd: path.join(DELIMIT_HOME, 'server'),
                    env: { PYTHONPATH: path.join(DELIMIT_HOME, 'server') }
                };
                fs.writeFileSync(GEMINI_CONFIG, JSON.stringify(geminiConfig, null, 2));
                log(`  ${green('✓')} Added delimit to Gemini CLI (${GEMINI_CONFIG})`);
            }
        } catch (e) {
            log(`  ${yellow('!')} Could not configure Gemini CLI: ${e.message}`);
        }
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
    log(`  ${green('✓')} ${installed} agents installed (${Object.keys(agents).length - installed} already existed)`);

    // Step 5: Create CLAUDE.md for first-run guidance
    step(5, 'Setting up first-run guidance...');

    const claudeMd = path.join(os.homedir(), 'CLAUDE.md');
    if (!fs.existsSync(claudeMd)) {
        fs.writeFileSync(claudeMd, getClaudeMdContent());
        log(`  ${green('✓')} Created ${claudeMd} with first-run guidance`);
    } else {
        // Check if existing CLAUDE.md is an older Delimit version that should be upgraded
        const existing = fs.readFileSync(claudeMd, 'utf-8');
        if (existing.includes('# Delimit AI Guardrails') || existing.includes('delimit_init') || existing.includes('delimit_lint')) {
            fs.writeFileSync(claudeMd, getClaudeMdContent());
            log(`  ${green('✓')} Updated ${claudeMd} with improved onboarding`);
        } else {
            log(`  ${dim('  CLAUDE.md already exists with custom content — skipped')}`);
        }
    }

    // Step 6: Auto-detect API keys for multi-model deliberation
    step(6, 'Detecting AI model API keys...');

    const modelsPath = path.join(DELIMIT_HOME, 'models.json');
    const keyDetection = {
        grok: { env: 'XAI_API_KEY', name: 'Grok (xAI)', found: false },
        gemini: { env: 'GOOGLE_APPLICATION_CREDENTIALS', name: 'Gemini (Vertex AI)', found: false },
        codex: { env: 'OPENAI_API_KEY', name: 'Codex (OpenAI)', found: false },
    };

    // Check which keys exist in environment
    for (const [id, info] of Object.entries(keyDetection)) {
        if (process.env[info.env]) {
            info.found = true;
        }
    }

    const foundKeys = Object.entries(keyDetection).filter(([, v]) => v.found);
    const missingKeys = Object.entries(keyDetection).filter(([, v]) => !v.found);

    if (foundKeys.length > 0 && !fs.existsSync(modelsPath)) {
        // Auto-generate models.json from detected keys
        const models = {};
        if (keyDetection.grok.found) {
            models.grok = {
                name: 'Grok 4',
                api_url: 'https://api.x.ai/v1/chat/completions',
                model: 'grok-4-0709',
                env_key: 'XAI_API_KEY',
                enabled: true,
            };
        }
        if (keyDetection.gemini.found) {
            const project = process.env.GOOGLE_CLOUD_PROJECT || 'default';
            models.gemini = {
                name: 'Gemini 2.5 Flash',
                api_url: `https://us-central1-aiplatform.googleapis.com/v1/projects/{project}/locations/us-central1/publishers/google/models/gemini-2.5-flash:generateContent`,
                model: 'gemini-2.5-flash',
                format: 'vertex_ai',
                enabled: true,
            };
        }
        if (keyDetection.codex.found) {
            models.codex = {
                name: 'Codex (GPT-5.4)',
                format: 'codex_cli',
                model: 'gpt-5.4',
                enabled: true,
            };
        }
        fs.writeFileSync(modelsPath, JSON.stringify(models, null, 2));
        log(`  ${green('✓')} Auto-configured ${foundKeys.length} model(s) for deliberation:`);
        foundKeys.forEach(([, v]) => log(`    ${green('✓')} ${v.name}`));
    } else if (fs.existsSync(modelsPath)) {
        try {
            const existing = JSON.parse(fs.readFileSync(modelsPath, 'utf-8'));
            const enabled = Object.values(existing).filter(m => m.enabled);
            log(`  ${green('✓')} ${enabled.length} model(s) already configured for deliberation`);
        } catch {
            log(`  ${yellow('!')} models.json exists but could not be read`);
        }
    } else {
        log(`  ${dim('  No AI API keys detected in environment')}`);
    }

    if (missingKeys.length > 0 && foundKeys.length < 2 && !fs.existsSync(modelsPath)) {
        log(`  ${dim('  For multi-model deliberation, set 2+ of:')}`);
        missingKeys.forEach(([, v]) => log(`    ${dim(`export ${v.env}=your-key`)}`));
    }

    // Step 7: Done
    step(7, 'Done!');
    log('');
    log(`  ${green('Delimit is installed.')} Your AI now has persistent memory and governance.`);
    log('');
    log('  Configured for:');
    const tools = ['Claude Code'];
    if (fs.existsSync(CODEX_CONFIG)) tools.push('Codex');
    if (fs.existsSync(path.join(os.homedir(), '.cursor'))) tools.push('Cursor');
    if (fs.existsSync(GEMINI_DIR)) tools.push('Gemini CLI');
    log(`  ${green('✓')} ${tools.join(', ')}`);

    // Show deliberation status
    if (foundKeys.length >= 2) {
        log(`  ${green('✓')} Multi-model deliberation: ${foundKeys.map(([,v]) => v.name).join(' + ')}`);
    } else if (foundKeys.length === 1) {
        log(`  ${yellow('!')} Deliberation: needs 1 more API key (${missingKeys.slice(0,2).map(([,v]) => v.env).join(' or ')})`);
    } else if (fs.existsSync(modelsPath)) {
        log(`  ${green('✓')} Deliberation: configured via ~/.delimit/models.json`);
    } else {
        log(`  ${dim('  Deliberation: not configured (optional — set API keys to enable)')}`);
    }
    log('');
    log('  Try it now:');
    log(`  ${bold('$ claude')}`);
    log('');
    log(`  Then say: ${blue('"check this project\'s health"')}`);
    log('');
    log('  Or try:');
    log(`  ${dim('-')} "add to ledger: set up CI pipeline"  ${dim('— start tracking tasks')}`);
    log(`  ${dim('-')} "what\'s on the ledger?"               ${dim('— see what\'s pending')}`);
    log(`  ${dim('-')} "delimit help"                        ${dim('— see all capabilities')}`);
    log('');
    log(`  ${dim('Config:')} ${MCP_CONFIG}`);
    log(`  ${dim('Server:')} ${actualServer}`);
    log(`  ${dim('Agents:')} ${AGENTS_DIR}`);
    log('');
    log(`  ${dim('Docs:')} https://delimit.ai/docs`);
    log(`  ${dim('GitHub:')} https://github.com/delimit-ai/delimit`);
    log('');
}

function getClaudeMdContent() {
    return `# Delimit

Your AI has persistent memory, verified execution, and governance.

## First time? Say one of these:
- "check this project's health" -- see what Delimit finds
- "add to ledger: [anything]" -- start tracking tasks
- "what's on the ledger?" -- see what's pending

## Returning? Your AI remembers:
- Ledger items persist across sessions
- Governance rules stay configured
- Memory carries forward

## On first session, your AI will automatically:
1. Diagnose the environment to verify everything is connected
2. Check the ledger for any pending items from previous sessions
3. If no governance exists yet, suggest initializing it

## Available Agents
- /lint -- check API specs for breaking changes
- /engineering -- build, test, refactor with governance checks
- /governance -- full compliance audit

## Need help?
Say "delimit help" for docs on any capability.
`;
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
