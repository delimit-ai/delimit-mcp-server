#!/usr/bin/env node

/**
 * Delimit™ Pre-Tool Hook
 * Validates tool usage against governance policies before execution
 */

const axios = require('axios');
const fs = require('fs');
const path = require('path');

const AGENT_URL = `http://127.0.0.1:${process.env.DELIMIT_AGENT_PORT || 7823}`;

async function checkGovernance(toolContext) {
    try {
        // Extract tool information
        const { tool, params, cwd, user } = toolContext;
        
        // Build governance context
        const context = {
            action: 'tool_execution',
            tool: tool,
            parameters: params,
            cwd: cwd,
            user: user,
            timestamp: new Date().toISOString(),
            environment: process.env.NODE_ENV || 'development'
        };
        
        // Special handling for file operations
        if (['Edit', 'Write', 'MultiEdit', 'Delete'].includes(tool)) {
            context.fileOperation = true;
            context.targetPath = params.file_path || params.path;
            
            // Check if targeting sensitive paths
            const sensitivePaths = [
                '/etc/',
                '/usr/bin/',
                '/boot/',
                '/.ssh/',
                '/root/.ssh/',
                '/.aws/',
                '/.config/gcloud/',
                '/credentials/',
                '/secrets/'
            ];
            
            if (context.targetPath && sensitivePaths.some(p => context.targetPath.includes(p))) {
                context.riskLevel = 'high';
                context.sensitiveOperation = true;
            }
        }
        
        // Special handling for command execution
        if (tool === 'Bash') {
            context.commandExecution = true;
            context.command = params.command;
            
            // Check for risky commands
            const riskyCommands = [
                'rm -rf',
                'chmod 777',
                'curl | sh',
                'wget | sh',
                'sudo',
                'pkill',
                'kill -9',
                '>',  // redirect overwrite
                'truncate'
            ];
            
            if (context.command && riskyCommands.some(cmd => context.command.includes(cmd))) {
                context.riskLevel = 'critical';
                context.riskyCommand = true;
            }
        }
        
        // Query governance agent
        const response = await axios.post(`${AGENT_URL}/evaluate`, context);
        
        // Handle the decision
        const decision = response.data;
        
        if (decision.action === 'block') {
            console.error(`[DELIMIT GOVERNANCE] ❌ Tool execution blocked: ${decision.reason}`);
            if (decision.rule) {
                console.error(`[DELIMIT GOVERNANCE] Rule: ${decision.rule}`);
            }
            process.exit(1);
        }
        
        if (decision.action === 'prompt') {
            console.warn(`[DELIMIT GOVERNANCE] ⚠️  Warning: ${decision.message}`);
            // In non-interactive mode, we allow with warning
            // In interactive mode, this would prompt the user
        }
        
        if (decision.action === 'allow') {
            console.log(`[DELIMIT GOVERNANCE] ✅ Tool execution allowed`);
        }
        
    } catch (error) {
        // If governance agent is not available, log and continue
        console.warn('[DELIMIT GOVERNANCE] Governance agent not available, proceeding with caution');
        console.debug(error.message);
    }
}

// Main execution
if (require.main === module) {
    const toolContext = {
        tool: process.env.CLAUDE_TOOL_NAME || process.argv[2],
        params: JSON.parse(process.env.CLAUDE_TOOL_PARAMS || process.argv[3] || '{}'),
        cwd: process.cwd(),
        user: process.env.USER
    };
    
    checkGovernance(toolContext).catch(console.error);
}

module.exports = { checkGovernance };