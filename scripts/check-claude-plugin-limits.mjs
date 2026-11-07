#!/usr/bin/env node
/**
 * Limits and review gate checker for the Delimit Claude Plugin.
 *
 * Verifies that the plugin meets Anthropic Claude Plugin Directory review criteria:
 *   1. Bundled readable source is present (no npx launcher, no runtime network/npm/pip installs).
 *   2. Manifest launches with `node` and points to bundled readable source.
 *   3. File count is strictly < 512.
 *   4. Every file size is strictly < 256 KiB.
 *   5. No binaries (no compiled libraries, ELF/Mach-O executables, or null bytes).
 *   6. No non-shell scripts in any subfolder (only .sh allowed in subfolders).
 *   7. No node_modules or auto-install lockfiles.
 *
 * Exits 0 on pass, 1 on failure.
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const REPO_ROOT = path.resolve(__dirname, '..');
const PLUGIN_DIR = path.join(REPO_ROOT, 'claude-plugin-bundled');
const MCP_CONFIG = path.join(PLUGIN_DIR, '.mcp.json');

const MAX_FILE_SIZE = 256 * 1024; // 256 KiB
const MAX_FILE_COUNT = 512;
const BINARY_EXTENSIONS = new Set(['.so', '.dylib', '.dll', '.exe', '.bin', '.node', '.o', '.a']);
const SCRIPT_EXTENSIONS = new Set(['.js', '.mjs', '.cjs', '.py', '.rb', '.pl', '.ts', '.php']);

function getAllFiles(dir, fileList = []) {
    if (!fs.existsSync(dir)) return fileList;
    const entries = fs.readdirSync(dir, { withFileTypes: true });
    for (const entry of entries) {
        const fullPath = path.join(dir, entry.name);
        if (entry.isDirectory()) {
            getAllFiles(fullPath, fileList);
        } else if (entry.isFile()) {
            fileList.push(fullPath);
        }
    }
    return fileList;
}

function isBinary(filePath) {
    const ext = path.extname(filePath).toLowerCase();
    if (BINARY_EXTENSIONS.has(ext)) return true;
    try {
        const buf = fs.readFileSync(filePath);
        const len = Math.min(buf.length, 512);
        for (let i = 0; i < len; i++) {
            if (buf[i] === 0) return true;
        }
    } catch {
        return false;
    }
    return false;
}

export function checkPluginLimits() {
    const errors = [];
    const warnings = [];

    console.log('[check-claude-plugin-limits] Checking Claude plugin limits...');

    if (!fs.existsSync(PLUGIN_DIR)) {
        errors.push(`Plugin directory does not exist: ${PLUGIN_DIR}`);
        return { ok: false, errors, warnings };
    }

    // 1. Check .mcp.json
    if (!fs.existsSync(MCP_CONFIG)) {
        errors.push(`Missing .mcp.json in plugin directory: ${MCP_CONFIG}`);
    } else {
        try {
            const raw = fs.readFileSync(MCP_CONFIG, 'utf8');
            const mcp = JSON.parse(raw);
            const delimitServer = mcp?.mcpServers?.delimit;

            if (!delimitServer) {
                errors.push(`.mcp.json must define mcpServers.delimit`);
            } else {
                if (delimitServer.command !== 'node') {
                    errors.push(`.mcp.json command must be "node", got "${delimitServer.command}" (npx launcher triggers hold)`);
                }
                const args = Array.isArray(delimitServer.args) ? delimitServer.args : [];
                if (args.some(a => typeof a === 'string' && a.includes('npx'))) {
                    errors.push(`.mcp.json args must not invoke npx`);
                }

                // Check target script exists
                const targetArg = args.find(a => typeof a === 'string' && !a.startsWith('-'));
                if (!targetArg) {
                    errors.push(`.mcp.json must specify a script argument for node`);
                } else {
                    const resolvedTarget = targetArg.replace('${CLAUDE_PLUGIN_ROOT}', PLUGIN_DIR);
                    const absTarget = path.isAbsolute(resolvedTarget)
                        ? resolvedTarget
                        : path.join(PLUGIN_DIR, resolvedTarget);
                    if (!fs.existsSync(absTarget)) {
                        errors.push(`Bundled source script not found: ${absTarget} (specified in .mcp.json as "${targetArg}")`);
                    }
                }
            }
        } catch (e) {
            errors.push(`Failed to parse .mcp.json: ${e.message}`);
        }
    }

    // 2. Check forbidden directories
    if (fs.existsSync(path.join(PLUGIN_DIR, 'node_modules'))) {
        errors.push(`Forbidden "node_modules" directory exists in plugin directory`);
    }

    if (fs.existsSync(path.join(PLUGIN_DIR, 'package-lock.json'))) {
        errors.push(`Forbidden package-lock.json at plugin root (triggers LOCKFILE_AUTO_INSTALL hold)`);
    }

    // 3. Inspect all files
    const files = getAllFiles(PLUGIN_DIR);

    if (files.length >= MAX_FILE_COUNT) {
        errors.push(`File count ${files.length} exceeds limit of ${MAX_FILE_COUNT}`);
    }

    let largestFile = { path: '', size: 0 };

    for (const file of files) {
        const rel = path.relative(PLUGIN_DIR, file);
        const stat = fs.statSync(file);

        if (stat.size > largestFile.size) {
            largestFile = { path: rel, size: stat.size };
        }

        if (stat.size >= MAX_FILE_SIZE) {
            errors.push(`File ${rel} (${(stat.size / 1024).toFixed(1)} KiB) exceeds 256 KiB limit (${MAX_FILE_SIZE} bytes)`);
        }

        if (isBinary(file)) {
            errors.push(`Binary file detected: ${rel}`);
        }

        // Subfolder script check
        const dirOfFile = path.dirname(rel);
        if (dirOfFile !== '.') {
            const ext = path.extname(file).toLowerCase();
            if (SCRIPT_EXTENSIONS.has(ext)) {
                errors.push(`Non-shell script found in subfolder "${dirOfFile}": ${rel}. Only shell scripts (.sh) allowed in subfolders.`);
            }
        }
    }

    console.log(`[check-claude-plugin-limits] Total files: ${files.length} / ${MAX_FILE_COUNT}`);
    console.log(`[check-claude-plugin-limits] Largest file: ${largestFile.path} (${(largestFile.size / 1024).toFixed(2)} KiB / 256 KiB)`);

    const ok = errors.length === 0;
    if (ok) {
        console.log('[check-claude-plugin-limits] PASS: All plugin limits and review rules satisfied.');
    } else {
        console.error(`[check-claude-plugin-limits] FAIL: ${errors.length} violation(s) found:`);
        for (const err of errors) {
            console.error(`  - ${err}`);
        }
    }

    return { ok, errors, warnings, fileCount: files.length, largestFile };
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
    const { ok } = checkPluginLimits();
    process.exit(ok ? 0 : 1);
}
