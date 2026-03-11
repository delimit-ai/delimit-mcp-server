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
    .description('Dynamic AI Governance with seamless mode switching')
    .version('2.0.0');

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

// Doctor command - diagnose issues
program
    .command('doctor')
    .description('Diagnose Delimit configuration and issues')
    .action(async () => {
        console.log(chalk.blue.bold('\n🩺 Delimit Doctor\n'));
        const issues = [];
        const warnings = [];
        const info = [];
        
        // Check agent status
        const agentRunning = await checkAgent();
        if (!agentRunning) {
            issues.push('Agent is not running. Run "delimit status" to start it.');
        } else {
            info.push('Agent is running and responsive');
        }
        
        // Check Git hooks
        try {
            const hooksPath = execSync('git config --global core.hooksPath').toString().trim();
            if (hooksPath.includes('.delimit')) {
                info.push('Git hooks are configured correctly');
                
                // Check hook files exist
                const hookFiles = ['pre-commit', 'pre-push'];
                hookFiles.forEach(hook => {
                    const hookFile = path.join(hooksPath, hook);
                    if (!fs.existsSync(hookFile)) {
                        warnings.push(`Missing hook file: ${hook}`);
                    }
                });
            } else {
                warnings.push('Git hooks not pointing to Delimit. Run "delimit install" to fix.');
            }
        } catch (e) {
            issues.push('Git hooks not configured. Run "delimit install" to set up.');
        }
        
        // Check PATH
        const pathHasDelimit = process.env.PATH.includes('.delimit/shims');
        if (pathHasDelimit) {
            warnings.push('PATH hijacking is active (for AI tool interception)');
        } else {
            info.push('PATH is clean (no AI tool interception)');
        }
        
        // Check policy files
        const policies = [];
        if (fs.existsSync('delimit.yml')) {
            policies.push('project');
            // Validate policy
            try {
                const policy = yaml.load(fs.readFileSync('delimit.yml', 'utf8'));
                if (!policy.rules) {
                    warnings.push('Project policy has no rules defined');
                }
            } catch (e) {
                issues.push(`Project policy is invalid: ${e.message}`);
            }
        }
        
        const userPolicyPath = path.join(process.env.HOME, '.config', 'delimit', 'delimit.yml');
        if (fs.existsSync(userPolicyPath)) {
            policies.push('user');
        }
        
        if (policies.length === 0) {
            warnings.push('No policy files found. Run "delimit policy --init" to create one.');
        } else {
            info.push(`Policy files loaded: ${policies.join(', ')}`);
        }
        
        // Check audit log
        const auditDir = path.join(process.env.HOME, '.delimit', 'audit');
        if (fs.existsSync(auditDir)) {
            const files = fs.readdirSync(auditDir);
            info.push(`Audit log has ${files.length} day(s) of history`);
        } else {
            warnings.push('No audit logs found yet');
        }
        
        // Display results
        if (issues.length > 0) {
            console.log(chalk.red.bold('❌ Issues Found:\n'));
            issues.forEach(issue => console.log(chalk.red(`  • ${issue}`)));
            console.log();
        }
        
        if (warnings.length > 0) {
            console.log(chalk.yellow.bold('⚠️  Warnings:\n'));
            warnings.forEach(warning => console.log(chalk.yellow(`  • ${warning}`)));
            console.log();
        }
        
        if (info.length > 0) {
            console.log(chalk.green.bold('✅ Working Correctly:\n'));
            info.forEach(item => console.log(chalk.green(`  • ${item}`)));
            console.log();
        }
        
        // Overall status
        if (issues.length === 0) {
            console.log(chalk.green.bold('🎉 Delimit is healthy!'));
        } else {
            console.log(chalk.red.bold('🔧 Please fix the issues above'));
            process.exit(1);
        }
    });

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
    .description('Remove Delimit governance')
    .action(async () => {
        const { confirm } = await inquirer.prompt([{
            type: 'confirm',
            name: 'confirm',
            message: 'This will remove all Delimit governance. Continue?',
            default: false
        }]);
        
        if (!confirm) return;
        
        // Remove Git hooks
        try {
            execSync('git config --global --unset core.hooksPath');
            console.log(chalk.green('✓ Removed Git hooks'));
        } catch (e) {}
        
        // Remove from PATH
        const profiles = ['.bashrc', '.zshrc', '.profile'];
        profiles.forEach(profile => {
            const profilePath = path.join(process.env.HOME, profile);
            if (fs.existsSync(profilePath)) {
                let content = fs.readFileSync(profilePath, 'utf8');
                content = content.replace(/# Delimit Governance Layer[\s\S]*?fi\n/g, '');
                fs.writeFileSync(profilePath, content);
            }
        });
        console.log(chalk.green('✓ Removed PATH modifications'));
        
        console.log(chalk.yellow('\nRestart your terminal to complete uninstallation'));
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

// Hook handler (called by Git hooks)
program
    .command('hook <type>')
    .description('Internal hook handler')
    .action(async (type) => {
        await ensureAgent();
        
        // Gather context
        const context = {
            command: type,
            pwd: process.cwd(),
            gitBranch: 'unknown',
            files: [],
            diff: ''
        };
        
        // Try to get Git info, but don't fail if not in repo
        try {
            context.gitBranch = execSync('git branch --show-current 2>/dev/null').toString().trim() || 'unknown';
        } catch (e) {
            // Not in a Git repo or Git not available
            context.gitBranch = 'unknown';
        }
        
        if (type === 'pre-commit') {
            try {
                context.files = execSync('git diff --cached --name-only 2>/dev/null').toString().split('\n').filter(f => f);
                context.diff = execSync('git diff --cached 2>/dev/null').toString();
            } catch (e) {
                // Not in a Git repo or no staged changes
                context.files = [];
                context.diff = '';
            }
        } else if (type === 'pre-push') {
            try {
                // Get commits to be pushed
                context.files = execSync('git diff --name-only @{upstream}...HEAD 2>/dev/null').toString().split('\n').filter(f => f);
                context.diff = execSync('git diff @{upstream}...HEAD 2>/dev/null').toString();
            } catch (e) {
                // No upstream or not in repo
                context.files = [];
                context.diff = '';
            }
        }
        
        // Query agent for decision
        const { data: decision } = await axios.post(`${AGENT_URL}/evaluate`, context);
        
        // Display decision
        if (decision.message) {
            const color = decision.action === 'block' ? chalk.red :
                         decision.action === 'prompt' ? chalk.yellow :
                         chalk.blue;
            console.log(color(decision.message));
        }
        
        // Handle the decision
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
                
                if (!override) {
                    process.exit(1);
                }
            }
        } else if (decision.action === 'prompt') {
            const { proceed } = await inquirer.prompt([{
                type: 'confirm',
                name: 'proceed',
                message: 'Continue with this action?',
                default: false
            }]);
            
            if (!proceed) {
                process.exit(1);
            }
        }
        
        // Action allowed
        process.exit(0);
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
        console.log(chalk.green(`Created .delimit/policies.yml (preset: ${preset})`));
        console.log('');
        console.log(`  ${chalk.bold('strict')}  — zero tolerance, all breaking changes are errors`);
        console.log(`  ${chalk.bold('default')} — balanced, blocks destructive changes, warns on risky`);
        console.log(`  ${chalk.bold('relaxed')} — warnings only, never blocks CI`);
        console.log('');
        console.log(`Switch preset: ${chalk.bold('delimit init --preset strict')}`);
        console.log('');
        console.log('Next steps:');
        console.log(`  ${chalk.bold('delimit lint')} old.yaml new.yaml   — check for breaking changes`);
        console.log(`  ${chalk.bold('delimit diff')} old.yaml new.yaml   — see all changes`);
        console.log(`  ${chalk.bold('delimit explain')} old.yaml new.yaml — human-readable summary`);
    });

// Lint command — diff + policy (primary command)
// Supports zero-spec mode: `delimit lint` (no args) auto-extracts from FastAPI
program
    .command('lint [old_spec] [new_spec]')
    .description('Lint API specs for breaking changes and policy violations')
    .option('-p, --policy <file>', 'Custom policy file')
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
                result = apiEngine.lint(
                    path.resolve(oldSpec),
                    path.resolve(newSpec),
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
            const result = apiEngine.diff(
                path.resolve(oldSpec),
                path.resolve(newSpec)
            );

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
            const result = apiEngine.explain(
                path.resolve(oldSpec),
                path.resolve(newSpec),
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

program.parse();