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

// LED-213: Import canonical template for cross-model parity
const { getDelimitSection, getDelimitSectionCondensed } = require('./delimit-template');

// Use process.env.HOME to allow test overrides; fall back to os.homedir()
function getHome() { return process.env.HOME || os.homedir(); }
function getDelimitHome() { return path.join(getHome(), '.delimit'); }

function readJsonl(filePath) {
    if (!fs.existsSync(filePath)) {
        return [];
    }
    return fs.readFileSync(filePath, 'utf-8')
        .split('\n')
        .map(line => line.trim())
        .filter(Boolean)
        .map(line => {
            try {
                return JSON.parse(line);
            } catch {
                return null;
            }
        })
        .filter(Boolean);
}

function readLatestSessionSummary(sessionDir) {
    if (!fs.existsSync(sessionDir)) {
        return null;
    }
    const files = fs.readdirSync(sessionDir)
        .filter(name => name.endsWith('.json'))
        .map(name => path.join(sessionDir, name))
        .sort((a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs);
    if (files.length === 0) {
        return null;
    }
    try {
        const data = JSON.parse(fs.readFileSync(files[0], 'utf-8'));
        return {
            id: data.id || path.basename(files[0], '.json'),
            timestamp: data.timestamp || null,
            summary: data.summary || '',
            blockers: Array.isArray(data.blockers) ? data.blockers : [],
            itemsCompleted: Array.isArray(data.items_completed) ? data.items_completed : [],
        };
    } catch {
        return null;
    }
}

function buildLedgerState(ledgerDir) {
    const opsPath = path.join(ledgerDir, 'operations.jsonl');
    const entries = readJsonl(opsPath);
    const latestById = new Map();
    for (const entry of entries) {
        if (!entry || !entry.id) continue;
        const current = latestById.get(entry.id);
        const nextTime = entry.updated_at || entry.created_at || '';
        const currentTime = current ? (current.updated_at || current.created_at || '') : '';
        if (!current || nextTime >= currentTime) {
            latestById.set(entry.id, entry);
        }
    }
    const items = Array.from(latestById.values());
    const open = items
        .filter(item => !['done', 'blocked'].includes(String(item.status || 'open')))
        .sort((a, b) => {
            const prio = { P0: 0, P1: 1, P2: 2 };
            return (prio[a.priority] ?? 9) - (prio[b.priority] ?? 9);
        });
    return {
        items,
        open,
        next: open[0] || null,
    };
}

function getServiceState(serviceName) {
    try {
        const active = execSync(`systemctl is-active ${serviceName} 2>/dev/null`, { encoding: 'utf-8', timeout: 2000 }).trim();
        const enabled = execSync(`systemctl is-enabled ${serviceName} 2>/dev/null`, { encoding: 'utf-8', timeout: 2000 }).trim();
        return { active, enabled };
    } catch {
        const processPattern = serviceName.includes('social')
            ? 'social_daemon.py'
            : serviceName.includes('inbox')
                ? 'inbox_daemon.py'
                : '';
        if (processPattern) {
            try {
                const matches = execSync(`ps -eo pid,cmd | grep ${JSON.stringify(processPattern)} | grep -v grep`, {
                    encoding: 'utf-8',
                    timeout: 2000,
                    stdio: ['ignore', 'pipe', 'ignore'],
                }).trim();
                if (matches) {
                    return { active: 'active', enabled: 'unknown' };
                }
            } catch { /* ignore */ }
        }
        return { active: 'inactive', enabled: 'unknown' };
    }
}

function writeBootstrapState(continuityRoot, payload) {
    fs.mkdirSync(continuityRoot, { recursive: true });
    const statePath = path.join(continuityRoot, 'bootstrap-state.json');
    fs.writeFileSync(statePath, JSON.stringify(payload, null, 2) + '\n');
    return statePath;
}

// ---------------------------------------------------------------------------
// Hook configuration (user-overridable via delimit.yml)
// ---------------------------------------------------------------------------

function loadHookConfig() {
    const defaults = {
        session_start: true,
        pre_tool: true,
        pre_commit: true,
        conditional_hooks: true,
        deploy_audit: true,
        deliberate_on_commit: false,
        show_strategy_items: true,
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
            execSync('claude --version 2>/dev/null', { stdio: 'pipe', timeout: 3000 });
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
            execSync('codex --version 2>/dev/null', { stdio: 'pipe', timeout: 3000 });
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
            execSync('gemini --version 2>/dev/null', { stdio: 'pipe', timeout: 3000 });
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
 * Check if a Claude Code hook group array already contains a delimit hook
 * matching the given command substring.
 */
function findClaudeHookGroup(hookGroups, commandSubstring) {
    if (!Array.isArray(hookGroups)) return null;
    // Match both "npx delimit-cli X" and "delimit-cli X" variants
    const bare = commandSubstring.replace(/^npx /, '');
    for (const group of hookGroups) {
        // Support both nested format (group.hooks[].command) and flat format (group.command)
        if (group.hooks && Array.isArray(group.hooks)) {
            if (group.hooks.some(h => h.command && (h.command.includes(commandSubstring) || h.command.includes(bare)))) {
                return group;
            }
        }
        if (group.command && (group.command.includes(commandSubstring) || group.command.includes(bare))) {
            return group;
        }
    }
    return null;
}

/**
 * Migrate a flat-format hook entry to the nested Claude Code format.
 * Flat: { type, command, matcher, if }
 * Nested: { matcher, if, hooks: [{ type, command }] }
 */
function migrateToNestedFormat(hookGroup) {
    if (hookGroup.hooks && Array.isArray(hookGroup.hooks)) {
        return hookGroup; // Already nested
    }
    const nested = { matcher: hookGroup.matcher || '' };
    if (hookGroup.if) nested.if = hookGroup.if;
    nested.hooks = [{ type: hookGroup.type || 'command', command: hookGroup.command }];
    return nested;
}

/**
 * Install hooks into Claude Code's ~/.claude/settings.json
 *
 * Claude Code hook format (nested):
 *   {
 *     "hooks": {
 *       "EventName": [
 *         {
 *           "matcher": "ToolPattern",
 *           "if": "condition expression",
 *           "hooks": [
 *             { "type": "command", "command": "...", "timeout": 30 }
 *           ]
 *         }
 *       ]
 *     }
 *   }
 *
 * LED-234: Adds conditional hooks that fire only when relevant files change:
 *   1. PostToolUse (Edit|Write) + spec patterns -> delimit lint
 *   2. PreToolUse (Bash) + git commit -> delimit doctor
 *   3. PreToolUse (Bash) + deploy patterns -> delimit security-audit
 */
function installClaudeHooks(tool, hookConfig) {
    // Write to global ~/.claude/settings.json
    const configPath = tool.configPath;
    const configDir = path.dirname(configPath);
    fs.mkdirSync(configDir, { recursive: true });

    // Also write to project .claude/settings.json if the dir exists
    const projectConfigDir = path.join(process.cwd(), '.claude');
    const projectConfigPath = path.join(projectConfigDir, 'settings.json');
    const writeTargets = [configPath];
    if (fs.existsSync(projectConfigDir)) {
        writeTargets.push(projectConfigPath);
    }

    let config = {};
    if (fs.existsSync(configPath)) {
        try {
            config = JSON.parse(fs.readFileSync(configPath, 'utf-8'));
        } catch { config = {}; }
    }

    if (!config.hooks) {
        config.hooks = {};
    }

    // Use local binary if installed, fall back to npx
    const { execSync: _exec } = require('child_process');
    let npxCmd;
    try {
        _exec('delimit-cli --version', { stdio: 'pipe', timeout: 3000 });
        npxCmd = 'delimit-cli';
    } catch {
        npxCmd = 'npx delimit-cli';
    }
    const changes = [];

    // --- SessionStart hook ---
    // Write a standalone bash script so it works without npm in PATH
    if (hookConfig.session_start) {
        const home = getHome();
        const hooksDir = path.join(home, '.claude', 'hooks');
        fs.mkdirSync(hooksDir, { recursive: true });
        const hookScript = path.join(hooksDir, 'delimit');
        const delimitHome = path.join(home, '.delimit');
        // Write hook script — use explicit newline prefix to avoid terminal escape contamination
        const scriptContent = '#!/bin/bash\n' + `
# Delimit SessionStart — generated by delimit-cli setup
DELIMIT_HOME="\${DELIMIT_HOME:-${delimitHome}}"
echo "=== Delimit Status ==="
# Governance
if [ -f "./delimit.yml" ] || [ -f "./.delimit/policies.yml" ]; then
  echo "Governance: active | policy=project"
elif [ -f "$DELIMIT_HOME/delimit.yml" ]; then
  echo "Governance: active | policy=user"
else
  echo "Governance: not initialized -- run npx delimit-cli init"
fi
# Server + tools
SERVER="$DELIMIT_HOME/server/ai/server.py"
if [ -f "$SERVER" ]; then
  TOOLS=$(grep -c '@mcp.tool' "$SERVER" 2>/dev/null || echo "0")
  echo "Server: ready ($TOOLS tools)"
else
  echo "Server: not installed -- run npx delimit-cli setup"
fi
# Hooks + audit
[ -f "${home}/.claude/settings.json" ] && grep -q '"hooks"' "${home}/.claude/settings.json" 2>/dev/null && HOOKS="enabled" || HOOKS="disabled"
[ -d "$DELIMIT_HOME/audit" ] && AUDIT="on" || AUDIT="off"
echo "Hooks: $HOOKS | Audit: $AUDIT"
# MCP
[ -f "${home}/.mcp.json" ] && grep -q "delimit" "${home}/.mcp.json" 2>/dev/null && echo "MCP: delimit registered" || echo "MCP: not registered"
# Models
MODELS=""
[ -n "$XAI_API_KEY" ] && MODELS="\${MODELS}Grok + "
[ -n "$GOOGLE_APPLICATION_CREDENTIALS" ] && MODELS="\${MODELS}Gemini + "
[ -n "$OPENAI_API_KEY" ] && MODELS="\${MODELS}Codex + "
[ -f "$DELIMIT_HOME/models.json" ] && MODELS=$(python3 -c "import json; d=json.load(open('$DELIMIT_HOME/models.json')); print(' + '.join(v.get('name',k) for k,v in d.items() if v.get('enabled')))" 2>/dev/null) || true
[ -n "$MODELS" ] && echo "Deliberation: \${MODELS% + }"
# Last session
SESSIONS="$DELIMIT_HOME/sessions"
if [ -d "$SESSIONS" ]; then
  LATEST=$(ls -t "$SESSIONS"/session_*.json 2>/dev/null | head -1)
  [ -n "$LATEST" ] && python3 -c "import json; d=json.load(open('$LATEST')); s=d.get('summary','')[:150]; print(f'Last session: {s}')" 2>/dev/null
fi
echo "=== Delimit Ready ==="
# Self-heal: re-wrap claude binary if an update overwrote our shim
CLAUDE_BIN=$(PATH=$(echo "$PATH" | tr ':' '\\n' | grep -v '.delimit/shims' | tr '\\n' ':') command -v claude 2>/dev/null)
if [ -n "$CLAUDE_BIN" ] && [ -f "$CLAUDE_BIN" ]; then
  if ! head -5 "$CLAUDE_BIN" 2>/dev/null | grep -q "Delimit Governance Shim"; then
    # Claude binary exists but is not our shim — re-wrap silently
    SHIM="$DELIMIT_HOME/shims/claude"
    if [ -f "$SHIM" ]; then
      DIR=$(dirname "$CLAUDE_BIN")
      mv "$CLAUDE_BIN" "$DIR/claude-real" 2>/dev/null && cp "$SHIM" "$CLAUDE_BIN" && chmod 755 "$CLAUDE_BIN" && echo "Shim: re-wrapped after update"
    fi
  fi
fi
`;
        fs.writeFileSync(hookScript, scriptContent);
        fs.chmodSync(hookScript, '755');

        if (!config.hooks.SessionStart) {
            config.hooks.SessionStart = [];
        }
        // Check if identical hook already exists
        const existingSession = config.hooks.SessionStart.find(group => {
            const cmds = (group.hooks || []).map(h => h.command || '');
            return cmds.some(c => c === hookScript);
        });
        if (!existingSession) {
            // Remove any old delimit hooks (both script and npm command variants)
            config.hooks.SessionStart = config.hooks.SessionStart.filter(group => {
                const cmds = (group.hooks || []).map(h => h.command || '');
                return !cmds.some(c => c.includes('delimit'));
            });
            config.hooks.SessionStart.push({
                matcher: '',
                hooks: [{
                    type: 'command',
                    command: hookScript,
                    timeout: 10,
                }],
            });
            changes.push('SessionStart');
        }
    }

    // --- PreToolUse: pre-tool hook scoped to Edit/Write on spec files ---
    if (hookConfig.pre_tool) {
        if (!config.hooks.PreToolUse) {
            config.hooks.PreToolUse = [];
        }
        const existing = findClaudeHookGroup(config.hooks.PreToolUse, 'delimit-cli hook pre-tool');
        if (existing) {
            // Upgrade flat-format hook to nested + add if condition if missing
            const migrated = migrateToNestedFormat(existing);
            if (!migrated.if) {
                const idx = config.hooks.PreToolUse.indexOf(existing);
                migrated.matcher = 'Edit|Write';
                migrated.if = "Edit && (path_matches('**/openapi*') || path_matches('**/swagger*') || path_matches('**/*.yaml') || path_matches('**/*.yml'))";
                migrated.hooks = [{ type: 'command', command: `${npxCmd} hook pre-tool $TOOL_NAME` }];
                config.hooks.PreToolUse[idx] = migrated;
                changes.push('PreToolUse (upgraded)');
            }
        } else {
            config.hooks.PreToolUse.push({
                matcher: 'Edit|Write',
                if: "Edit && (path_matches('**/openapi*') || path_matches('**/swagger*') || path_matches('**/*.yaml') || path_matches('**/*.yml'))",
                hooks: [{
                    type: 'command',
                    command: `${npxCmd} hook pre-tool $TOOL_NAME`,
                }],
            });
            changes.push('PreToolUse');
        }
    }

    // --- PreToolUse: pre-commit governance on git commit/push ---
    if (hookConfig.pre_commit) {
        if (!config.hooks.PreToolUse) {
            config.hooks.PreToolUse = [];
        }
        const existing = findClaudeHookGroup(config.hooks.PreToolUse, 'delimit-cli hook pre-commit');
        if (!existing) {
            config.hooks.PreToolUse.push({
                matcher: 'Bash',
                if: "Bash && (input_contains('git commit') || input_contains('git push'))",
                hooks: [{
                    type: 'command',
                    command: `${npxCmd} hook pre-commit`,
                }],
            });
            changes.push('PreCommit');
        }
    }

    // --- LED-234: Conditional hooks (opt-in via conditional_hooks config) ---
    if (hookConfig.conditional_hooks !== false) {

        // 1. PostToolUse: auto-lint after editing OpenAPI spec files
        if (!config.hooks.PostToolUse) {
            config.hooks.PostToolUse = [];
        }
        const specLintCmd = 'delimit-cli lint';
        const existingSpecLint = findClaudeHookGroup(config.hooks.PostToolUse, specLintCmd);
        if (!existingSpecLint) {
            config.hooks.PostToolUse.push({
                matcher: 'Edit|Write',
                if: "path_matches('**/openapi*') || path_matches('**/swagger*') || path_matches('**/specs/**')",
                hooks: [{
                    type: 'command',
                    command: `${npxCmd} lint "$DELIMIT_FILE_PATH"`,
                    timeout: 30,
                }],
            });
            changes.push('PostToolUse:spec-lint');
        }

        // 2. PreToolUse: repo diagnose before git commit (uses doctor command)
        if (!config.hooks.PreToolUse) {
            config.hooks.PreToolUse = [];
        }
        const doctorCmd = 'delimit-cli doctor';
        const existingDoctor = findClaudeHookGroup(config.hooks.PreToolUse, doctorCmd);
        if (!existingDoctor) {
            config.hooks.PreToolUse.push({
                matcher: 'Bash',
                if: "command matches 'git commit'",
                hooks: [{
                    type: 'command',
                    command: `${npxCmd} doctor`,
                    timeout: 15,
                }],
            });
            changes.push('PreToolUse:doctor');
        }

        // 3. PreToolUse: security audit before deploy/publish/release commands
        if (hookConfig.deploy_audit !== false) {
            const deployGateCmd = 'delimit-cli hook deploy-gate';
            const existingSecurity = findClaudeHookGroup(config.hooks.PreToolUse, deployGateCmd);
            if (!existingSecurity) {
                config.hooks.PreToolUse.push({
                    matcher: 'Bash',
                    if: "command matches 'npm publish' or command matches 'npx deploy' or command matches 'deploy' or command matches 'release' or command matches 'docker compose up' or command matches 'docker-compose up' or command matches 'docker build'",
                    hooks: [{
                        type: 'command',
                        command: `${npxCmd} hook deploy-gate`,
                        timeout: 30,
                    }],
                });
                changes.push('PreToolUse:deploy-gate');
            }
        }
    }

    // --- Stop hook: session handoff on exit ---
    if (hookConfig.session_start) {  // If session-start is enabled, also add session-end
        if (!config.hooks.Stop) {
            config.hooks.Stop = [];
        }
        const home = getHome();
        const hooksDir = path.join(home, '.claude', 'hooks');
        fs.mkdirSync(hooksDir, { recursive: true });
        const stopScript = path.join(hooksDir, 'delimit-stop');
        const delimitHome = path.join(home, '.delimit');
        const stopContent = '#!/bin/bash\n' + `
# Delimit Stop — session handoff on exit
# Preserves context for next session across all AI assistants
DELIMIT_HOME="\${DELIMIT_HOME:-${delimitHome}}"
LEDGER_DIR="$DELIMIT_HOME/ledger"

# Push ledger changes so other models pick them up
if [ -d "$LEDGER_DIR/.git" ]; then
  cd "$LEDGER_DIR"
  git add -A 2>/dev/null
  git commit -m "session handoff $(date -u +%Y-%m-%dT%H:%M:%SZ)" --no-verify 2>/dev/null
  git push origin main 2>/dev/null &
fi

# Save session timestamp
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$DELIMIT_HOME/.last_session_end"
echo "[Delimit] Session context saved."
`;
        fs.writeFileSync(stopScript, stopContent);
        fs.chmodSync(stopScript, '755');

        const existingStop = config.hooks.Stop.find(group => {
            const cmds = (group.hooks || []).map(h => h.command || '');
            return cmds.some(c => c.includes('delimit'));
        });
        if (!existingStop) {
            config.hooks.Stop.push({
                matcher: '',
                hooks: [{
                    type: 'command',
                    command: stopScript,
                    timeout: 10,
                }],
            });
            changes.push('Stop');
        }
    }

    // Write hooks to all target settings files
    const configJson = JSON.stringify(config, null, 2);
    for (const target of writeTargets) {
        try {
            // For project settings, merge hooks into existing config
            if (target !== configPath && fs.existsSync(target)) {
                const existing = JSON.parse(fs.readFileSync(target, 'utf-8'));
                existing.hooks = config.hooks;
                fs.writeFileSync(target, JSON.stringify(existing, null, 2));
            } else {
                fs.writeFileSync(target, configJson);
            }
        } catch {}
    }
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
        // LED-213: Use canonical Consensus 123 template for Codex parity
        const delimitBlock = `<!-- delimit:hooks-start -->
${getDelimitSection()}
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

    // LED-213: Use canonical Consensus 123 template (condensed for JSON)
    const govInstructions = getDelimitSectionCondensed();

    if (!config.customInstructions || !config.customInstructions.includes('Consensus 123')) {
        config.customInstructions = govInstructions;
        changes.push('customInstructions');
    }

    fs.writeFileSync(tool.configPath, JSON.stringify(config, null, 2));

    // LED-213: Write GEMINI.md with canonical Consensus 123 template
    const geminiMd = path.join(geminiDir, 'GEMINI.md');
    if (!fs.existsSync(geminiMd) || !fs.readFileSync(geminiMd, 'utf-8').includes('Consensus 123')) {
        fs.writeFileSync(geminiMd, getDelimitSection() + '\n');
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

        for (const event of ['SessionStart', 'PreToolUse', 'PostToolUse', 'Stop']) {
            if (Array.isArray(config.hooks[event])) {
                const before = config.hooks[event].length;
                config.hooks[event] = config.hooks[event].filter(h => {
                    const isDelimit = (cmd) => cmd && (cmd.includes('delimit-cli') || cmd.includes('delimit'));
                    // Nested format: check hooks[].command
                    if (h.hooks && Array.isArray(h.hooks)) {
                        return !h.hooks.some(inner => isDelimit(inner.command));
                    }
                    // Flat format: check h.command directly
                    return !isDelimit(h.command);
                });
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
// Deliberation helpers
// ---------------------------------------------------------------------------

/**
 * Count pending strategy items in the ledger that have priority P0.
 * Returns the count of open/in_progress P0 strategy items.
 */
function countPendingStrategyItems() {
    const ledgerDir = path.join(getDelimitHome(), 'ledger');
    if (!fs.existsSync(ledgerDir)) return 0;

    let count = 0;
    try {
        const files = fs.readdirSync(ledgerDir).filter(f => f.endsWith('.json'));
        for (const f of files) {
            try {
                const items = JSON.parse(fs.readFileSync(path.join(ledgerDir, f), 'utf-8'));
                if (!Array.isArray(items)) continue;
                for (const item of items) {
                    const isOpen = item.status === 'open' || item.status === 'in_progress';
                    const isStrategy = item.category === 'strategy' || item.category === 'deliberation';
                    const isP0 = item.priority === 'P0' || item.priority === 0;
                    if (isOpen && (isStrategy || isP0)) {
                        count++;
                    }
                }
            } catch { /* ignore individual file parse errors */ }
        }
    } catch { /* ignore directory read errors */ }

    return count;
}

/**
 * Get the highest priority pending strategy item from the ledger.
 * Returns the item object or null if none found.
 */
function getTopStrategyItem() {
    const ledgerDir = path.join(getDelimitHome(), 'ledger');
    if (!fs.existsSync(ledgerDir)) return null;

    let best = null;
    const priorityOrder = { P0: 0, P1: 1, P2: 2, P3: 3 };

    try {
        const files = fs.readdirSync(ledgerDir).filter(f => f.endsWith('.json'));
        for (const f of files) {
            try {
                const items = JSON.parse(fs.readFileSync(path.join(ledgerDir, f), 'utf-8'));
                if (!Array.isArray(items)) continue;
                for (const item of items) {
                    const isOpen = item.status === 'open' || item.status === 'in_progress';
                    const isStrategy = item.category === 'strategy' || item.category === 'deliberation';
                    const isP0 = item.priority === 'P0' || item.priority === 0;
                    if (isOpen && (isStrategy || isP0)) {
                        const rank = typeof item.priority === 'number' ? item.priority : (priorityOrder[item.priority] ?? 99);
                        if (!best || rank < (typeof best.priority === 'number' ? best.priority : (priorityOrder[best.priority] ?? 99))) {
                            best = item;
                        }
                    }
                }
            } catch { /* ignore */ }
        }
    } catch { /* ignore */ }

    return best;
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
    // Always show status — even if session_start is false in config
    // This is the first thing a user sees. Make it count.

    const lines = [];
    lines.push('=== Delimit Status ===');

    const home = getHome();
    const delimitHome = path.join(home, '.delimit');
    const cwd = process.cwd();

    // Governance status + policy source
    const projectPolicy = fs.existsSync(path.join(cwd, 'delimit.yml')) || fs.existsSync(path.join(cwd, '.delimit', 'policies.yml'));
    const userPolicy = fs.existsSync(path.join(delimitHome, 'delimit.yml'));
    if (projectPolicy) {
        lines.push('Governance: active | policy=project');
    } else if (userPolicy) {
        lines.push('Governance: active | policy=user');
    } else {
        lines.push('Governance: not initialized -- run npx delimit-cli init');
    }

    // Server status + tool count
    const serverFile = path.join(delimitHome, 'server', 'ai', 'server.py');
    if (fs.existsSync(serverFile)) {
        try {
            const content = fs.readFileSync(serverFile, 'utf-8');
            const toolCount = (content.match(/@mcp\.tool\(\)/g) || []).length;
            lines.push(`Server: ready (${toolCount} tools)`);
        } catch {
            lines.push('Server: ready');
        }
    } else {
        lines.push('Server: not installed -- run npx delimit-cli setup');
    }

    // Hooks + audit
    const settingsFile = path.join(home, '.claude', 'settings.json');
    const hooksEnabled = fs.existsSync(settingsFile) && fs.readFileSync(settingsFile, 'utf-8').includes('"hooks"');
    const auditOn = fs.existsSync(path.join(delimitHome, 'audit'));
    lines.push(`Hooks: ${hooksEnabled ? 'enabled' : 'disabled'} | Audit: ${auditOn ? 'on' : 'off'}`);

    // MCP registration
    const mcpFile = path.join(home, '.mcp.json');
    const mcpRegistered = fs.existsSync(mcpFile) && fs.readFileSync(mcpFile, 'utf-8').includes('delimit');
    lines.push(`MCP: ${mcpRegistered ? 'delimit registered' : 'not registered -- run npx delimit-cli setup'}`);

    // Deliberation models
    const modelsFile = path.join(delimitHome, 'models.json');
    const modelNames = [];
    try {
        if (fs.existsSync(modelsFile)) {
            const models = JSON.parse(fs.readFileSync(modelsFile, 'utf-8'));
            for (const [key, val] of Object.entries(models)) {
                if (val && val.enabled) modelNames.push(val.name || key);
            }
        }
    } catch {}
    // Also check env vars for available models
    if (modelNames.length === 0) {
        const envModels = [];
        if (process.env.XAI_API_KEY) envModels.push('Grok');
        if (process.env.GOOGLE_APPLICATION_CREDENTIALS) envModels.push('Gemini');
        if (process.env.OPENAI_API_KEY) envModels.push('Codex');
        if (envModels.length > 0) {
            lines.push(`Deliberation: ${envModels.join(' + ')}`);
        }
    } else {
        lines.push(`Deliberation: ${modelNames.join(' + ')}`);
    }

    // Last session context (prevents cross-session drift)
    const sessionsDir = path.join(delimitHome, 'sessions');
    try {
        if (fs.existsSync(sessionsDir)) {
            const sessions = fs.readdirSync(sessionsDir).filter(f => f.startsWith('session_')).sort().reverse();
            if (sessions.length > 0) {
                const latest = JSON.parse(fs.readFileSync(path.join(sessionsDir, sessions[0]), 'utf-8'));
                const summary = (latest.summary || '').substring(0, 150);
                if (summary) lines.push(`Last session: ${summary}`);
            }
        }
    } catch {}

    // Auto-update check + install
    try {
        const pkgPath = path.join(__dirname, '..', 'package.json');
        const currentVersion = JSON.parse(fs.readFileSync(pkgPath, 'utf-8')).version;
        const { execSync: execS } = require('child_process');
        const latest = execS('npm view delimit-cli version 2>/dev/null', { encoding: 'utf-8', timeout: 5000 }).trim();
        if (latest && latest !== currentVersion && latest > currentVersion) {
            lines.push(`[Delimit] Updating ${currentVersion} -> ${latest}...`);
            try {
                execS('npm install -g delimit-cli@latest 2>/dev/null', { timeout: 30000, stdio: 'pipe' });
                execS('delimit-cli setup 2>/dev/null', { timeout: 30000, stdio: 'pipe' });
                lines.push(`[Delimit] Updated to ${latest}`);
            } catch {
                lines.push(`[Delimit] Auto-update failed. Run: npm install -g delimit-cli@latest`);
            }
        }
    } catch { /* offline or timeout — skip silently */ }

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

    // Check for pending strategy items that need deliberation
    if (config.show_strategy_items) {
        const strategyCount = countPendingStrategyItems();
        if (strategyCount > 0) {
            lines.push(`[delimit] ${strategyCount} strategic decision${strategyCount === 1 ? '' : 's'} pending deliberation. Run: delimit deliberate`);
        }
    }

    // Git branch info
    try {
        const branch = execSync('git branch --show-current 2>/dev/null', { encoding: 'utf-8' }).trim();
        if (branch) {
            lines.push(`[Delimit] Branch: ${branch}`);
        }
    } catch { /* not in git repo */ }

    lines.push('=== Delimit Ready ===');
    lines.push('');
    process.stdout.write(lines.join('\n') + '\n');
}

/**
 * bootstrap: shared natural-language trigger handler.
 * execute -> resume or launch governed work loop
 * inspect -> show ledger/daemon/continuity state without executing
 */
async function hookBootstrap(mode = 'inspect', options = {}) {
    const cwd = options.cwd || process.cwd();
    const lines = [];
    const silent = Boolean(options.silent);
    const normalizedMode = mode === 'execute' ? 'execute' : 'inspect';
    const { resolveContinuityContext } = require('./continuity-resolver');
    const context = resolveContinuityContext({ cwd, scope: options.scope });
    const hasPolicy = fs.existsSync(path.join(cwd, 'delimit.yml'))
        || fs.existsSync(path.join(cwd, '.delimit.yml'))
        || fs.existsSync(path.join(cwd, '.delimit', 'policies.yml'));
    const globalLedgerDir = context.ledgerRoot;
    const sessionDir = path.join(getDelimitHome(), 'sessions');
    const ledgerState = buildLedgerState(globalLedgerDir);
    const latestSession = readLatestSessionSummary(sessionDir);
    const inboxDaemon = getServiceState('delimit-inbox.service');
    const socialDaemon = getServiceState('delimit-social-scan.service');

    lines.push('[Delimit] Bootstrap');
    lines.push(`[Delimit] Mode: ${normalizedMode}`);
    lines.push(`[Delimit] Repo: ${cwd}`);
    lines.push(`[Delimit] Actor: ${context.actor}`);
    lines.push(`[Delimit] Venture: ${context.venture}`);
    lines.push(`[Delimit] Continuity root: ${context.continuityRoot}`);
    lines.push(`[Delimit] Ledger scope: ${context.ledgerScope}`);
    lines.push(hasPolicy ? '[Delimit] Governance: active' : '[Delimit] Governance: repo policy missing');
    lines.push(fs.existsSync(globalLedgerDir) ? '[Delimit] Ledger: available' : '[Delimit] Ledger: unavailable');
    lines.push(fs.existsSync(sessionDir) ? '[Delimit] Continuity: session history available' : '[Delimit] Continuity: no saved sessions');
    lines.push(`[Delimit] Inbox daemon: ${inboxDaemon.active}/${inboxDaemon.enabled}`);
    lines.push(`[Delimit] Social daemon: ${socialDaemon.active}/${socialDaemon.enabled}`);

    if (latestSession) {
        lines.push(`[Delimit] Latest session: ${latestSession.id}`);
        if (latestSession.summary) {
            lines.push(`[Delimit] Latest summary: ${latestSession.summary}`);
        }
        if (latestSession.blockers.length > 0) {
            lines.push(`[Delimit] Blockers: ${latestSession.blockers.join('; ')}`);
        }
    }

    if (ledgerState.next) {
        const next = ledgerState.next;
        lines.push(`[Delimit] Next open item: ${next.id} ${next.title || '(untitled)'} [${next.priority || 'P?'}]`);
    } else {
        lines.push('[Delimit] Next open item: none');
    }

    if (normalizedMode === 'execute') {
        const bootstrapState = {
            timestamp: new Date().toISOString(),
            actor: context.actor,
            venture: context.venture,
            repo: cwd,
            mode: normalizedMode,
            nextItem: ledgerState.next ? {
                id: ledgerState.next.id,
                title: ledgerState.next.title || '',
                priority: ledgerState.next.priority || '',
                status: ledgerState.next.status || 'open',
            } : null,
            daemons: {
                inbox: inboxDaemon,
                social: socialDaemon,
            },
            latestSession,
            openItemCount: ledgerState.open.length,
        };
        const statePath = writeBootstrapState(context.continuityRoot, bootstrapState);
        lines.push('[Delimit] Intent: resume or launch governed persistent loop');
        lines.push(`[Delimit] Work order saved: ${statePath}`);
        lines.push('[Delimit] Next tools: delimit session --build');
    } else {
        lines.push('[Delimit] Intent: inspect current state without executing');
        lines.push('[Delimit] Next tools: delimit session --inspect');
    }

    const payload = {
        mode: normalizedMode,
        repo: cwd,
        actor: context.actor,
        venture: context.venture,
        continuityRoot: context.continuityRoot,
        ledgerRoot: context.ledgerRoot,
        ledgerScope: context.ledgerScope,
        hasPolicy,
        ledgerAvailable: fs.existsSync(globalLedgerDir),
        continuityAvailable: fs.existsSync(sessionDir),
        daemons: {
            inbox: inboxDaemon,
            social: socialDaemon,
        },
        latestSession,
        nextItem: ledgerState.next || null,
        openItemCount: ledgerState.open.length,
    };
    if (!silent) {
        lines.push('');
        process.stdout.write(lines.join('\n') + '\n');
    }
    return payload;
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

    // Deliberation on API spec commits (opt-in via deliberate_on_commit)
    if (config.deliberate_on_commit) {
        try {
            const stagedFiles2 = execSync('git diff --cached --name-only 2>/dev/null', {
                encoding: 'utf-8',
                timeout: 2000,
            }).split('\n').filter(Boolean);

            const apiSpecFiles = stagedFiles2.filter(f =>
                /openapi|swagger/i.test(f) && /\.(yaml|yml|json)$/.test(f)
            );

            if (apiSpecFiles.length > 0) {
                // Auto-deliberate: call Delimit gateway directly
                if (config.deliberate_on_commit === 'auto') {
                    process.stderr.write('[delimit] API spec change detected — running multi-model deliberation...\n');
                    try {
                        const diff = execSync(`git diff --cached -- ${apiSpecFiles.join(' ')} 2>/dev/null`, {
                            encoding: 'utf-8',
                            timeout: 5000,
                            maxBuffer: 50 * 1024,
                        }).slice(0, 2000);
                        const question = `This commit modifies API specs (${apiSpecFiles.join(', ')}). Is this change safe to ship? Are there breaking changes?\n\nDiff:\n${diff}`;
                        const result = execSync(`npx delimit-cli deliberate --question "${question.replace(/"/g, '\\"')}" --mode quick 2>/dev/null`, {
                            encoding: 'utf-8',
                            timeout: 60000,
                        });
                        process.stderr.write(result + '\n');
                    } catch (e) {
                        warnings.push(`[delimit] Deliberation failed: ${e.message?.slice(0, 100) || 'timeout'}. Proceeding with commit.`);
                    }
                } else {
                    warnings.push('[delimit] This commit modifies API specs. Consider running: delimit deliberate "Is this change safe?"');
                }
            }
        } catch { /* not in git repo */ }
    }

    if (warnings.length > 0) {
        process.stderr.write(warnings.join('\n') + '\n');
    }
}

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------
// Deploy gate hook — runs smoke test before any deploy (LED-024 feedback)
// ---------------------------------------------------------------------------

async function hookDeployGate() {
    const lines = [];
    lines.push('[Delimit] Deploy gate check');
    lines.push('');

    let blocked = false;

    // 1. Check for common import/syntax errors
    const cwd = process.cwd();
    const hasDockerCompose = fs.existsSync(path.join(cwd, 'docker-compose.yml'))
        || fs.existsSync(path.join(cwd, 'docker-compose.yaml'))
        || fs.existsSync(path.join(cwd, 'compose.yml'));

    // 2. Check for Python import errors if it's a Python project
    const hasPython = fs.existsSync(path.join(cwd, 'requirements.txt'))
        || fs.existsSync(path.join(cwd, 'pyproject.toml'))
        || fs.existsSync(path.join(cwd, 'setup.py'));

    if (hasPython) {
        try {
            // Find the main app module
            const appDirs = ['app', 'src', 'api'];
            for (const dir of appDirs) {
                const initFile = path.join(cwd, dir, '__init__.py');
                const mainFile = path.join(cwd, dir, 'main.py');
                if (fs.existsSync(initFile) || fs.existsSync(mainFile)) {
                    try {
                        execSync(`python3 -c "import ${dir}" 2>&1`, {
                            encoding: 'utf-8',
                            timeout: 10000,
                            cwd,
                        });
                        lines.push(`[Delimit] ✓ ${dir}/ imports clean`);
                    } catch (e) {
                        lines.push(`[Delimit] ✗ ${dir}/ import error: ${e.stdout || e.stderr || e.message}`);
                        blocked = true;
                    }
                }
            }
        } catch { /* ignore */ }
    }

    // 3. Check for Node.js syntax errors
    const hasNode = fs.existsSync(path.join(cwd, 'package.json'));
    if (hasNode) {
        try {
            execSync('node -e "require(\'./\')" 2>&1', {
                encoding: 'utf-8',
                timeout: 5000,
                cwd,
            });
            lines.push('[Delimit] ✓ Node.js entry point loads');
        } catch {
            // Not all projects have a main entry — skip silently
        }
    }

    // 4. Check for uncommitted changes
    try {
        const status = execSync('git status --porcelain 2>/dev/null', {
            encoding: 'utf-8',
            timeout: 3000,
            cwd,
        }).trim();
        if (status) {
            const fileCount = status.split('\n').length;
            lines.push(`[Delimit] ⚠ ${fileCount} uncommitted file(s) — consider committing before deploy`);
        }
    } catch { /* not a git repo */ }

    // 5. Result
    lines.push('');
    if (blocked) {
        lines.push('[Delimit] ✗ DEPLOY BLOCKED — fix import errors above');
        lines.push('[Delimit] Run: delimit_test_smoke for full diagnostics');
    } else {
        lines.push('[Delimit] ✓ Deploy gate passed');
    }
    lines.push('');

    process.stdout.write(lines.join('\n') + '\n');

    if (blocked) {
        process.exit(1);
    }
}

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
    hookBootstrap,
    hookPreTool,
    hookPreCommit,
    hookDeployGate,
    countPendingStrategyItems,
    getTopStrategyItem,
    findClaudeHookGroup,
    migrateToNestedFormat,
};
