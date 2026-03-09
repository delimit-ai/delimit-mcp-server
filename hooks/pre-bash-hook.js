#!/usr/bin/env node
const axios = require('axios');
const AGENT_URL = `http://127.0.0.1:${process.env.DELIMIT_AGENT_PORT || 7823}`;

async function validateBash(params) {
    const riskyCommands = ['rm -rf', 'chmod 777', 'sudo', '> /dev/sda'];
    const command = params.command || '';
    
    if (riskyCommands.some(cmd => command.includes(cmd))) {
        console.error('[DELIMIT] ⚠️  Risky command detected');
        try {
            const { data } = await axios.post(`${AGENT_URL}/evaluate`, {
                action: 'bash_command',
                command: command,
                riskLevel: 'high'
            });
            if (data.action === 'block') {
                console.error('[DELIMIT] ❌ Command blocked by governance policy');
                process.exit(1);
            }
        } catch (e) {
            console.warn('[DELIMIT] Governance agent not available');
        }
    }
}

if (require.main === module) {
    const params = JSON.parse(process.argv[2] || '{}');
    validateBash(params);
}
