#!/usr/bin/env node
/**
 * Postinstall — anonymous install ping + setup hint.
 * No PII. Silent fail. Never blocks install.
 */

// Print setup hint with quick start
const v = require('../package.json').version;
console.log('');
console.log('  \x1b[1m\x1b[35mDelimit\x1b[0m v' + v + ' installed');
console.log('');
console.log('  Quick start:');
console.log('    \x1b[32mdelimit doctor\x1b[0m        Check your setup, fix what\'s missing');
console.log('    \x1b[32mdelimit simulate\x1b[0m      Dry-run: see what governance would block');
console.log('    \x1b[32mdelimit status\x1b[0m        Visual dashboard of your governance posture');
console.log('    \x1b[32mdelimit setup\x1b[0m         Install MCP governance for AI assistants');
console.log('');
console.log('  Docs:       \x1b[36mhttps://delimit.ai/docs\x1b[0m');
console.log('  Star us:    \x1b[36mhttps://github.com/delimit-ai/delimit-mcp-server\x1b[0m');
console.log('');

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
