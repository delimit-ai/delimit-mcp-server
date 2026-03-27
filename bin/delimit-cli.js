#!/usr/bin/env node

const { Command } = require('commander');
const axios = require('axios');
const fs = require('fs');
const path = require('path');
const { execSync, spawn } = require('child_process');
const chalk = require('chalk');
const inquirer = require('inquirer');
const DelimitAuthSetup = require('../lib/auth-setup');
const DelimitHooksInstaller = require('../lib/hooks-installer');
const crossModelHooks = require('../lib/cross-model-hooks');

const AGENT_URL = `http://127.0.0.1:${process.env.DELIMIT_AGENT_PORT || 7823}`;
const program = new Command();

const yaml = require('js-yaml');

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
            stdio: 'ignore'
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
    .version(require('../package.json').version);

// Install command with modes
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
    .description('Show governance status')

    .option('--verbose', 'Show detailed status')
    .action(async (options) => {
        const agentRunning = await checkAgent();
        
        console.log(chalk.blue.bold('\nDelimit Governance Status\n'));
        console.log('Agent:', agentRunning ? chalk.green('✓ Running') : chalk.red('✗ Not running'));
        
        if (agentRunning) {
            const { data } = await axios.get(`${AGENT_URL}/status`);
            
            // Mode information
            console.log('\n' + chalk.bold('Mode Configuration:'));
            console.log(`  Current Mode: ${chalk.bold(data.sessionMode)}`);
            if (data.defaultMode) {
                console.log(`  Default Mode: ${data.defaultMode}`);
            }
            if (data.effectiveMode && data.effectiveMode !== data.sessionMode) {
                console.log(`  Effective Mode: ${chalk.yellow(data.effectiveMode)} (escalated)`);
            }
            
            // Policies
            console.log('\n' + chalk.bold('Policies:'));
            if (data.policiesLoaded.length > 0) {
                data.policiesLoaded.forEach(policy => {
                    console.log(`  • ${policy}`);
                });
                if (data.totalRules) {
                    console.log(`  Total Rules: ${data.totalRules}`);
                }
            } else {
                console.log('  No policies loaded');
            }
            
            // Recent activity
            console.log('\n' + chalk.bold('Activity:'));
            console.log(`  Audit Log Entries: ${data.auditLogSize}`);
            if (data.lastDecision) {
                const timeSince = Date.now() - new Date(data.lastDecision.timestamp);
                const minutes = Math.floor(timeSince / 60000);
                console.log(`  Last Decision: ${minutes} minutes ago (${data.lastDecision.action})`);
            }
            console.log(`  Uptime: ${Math.floor(data.uptime / 60)} minutes`);
            
            // Verbose mode shows recent decisions
            if (options.verbose && data.recentDecisions) {
                console.log('\n' + chalk.bold('Recent Decisions:'));
                data.recentDecisions.forEach(decision => {
                    const color = decision.action === 'block' ? chalk.red :
                                 decision.action === 'prompt' ? chalk.yellow :
                                 chalk.green;
                    console.log(`  ${decision.timestamp} | ${color(decision.mode)} | ${decision.rule || 'no rule'}`);
                });
            }
        }
        
        // System integration
        console.log('\n' + chalk.bold('System Integration:'));
        
        // Git hooks
        try {
            const hooksPath = execSync('git config --global core.hooksPath').toString().trim();
            const hooksActive = hooksPath.includes('.delimit');
            console.log(`  Git Hooks: ${hooksActive ? chalk.green('✓ Active') : chalk.yellow('⚠ Not configured')}`);
        } catch (e) {
            console.log(`  Git Hooks: ${chalk.red('✗ Not configured')}`);
        }
        
        // PATH
        if (process.env.PATH.includes('.delimit/shims')) {
            console.log(`  AI Tool Interception: ${chalk.green('✓ Active')}`);
        } else {
            console.log(`  AI Tool Interception: ${chalk.gray('Not active')}`);
        }
        
        // Policy files
        const policyFiles = [];
        if (fs.existsSync('delimit.yml')) {
            policyFiles.push('project');
        }
        if (fs.existsSync(path.join(process.env.HOME, '.config', 'delimit', 'delimit.yml'))) {
            policyFiles.push('user');
        }
        console.log(`  Policy Files: ${policyFiles.length > 0 ? policyFiles.join(', ') : chalk.gray('none')}`);
        
        if (options.verbose) {
            console.log('\n' + chalk.gray('Run "delimit doctor" for detailed diagnostics'));
        }
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
            // TODO: Implement validation
            console.log(chalk.yellow('Policy validation coming soon'));
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

        // 7. Shims
        const shimsDir = path.join(HOME, '.delimit', 'shims');
        if (fs.existsSync(shimsDir)) {
            changes.push({ target: '~/.delimit/shims/', action: 'Remove CLI shims directory' });
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
    .description('Governance hook handler (session-start | pre-tool | pre-commit)')
    .action(async (event, toolName) => {
        try {
            switch (event) {
                case 'session-start':
                    await crossModelHooks.hookSessionStart();
                    break;
                case 'pre-tool':
                    await crossModelHooks.hookPreTool(toolName || 'unknown');
                    break;
                case 'pre-commit':
                    await crossModelHooks.hookPreCommit();
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

// Init command — scaffold .delimit/ config
program
    .command('init')
    .description('Initialize Delimit API governance in this project')
    .option('--preset <name>', 'Policy preset: strict, default, or relaxed', 'default')
    .action(async (options) => {
        const configDir = path.join(process.cwd(), '.delimit');
        const policyFile = path.join(configDir, 'policies.yml');

        if (fs.existsSync(policyFile)) {
            console.log(chalk.yellow('Already initialized — .delimit/policies.yml exists'));
            return;
        }

        const preset = options.preset.toLowerCase();
        if (!POLICY_PRESETS[preset]) {
            console.log(chalk.red(`Unknown preset "${preset}". Choose: strict, default, or relaxed`));
            return;
        }

        fs.mkdirSync(configDir, { recursive: true });
        fs.writeFileSync(policyFile, POLICY_PRESETS[preset]);
        console.log(chalk.green(`\n  Created .delimit/policies.yml (preset: ${preset})\n`));

        // Auto-detect OpenAPI spec files
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
        const foundSpecs = specPatterns.filter(p => fs.existsSync(path.join(process.cwd(), p)));

        if (foundSpecs.length > 0) {
            const specPath = foundSpecs[0];
            console.log(`  Detected spec: ${chalk.bold(specPath)}`);
            console.log('');
            console.log(chalk.bold('  Workflow template:\n'));
            console.log(chalk.gray(`  name: API Governance
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
        - uses: delimit-ai/delimit@v1
          with:
            old_spec: _base/${specPath}
            new_spec: ${specPath}
            mode: advisory`));
            console.log('');

            // Auto-write the workflow file
            const workflowDir = path.join(process.cwd(), '.github', 'workflows');
            const workflowFile = path.join(workflowDir, 'api-governance.yml');

            if (!fs.existsSync(workflowFile)) {
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
      - uses: delimit-ai/delimit@v1
        with:
          old_spec: _base/${specPath}
          new_spec: ${specPath}
          mode: advisory
`;
                    fs.writeFileSync(workflowFile, workflowContent);
                    console.log(chalk.green(`  Created .github/workflows/api-governance.yml\n`));
                } catch (err) {
                    console.log(chalk.yellow(`  Could not write workflow file: ${err.message}`));
                    console.log(chalk.bold('  Add this to .github/workflows/api-governance.yml manually (shown above)\n'));
                }
            } else {
                console.log(chalk.yellow('  .github/workflows/api-governance.yml already exists — skipped\n'));
            }
        } else {
            console.log('  No OpenAPI spec file detected.');
            console.log(`  Delimit also supports ${chalk.bold('Zero-Spec Mode')} — run ${chalk.bold('delimit lint')} in a FastAPI/NestJS/Express project.`);
            console.log('');
        }

        console.log(`  ${chalk.bold('Presets')}: strict | default | relaxed`);
        console.log(`  Switch: ${chalk.bold('delimit init --preset strict')}\n`);
        console.log('Next steps:');
        console.log(`  ${chalk.bold('delimit lint')} old.yaml new.yaml   — check for breaking changes`);
        console.log(`  ${chalk.bold('delimit diff')} old.yaml new.yaml   — see all changes`);
        console.log(`  ${chalk.bold('delimit explain')} old.yaml new.yaml — human-readable summary`);
    });

// Doctor command — verify setup is correct
program
    .command('doctor')
    .description('Verify Delimit setup and diagnose common issues')
    .action(async () => {
        console.log(chalk.bold('\n  Delimit Doctor\n'));
        let ok = 0;
        let warn = 0;
        let fail = 0;

        // Check policy file
        const policyPath = path.join(process.cwd(), '.delimit', 'policies.yml');
        if (fs.existsSync(policyPath)) {
            console.log(chalk.green('  ✓ .delimit/policies.yml found'));
            ok++;
            try {
                const yaml = require('js-yaml');
                const policy = yaml.load(fs.readFileSync(policyPath, 'utf8'));
                if (policy && (policy.rules !== undefined || policy.override_defaults !== undefined)) {
                    console.log(chalk.green('  ✓ Policy file is valid YAML'));
                    ok++;
                } else {
                    console.log(chalk.yellow('  ⚠ Policy file has no rules section'));
                    warn++;
                }
            } catch (e) {
                console.log(chalk.red(`  ✗ Policy file has invalid YAML: ${e.message}`));
                fail++;
            }
        } else {
            console.log(chalk.red('  ✗ No .delimit/policies.yml — run: delimit init'));
            fail++;
        }

        // Check for OpenAPI spec
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
            console.log(chalk.green(`  ✓ OpenAPI spec found: ${foundSpecs[0]}`));
            ok++;
        } else {
            // Check for framework (Zero-Spec candidate)
            const pkgJson = path.join(process.cwd(), 'package.json');
            const reqTxt = path.join(process.cwd(), 'requirements.txt');
            if (fs.existsSync(pkgJson) || fs.existsSync(reqTxt)) {
                console.log(chalk.yellow('  ⚠ No OpenAPI spec file — Zero-Spec Mode may work if this is a FastAPI/NestJS/Express project'));
                warn++;
            } else {
                console.log(chalk.red('  ✗ No OpenAPI spec file found'));
                fail++;
            }
        }

        // Check for GitHub workflow
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
                console.log(chalk.green('  ✓ GitHub Action workflow found'));
                ok++;
            } else {
                console.log(chalk.yellow('  ⚠ No Delimit GitHub Action workflow — run delimit init for setup instructions'));
                warn++;
            }
        } else {
            console.log(chalk.yellow('  ⚠ No .github/workflows/ directory'));
            warn++;
        }

        // Check git
        try {
            const { execSync } = require('child_process');
            execSync('git rev-parse --git-dir', { stdio: 'pipe' });
            console.log(chalk.green('  ✓ Git repository detected'));
            ok++;
        } catch {
            console.log(chalk.yellow('  ⚠ Not a git repository'));
            warn++;
        }

        // Summary
        console.log('');
        if (fail === 0 && warn === 0) {
            console.log(chalk.green.bold('  All checks passed! Ready to lint.\n'));
            console.log('Keep Building.\n');
        } else if (fail === 0) {
            console.log(chalk.yellow.bold(`  ${ok} passed, ${warn} warning(s). Setup looks good.\n`));
        } else {
            console.log(chalk.red.bold(`  ${ok} passed, ${warn} warning(s), ${fail} error(s). Fix errors above.\n`));
        }
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
                        console.log('  Usage: delimit lint <old_spec> <new_spec>');
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

            // Decision banner
            const decision = result.decision;
            const semver = result.semver;
            const banner = decision === 'fail'
                ? chalk.red.bold('FAIL')
                : decision === 'warn'
                    ? chalk.yellow.bold('WARN')
                    : chalk.green.bold('PASS');

            const bump = semver ? ` — ${chalk.bold(semver.bump.toUpperCase())}` : '';
            const nextVer = semver && semver.next_version ? ` (${semver.next_version})` : '';

            console.log(`\n${banner}${bump}${nextVer}\n`);

            // Summary
            const s = result.summary;
            console.log(`  Changes: ${s.total_changes} total, ${s.breaking_changes} breaking`);
            if (s.violations > 0) {
                console.log(`  Violations: ${s.errors} error(s), ${s.warnings} warning(s)`);
            }
            console.log('');

            // Violations
            const violations = result.violations || [];
            if (violations.length > 0) {
                violations.forEach(v => {
                    const icon = v.severity === 'error' ? chalk.red('ERR') : chalk.yellow('WRN');
                    console.log(`  ${icon}  ${v.message}`);
                    if (v.path) console.log(`       ${chalk.gray(v.path)}`);
                });
                console.log('');
            }

            // Non-breaking changes
            const safe = (result.all_changes || []).filter(c => !c.is_breaking);
            if (safe.length > 0) {
                console.log(chalk.green('  Additions:'));
                safe.forEach(c => console.log(`    + ${c.message}`));
                console.log('');
            }

            if (decision === 'pass') {
                console.log('Keep Building.\n');
            }

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

        // Write license file
        const crypto = require('crypto');
        const machineHash = crypto.createHash('sha256').update(os.homedir()).digest('hex').slice(0, 16);
        const licenseData = {
            key: key,
            tier: 'pro',
            valid: true,
            activated_at: Date.now() / 1000,
            machine_hash: machineHash,
        };

        if (!fs.existsSync(licenseDir)) {
            fs.mkdirSync(licenseDir, { recursive: true });
        }
        fs.writeFileSync(licensePath, JSON.stringify(licenseData, null, 2));
        console.log(chalk.green('License activated successfully.'));
        console.log(chalk.dim('Tier: pro'));
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

// Version subcommand alias (users type 'delimit version' not 'delimit -V')
program
    .command('version')
    .description('Show version')
    .action(() => {
        console.log(require('../package.json').version);
    });

// Hide legacy/internal commands from --help
['install', 'mode', 'status', 'policy', 'auth', 'audit',
 'explain-decision', 'uninstall', 'proxy', 'hook'].forEach(name => {
    const cmd = program.commands.find(c => c.name() === name);
    if (cmd) cmd._hidden = true;
});

program.parse();