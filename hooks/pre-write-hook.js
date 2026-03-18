#!/usr/bin/env node
const axios = require('axios');
const path = require('path');
const AGENT_URL = `http://127.0.0.1:${process.env.DELIMIT_AGENT_PORT || 7823}`;

async function validateWrite(params) {
    const filePath = params.file_path || params.path || '';
    const sensitivePaths = ['/etc/', '/.ssh/', '/.aws/', '/credentials/'];
    
    if (sensitivePaths.some(p => filePath.includes(p))) {
        console.warn('[DELIMIT] ⚠️  Sensitive file operation detected');
        try {
            const { data } = await axios.post(`${AGENT_URL}/evaluate`, {
                action: 'file_write',
                path: filePath,
                riskLevel: 'critical'
            });
            if (data.action === 'block') {
                console.error('[DELIMIT] ❌ File operation blocked by governance policy');
                process.exit(1);
            }
        } catch (e) {
            console.warn('[DELIMIT] Governance agent not available');
        }
    }
}

if (require.main === module) {
    const params = JSON.parse(process.argv[2] || '{}');
    validateWrite(params);
}
