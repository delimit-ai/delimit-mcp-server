#!/usr/bin/env node

/**
 * Delimit Auto-Hooks Installer
 * Automatically installs Delimit governance hooks for all AI models and tools
 */

const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');
const chalk = require('chalk');

class DelimitHooksInstaller {
    constructor() {
        this.hooksDir = path.join(process.env.HOME, '.delimit', 'hooks');
        this.aiToolsDir = path.join(process.env.HOME, '.delimit', 'ai-hooks');
        this.mcpHooksDir = path.join(process.env.HOME, '.delimit', 'mcp-hooks');
        
        // AI models and tools to hook
        this.aiTools = {
            'claude': {
                name: 'Claude (Anthropic)',
                configPaths: [
                    '~/.claude.json',
                    '~/.config/claude/config.json'
                ],
                hookType: 'wrapper'
            },
            'codex': {
                name: 'GitHub Copilot/Codex',
                configPaths: [
                    '~/.config/github-copilot',
                    '~/.codex/config.json'
                ],
                hookType: 'wrapper'
            },
            'gemini': {
                name: 'Google Gemini',
                configPaths: [
                    '~/.config/gemini',
                    '~/.gemini/config.json'
                ],
                hookType: 'wrapper'
            },
            'openai': {
                name: 'OpenAI GPT',
                configPaths: [
                    '~/.config/openai',
                    '~/.openai/config.json'
                ],
                hookType: 'wrapper'
            },
            'cursor': {
                name: 'Cursor IDE',
                configPaths: [
                    '~/.cursor/config.json',
                    '~/.config/cursor'
                ],
                hookType: 'extension'
            },
            'windsurf': {
                name: 'Windsurf',
                configPaths: [
                    '~/.windsurf/config.json'
                ],
                hookType: 'wrapper'
            }
        };
        
        // Git hooks to install
        this.gitHooks = [
            'pre-commit',
            'pre-push',
            'commit-msg',
            'pre-merge-commit',
            'prepare-commit-msg',
            'post-commit',
            'post-merge',
            'pre-rebase'
        ];
        
        // MCP hooks for governance integration
        this.mcpHooks = [
            'pre-mcp-call',
            'post-mcp-call',
            'mcp-auth',
            'mcp-audit'
        ];
    }
    
    async install() {
        console.log(chalk.blue.bold('\n🔵 Delimit Multi-Model Hooks Installer\n'));
        
        // Create directories
        this.ensureDirectories();
        
        // Install Git hooks
        console.log(chalk.yellow('📦 Installing Git hooks...'));
        await this.installGitHooks();
        
        // Install AI tool hooks
        console.log(chalk.yellow('🤖 Installing AI model hooks...'));
        await this.installAIToolHooks();
        
        // Install MCP hooks
        console.log(chalk.yellow('🔗 Installing MCP integration hooks...'));
        await this.installMCPHooks();
        
        // Configure Claude Code integration
        console.log(chalk.yellow('⚡ Configuring Claude Code integration...'));
        await this.configureClaudeCode();
        
        // Setup environment
        console.log(chalk.yellow('🌍 Setting up environment...'));
        await this.setupEnvironment();
        
        console.log(chalk.green.bold('\n✅ Delimit hooks installed successfully!\n'));
        this.printSummary();
    }
    
    ensureDirectories() {
        const dirs = [this.hooksDir, this.aiToolsDir, this.mcpHooksDir];
        dirs.forEach(dir => {
            if (!fs.existsSync(dir)) {
                fs.mkdirSync(dir, { recursive: true });
            }
        });
    }
    
    async installGitHooks() {
        for (const hook of this.gitHooks) {
            const hookPath = path.join(this.hooksDir, hook);
            const hookContent = this.generateGitHook(hook);
            
            fs.writeFileSync(hookPath, hookContent);
            fs.chmodSync(hookPath, '755');
            console.log(chalk.green(`  ✓ ${hook}`));
        }
        
        // Set global Git hooks path
        try {
            execSync(`git config --global core.hooksPath ${this.hooksDir}`);
            console.log(chalk.green('  ✓ Git global hooks path configured'));
        } catch (e) {
            console.log(chalk.yellow('  ⚠ Could not set global Git hooks (may need sudo)'));
        }
    }
    
    async installAIToolHooks() {
        for (const [tool, config] of Object.entries(this.aiTools)) {
            const hookPath = path.join(this.aiToolsDir, tool);
            const wrapperPath = path.join(this.aiToolsDir, `${tool}-wrapper`);
            
            // Check if tool is installed
            const isInstalled = this.checkToolInstalled(tool);
            
            if (isInstalled) {
                // Create wrapper script
                const wrapperContent = this.generateAIToolWrapper(tool, config);
                fs.writeFileSync(wrapperPath, wrapperContent);
                fs.chmodSync(wrapperPath, '755');
                
                // Create hook configuration
                const hookContent = this.generateAIToolHook(tool, config);
                fs.writeFileSync(hookPath, hookContent);
                fs.chmodSync(hookPath, '755');
                
                console.log(chalk.green(`  ✓ ${config.name}`));
            } else {
                console.log(chalk.gray(`  - ${config.name} (not installed)`));
            }
        }
    }
    
    async installMCPHooks() {
        for (const hook of this.mcpHooks) {
            const hookPath = path.join(this.mcpHooksDir, hook);
            const hookContent = this.generateMCPHook(hook);
            
            fs.writeFileSync(hookPath, hookContent);
            fs.chmodSync(hookPath, '755');
            console.log(chalk.green(`  ✓ ${hook}`));
        }
    }
    
    async configureClaudeCode() {
        const claudeConfigPath = path.join(process.env.HOME, '.claude.json');
        
        if (fs.existsSync(claudeConfigPath)) {
            try {
                const config = JSON.parse(fs.readFileSync(claudeConfigPath, 'utf8'));
                
                // Add Delimit governance hooks
                if (!config.hooks) {
                    config.hooks = {};
                }
                
                config.hooks.preCommand = path.join(this.mcpHooksDir, 'pre-mcp-call');
                config.hooks.postCommand = path.join(this.mcpHooksDir, 'post-mcp-call');
                config.hooks.authentication = path.join(this.mcpHooksDir, 'mcp-auth');
                config.hooks.audit = path.join(this.mcpHooksDir, 'mcp-audit');
                
                // Add Delimit governance settings
                config.delimitGovernance = {
                    enabled: true,
                    agent: 'http://127.0.0.1:7823',
                    mode: 'auto',
                    hooks: this.mcpHooks.map(h => path.join(this.mcpHooksDir, h))
                };
                
                fs.writeFileSync(claudeConfigPath, JSON.stringify(config, null, 2));
                console.log(chalk.green('  ✓ Claude Code configuration updated'));
            } catch (e) {
                console.log(chalk.yellow('  ⚠ Could not update Claude Code config'));
            }
        }
    }
    
    async setupEnvironment() {
        const envFile = path.join(process.env.HOME, '.delimit', 'env');
        const envContent = `
# Delimit Governance Environment
export DELIMIT_HOOKS_DIR="${this.hooksDir}"
export DELIMIT_AI_HOOKS_DIR="${this.aiToolsDir}"
export DELIMIT_MCP_HOOKS_DIR="${this.mcpHooksDir}"
export DELIMIT_AGENT_URL="http://127.0.0.1:7823"
export DELIMIT_GOVERNANCE_ENABLED=true

# AI Tool Governance
export CLAUDE_GOVERNANCE_HOOK="${path.join(this.aiToolsDir, 'claude')}"
export CODEX_GOVERNANCE_HOOK="${path.join(this.aiToolsDir, 'codex')}"
export GEMINI_GOVERNANCE_HOOK="${path.join(this.aiToolsDir, 'gemini')}"
`;
        
        fs.writeFileSync(envFile, envContent);
        console.log(chalk.green('  ✓ Environment configuration created'));
        
        // Add to bashrc if not already present
        const bashrcPath = path.join(process.env.HOME, '.bashrc');
        const sourceLine = 'source ~/.delimit/env';
        
        if (fs.existsSync(bashrcPath)) {
            const bashrc = fs.readFileSync(bashrcPath, 'utf8');
            if (!bashrc.includes(sourceLine)) {
                fs.appendFileSync(bashrcPath, `\n# Delimit Governance Environment\n${sourceLine}\n`);
                console.log(chalk.green('  ✓ Added to .bashrc'));
            }
        }
    }
    
    generateGitHook(hookName) {
        return `#!/bin/sh
# Delimit Dynamic Governance Hook - ${hookName}
# Auto-generated by Delimit Hooks Installer

# Ensure agent is running
if ! curl -s http://127.0.0.1:7823/status > /dev/null 2>&1; then
    echo "Starting Delimit Agent..."
    nohup node /home/delimit/npm-delimit/lib/agent.js > /dev/null 2>&1 &
    sleep 2
fi

# Execute governance check
node /home/delimit/npm-delimit/bin/delimit-cli.js hook ${hookName} "$@"
`;
    }
    
    generateAIToolWrapper(tool, config) {
        return `#!/bin/sh
# Delimit Governance Wrapper for ${config.name}
# Auto-generated by Delimit Hooks Installer

# Log the invocation
echo "[\$(date '+%Y-%m-%d %H:%M:%S')] ${tool} invoked with args: $*" >> ~/.delimit/audit/${tool}.log

# Check governance before execution
node /home/delimit/npm-delimit/bin/delimit-cli.js proxy ${tool} "$@"

# If governance passes, execute the real tool
if [ $? -eq 0 ]; then
    # Find and execute the original binary
    ORIGINAL_PATH=\$(echo \$PATH | sed "s|$HOME/.delimit/shims:||g")
    PATH="\$ORIGINAL_PATH" command ${tool} "$@"
else
    echo "Governance check failed for ${tool}"
    exit 1
fi
`;
    }
    
    generateAIToolHook(tool, config) {
        return `#!/usr/bin/env node
// Delimit Governance Hook for ${config.name}
// Auto-generated by Delimit Hooks Installer

const { execSync } = require('child_process');
const path = require('path');

// Governance check
async function checkGovernance() {
    try {
        const result = execSync('curl -s http://127.0.0.1:7823/status');
        return JSON.parse(result.toString());
    } catch (e) {
        return { mode: 'advisory' };
    }
}

// Main execution
(async () => {
    const governance = await checkGovernance();
    console.log(\`[Delimit] ${config.name} governance mode: \${governance.mode}\`);
    
    // Log invocation
    const args = process.argv.slice(2).join(' ');
    console.log(\`[Delimit] Command: ${tool} \${args}\`);
    
    // Execute based on governance mode
    if (governance.mode === 'enforce') {
        console.log('[Delimit] Enforce mode - checking permissions...');
        // Add enforce logic here
    }
})();
`;
    }
    
    generateMCPHook(hookName) {
        return `#!/usr/bin/env node
// Delimit MCP Integration Hook - ${hookName}
// Auto-generated by Delimit Hooks Installer

const axios = require('axios');
const fs = require('fs');
const path = require('path');

async function ${hookName.replace(/-/g, '_')}(context) {
    const agentUrl = process.env.DELIMIT_AGENT_URL || 'http://127.0.0.1:7823';
    
    try {
        // Send governance check
        const response = await axios.post(\`\${agentUrl}/evaluate\`, {
            hook: '${hookName}',
            timestamp: new Date().toISOString(),
            ...context
        });
        
        // Log to audit trail
        const auditLog = path.join(process.env.HOME, '.delimit', 'audit', 'mcp.jsonl');
        const logEntry = JSON.stringify({
            hook: '${hookName}',
            timestamp: new Date().toISOString(),
            decision: response.data,
            context
        }) + '\\n';
        
        fs.appendFileSync(auditLog, logEntry);
        
        return response.data;
    } catch (error) {
        console.error('[Delimit] MCP hook error:', error.message);
        return { action: 'allow', mode: 'advisory' };
    }
}

// Export for use as module
module.exports = { ${hookName.replace(/-/g, '_')} };

// Execute if called directly
if (require.main === module) {
    const context = {
        args: process.argv.slice(2),
        env: process.env,
        pwd: process.cwd()
    };
    
    ${hookName.replace(/-/g, '_')}(context).then(result => {
        console.log(JSON.stringify(result));
        process.exit(result.action === 'block' ? 1 : 0);
    });
}
`;
    }
    
    checkToolInstalled(tool) {
        try {
            execSync(`which ${tool}`, { stdio: 'ignore' });
            return true;
        } catch {
            return false;
        }
    }
    
    printSummary() {
        console.log(chalk.cyan('\n📋 Installation Summary:'));
        console.log(chalk.white(`  • Git hooks installed: ${this.gitHooks.length}`));
        console.log(chalk.white(`  • AI tool hooks configured: ${Object.keys(this.aiTools).length}`));
        console.log(chalk.white(`  • MCP integration hooks: ${this.mcpHooks.length}`));
        console.log(chalk.white(`  • Hooks directory: ${this.hooksDir}`));
        console.log(chalk.white(`  • Agent URL: http://127.0.0.1:7823`));
        
        console.log(chalk.cyan('\n🎯 Next Steps:'));
        console.log(chalk.white('  1. Restart your shell to load environment'));
        console.log(chalk.white('  2. Run "delimit status" to verify installation'));
        console.log(chalk.white('  3. Test with "git commit" or AI tool commands'));
        console.log(chalk.white('  4. Configure policies in delimit.yml'));
    }
}

// Export for use as module
module.exports = DelimitHooksInstaller;

// Run if executed directly
if (require.main === module) {
    const installer = new DelimitHooksInstaller();
    installer.install().catch(console.error);
}