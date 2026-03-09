#!/usr/bin/env node
// Triggers governance check on keywords
const keywords = ['governance', 'policy', 'compliance', 'audit'];
const message = process.argv[2] || '';

if (keywords.some(k => message.toLowerCase().includes(k))) {
    console.log('[DELIMIT] Governance check triggered');
    require('child_process').execSync('delimit status --verbose');
}
