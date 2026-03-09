#!/usr/bin/env node

/**
 * Delimit™ Post-Tool Hook
 * Collects evidence and audit information after tool execution
 */

const axios = require('axios');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const AGENT_URL = `http://127.0.0.1:${process.env.DELIMIT_AGENT_PORT || 7823}`;
const EVIDENCE_DIR = path.join(process.env.HOME, '.delimit', 'evidence');

async function collectEvidence(toolContext) {
    try {
        // Ensure evidence directory exists
        fs.mkdirSync(EVIDENCE_DIR, { recursive: true });
        
        // Extract tool information
        const { tool, params, result, cwd, user, exitCode } = toolContext;
        
        // Build evidence record
        const evidence = {
            id: crypto.randomBytes(16).toString('hex'),
            timestamp: new Date().toISOString(),
            tool: tool,
            parameters: params,
            result: result,
            cwd: cwd,
            user: user,
            exitCode: exitCode,
            environment: process.env.NODE_ENV || 'development'
        };
        
        // Special handling for file operations - capture file state
        if (['Edit', 'Write', 'MultiEdit'].includes(tool)) {
            const filePath = params.file_path || params.path;
            if (filePath && fs.existsSync(filePath)) {
                const stats = fs.statSync(filePath);
                evidence.fileState = {
                    path: filePath,
                    size: stats.size,
                    modified: stats.mtime,
                    permissions: stats.mode.toString(8),
                    hash: crypto.createHash('sha256')
                        .update(fs.readFileSync(filePath))
                        .digest('hex')
                };
            }
        }
        
        // Special handling for command execution - capture output
        if (tool === 'Bash') {
            evidence.commandOutput = {
                command: params.command,
                stdout: result?.stdout || '',
                stderr: result?.stderr || '',
                exitCode: exitCode
            };
        }
        
        // Store evidence locally
        const evidenceFile = path.join(
            EVIDENCE_DIR,
            `${new Date().toISOString().split('T')[0]}_${evidence.id}.json`
        );
        fs.writeFileSync(evidenceFile, JSON.stringify(evidence, null, 2));
        
        // Send to governance agent for audit logging
        try {
            await axios.post(`${AGENT_URL}/audit`, {
                action: 'tool_executed',
                tool: tool,
                evidence: evidence
            });
        } catch (err) {
            // Agent might not be running, that's okay
            console.debug('[DELIMIT EVIDENCE] Could not send to agent:', err.message);
        }
        
        // Check for anomalies
        if (exitCode !== 0) {
            console.warn(`[DELIMIT EVIDENCE] ⚠️  Tool execution failed with exit code ${exitCode}`);
        }
        
        // Special handling for sensitive operations
        const sensitivePaths = ['/etc/', '/.ssh/', '/.aws/', '/credentials/'];
        if (evidence.fileState && sensitivePaths.some(p => evidence.fileState.path.includes(p))) {
            console.warn(`[DELIMIT EVIDENCE] ⚠️  Sensitive file modified: ${evidence.fileState.path}`);
            
            // Create high-priority evidence record
            const alertFile = path.join(
                EVIDENCE_DIR,
                'alerts',
                `ALERT_${evidence.id}.json`
            );
            fs.mkdirSync(path.dirname(alertFile), { recursive: true });
            fs.writeFileSync(alertFile, JSON.stringify({
                ...evidence,
                alertLevel: 'high',
                reason: 'Sensitive file modification'
            }, null, 2));
        }
        
    } catch (error) {
        console.error('[DELIMIT EVIDENCE] Error collecting evidence:', error.message);
    }
}

// Main execution
if (require.main === module) {
    const toolContext = {
        tool: process.env.CLAUDE_TOOL_NAME || process.argv[2],
        params: JSON.parse(process.env.CLAUDE_TOOL_PARAMS || process.argv[3] || '{}'),
        result: JSON.parse(process.env.CLAUDE_TOOL_RESULT || process.argv[4] || '{}'),
        exitCode: parseInt(process.env.CLAUDE_TOOL_EXIT_CODE || process.argv[5] || '0'),
        cwd: process.cwd(),
        user: process.env.USER
    };
    
    collectEvidence(toolContext).catch(console.error);
}

module.exports = { collectEvidence };