#!/usr/bin/env node

/**
 * Delimit Proxy Handler - Intercepts AI tool commands
 * This module wraps AI tools (claude, gemini, codex) with governance
 */

const { spawn } = require('child_process');
const axios = require('axios');
const path = require('path');
const fs = require('fs');

const AGENT_URL = `http://127.0.0.1:${process.env.DELIMIT_AGENT_PORT || 7823}`;

async function proxyAITool(tool, args) {
    console.log(`\x1b[34m\x1b[1m[Delimit]\x1b[0m Governance check for ${tool}...`);
    
    // Gather context for governance decision
    const context = {
        command: `ai-tool-${tool}`,
        pwd: process.cwd(),
        gitBranch: 'unknown',
        files: [],
        tool: tool,
        args: args.join(' ')
    };
    
    // Try to get Git branch if in repo
    try {
        const { execSync } = require('child_process');
        context.gitBranch = execSync('git branch --show-current 2>/dev/null').toString().trim() || 'unknown';
    } catch (e) {}
    
    // Check if agent is running
    let decision;
    try {
        const response = await axios.post(`${AGENT_URL}/evaluate`, context);
        decision = response.data;
    } catch (e) {
        // Agent not running - allow with warning
        console.log(`\x1b[33m\x1b[1m[Delimit WARNING]\x1b[0m Agent not running - proceeding without governance`);
        decision = { action: 'allow' };
    }
    
    // Display decision
    if (decision.message) {
        console.log(decision.message);
    }
    
    // Handle the decision
    if (decision.action === 'block') {
        console.error(`\x1b[31m\x1b[1m[Delimit BLOCKED]\x1b[0m ${tool} execution blocked by governance policy`);
        console.error(`Run 'delimit explain last' for details`);
        process.exit(1);
    } else if (decision.action === 'prompt') {
        // In non-interactive mode, treat as block
        console.error(`\x1b[33m\x1b[1m[Delimit GUARDED]\x1b[0m ${tool} requires confirmation`);
        console.error(`Use --force flag to bypass or switch to advisory mode`);
        process.exit(1);
    }
    
    // Find the real tool
    const originalPath = process.env.PATH.replace(/[^:]*delimit[^:]*:?/g, '');
    const realTool = findExecutable(tool, originalPath);
    
    if (!realTool) {
        console.error(`\x1b[31m\x1b[1m[Delimit ERROR]\x1b[0m Original ${tool} not found`);
        process.exit(127);
    }
    
    // Execute the real tool
    console.log(`\x1b[32m\x1b[1m[Delimit ALLOWED]\x1b[0m Executing ${tool}...`);
    const child = spawn(realTool, args, {
        stdio: 'inherit',
        env: { ...process.env, DELIMIT_WRAPPED: 'true' }
    });
    
    child.on('exit', (code) => {
        process.exit(code || 0);
    });
}

function findExecutable(command, searchPath) {
    const paths = searchPath.split(':');
    for (const dir of paths) {
        const fullPath = path.join(dir, command);
        try {
            fs.accessSync(fullPath, fs.constants.X_OK);
            return fullPath;
        } catch (e) {
            // Continue searching
        }
    }
    return null;
}

// Export for use as module
module.exports = { proxyAITool };

// If run directly, handle as CLI
if (require.main === module) {
    const tool = process.argv[2];
    const args = process.argv.slice(3);
    
    if (!tool) {
        console.error('Usage: proxy-handler <tool> [args...]');
        process.exit(1);
    }
    
    proxyAITool(tool, args).catch(err => {
        console.error(`\x1b[31m\x1b[1m[Delimit ERROR]\x1b[0m`, err.message);
        process.exit(1);
    });
}