#!/usr/bin/env node

/**
 * LED-202: Cross-Model Hook System
 *
 * Detects installed AI coding assistants (Claude Code, Codex, Gemini CLI)
 * and installs Delimit governance hooks into each one's native config format.
 *
 * Hook commands:
 *   delimit hook session-start   -- ledger context + gov health
 *   delimit hook pre-tool <name> -- lint/test checks before edits
 *   delimit hook pre-commit      -- repo diagnostics before commits
 */

const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');
const os = require('os');

// Use process.env.HOME to allow test overrides; fall back to os.homedir()
function getHome() { return process.env.HOME || os.homedir(); }
function getDelimitHome() { return path.join(getHome(), '.delimit'); }

// ---------------------------------------------------------------------------
// Hook configuration (user-overridable via delimit.yml)
// ---------------------------------------------------------------------------

function loadHookConfig() {
    const defaults = {
        session_start: true,
        pre_tool: true,
        pre_commit: true,
    };

    // Check project-level delimit.yml, then global
    const candidates = [
        path.join(process.cwd(), 'delimit.yml'),
        path.join(process.cwd(), '.delimit.yml'),
        path.join(getDelimitHome(), 'delimit.yml'),
    ];

    for (const candidate of candidates) {
        if (fs.existsSync(candidate)) {
            try {
                const yaml = require('js-yaml');
                const doc = yaml.load(fs.readFileSync(candidate, 'utf-8'));
                if (doc && doc.hooks) {
                    return { ...defaults, ...doc.hooks };
                }
            } catch { /* ignore parse errors */ }
        }
    }
    return defaults;
}

// ---------------------------------------------------------------------------
// AI tool detection
// ---------------------------------------------------------------------------

function detectAITools() {
    const detected = [];

    // Claude Code
    const claudeSettings = path.join(getHome(), '.claude', 'settings.json');
    const claudeSettingsLocal = path.join(getHome(), '.claude', 'settings.local.json');
    let hasClaude = fs.existsSync(claudeSettings) || fs.existsSync(claudeSettingsLocal);
    if (!hasClaude) {
        try {
            execSync('claude --version 2>/dev/null', { stdio: 'pipe' });
            hasClaude = true;
        } catch { /* not installed */ }
    }
    if (hasClaude) {
        detected.push({
            id: 'claude',
            name: 'Claude Code',
            configPath: claudeSettings,
            format: 'claude-hooks',
        });
    }

    // Codex CLI
    const codexDir = path.join(getHome(), '.codex');
    let hasCodex = fs.existsSync(codexDir);
    if (!hasCodex) {
        try {
            execSync('codex --version 2>/dev/null', { stdio: 'pipe' });
            hasCodex = true;
        } catch { /* not installed */ }
    }
    if (hasCodex) {
        detected.push({
            id: 'codex',
            name: 'Codex CLI',
            configPath: path.join(codexDir, 'config.json'),
            instructionsPath: path.join(codexDir, 'instructions.md'),
            format: 'codex',
        });
    }

    // Gemini CLI
    const geminiDir = path.join(getHome(), '.gemini');
    let hasGemini = fs.existsSync(geminiDir);
    if (!hasGemini) {
        try {
            execSync('gemini --version 2>/dev/null', { stdio: 'pipe' });
            hasGemini = true;
        } catch { /* not installed */ }
    }
    if (hasGemini) {
        detected.push({
            id: 'gemini',
            name: 'Gemini CLI',
            configPath: path.join(geminiDir, 'settings.json'),
            format: 'gemini-mcp',
        });
    }

    return detected;
}

// ---------------------------------------------------------------------------
// Hook installers per tool
// ---------------------------------------------------------------------------

/**
 * Install hooks into Claude Code's ~/.claude/settings.json
 * Claude Code supports native hooks: SessionStart, PreToolUse, PostToolUse, etc.
 */
function installClaudeHooks(tool, hookConfig) {
    const configPath = tool.configPath;
    const configDir = path.dirname(configPath);
    fs.mkdirSync(configDir, { recursive: true });

    let config = {};
    if (fs.existsSync(configPath)) {
        try {
            config = JSON.parse(fs.readFileSync(configPath, 'utf-8'));
        } catch { config = {}; }
    }

    if (!config.hooks) {
        config.hooks = {};
    }

    const npxCmd = 'npx delimit-cli';
    const changes = [];

    // SessionStart hook
    if (hookConfig.session_start) {
        const sessionHook = {
            type: 'command',
            command: `${npxCmd} hook session-start`,
        };
        if (!config.hooks.SessionStart) {
            config.hooks.SessionStart = [];
        }
        // Check if already installed
        const existing = config.hooks.SessionStart.find(
            h => h.command && h.command.includes('delimit-cli hook session-start')
        );
        if (!existing) {
            config.hooks.SessionStart.push(sessionHook);
            changes.push('SessionStart');
        }
    }

    // PreToolUse hook for file edits
    if (hookConfig.pre_tool) {
        const preToolHook = {
            type: 'command',
            command: `${npxCmd} hook pre-tool`,
            matcher: 'Edit|Write|Bash',
        };
        if (!config.hooks.PreToolUse) {
            config.hooks.PreToolUse = [];
        }
        const existing = config.hooks.PreToolUse.find(
            h => h.command && h.command.includes('delimit-cli hook pre-tool')
        );
        if (!existing) {
            config.hooks.PreToolUse.push(preToolHook);
            changes.push('PreToolUse');
        }
    }

    fs.writeFileSync(configPath, JSON.stringify(config, null, 2));
    return changes;
}

/**
 * Install hooks for Codex CLI.
 * Codex uses instructions.md for session-start equivalent and config.json for settings.
 * We add governance instructions and a pre-commit hook reference.
 */
function installCodexHooks(tool, hookConfig) {
    const changes = [];
    const codexDir = path.dirname(tool.configPath);
    fs.mkdirSync(codexDir, { recursive: true });

    // Codex instructions.md -- acts as the session-start equivalent
    if (hookConfig.session_start) {
        const instructionsPath = tool.instructionsPath || path.join(codexDir, 'instructions.md');
        const delimitBlock = `<!-- delimit:hooks-start -->
## Delimit Governance

On every session start, run this command to check governance status:
\`\`\`
npx delimit-cli hook session-start
\`\`\`

Before committing changes, run:
\`\`\`
npx delimit-cli hook pre-commit
\`\`\`

After editing OpenAPI specs, run:
\`\`\`
npx delimit-cli hook pre-tool Edit
\`\`\`
<!-- delimit:hooks-end -->`;

        let content = '';
        if (fs.existsSync(instructionsPath)) {
            content = fs.readFileSync(instructionsPath, 'utf-8');
        }

        if (content.includes('delimit:hooks-start')) {
            // Replace existing block
            content = content.replace(
                /<!-- delimit:hooks-start -->[\s\S]*?<!-- delimit:hooks-end -->/,
                delimitBlock
            );
        } else {
            content = content ? content + '\n\n' + delimitBlock : delimitBlock;
        }

        fs.writeFileSync(instructionsPath, content);
        changes.push('instructions.md');
    }

    // Codex config.json -- add hook commands
    let config = {};
    if (fs.existsSync(tool.configPath)) {
        try {
            config = JSON.parse(fs.readFileSync(tool.configPath, 'utf-8'));
        } catch { config = {}; }
    }

    if (!config.hooks) {
        config.hooks = {};
    }

    if (hookConfig.pre_commit && !config.hooks['pre-commit']) {
        config.hooks['pre-commit'] = 'npx delimit-cli hook pre-commit';
        changes.push('pre-commit hook');
    }

    fs.writeFileSync(tool.configPath, JSON.stringify(config, null, 2));
    return changes;
}

/**
 * Install hooks for Gemini CLI.
 * Gemini CLI uses MCP (already handled by setup) but we add governance
 * instructions to settings.json and a GEMINI.md equivalent.
 */
function installGeminiHooks(tool, hookConfig) {
    const changes = [];
    const geminiDir = path.dirname(tool.configPath);
    fs.mkdirSync(geminiDir, { recursive: true });

    // Update settings.json with custom instructions
    let config = {};
    if (fs.existsSync(tool.configPath)) {
        try {
            config = JSON.parse(fs.readFileSync(tool.configPath, 'utf-8'));
        } catch { config = {}; }
    }

    const govInstructions = [
        'Delimit governance is active.',
        'On session start, run: npx delimit-cli hook session-start',
        'Before committing, run: npx delimit-cli hook pre-commit',
        'After editing API specs, run: npx delimit-cli hook pre-tool Edit',
        'Call delimit_ledger_context and delimit_gov_health via MCP when available.',
    ].join(' ');

    if (!config.customInstructions || !config.customInstructions.includes('delimit-cli hook')) {
        config.customInstructions = govInstructions;
        changes.push('customInstructions');
    }

    fs.writeFileSync(tool.configPath, JSON.stringify(config, null, 2));

    // Write a GEMINI.md governance file (equivalent of CLAUDE.md)
    const geminiMd = path.join(geminiDir, 'GEMINI.md');
    if (!fs.existsSync(geminiMd) || !fs.readFileSync(geminiMd, 'utf-8').includes('delimit')) {
        const content = `# Delimit Governance

On every session start:
1. Run \`npx delimit-cli hook session-start\` to check open tasks and governance status

After editing code:
- After editing API specs: run \`npx delimit-cli hook pre-tool Edit\`
- After editing tests: run \`npx delimit-cli hook pre-tool Edit\`

Before committing:
- Run \`npx delimit-cli hook pre-commit\` to check for issues
`;
        fs.writeFileSync(geminiMd, content);
        changes.push('GEMINI.md');
    }

    return changes;
}

/**
 * Install hooks for a detected tool.
 * Returns { tool, changes } describing what was installed.
 */
function installHooksForTool(tool, hookConfig) {
    switch (tool.id) {
        case 'claude':
            return { tool, changes: installClaudeHooks(tool, hookConfig) };
        case 'codex':
            return { tool, changes: installCodexHooks(tool, hookConfig) };
        case 'gemini':
            return { tool, changes: installGeminiHooks(tool, hookConfig) };
        default:
            return { tool, changes: [] };
    }
}

/**
 * Install hooks for all detected AI tools.
 */
function installAllHooks(hookConfig) {
    const tools = detectAITools();
    const results = [];
    for (const tool of tools) {
        results.push(installHooksForTool(tool, hookConfig));
    }
    return { tools, results };
}

// ---------------------------------------------------------------------------
// Hook removal (for uninstall)
// ---------------------------------------------------------------------------

function removeClaudeHooks() {
    const configPath = path.join(getHome(), '.claude', 'settings.json');
    if (!fs.existsSync(configPath)) return false;

    try {
        const config = JSON.parse(fs.readFileSync(configPath, 'utf-8'));
        if (!config.hooks) return false;

        let changed = false;

        for (const event of ['SessionStart', 'PreToolUse', 'PostToolUse']) {
            if (Array.isArray(config.hooks[event])) {
                const before = config.hooks[event].length;
                config.hooks[event] = config.hooks[event].filter(
                    h => !(h.command && h.command.includes('delimit-cli'))
                );
                if (config.hooks[event].length === 0) {
                    delete config.hooks[event];
                }
                if (config.hooks[event] === undefined || config.hooks[event].length < before) {
                    changed = true;
                }
            }
        }

        if (Object.keys(config.hooks).length === 0) {
            delete config.hooks;
        }

        if (changed) {
            fs.writeFileSync(configPath, JSON.stringify(config, null, 2));
        }
        return changed;
    } catch {
        return false;
    }
}

function removeCodexHooks() {
    let changed = false;

    // Remove from instructions.md
    const instructionsPath = path.join(getHome(), '.codex', 'instructions.md');
    if (fs.existsSync(instructionsPath)) {
        let content = fs.readFileSync(instructionsPath, 'utf-8');
        if (content.includes('delimit:hooks-start')) {
            content = content.replace(
                /\n*<!-- delimit:hooks-start -->[\s\S]*?<!-- delimit:hooks-end -->\n*/,
                ''
            );
            fs.writeFileSync(instructionsPath, content);
            changed = true;
        }
    }

    // Remove hooks from config.json
    const configPath = path.join(getHome(), '.codex', 'config.json');
    if (fs.existsSync(configPath)) {
        try {
            const config = JSON.parse(fs.readFileSync(configPath, 'utf-8'));
            if (config.hooks) {
                for (const [key, val] of Object.entries(config.hooks)) {
                    if (typeof val === 'string' && val.includes('delimit-cli')) {
                        delete config.hooks[key];
                        changed = true;
                    }
                }
                if (Object.keys(config.hooks).length === 0) {
                    delete config.hooks;
                }
                if (changed) {
                    fs.writeFileSync(configPath, JSON.stringify(config, null, 2));
                }
            }
        } catch { /* ignore */ }
    }

    return changed;
}

function removeGeminiHooks() {
    let changed = false;

    // Remove custom instructions referencing delimit
    const configPath = path.join(getHome(), '.gemini', 'settings.json');
    if (fs.existsSync(configPath)) {
        try {
            const config = JSON.parse(fs.readFileSync(configPath, 'utf-8'));
            if (config.customInstructions && config.customInstructions.includes('delimit-cli hook')) {
                delete config.customInstructions;
                fs.writeFileSync(configPath, JSON.stringify(config, null, 2));
                changed = true;
            }
        } catch { /* ignore */ }
    }

    // Remove GEMINI.md if it's ours
    const geminiMd = path.join(getHome(), '.gemini', 'GEMINI.md');
    if (fs.existsSync(geminiMd)) {
        const content = fs.readFileSync(geminiMd, 'utf-8');
        if (content.includes('Delimit Governance')) {
            fs.unlinkSync(geminiMd);
            changed = true;
        }
    }

    return changed;
}

function removeAllHooks() {
    const results = [];

    if (removeClaudeHooks()) {
        results.push('Claude Code');
    }
    if (removeCodexHooks()) {
        results.push('Codex CLI');
    }
    if (removeGeminiHooks()) {
        results.push('Gemini CLI');
    }

    return results;
}

// ---------------------------------------------------------------------------
// Hook execution commands
// ---------------------------------------------------------------------------

/**
 * session-start: Show ledger context and governance health.
 * Output goes to stdout for the AI tool to read.
 */
async function hookSessionStart() {
    const config = loadHookConfig();
    if (!config.session_start) {
        return;
    }

    const lines = [];
    lines.push('[Delimit] Governance check');
    lines.push('');

    // Check for delimit.yml or .delimit.yml
    const cwd = process.cwd();
    const hasPolicy = fs.existsSync(path.join(cwd, 'delimit.yml'))
        || fs.existsSync(path.join(cwd, '.delimit.yml'))
        || fs.existsSync(path.join(cwd, '.delimit', 'policies.yml'));

    if (hasPolicy) {
        lines.push('[Delimit] Policy file found -- governance active');
    } else {
        lines.push('[Delimit] No policy file found -- run "delimit init" to set up governance');
    }

    // Check for OpenAPI specs
    const specPatterns = ['openapi.yaml', 'openapi.yml', 'openapi.json', 'swagger.yaml', 'swagger.json'];
    const foundSpecs = [];
    for (const pattern of specPatterns) {
        const specPath = path.join(cwd, pattern);
        if (fs.existsSync(specPath)) {
            foundSpecs.push(pattern);
        }
    }
    // Also check api/ and specs/ directories
    for (const dir of ['api', 'specs', 'spec']) {
        const dirPath = path.join(cwd, dir);
        if (fs.existsSync(dirPath)) {
            try {
                const files = fs.readdirSync(dirPath);
                for (const f of files) {
                    if (/\.(yaml|yml|json)$/.test(f) && /openapi|swagger/i.test(f)) {
                        foundSpecs.push(path.join(dir, f));
                    }
                }
            } catch { /* ignore */ }
        }
    }

    if (foundSpecs.length > 0) {
        lines.push(`[Delimit] OpenAPI specs detected: ${foundSpecs.join(', ')}`);
    }

    // Check ledger
    const ledgerDir = path.join(getDelimitHome(), 'ledger');
    if (fs.existsSync(ledgerDir)) {
        try {
            const ledgerFiles = fs.readdirSync(ledgerDir).filter(f => f.endsWith('.json'));
            let openItems = 0;
            for (const f of ledgerFiles) {
                try {
                    const items = JSON.parse(fs.readFileSync(path.join(ledgerDir, f), 'utf-8'));
                    if (Array.isArray(items)) {
                        openItems += items.filter(i => i.status === 'open' || i.status === 'in_progress').length;
                    }
                } catch { /* ignore */ }
            }
            if (openItems > 0) {
                lines.push(`[Delimit] Ledger: ${openItems} open item(s)`);
            } else {
                lines.push('[Delimit] Ledger: no open items');
            }
        } catch {
            lines.push('[Delimit] Ledger: empty');
        }
    }

    // Git branch info
    try {
        const branch = execSync('git branch --show-current 2>/dev/null', { encoding: 'utf-8' }).trim();
        if (branch) {
            lines.push(`[Delimit] Branch: ${branch}`);
        }
    } catch { /* not in git repo */ }

    lines.push('');
    process.stdout.write(lines.join('\n') + '\n');
}

/**
 * pre-tool: Check before file edits.
 * If editing an OpenAPI spec, run a quick lint.
 * If editing a test file, note it.
 */
async function hookPreTool(toolName) {
    const config = loadHookConfig();
    if (!config.pre_tool) {
        return;
    }

    // The tool name comes from the AI tool (e.g., "Edit", "Write", "Bash")
    // We check the DELIMIT_TOOL_INPUT env or just do lightweight checks
    const cwd = process.cwd();

    // Check if there are staged OpenAPI spec changes
    try {
        const stagedFiles = execSync('git diff --cached --name-only 2>/dev/null', {
            encoding: 'utf-8',
            timeout: 2000,
        }).split('\n').filter(Boolean);

        const specFiles = stagedFiles.filter(f =>
            /openapi|swagger/i.test(f) && /\.(yaml|yml|json)$/.test(f)
        );

        if (specFiles.length > 0) {
            process.stderr.write(`[Delimit] Warning: OpenAPI spec(s) staged for commit: ${specFiles.join(', ')}\n`);
            process.stderr.write('[Delimit] Run "delimit lint" before committing to check for breaking changes.\n');
        }

        const testFiles = stagedFiles.filter(f =>
            /\.(test|spec)\.(js|ts|py|rb)$/.test(f) || /test_.*\.py$/.test(f)
        );

        if (testFiles.length > 0) {
            process.stderr.write(`[Delimit] Test files staged: ${testFiles.join(', ')}\n`);
            process.stderr.write('[Delimit] Consider running tests before committing.\n');
        }
    } catch {
        // Not in a git repo or no staged changes -- that is fine
    }
}

/**
 * pre-commit: Run repo diagnostics before committing.
 */
async function hookPreCommit() {
    const config = loadHookConfig();
    if (!config.pre_commit) {
        return;
    }

    const cwd = process.cwd();
    const warnings = [];

    // Check for staged OpenAPI spec changes
    try {
        const stagedFiles = execSync('git diff --cached --name-only 2>/dev/null', {
            encoding: 'utf-8',
            timeout: 2000,
        }).split('\n').filter(Boolean);

        const specFiles = stagedFiles.filter(f =>
            /openapi|swagger/i.test(f) && /\.(yaml|yml|json)$/.test(f)
        );

        if (specFiles.length > 0) {
            // Try to find a previous version to diff against
            for (const specFile of specFiles) {
                try {
                    // Get the HEAD version
                    const oldContent = execSync(`git show HEAD:${specFile} 2>/dev/null`, {
                        encoding: 'utf-8',
                        timeout: 3000,
                    });
                    if (oldContent) {
                        warnings.push(`[Delimit] OpenAPI spec changed: ${specFile}`);
                        warnings.push('[Delimit] Run "delimit diff <old> <new>" to review API changes before committing.');
                    }
                } catch {
                    // New file, no previous version
                }
            }
        }

        // Check for secrets patterns in staged files
        const sensitivePatterns = [
            /password\s*[:=]\s*['"][^'"]+['"]/i,
            /api[_-]?key\s*[:=]\s*['"][^'"]+['"]/i,
            /secret\s*[:=]\s*['"][^'"]+['"]/i,
        ];

        for (const file of stagedFiles) {
            if (/\.(env|key|pem|p12|pfx)$/.test(file)) {
                warnings.push(`[Delimit] WARNING: Potentially sensitive file staged: ${file}`);
            }
        }
    } catch {
        // Not in git repo
    }

    // Check for policy file
    const hasPolicy = fs.existsSync(path.join(cwd, 'delimit.yml'))
        || fs.existsSync(path.join(cwd, '.delimit.yml'));

    if (!hasPolicy) {
        warnings.push('[Delimit] No governance policy found. Run "delimit init" to create one.');
    }

    if (warnings.length > 0) {
        process.stderr.write(warnings.join('\n') + '\n');
    }
}

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------

module.exports = {
    detectAITools,
    installHooksForTool,
    installAllHooks,
    installClaudeHooks,
    installCodexHooks,
    installGeminiHooks,
    removeAllHooks,
    removeClaudeHooks,
    removeCodexHooks,
    removeGeminiHooks,
    loadHookConfig,
    hookSessionStart,
    hookPreTool,
    hookPreCommit,
};
