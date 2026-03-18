#!/usr/bin/env node
// Triggers auth setup on keywords
const keywords = ['setup credentials', 'github key', 'api key'];
const message = process.argv[2] || '';

if (keywords.some(k => message.toLowerCase().includes(k))) {
    console.log('[DELIMIT] Authentication setup triggered');
    require('child_process').execSync('delimit auth');
}
