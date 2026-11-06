#!/usr/bin/env node
/**
 * Reproducible bundling step for the Delimit Claude Plugin.
 *
 * Copies the self-contained MCP server source into `claude-plugin/server.cjs`
 * and updates `claude-plugin/.mcp.json` to launch with `node` instead of `npx`.
 *
 * Enforces Anthropic Claude Plugin Directory review limits:
 *   - No runtime network/npx install
 *   - No node_modules
 *   - No binaries
 *   - Every file < 256 KiB
 *   - Total files < 512
 *   - No non-shell scripts in subfolders
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const REPO_ROOT = path.resolve(__dirname, '..');

const SOURCE_SERVER = path.join(REPO_ROOT, 'lib', 'claude-plugin-server.cjs');
const PLUGIN_DIR = path.join(REPO_ROOT, 'claude-plugin');
const TARGET_SERVER = path.join(PLUGIN_DIR, 'server.cjs');
const TARGET_MCP = path.join(PLUGIN_DIR, '.mcp.json');

const MAX_FILE_SIZE = 256 * 1024; // 256 KiB
const MAX_FILE_COUNT = 512;
const BINARY_EXTENSIONS = new Set(['.so', '.dylib', '.dll', '.exe', '.bin', '.node']);
const SCRIPT_EXTENSIONS = new Set(['.js', '.mjs', '.cjs', '.py', '.rb', '.pl', '.ts']);

export function getAllFiles(dir, fileList = []) {
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

export function isBinaryFile(filePath) {
    const ext = path.extname(filePath).toLowerCase();
    if (BINARY_EXTENSIONS.has(ext)) return true;
    const buffer = fs.readFileSync(filePath);
    // Check first 512 bytes for null byte (excluding common non-binary formats)
    const checkLen = Math.min(buffer.length, 512);
    for (let i = 0; i < checkLen; i++) {
        if (buffer[i] === 0) return true;
    }
    return false;
}

export function buildPlugin() {
    console.log('[build-claude-plugin] Starting Claude plugin bundling...');

    if (!fs.existsSync(SOURCE_SERVER)) {
        throw new Error(`Source server missing at: ${SOURCE_SERVER}`);
    }

    if (!fs.existsSync(PLUGIN_DIR)) {
        throw new Error(`Plugin directory missing at: ${PLUGIN_DIR}`);
    }

    // 1. Copy server.cjs into claude-plugin/
    console.log(`[build-claude-plugin] Copying ${path.relative(REPO_ROOT, SOURCE_SERVER)} -> ${path.relative(REPO_ROOT, TARGET_SERVER)}`);
    fs.copyFileSync(SOURCE_SERVER, TARGET_SERVER);

    // 2. Update .mcp.json to launch with node
    const mcpConfig = {
        mcpServers: {
            delimit: {
                command: 'node',
                args: [
                    '${CLAUDE_PLUGIN_ROOT}/server.cjs',
                ],
            },
        },
    };

    console.log(`[build-claude-plugin] Updating ${path.relative(REPO_ROOT, TARGET_MCP)} to launch with node`);
    fs.writeFileSync(TARGET_MCP, JSON.stringify(mcpConfig, null, 2) + '\n', 'utf8');

    // 3. Verify limits immediately after bundling
    const files = getAllFiles(PLUGIN_DIR);
    console.log(`[build-claude-plugin] Bundled plugin contains ${files.length} file(s)`);

    if (files.length >= MAX_FILE_COUNT) {
        throw new Error(`Plugin file count (${files.length}) exceeds maximum limit of ${MAX_FILE_COUNT}`);
    }

    let largestFile = { path: '', size: 0 };

    for (const file of files) {
        const rel = path.relative(PLUGIN_DIR, file);
        const stat = fs.statSync(file);

        if (stat.size > largestFile.size) {
            largestFile = { path: rel, size: stat.size };
        }

        if (stat.size > MAX_FILE_SIZE) {
            throw new Error(`File ${rel} (${stat.size} bytes) exceeds limit of ${MAX_FILE_SIZE} bytes (256 KiB)`);
        }

        if (isBinaryFile(file)) {
            throw new Error(`Binary file detected in plugin: ${rel}`);
        }

        // Check for non-shell scripts in subfolders
        const dirOfFile = path.dirname(rel);
        if (dirOfFile !== '.') {
            const ext = path.extname(file).toLowerCase();
            if (SCRIPT_EXTENSIONS.has(ext)) {
                throw new Error(`Non-shell script found in subfolder: ${rel}. Only shell scripts (.sh) allowed in subfolders.`);
            }
        }
    }

    if (fs.existsSync(path.join(PLUGIN_DIR, 'node_modules'))) {
        throw new Error('Plugin directory contains forbidden node_modules folder');
    }

    console.log(`[build-claude-plugin] Largest file: ${largestFile.path} (${(largestFile.size / 1024).toFixed(2)} KiB)`);
    console.log('[build-claude-plugin] SUCCESS: Bundled plugin meets all Anthropic review limits.');
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
    try {
        buildPlugin();
    } catch (err) {
        console.error(`[build-claude-plugin] FAIL: ${err.message}`);
        process.exit(1);
    }
}
