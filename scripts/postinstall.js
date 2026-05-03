#!/usr/bin/env node
/**
 * Postinstall — anonymous install ping + setup hint.
 *
 * v4.5.2 (LED-1188) install hardening:
 *   - Top-level try/catch ensures NO postinstall failure can ever block
 *     `npm install delimit-cli`. Per the customer-protection rule in
 *     /root/.claude/CLAUDE.md, npm publish is a production deploy and a
 *     postinstall crash on a Pro user's machine is a customer-facing
 *     incident regardless of root cause.
 *   - EROFS / EACCES / EPERM / ENOSPC / ENOENT on stdout writes soft-fail
 *     silently. (Some sandbox installers redirect stdout to a read-only
 *     pipe.)
 *   - Network telemetry stays best-effort; no crash if DNS / TLS / proxy
 *     misbehaves. DELIMIT_NO_TELEMETRY=1 honored as kill switch.
 *   - Idempotent — re-running install is a no-op, never corrupts state.
 *     This file does not write to ~/.delimit/; that's bin/delimit-setup.js.
 *
 * No PII. Silent fail. Never blocks install.
 */

(function postinstall() {
    'use strict';

    // --- 1. setup hint ------------------------------------------------------
    // Wrapped in try/catch because console.log can throw on EPIPE / EBADF
    // when the parent npm process closed stdout early.
    let pkg;
    try {
        pkg = require('../package.json');
    } catch (e) {
        // package.json missing or unreadable — nothing to print, nothing
        // to ping. This is a partial-install state; let the install
        // complete so `delimit doctor` can diagnose later.
        return;
    }
    const v = (pkg && pkg.version) || '?';

    function safeLog(msg) {
        try { process.stdout.write(msg + '\n'); }
        catch (_) { /* EPIPE / EBADF / EROFS on stdout — give up silently */ }
    }

    try {
        safeLog('');
        safeLog('  \x1b[1m\x1b[35mDelimit\x1b[0m v' + v + ' installed');
        safeLog('');
        safeLog('  Quick start:');
        safeLog('    \x1b[32mdelimit doctor\x1b[0m        Check your setup, fix what\'s missing');
        safeLog('    \x1b[32mdelimit simulate\x1b[0m      Dry-run: see what governance would block');
        safeLog('    \x1b[32mdelimit status\x1b[0m        Visual dashboard of your governance posture');
        safeLog('    \x1b[32mdelimit setup\x1b[0m         Install MCP governance for AI assistants');
        safeLog('');
        safeLog('  Docs:       \x1b[36mhttps://delimit.ai/docs\x1b[0m');
        safeLog('  Star us:    \x1b[36mhttps://github.com/delimit-ai/delimit-mcp-server\x1b[0m');
        safeLog('');
    } catch (_) { /* never block install on a print failure */ }

    // --- 2. anonymous install telemetry ------------------------------------
    // Honor opt-out and corporate proxy environments. The HTTPS request is
    // silent-fail at every level (DNS / TCP / TLS / write / response).
    const tele = (process.env.DELIMIT_NO_TELEMETRY || '').toLowerCase();
    if (tele === '1' || tele === 'true' || tele === 'yes') return;

    try {
        const https = require('https');
        const data = JSON.stringify({
            event: 'install',
            version: v,
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
        // Catch every error class: ENOTFOUND, ECONNREFUSED, ETIMEDOUT,
        // CERT_HAS_EXPIRED, EPROTO, etc. None should ever propagate.
        req.on('error', () => {});
        req.on('timeout', () => { try { req.destroy(); } catch (_) {} });
        req.write(data);
        req.end();
    } catch (_) { /* silent fail — never block install */ }
})();

// Outermost guard: even if the IIFE above throws synchronously somehow
// (require() race, V8 bug, etc), don't propagate a non-zero exit code.
process.on('uncaughtException', () => { /* swallow */ });
