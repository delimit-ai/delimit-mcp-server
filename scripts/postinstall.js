#!/usr/bin/env node
/**
 * Postinstall — anonymous install ping + setup hint.
 * No PII. Silent fail. Never blocks install.
 */

// Print setup hint
console.log('\n  Run: npx delimit-cli setup\n');

// Anonymous telemetry ping — no PII, just "someone installed"
try {
    const https = require('https');
    const data = JSON.stringify({
        event: 'install',
        version: require('../package.json').version,
        node: process.version,
        platform: process.platform,
        arch: process.arch,
        ts: new Date().toISOString()
    });
    const req = https.request({
        hostname: 'delimit.ai',
        path: '/api/telemetry',
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'Content-Length': Buffer.byteLength(data)
        },
        timeout: 3000
    });
    req.on('error', () => {}); // silent fail
    req.on('timeout', () => { req.destroy(); });
    req.write(data);
    req.end();
} catch (e) { /* silent fail */ }
