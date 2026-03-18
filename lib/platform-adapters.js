/**
 * Delimit™ Platform Adapters
 * Handles different naming conventions and integration methods for various AI platforms
 */

const fs = require('fs');
const path = require('path');
const os = require('os');

class PlatformAdapter {
    constructor() {
        this.HOME = os.homedir();
    }
    
    /**
     * GitHub Codex uses "Skills" instead of hooks
     */
    setupCodexSkills() {
        const codexConfig = {
            configPath: path.join(this.HOME, '.codex', 'skills'),
            configFile: 'skills.json',
            format: 'skill'
        };
        
        const skills = {
            "skills": [
                {
                    "name": "delimit-governance",
                    "description": "Governance validation skill",
                    "type": "validation",
                    "handler": "/home/delimit/npm-delimit/adapters/codex-skill.js",
                    "triggers": ["pre-code-generation", "pre-suggestion"],
                    "enabled": true
                },
                {
                    "name": "delimit-security",
                    "description": "Security validation skill",
                    "type": "security",
                    "handler": "/home/delimit/npm-delimit/adapters/codex-security.js",
                    "enabled": true
                }
            ],
            "commands": {
                "governance": {
                    "description": "Check governance status",
                    "handler": "/home/delimit/npm-delimit/bin/delimit-cli.js",
                    "args": ["status"]
                }
            }
        };
        
        // Create Codex skills directory
        fs.mkdirSync(codexConfig.configPath, { recursive: true });
        fs.writeFileSync(
            path.join(codexConfig.configPath, codexConfig.configFile),
            JSON.stringify(skills, null, 2)
        );
        
        console.log('✓ Codex Skills configured');
        return codexConfig;
    }
    
    /**
     * Google Gemini uses "Extensions" and "Actions"
     */
    setupGeminiExtensions() {
        const geminiConfig = {
            configPath: path.join(this.HOME, '.gemini', 'extensions'),
            configFile: 'extensions.yaml',
            format: 'extension'
        };
        
        const extensions = `# Gemini Extensions Configuration
extensions:
  - id: delimit-governance
    name: Delimit Governance Extension
    version: 2.0.0
    actions:
      - id: validate-code
        name: Validate Code
        trigger: before_code_generation
        handler: /home/delimit/npm-delimit/adapters/gemini-action.js
      - id: collect-evidence
        name: Collect Evidence
        trigger: after_response
        handler: /home/delimit/npm-delimit/adapters/gemini-evidence.js
    commands:
      - command: "@governance"
        description: Check governance status
        action: run_script
        script: /home/delimit/npm-delimit/bin/delimit-cli.js
        args: ["status"]
`;
        
        // Create Gemini extensions directory
        fs.mkdirSync(geminiConfig.configPath, { recursive: true });
        fs.writeFileSync(
            path.join(geminiConfig.configPath, geminiConfig.configFile),
            extensions
        );
        
        console.log('✓ Gemini Extensions configured');
        return geminiConfig;
    }
    
    /**
     * xAI Grok uses "Plugins"
     */
    setupXAIPlugins() {
        const xaiConfig = {
            configPath: path.join(this.HOME, '.xai', 'plugins'),
            configFile: 'plugins.toml',
            format: 'plugin'
        };
        
        const plugins = `# xAI Grok Plugins Configuration

[[plugins]]
name = "delimit-governance"
version = "2.0.0"
description = "Delimit Governance Plugin"
entry_point = "/home/delimit/npm-delimit/adapters/xai-plugin.js"

[plugins.hooks]
pre_prompt = true
post_response = true
code_validation = true

[plugins.commands]
governance = { cmd = "/home/delimit/npm-delimit/bin/delimit-cli.js", args = ["status"] }
audit = { cmd = "/home/delimit/npm-delimit/bin/delimit-cli.js", args = ["audit"] }
`;
        
        // Create xAI plugins directory
        fs.mkdirSync(xaiConfig.configPath, { recursive: true });
        fs.writeFileSync(
            path.join(xaiConfig.configPath, xaiConfig.configFile),
            plugins
        );
        
        console.log('✓ xAI Plugins configured');
        return xaiConfig;
    }
    
    /**
     * OpenAI uses "Functions" and "Tools"
     */
    setupOpenAITools() {
        const openaiConfig = {
            configPath: path.join(this.HOME, '.openai', 'tools'),
            configFile: 'tools.json',
            format: 'function'
        };
        
        const tools = {
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "delimit_governance_check",
                        "description": "Check governance compliance",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "description": "The action to validate"
                                },
                                "context": {
                                    "type": "object",
                                    "description": "Context for validation"
                                }
                            }
                        },
                        "handler": "/home/delimit/npm-delimit/adapters/openai-function.js"
                    }
                }
            ],
            "plugins": [
                {
                    "name": "delimit-governance",
                    "schema_version": "v1",
                    "api": {
                        "url": "http://localhost:7823/api",
                        "is_user_authenticated": false
                    }
                }
            ]
        };
        
        // Create OpenAI tools directory
        fs.mkdirSync(openaiConfig.configPath, { recursive: true });
        fs.writeFileSync(
            path.join(openaiConfig.configPath, openaiConfig.configFile),
            JSON.stringify(tools, null, 2)
        );
        
        console.log('✓ OpenAI Tools configured');
        return openaiConfig;
    }
    
    /**
     * Cursor uses "Extensions" (VSCode-based)
     */
    setupCursorExtensions() {
        const cursorConfig = {
            configPath: path.join(this.HOME, '.cursor', 'extensions'),
            configFile: 'extensions.json',
            format: 'vscode-extension'
        };
        
        const extensions = {
            "extensions": [
                {
                    "id": "delimit.governance",
                    "name": "Delimit Governance",
                    "version": "2.0.0",
                    "main": "/home/delimit/npm-delimit/adapters/cursor-extension.js",
                    "activationEvents": [
                        "onCommand:delimit.checkGovernance",
                        "onLanguage:javascript",
                        "onLanguage:typescript",
                        "onLanguage:python"
                    ],
                    "contributes": {
                        "commands": [
                            {
                                "command": "delimit.checkGovernance",
                                "title": "Delimit: Check Governance"
                            },
                            {
                                "command": "delimit.switchMode",
                                "title": "Delimit: Switch Mode"
                            }
                        ],
                        "configuration": {
                            "type": "object",
                            "properties": {
                                "delimit.mode": {
                                    "type": "string",
                                    "default": "advisory",
                                    "enum": ["advisory", "guarded", "enforce"]
                                }
                            }
                        }
                    }
                }
            ]
        };
        
        // Create Cursor extensions directory
        fs.mkdirSync(cursorConfig.configPath, { recursive: true });
        fs.writeFileSync(
            path.join(cursorConfig.configPath, cursorConfig.configFile),
            JSON.stringify(extensions, null, 2)
        );
        
        console.log('✓ Cursor Extensions configured');
        return cursorConfig;
    }
    
    /**
     * Windsurf uses "Workflows" and "Automations"
     */
    setupWindsurfWorkflows() {
        const windsurfConfig = {
            configPath: path.join(this.HOME, '.windsurf', 'workflows'),
            configFile: 'workflows.yaml',
            format: 'workflow'
        };
        
        const workflows = `# Windsurf Workflows Configuration
workflows:
  - name: delimit-governance
    description: Governance validation workflow
    version: 2.0.0
    triggers:
      - event: pre_generation
        handler: /home/delimit/npm-delimit/adapters/windsurf-trigger.js
      - event: post_generation
        handler: /home/delimit/npm-delimit/adapters/windsurf-validate.js
    automations:
      - name: validate_code
        trigger: on_code_change
        action: run_validation
      - name: collect_evidence
        trigger: on_commit
        action: store_evidence
    commands:
      governance:
        description: Check governance status
        script: /home/delimit/npm-delimit/bin/delimit-cli.js
        args: [status]
`;
        
        // Create Windsurf workflows directory
        fs.mkdirSync(windsurfConfig.configPath, { recursive: true });
        fs.writeFileSync(
            path.join(windsurfConfig.configPath, windsurfConfig.configFile),
            workflows
        );
        
        console.log('✓ Windsurf Workflows configured');
        return windsurfConfig;
    }
    
    /**
     * Anthropic Claude uses standard "hooks"
     */
    setupClaudeHooks() {
        // This is already handled by the main hooks.json
        console.log('✓ Claude hooks already configured');
        return {
            configPath: path.join(this.HOME, '.claude', 'hooks'),
            configFile: 'hooks.json',
            format: 'hook'
        };
    }
    
    /**
     * Detect installed AI tools and setup appropriate configurations
     */
    async setupAllPlatforms() {
        const results = {};
        
        // Check for each platform and set up if found
        const platforms = [
            { name: 'claude', check: '.claude', setup: () => this.setupClaudeHooks() },
            { name: 'codex', check: '.codex', setup: () => this.setupCodexSkills() },
            { name: 'gemini', check: '.gemini', setup: () => this.setupGeminiExtensions() },
            { name: 'xai', check: '.xai', setup: () => this.setupXAIPlugins() },
            { name: 'openai', check: '.openai', setup: () => this.setupOpenAITools() },
            { name: 'cursor', check: '.cursor', setup: () => this.setupCursorExtensions() },
            { name: 'windsurf', check: '.windsurf', setup: () => this.setupWindsurfWorkflows() }
        ];
        
        for (const platform of platforms) {
            const platformPath = path.join(this.HOME, platform.check);
            // Always create the configuration, even if platform isn't installed yet
            // This ensures it will work when the platform is installed later
            try {
                results[platform.name] = platform.setup();
                console.log(`✓ ${platform.name} configuration created`);
            } catch (error) {
                console.error(`⚠️  Failed to configure ${platform.name}: ${error.message}`);
            }
        }
        
        return results;
    }
}

module.exports = PlatformAdapter;