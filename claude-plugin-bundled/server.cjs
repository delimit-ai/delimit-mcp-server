#!/usr/bin/env node
'use strict';

/**
 * Delimit Claude Plugin - Bundled MCP Server (Records toolset).
 *
 * Provides persistent records (decisions, tasks, and handoffs) across sessions
 * without external npm dependencies, Python venv, or runtime network calls.
 * Runs directly on Node.js using standard library built-ins.
 *
 * Implements MCP JSON-RPC 2.0 protocol over stdio for the 10 core records tools:
 *   - delimit_version
 *   - delimit_help
 *   - delimit_ledger_add
 *   - delimit_ledger_list
 *   - delimit_ledger_update
 *   - delimit_ledger_done
 *   - delimit_ledger_context
 *   - delimit_handoff_create
 *   - delimit_handoff_list
 *   - delimit_handoff_acknowledge
 */

const fs = require('fs');
const path = require('path');
const os = require('os');
const crypto = require('crypto');
const readline = require('readline');

// --- Delimit Environment & Home Resolution (LED-1188 / LED-5658) ---

function getRealHome() {
    const testOverride = process.env._DELIMIT_TEST_ONLY_REAL_HOME;
    if (testOverride && testOverride.trim()) return testOverride.trim();
    try {
        const info = os.userInfo();
        if (info && info.homedir) return info.homedir;
    } catch {
        // Fall back to os.homedir()
    }
    return os.homedir();
}

function getDelimitHome() {
    const envHome = os.homedir();
    const fromEnv = process.env.DELIMIT_HOME || process.env.DELIMIT_NAMESPACE_ROOT;
    if (fromEnv && fromEnv.trim()) {
        const trimmed = fromEnv.trim();
        const realHome = getRealHome();
        const homeOverridden = envHome !== realHome;
        if (homeOverridden) {
            const ambientDefault = path.join(realHome, '.delimit');
            if (trimmed !== ambientDefault) return trimmed;
        } else {
            return trimmed;
        }
    }
    return path.join(envHome, '.delimit');
}

function resolveRepoRoot(startDir = process.cwd()) {
    let current = path.resolve(startDir);
    const seen = new Set();
    const homeDir = os.homedir();

    while (!seen.has(current)) {
        seen.add(current);
        if (
            fs.existsSync(path.join(current, '.git')) ||
            (current !== homeDir && fs.existsSync(path.join(current, '.delimit', 'ledger', 'operations.jsonl')))
        ) {
            return current;
        }
        const parent = path.dirname(current);
        if (parent === current) return null;
        current = parent;
    }
    return null;
}

function resolveVenture(ventureArg, cwd = process.cwd()) {
    if (ventureArg && typeof ventureArg === 'string' && ventureArg.trim()) {
        return ventureArg.trim();
    }
    const repo = resolveRepoRoot(cwd) || path.resolve(cwd);
    for (const dir of [path.resolve(cwd), repo]) {
        const pkgPath = path.join(dir, 'package.json');
        if (fs.existsSync(pkgPath)) {
            try {
                const pkg = JSON.parse(fs.readFileSync(pkgPath, 'utf8'));
                if (pkg && pkg.name) return pkg.name;
            } catch {}
        }
    }
    return path.basename(repo) || path.basename(cwd) || 'unsorted';
}

function getLedgerDir(cwd = process.cwd(), ventureArg = '') {
    const central = path.join(getDelimitHome(), 'ledger');
    const slug = resolveVenture(ventureArg, cwd);
    if (slug) {
        const sub = path.join(central, slug);
        if (fs.existsSync(path.join(sub, 'operations.jsonl'))) {
            return sub;
        }
    }
    return central;
}

/**
 * Shared filesystem helper for directory and file creation with strict permissions.
 *
 * Backward compatibility note:
 * Do NOT chmod pre-existing files or directories on users' machines
 * (backward compatibility: other local readers may depend on their current mode).
 * Directories created by the plugin get mode 0700 (mkdir with mode, then chmod to defeat umask).
 * Files created by the plugin get mode 0600 (open with mode 0o600 / O_CREAT, or write to a temp
 * file created 0600 then rename; atomic rewrites must preserve 0600 on the new file).
 */
function ensureDir0700(targetDir) {
    if (!targetDir) return;
    const resolved = path.resolve(targetDir);
    if (fs.existsSync(resolved)) {
        // Do NOT chmod pre-existing files or directories on users' machines
        // (backward compatibility: other local readers may depend on their current mode).
        return;
    }
    const stack = [];
    let curr = resolved;
    while (curr && !fs.existsSync(curr)) {
        stack.push(curr);
        const parent = path.dirname(curr);
        if (parent === curr) break;
        curr = parent;
    }
    while (stack.length > 0) {
        const dir = stack.pop();
        if (!fs.existsSync(dir)) {
            try {
                fs.mkdirSync(dir, { mode: 0o700 });
                try {
                    fs.chmodSync(dir, 0o700);
                } catch {
                    // Ignore chmod failures on platforms without POSIX permissions (e.g. Windows)
                }
            } catch (err) {
                if (!err || err.code !== 'EEXIST') {
                    throw err;
                }
            }
        }
    }
}

function writeFile0600(filePath, content) {
    ensureDir0700(path.dirname(filePath));

    let existingMode = null;
    try {
        const st = fs.statSync(filePath);
        existingMode = st.mode & 0o777;
    } catch (err) {
        if (err && err.code !== 'ENOENT') {
            throw err;
        }
    }

    // If the file already existed, preserve its existing mode (e.g. 0644 or 0600).
    // If it is newly created, use mode 0600.
    // Backward compatibility note:
    // Do NOT chmod pre-existing files or directories on users' machines
    // (backward compatibility: other local readers may depend on their current mode).
    const targetMode = existingMode !== null ? existingMode : 0o600;

    const dir = path.dirname(filePath);
    const tempPath = path.join(
        dir,
        `.${path.basename(filePath)}.${process.pid}.${Date.now()}.${crypto.randomBytes(4).toString('hex')}.tmp`
    );

    const fd = fs.openSync(tempPath, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL, targetMode);
    try {
        try {
            fs.fchmodSync(fd, targetMode);
        } catch {
            // Ignore chmod failures on platforms without POSIX permissions (e.g. Windows)
        }
        fs.writeFileSync(fd, content, 'utf8');
    } finally {
        fs.closeSync(fd);
    }
    try {
        fs.renameSync(tempPath, filePath);
    } catch (err) {
        try { fs.unlinkSync(tempPath); } catch {}
        throw err;
    }
}

function appendFile0600(filePath, content) {
    ensureDir0700(path.dirname(filePath));
    const exists = fs.existsSync(filePath);
    if (!exists) {
        // File does not exist yet: create with mode 0o600 and chmod to defeat ambient umask.
        // Backward compatibility note:
        // Do NOT chmod pre-existing files or directories on users' machines
        // (backward compatibility: other local readers may depend on their current mode).
        const fd = fs.openSync(filePath, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_APPEND, 0o600);
        try {
            try {
                fs.fchmodSync(fd, 0o600);
            } catch {
                // Ignore chmod failures on platforms without POSIX permissions (e.g. Windows)
            }
            fs.appendFileSync(fd, content, 'utf8');
        } finally {
            fs.closeSync(fd);
        }
    } else {
        // Pre-existing file: do NOT chmod to preserve existing mode (e.g. 0644).
        fs.appendFileSync(filePath, content, 'utf8');
    }
}

function getLedgerFilePath(cwd = process.cwd(), ledger = 'ops', ventureArg = '') {
    const ledgerDir = getLedgerDir(cwd, ventureArg);
    ensureDir0700(ledgerDir);
    const filename = ledger === 'strategy' ? 'strategy.jsonl' : 'operations.jsonl';
    return path.join(ledgerDir, filename);
}

function readJsonl(filePath) {
    if (!fs.existsSync(filePath)) return [];
    try {
        const content = fs.readFileSync(filePath, 'utf8');
        return content
            .split('\n')
            .map(line => line.trim())
            .filter(Boolean)
            .map(line => {
                try { return JSON.parse(line); } catch { return null; }
            })
            .filter(Boolean);
    } catch {
        return [];
    }
}

function escapePyString(str) {
    let result = '"';
    for (let i = 0; i < str.length; i++) {
        const code = str.charCodeAt(i);
        if (code === 0x22) {
            result += '\\"';
        } else if (code === 0x5c) {
            result += '\\\\';
        } else if (code === 0x08) {
            result += '\\b';
        } else if (code === 0x0c) {
            result += '\\f';
        } else if (code === 0x0a) {
            result += '\\n';
        } else if (code === 0x0d) {
            result += '\\r';
        } else if (code === 0x09) {
            result += '\\t';
        } else if (code < 0x20 || code >= 0x7f) {
            result += '\\u' + code.toString(16).padStart(4, '0');
        } else {
            result += str[i];
        }
    }
    result += '"';
    return result;
}

function pyJsonDumps(obj) {
    if (obj === null || obj === undefined) return 'null';
    if (typeof obj === 'boolean') return obj ? 'true' : 'false';
    if (typeof obj === 'number') {
        if (!Number.isFinite(obj)) return 'null';
        return String(obj);
    }
    if (typeof obj === 'string') return escapePyString(obj);
    if (Array.isArray(obj)) return '[' + obj.map(pyJsonDumps).join(', ') + ']';
    if (typeof obj === 'object') {
        const keys = Object.keys(obj).filter(k => obj[k] !== undefined).sort();
        return '{' + keys.map(k => escapePyString(k) + ': ' + pyJsonDumps(obj[k])).join(', ') + '}';
    }
    return 'null';
}

function appendJsonl(filePath, obj) {
    appendFile0600(filePath, pyJsonDumps(obj) + '\n');
}

function formatUtcTimestamp(d = new Date()) {
    return d.toISOString().replace(/\.\d{3}Z$/, 'Z');
}

function sealLedgerEntry(entry, existingItems) {
    const prevHash = (existingItems.length > 0 && existingItems[existingItems.length - 1].hash)
        ? existingItems[existingItems.length - 1].hash
        : 'genesis';
    entry.hash = crypto.createHash('sha256').update(prevHash + pyJsonDumps(entry)).digest('hex').slice(0, 16);
    entry.created_at = formatUtcTimestamp();
    return entry;
}

function buildLedgerSnapshot(filePath) {
    const entries = readJsonl(filePath);
    const latestById = new Map();

    for (const entry of entries) {
        if (!entry || !entry.id) continue;
        const current = latestById.get(entry.id) || {};
        if (entry.type === 'update') {
            const merged = { ...current };
            for (const [k, v] of Object.entries(entry)) {
                if (k === 'type') continue;
                if (k === 'note') {
                    merged.last_note = v;
                    merged.note = v;
                } else if (k === 'worked_by') {
                    merged.last_worked_by = v;
                    merged.worked_by = v;
                } else if (v !== undefined) {
                    merged[k] = v;
                }
            }
            if (entry.tags !== undefined && entry.tags !== null) {
                merged.tags = entry.tags;
            }
            merged.updated_at = entry.updated_at || entry.created_at;
            latestById.set(entry.id, merged);
        } else {
            latestById.set(entry.id, {
                ...entry,
                status: entry.status || 'open',
                truth_label: entry.truth_label || 'legacy/unclassified',
                verified_states: entry.verified_states || [],
                ...current,
            });
        }
    }

    const priorityWeight = { P0: 0, P1: 1, P2: 2, P3: 3 };
    const all = Array.from(latestById.values());
    const open = all
        .filter(item => !['done', 'blocked', 'archived'].includes(String(item.status || 'open').toLowerCase()))
        .sort((a, b) => (priorityWeight[a.priority] ?? 9) - (priorityWeight[b.priority] ?? 9));

    return { all, open, latestById };
}

function nextLedgerId(filePath, ledger = 'ops') {
    const prefix = ledger === 'strategy' ? 'STR' : 'LED';
    const entries = readJsonl(filePath);
    let max = 0;
    for (const e of entries) {
        if (e && typeof e.id === 'string' && e.type !== 'update') {
            const m = e.id.match(new RegExp(`^${prefix}-(\\d+)$`));
            if (m) {
                const n = parseInt(m[1], 10);
                if (n > max) max = n;
            }
        }
    }
    return `${prefix}-${String(max + 1).padStart(3, '0')}`;
}

// --- Handoff Receipts Support ---

function getProjectHash(projectPath) {
    let resolved = projectPath;
    try {
        resolved = fs.realpathSync.native ? fs.realpathSync.native(projectPath) : fs.realpathSync(projectPath);
    } catch {
        resolved = path.resolve(projectPath);
    }
    return crypto.createHash('sha256').update(resolved).digest('hex').slice(0, 12);
}

function getHandoffDir(projectPath) {
    const hash = getProjectHash(projectPath);
    return path.join(getDelimitHome(), 'handoff_receipts', hash);
}

function loadHandoffIndex(handoffDir) {
    const indexPath = path.join(handoffDir, 'index.json');
    if (!fs.existsSync(indexPath)) return { receipts: [] };
    try {
        const data = JSON.parse(fs.readFileSync(indexPath, 'utf8'));
        return Array.isArray(data.receipts) ? data : { receipts: [] };
    } catch {
        return { receipts: [] };
    }
}

function saveHandoffIndex(handoffDir, indexData) {
    writeFile0600(path.join(handoffDir, 'index.json'), JSON.stringify(indexData, null, 2));
}

const RECEIPT_BODY_FIELDS = [
    'receipt_id',
    'created_at',
    'from_model',
    'to_model',
    'project_path',
    'task_description',
    'completed',
    'not_completed',
    'assumptions',
    'blockers',
    'files_modified',
    'in_scope',
    'out_of_scope',
    'next_action',
    'priority',
    'created_by_model',
    'created_by_session',
];

function computeContentDigest(body) {
    const sorted = {};
    for (const field of RECEIPT_BODY_FIELDS) {
        sorted[field] = body[field] !== undefined ? body[field] : null;
    }
    const canonical = JSON.stringify(sorted);
    return crypto.createHash('sha256').update(canonical, 'utf8').digest('hex');
}

function coerceList(val) {
    if (!val) return [];
    if (Array.isArray(val)) return val.map(String).map(s => s.trim()).filter(Boolean);
    if (typeof val === 'string') {
        return val.split(',').map(s => s.trim()).filter(Boolean);
    }
    return [String(val)];
}

// --- Tools Implementation ---

const TOOL_DEFINITIONS = [
    {
        name: 'delimit_version',
        description: 'Return Delimit server version, tool count, and environment status.',
        inputSchema: {
            type: 'object',
            properties: {},
            required: [],
            additionalProperties: false,
        },
    },
    {
        name: 'delimit_help',
        description: 'Get help for a Delimit tool — purpose, parameters, examples.',
        inputSchema: {
            type: 'object',
            properties: {
                tool_name: {
                    type: 'string',
                    default: '',
                    description: 'Tool name (e.g. "lint", "gov_health"). Empty returns the workflows overview.',
                },
            },
            required: [],
            additionalProperties: false,
        },
    },
    {
        name: 'delimit_ledger_add',
        description: 'Add a new item to a project\'s ledger.',
        inputSchema: {
            type: 'object',
            properties: {
                title: { type: 'string', description: 'What needs to be done. Required.' },
                venture: { type: 'string', default: '', description: 'Project name or path. Empty = auto-detect from cwd.' },
                ledger: { type: 'string', default: 'ops', description: '"ops" (tasks, bugs, features) or "strategy" (decisions, direction).' },
                item_type: { type: 'string', default: 'task', description: 'task, fix, feat, strategy, consensus.' },
                priority: { type: 'string', default: 'P1', description: 'P0 (urgent), P1 (important), P2 (nice to have).' },
                description: { type: 'string', default: '', description: 'Details.' },
                source: { type: 'string', default: 'session', description: 'Where this came from (session, consensus, focus-group, etc).' },
                tags: {
                    anyOf: [{ type: 'string' }, { items: { type: 'string' }, type: 'array' }, { type: 'null' }],
                    default: null,
                    description: 'Labels/tags (e.g. ["deploy-ready", "ship"] or "deploy-ready,ship").',
                },
                acceptance_criteria: {
                    anyOf: [{ type: 'string' }, { items: { type: 'string' }, type: 'array' }, { type: 'null' }],
                    default: null,
                    description: 'List of testable "done when" conditions (e.g. "tests pass", "coverage > 80%").',
                },
                context: { type: 'string', default: '', description: 'Background info an AI agent needs to work on this item.' },
                tools_needed: {
                    anyOf: [{ type: 'string' }, { items: { type: 'string' }, type: 'array' }, { type: 'null' }],
                    default: null,
                    description: 'Delimit tools needed (e.g. "delimit_lint", "delimit_test_coverage").',
                },
                estimated_complexity: { type: 'string', default: '', description: 'small, medium, or large.' },
                worked_by: { type: 'string', default: '', description: 'Which AI model is working on this. Auto-detected if empty.' },
            },
            required: ['title'],
            additionalProperties: false,
        },
    },
    {
        name: 'delimit_ledger_list',
        description: 'List ledger items with rich filters, sort, and pagination (LED-1145).',
        inputSchema: {
            type: 'object',
            properties: {
                venture: { type: 'string', default: '', description: 'Project name/path. Empty = auto-detect.' },
                ledger: { type: 'string', default: 'both', description: '"ops", "strategy", or "both" (default).' },
                status: { type: 'string', default: '', description: 'Single-value status filter (back-compat).' },
                priority: { type: 'string', default: '', description: 'Single-value priority filter (back-compat).' },
                status_in: {
                    anyOf: [{ items: { type: 'string' }, type: 'array' }, { type: 'string' }, { type: 'null' }],
                    default: null,
                    description: 'Comma-separated statuses (e.g. "open,blocked").',
                },
                priority_in: {
                    anyOf: [{ items: { type: 'string' }, type: 'array' }, { type: 'string' }, { type: 'null' }],
                    default: null,
                    description: 'Comma-separated priorities (e.g. "P0,P1").',
                },
                tags_contains_all: {
                    anyOf: [{ items: { type: 'string' }, type: 'array' }, { type: 'string' }, { type: 'null' }],
                    default: null,
                    description: 'Comma-separated tags; item must contain ALL.',
                },
                text: { type: 'string', default: '', description: 'Case-insensitive substring match on title + description.' },
                linked_external_id: { type: 'string', default: '', description: 'Substring match in description / tags / context (github URL, Linear id, Discord thread).' },
                created_before: { type: 'string', default: '', description: 'ISO-8601 timestamp upper bound on creation time. If omitted, no upper bound is applied.' },
                created_after: { type: 'string', default: '', description: 'ISO-8601 timestamp lower bound on creation time. If omitted, no lower bound is applied.' },
                updated_before: { type: 'string', default: '', description: 'ISO-8601 timestamp upper bound on last-update time. If omitted, no upper bound is applied.' },
                updated_after: { type: 'string', default: '', description: 'ISO-8601 timestamp lower bound on last-update time. If omitted, no lower bound is applied.' },
                sort: { type: 'string', default: 'updated_at', description: '"updated_at" (default), "created_at", or "priority".' },
                order: { type: 'string', default: 'desc', description: '"asc" or "desc" (default).' },
                fields: { type: 'string', default: '', description: 'Response projection. "" / "*" = full; "slim" = subset; CSV = those fields only. Unknown names ERROR.' },
                limit: { type: 'integer', default: 50, description: 'Page size. Default 20.' },
                cursor: { type: 'string', default: '', description: 'Opaque pagination token from prior next_cursor. Becomes invalid if filters change between calls.' },
            },
            required: [],
            additionalProperties: false,
        },
    },
    {
        name: 'delimit_ledger_update',
        description: 'Update any field on an existing ledger item.',
        inputSchema: {
            type: 'object',
            properties: {
                item_id: { type: 'string', description: 'Ledger item id, e.g. "LED-001" or "STR-001". Required.' },
                venture: { type: 'string', default: '', description: 'Project name/path. Empty = auto-detect.' },
                status: { type: 'string', default: '', description: 'New status — "open", "in_progress", "blocked", "done".' },
                priority: { type: 'string', default: '', description: 'New priority — "P0", "P1", "P2".' },
                title: { type: 'string', default: '', description: 'New title.' },
                description: { type: 'string', default: '', description: 'New description.' },
                note: { type: 'string', default: '', description: 'Append a note/comment to the item.' },
                assignee: { type: 'string', default: '', description: 'Assign to person or agent (e.g. "founder", "claude").' },
                due_date: { type: 'string', default: '', description: 'ISO date string (e.g. "2026-04-01").' },
                labels: {
                    anyOf: [{ items: { type: 'string' }, type: 'array' }, { type: 'string' }, { type: 'null' }],
                    default: null,
                    description: 'Labels/tags as comma string or list.',
                },
                blocked_by: { type: 'string', default: '', description: 'Item id that blocks this one (e.g. "LED-025").' },
                blocks: { type: 'string', default: '', description: 'Item id that this one blocks (e.g. "STR-005").' },
                worked_by: { type: 'string', default: '', description: 'AI model working on this. Empty = auto-detect.' },
                evidence: {
                    anyOf: [{ type: 'string' }, { additionalProperties: true, type: 'object' }, { type: 'null' }],
                    default: null,
                    description: 'Optional evidence object {class, kind, ref, observed_at, observed_by} (LED-5321 M4). Dict or JSON string.',
                },
                review_transcript: { type: 'string', default: '', description: 'Optional transcript path for independently_reviewed binding.' },
                review_diff_path: { type: 'string', default: '', description: 'Optional diff file path the transcript must bind to.' },
                review_diff_text: { type: 'string', default: '', description: 'Optional inline diff text the transcript must bind to.' },
            },
            required: ['item_id'],
            additionalProperties: false,
        },
    },
    {
        name: 'delimit_ledger_done',
        description: 'Mark a ledger item as done (convenience wrapper).',
        inputSchema: {
            type: 'object',
            properties: {
                item_id: { type: 'string', description: 'Ledger item id (e.g. "LED-001"). Required.' },
                note: { type: 'string', default: '', description: 'Optional completion note. If the note contains a GitHub PR URL, it will be auto-extracted as ship proof.' },
                venture: { type: 'string', default: '', description: 'Project name or path. Empty = auto-detect.' },
                commit_sha: { type: 'string', default: '', description: 'LED-1408: optional merge-commit SHA proving the fix shipped. Recorded as ship_proof on the event; verified=True flag set on the item.' },
                pr_url: { type: 'string', default: '', description: 'LED-1408: optional GitHub PR URL proving the fix shipped. Parsed into pr_owner/pr_repo/pr_number; verified=True flag set on the item.' },
                evidence: {
                    anyOf: [{ type: 'string' }, { additionalProperties: true, type: 'object' }, { type: 'null' }],
                    default: null,
                    description: 'Optional evidence object {class, kind, ref, observed_at, observed_by} (LED-5321 M4). Dict or JSON string.',
                },
                review_transcript: { type: 'string', default: '', description: 'Optional transcript path for independently_reviewed binding.' },
                review_diff_path: { type: 'string', default: '', description: 'Optional diff file path the transcript must bind to.' },
                review_diff_text: { type: 'string', default: '', description: 'Optional inline diff text the transcript must bind to.' },
            },
            required: ['item_id'],
            additionalProperties: false,
        },
    },
    {
        name: 'delimit_ledger_context',
        description: 'Read the active ledger context (open tasks, priority items, summary counts) to resume work.',
        inputSchema: {
            type: 'object',
            properties: {
                venture: { type: 'string', default: '', description: 'Project name or path. Empty = auto-detect from cwd.' },
            },
            required: [],
            additionalProperties: false,
        },
    },
    {
        name: 'delimit_handoff_create',
        description: 'Create a handoff receipt when transitioning between agents.',
        inputSchema: {
            type: 'object',
            properties: {
                task_description: { type: 'string', default: '', description: 'What the task was (one line).' },
                completed: { type: 'string', default: '', description: 'Comma-separated completed items.' },
                not_completed: { type: 'string', default: '', description: 'Comma-separated items not completed (with reasons).' },
                assumptions: { type: 'string', default: '', description: 'Comma-separated assumptions made.' },
                blockers: { type: 'string', default: '', description: 'Comma-separated blockers encountered.' },
                files_modified: { type: 'string', default: '', description: 'JSON list of {path, change_type, summary} dicts, or empty to auto-detect.' },
                in_scope: { type: 'string', default: '', description: 'Comma-separated in-scope items.' },
                out_of_scope: { type: 'string', default: '', description: 'Comma-separated explicitly excluded items.' },
                next_action: { type: 'string', default: '', description: 'First thing the receiving agent should do.' },
                priority: { type: 'string', default: 'P1', description: 'P0 / P1 (default) / P2.' },
                to_model: { type: 'string', default: 'any', description: 'Target model name or "any" (default).' },
            },
            required: [],
            additionalProperties: false,
        },
    },
    {
        name: 'delimit_handoff_list',
        description: 'List session handoff receipts.',
        inputSchema: {
            type: 'object',
            properties: {
                status: { type: 'string', default: 'pending', description: '"pending" (default), "acknowledged", or "all".' },
                project_path: { type: 'string', default: '', description: 'Optional exact project namespace. Blank aggregates all namespaces.' },
                limit: { type: 'integer', default: 50, description: 'Maximum receipts to return, 1-200. Default 50.' },
            },
            required: [],
            additionalProperties: false,
        },
    },
    {
        name: 'delimit_handoff_acknowledge',
        description: 'Acknowledge a pending handoff receipt before starting work.',
        inputSchema: {
            type: 'object',
            properties: {
                receipt_id: { type: 'string', default: '', description: 'Receipt id to acknowledge. Required (empty string returns an error payload).' },
                notes: { type: 'string', default: '', description: 'Optional notes from the receiving agent.' },
                project_path: { type: 'string', default: '', description: 'Optional exact project namespace. Blank searches all namespaces; an explicit path never writes outside that namespace.' },
            },
            required: [],
            additionalProperties: false,
        },
    },
];

function handleDelimitVersion() {
    return {
        version: '4.22.0',
        server: 'delimit-mcp',
        total_tools: 10,
        bundled: true,
        runtime: 'node',
        next_steps: [],
    };
}

function handleDelimitHelp(args) {
    return {
        message: 'Delimit Claude Plugin (bundled records toolset)',
        workflows: [
            {
                name: 'Record Work & Decisions',
                pain: 'Tasks and decisions get lost across session windows',
                start_with: 'delimit_ledger_add',
            },
            {
                name: 'Structured Handoff',
                pain: 'Agent-to-agent session boundaries need verified receipts',
                start_with: 'delimit_handoff_create',
            },
            {
                name: 'Resume & Open Work',
                pain: 'Pick up where previous session or assistant left off',
                start_with: 'delimit_ledger_context',
            },
        ],
        total_tools: 10,
        next_steps: [],
    };
}

function findItemInLedger(itemId, cwd = process.cwd(), ventureArg = '') {
    for (const ledgerName of ['ops', 'strategy']) {
        const filePath = getLedgerFilePath(cwd, ledgerName, ventureArg);
        const entries = readJsonl(filePath);
        for (const item of entries) {
            if (item && item.id === itemId && item.type !== 'update') {
                return { ledgerName, filePath, item };
            }
        }
    }
    return null;
}

function handleLedgerAdd(args) {
    const cwd = process.cwd();
    const title = String(args.title || '').trim();
    if (!title) throw new Error('title is required');

    const venture = resolveVenture(args.venture, cwd);
    const ledger = (args.ledger || 'ops').toLowerCase() === 'strategy' ? 'strategy' : 'ops';
    const filePath = getLedgerFilePath(cwd, ledger, args.venture);
    const id = nextLedgerId(filePath, ledger);

    const tags = coerceList(args.tags);
    const criteria = coerceList(args.acceptance_criteria);
    const tools = coerceList(args.tools_needed);

    const entry = {
        id,
        title,
        type: args.item_type || 'task',
        priority: args.priority || 'P1',
        description: args.description || '',
        source: args.source || 'session',
        venture,
        status: 'open',
        tags,
        worked_by: args.worked_by || 'unknown',
        verified_states: [],
    };
    if (criteria.length) entry.acceptance_criteria = criteria;
    if (args.context) entry.context = String(args.context);
    if (tools.length) entry.tools_needed = tools;
    if (args.estimated_complexity) entry.estimated_complexity = String(args.estimated_complexity);

    const existingEntries = readJsonl(filePath);
    sealLedgerEntry(entry, existingEntries);

    appendJsonl(filePath, entry);
    const snapshot = buildLedgerSnapshot(filePath);

    return {
        added: entry,
        ledger,
        venture,
        total_items: snapshot.all.length,
        next_steps: [
            {
                tool: 'delimit_ledger_context',
                reason: 'See updated ledger state',
                premium: false,
            },
        ],
    };
}

function handleLedgerList(args) {
    const cwd = process.cwd();
    const venture = resolveVenture(args.venture, cwd);
    const ledgerArg = (args.ledger || 'both').toLowerCase();

    const ledgersToRead = [];
    if (ledgerArg === 'both') {
        ledgersToRead.push('ops', 'strategy');
    } else if (ledgerArg === 'strategy') {
        ledgersToRead.push('strategy');
    } else {
        ledgersToRead.push('ops');
    }

    const itemsByLedger = {};
    for (const lName of ['ops', 'strategy']) {
        if (ledgersToRead.includes(lName)) {
            itemsByLedger[lName] = [];
        }
    }

    let totalItems = 0;
    let openCount = 0;
    let doneCount = 0;
    let inProgressCount = 0;
    let blockedCount = 0;
    let archivedCount = 0;

    for (const lName of ledgersToRead) {
        const filePath = getLedgerFilePath(cwd, lName, args.venture);
        const snapshot = buildLedgerSnapshot(filePath);
        let items = snapshot.all;

        for (const item of items) {
            const s = String(item.status || 'open').toLowerCase();
            if (s === 'open') openCount++;
            else if (s === 'done') doneCount++;
            else if (s === 'in_progress') inProgressCount++;
            else if (s === 'blocked') blockedCount++;
            else if (s === 'archived') archivedCount++;
        }

        if (args.status) {
            const s = String(args.status).toLowerCase();
            items = items.filter(i => String(i.status || '').toLowerCase() === s);
        }
        if (args.priority) {
            const p = String(args.priority).toUpperCase();
            items = items.filter(i => String(i.priority || '').toUpperCase() === p);
        }
        if (args.text) {
            const txt = String(args.text).toLowerCase();
            items = items.filter(i => (i.title || '').toLowerCase().includes(txt) || (i.description || '').toLowerCase().includes(txt));
        }

        const formattedItems = items.map(i => ({
            ...i,
            truth_label: i.truth_label || 'legacy/unclassified',
            ledger: lName,
        }));

        itemsByLedger[lName] = formattedItems;
        totalItems += formattedItems.length;
    }

    const limit = Math.max(1, parseInt(args.limit, 10) || 50);

    return {
        venture,
        items: itemsByLedger,
        summary: {
            total: totalItems,
            open: openCount,
            done: doneCount,
            in_progress: inProgressCount,
            blocked: blockedCount,
            archived: archivedCount,
        },
        next_cursor: null,
        next_steps: [
            {
                tool: 'delimit_ledger_context',
                reason: 'GOVERNANCE LOOP: check ledger for next action',
                premium: false,
                required: true,
            },
        ],
    };
}

function handleLedgerUpdate(args) {
    const cwd = process.cwd();
    const itemId = String(args.item_id || '').trim();
    if (!itemId) throw new Error('item_id is required');

    const found = findItemInLedger(itemId, cwd, args.venture);
    if (!found) {
        return {
            error: `Item ${itemId} not found in project ledger`,
        };
    }

    const now = formatUtcTimestamp();
    const updateRecord = {
        id: itemId,
        type: 'update',
        updated_at: now,
        worked_by: args.worked_by || 'unknown',
    };
    if (args.title !== undefined && args.title !== '') updateRecord.title = String(args.title);
    if (args.status !== undefined && args.status !== '') updateRecord.status = String(args.status);
    if (args.priority !== undefined && args.priority !== '') updateRecord.priority = String(args.priority);
    if (args.description !== undefined && args.description !== '') updateRecord.description = String(args.description);
    if (args.note !== undefined && args.note !== '') updateRecord.note = String(args.note);
    if (args.assignee !== undefined && args.assignee !== '') updateRecord.assignee = String(args.assignee);
    if (args.due_date !== undefined && args.due_date !== '') updateRecord.due_date = String(args.due_date);
    if (args.labels !== undefined && args.labels !== null) {
        const labelsList = coerceList(args.labels);
        updateRecord.labels = labelsList;
        updateRecord.tags = labelsList;
    }
    if (args.blocked_by !== undefined && args.blocked_by !== '') updateRecord.blocked_by = String(args.blocked_by);
    if (args.blocks !== undefined && args.blocks !== '') updateRecord.blocks = String(args.blocks);

    const existingEntries = readJsonl(found.filePath);
    sealLedgerEntry(updateRecord, existingEntries);
    appendJsonl(found.filePath, updateRecord);

    return {
        updated: itemId,
        changes: updateRecord,
        ledger: found.ledgerName,
        next_steps: [
            {
                tool: 'delimit_ledger_context',
                reason: 'GOVERNANCE LOOP: check ledger for next action',
                premium: false,
                required: true,
            },
        ],
    };
}

function handleLedgerDone(args) {
    const cwd = process.cwd();
    const itemId = String(args.item_id || '').trim();
    if (!itemId) throw new Error('item_id is required');

    const found = findItemInLedger(itemId, cwd, args.venture);
    if (!found) {
        return {
            error: `Item ${itemId} not found in project ledger`,
        };
    }

    const now = formatUtcTimestamp();
    const updateRecord = {
        id: itemId,
        type: 'update',
        updated_at: now,
        worked_by: 'unknown',
        status: 'done',
        note: args.note || '',
        ship_proof: {
            verified: Boolean(args.commit_sha || args.pr_url),
            commit_sha: args.commit_sha || null,
            pr_url: args.pr_url || null,
        },
        evidence: {
            class: 'assertion',
            kind: 'assessment',
            ref: '',
            observed_at: now,
            observed_by: 'unknown',
        },
    };

    const existingEntries = readJsonl(found.filePath);
    sealLedgerEntry(updateRecord, existingEntries);
    appendJsonl(found.filePath, updateRecord);

    return {
        updated: itemId,
        changes: updateRecord,
        ledger: found.ledgerName,
        next_steps: [
            {
                tool: 'delimit_ledger_context',
                reason: "See what's next",
                premium: false,
            },
        ],
    };
}

function handleLedgerContext(args) {
    const cwd = process.cwd();
    const venture = resolveVenture(args.venture, cwd);
    const opsPath = getLedgerFilePath(cwd, 'ops', args.venture);
    const snapshot = buildLedgerSnapshot(opsPath);

    const all = snapshot.all;
    const open = snapshot.open;

    let openCount = 0;
    let done = 0;
    let inProgress = 0;
    let blocked = 0;
    let archived = 0;

    for (const item of all) {
        const s = String(item.status || 'open').toLowerCase();
        if (s === 'open') openCount++;
        else if (s === 'done') done++;
        else if (s === 'in_progress') inProgress++;
        else if (s === 'blocked') blocked++;
        else if (s === 'archived') archived++;
    }

    const nextUp = open.slice(0, 5).map(item => ({
        id: item.id,
        title: item.title || '',
        priority: item.priority || 'P1',
        truth_label: item.truth_label || 'legacy/unclassified',
        verified_states: item.verified_states || [],
    }));

    return {
        venture,
        open_items: open.length,
        next_up: nextUp,
        summary: {
            total: open.length,
            open: openCount,
            done,
            in_progress: inProgress,
            blocked,
            archived,
        },
        next_steps: [],
    };
}

function handleHandoffCreate(args) {
    const cwd = process.cwd();
    const taskDesc = String(args.task_description || '').trim();
    const completed = coerceList(args.completed);
    const notCompleted = coerceList(args.not_completed);
    const assumptions = coerceList(args.assumptions);
    const blockers = coerceList(args.blockers);
    const inScope = coerceList(args.in_scope);
    const outOfScope = coerceList(args.out_of_scope);

    let files = [];
    if (args.files_modified) {
        if (typeof args.files_modified === 'string') {
            try {
                const parsed = JSON.parse(args.files_modified);
                if (Array.isArray(parsed)) files = parsed;
            } catch {
                files = coerceList(args.files_modified).map(f => ({ path: f, change_type: 'modified', summary: '' }));
            }
        } else if (Array.isArray(args.files_modified)) {
            files = args.files_modified;
        }
    }

    const receiptId = crypto.randomBytes(4).toString('hex');
    const createdAt = new Date().toISOString();
    const fromModel = 'claude';
    const toModel = args.to_model || 'any';
    const priority = args.priority || 'P1';
    const nextAction = args.next_action || '';

    const handoffDir = getHandoffDir(cwd);
    ensureDir0700(handoffDir);

    const bodyForDigest = {
        assumptions,
        blockers,
        completed,
        created_at: createdAt,
        created_by_model: fromModel,
        created_by_session: process.env.DELIMIT_SESSION_ID || process.env.DELIMIT_SESSION || '',
        files_modified: files,
        from_model: fromModel,
        in_scope: inScope,
        next_action: nextAction,
        not_completed: notCompleted,
        out_of_scope: outOfScope,
        priority,
        project_path: cwd,
        receipt_id: receiptId,
        task_description: taskDesc,
        to_model: toModel,
    };
    const contentDigest = computeContentDigest(bodyForDigest);

    const formattedLines = [
        '=== HANDOFF RECEIPT ===',
        `ID: ${receiptId}`,
        `From: ${fromModel} | To: ${toModel}`,
        `Task: ${taskDesc}`,
        `Priority: ${priority}`,
        `Created: ${createdAt}`,
        'Integrity: ok | Truth: ASSERTED | Stale: no | Owner-backed: false',
        `Origin: model=${fromModel} session=?`,
        '',
        'COMPLETED:',
        ...(completed.length ? completed.map(c => `  [x] ${c}`) : ['  (none)']),
        '',
        'NOT COMPLETED:',
        ...(notCompleted.length ? notCompleted.map(nc => `  [ ] ${nc}`) : ['  (none)']),
        '',
        `NEXT ACTION: ${nextAction || '(none specified)'}`,
        '',
        `To acknowledge: delimit_handoff_acknowledge(receipt_id="${receiptId}")`,
        '========================',
    ];
    const formatted = formattedLines.join('\n');

    const receipt = {
        receipt_id: receiptId,
        created_at: createdAt,
        from_model: fromModel,
        to_model: toModel,
        project_path: cwd,
        task_description: taskDesc,
        completed,
        not_completed: notCompleted,
        assumptions,
        blockers,
        files_modified: files,
        in_scope: inScope,
        out_of_scope: outOfScope,
        next_action: nextAction,
        priority,
        acknowledged: false,
        acknowledged_at: '',
        acknowledged_by: '',
        acknowledge_notes: '',
        logical_session_id: '',
        soul_id: '',
        handoff_id: '',
        content_digest: contentDigest,
        created_by_model: fromModel,
        created_by_session: process.env.DELIMIT_SESSION_ID || process.env.DELIMIT_SESSION || '',
        integrity: 'ok',
        stale: false,
        owner_backed: false,
    };

    writeFile0600(path.join(handoffDir, `${receiptId}.json`), JSON.stringify(receipt, null, 2));

    const index = loadHandoffIndex(handoffDir);
    index.receipts.push({
        receipt_id: receiptId,
        created_at: createdAt,
        task_description: taskDesc,
        from_model: fromModel,
        to_model: toModel,
        priority,
        acknowledged: false,
    });
    saveHandoffIndex(handoffDir, index);

    return {
        status: 'created',
        receipt_id: receiptId,
        project: cwd,
        task_description: taskDesc,
        completed_count: completed.length,
        not_completed_count: notCompleted.length,
        files_count: files.length,
        next_action: nextAction,
        priority,
        formatted,
        message: `Handoff receipt ${receiptId} created. Receiving agent should run delimit_handoff_acknowledge(receipt_id="${receiptId}").`,
        next_steps: [
            {
                tool: 'delimit_ledger_context',
                reason: 'GOVERNANCE LOOP: check ledger for next action',
                premium: false,
                required: true,
            },
        ],
    };
}

function handleHandoffList(args) {
    const cwd = process.cwd();
    const status = String(args.status || 'pending').toLowerCase();
    const limit = Math.max(1, Math.min(200, parseInt(args.limit, 10) || 50));
    const projectPath = args.project_path ? String(args.project_path).trim() : '';

    const baseDir = path.join(getDelimitHome(), 'handoff_receipts');
    let candidateDirs = [];

    if (projectPath) {
        candidateDirs.push(getHandoffDir(projectPath));
    } else if (fs.existsSync(baseDir)) {
        try {
            const allDirs = fs.readdirSync(baseDir)
                .map(d => path.join(baseDir, d))
                .filter(d => fs.statSync(d).isDirectory());
            candidateDirs.push(...allDirs);
        } catch { /* ignore */ }
    } else {
        candidateDirs.push(getHandoffDir(cwd));
    }

    let allReceipts = [];
    const seenIds = new Set();
    for (const dir of candidateDirs) {
        const index = loadHandoffIndex(dir);
        for (const entry of index.receipts) {
            if (!entry || !entry.receipt_id || seenIds.has(entry.receipt_id)) continue;
            seenIds.add(entry.receipt_id);
            const rcptFile = path.join(dir, `${entry.receipt_id}.json`);
            let rcpt = null;
            if (fs.existsSync(rcptFile)) {
                try {
                    rcpt = JSON.parse(fs.readFileSync(rcptFile, 'utf8'));
                } catch {}
            }
            if (!rcpt) continue;

            const isAck = Boolean(rcpt.acknowledged);
            if (status === 'all' || (status === 'pending' && !isAck) || (status === 'acknowledged' && isAck)) {
                allReceipts.push(rcpt);
            }
        }
    }

    allReceipts.sort((a, b) => {
        const cmp = String(b.created_at || '').localeCompare(String(a.created_at || ''));
        if (cmp !== 0) return cmp;
        return String(b.receipt_id || '').localeCompare(String(a.receipt_id || ''));
    });

    const totalCount = allReceipts.length;
    const page = allReceipts.slice(0, limit);
    const truncated = totalCount > page.length;

    if (page.length === 0) {
        return {
            status: 'empty',
            filter: status,
            project_path: projectPath,
            limit,
            count: 0,
            total_count: 0,
            truncated: false,
            message: `No ${status} handoff receipts found.`,
            next_steps: [],
        };
    }

    const formattedList = page.map(r => ({
        receipt_id: r.receipt_id,
        project_path: r.project_path,
        created_at: r.created_at,
        task_description: r.task_description,
        from_model: r.from_model,
        to_model: r.to_model,
        priority: r.priority,
        acknowledged: Boolean(r.acknowledged),
        completed_count: (r.completed || []).length,
        not_completed_count: (r.not_completed || []).length,
        next_action: r.next_action || '',
    }));

    return {
        status: 'ok',
        filter: status,
        project_path: projectPath,
        limit,
        count: page.length,
        total_count: totalCount,
        truncated,
        receipts: formattedList,
        message: `${page.length} ${status} receipt(s) found.`,
        next_steps: [
            {
                tool: 'delimit_ledger_context',
                reason: 'check ledger for next action',
                premium: false,
            },
        ],
    };
}

function handleHandoffAcknowledge(args) {
    const receiptId = String(args.receipt_id || '').trim();
    if (!receiptId) {
        return {
            status: 'error',
            message: 'receipt_id is required.',
        };
    }
    const notes = String(args.notes || '');
    const model = 'claude';
    const projectPath = args.project_path ? String(args.project_path).trim() : '';

    const baseDir = path.join(getDelimitHome(), 'handoff_receipts');
    let foundDir = null;
    let foundPath = null;

    if (projectPath) {
        const candidate = path.join(getHandoffDir(projectPath), `${receiptId}.json`);
        if (fs.existsSync(candidate)) {
            foundDir = path.dirname(candidate);
            foundPath = candidate;
        }
    } else if (fs.existsSync(baseDir)) {
        try {
            const subdirs = fs.readdirSync(baseDir).map(d => path.join(baseDir, d));
            for (const d of subdirs) {
                const target = path.join(d, `${receiptId}.json`);
                if (fs.existsSync(target)) {
                    foundDir = d;
                    foundPath = target;
                    break;
                }
            }
        } catch {}
    }

    if (!foundPath) {
        return {
            status: 'not_found',
            message: `No receipt with ID '${receiptId}' found.`,
        };
    }

    try {
        const receipt = JSON.parse(fs.readFileSync(foundPath, 'utf8'));
        receipt.acknowledged = true;
        receipt.acknowledged_at = new Date().toISOString();
        receipt.acknowledged_by = model;
        receipt.acknowledge_notes = notes;
        writeFile0600(foundPath, JSON.stringify(receipt, null, 2));

        const index = loadHandoffIndex(foundDir);
        for (const item of index.receipts) {
            if (item && item.receipt_id === receiptId) {
                item.acknowledged = true;
                break;
            }
        }
        saveHandoffIndex(foundDir, index);

        return {
            status: 'acknowledged',
            receipt_id: receiptId,
            next_action: receipt.next_action || '',
            message: `Receipt ${receiptId} acknowledged. Next action: ${receipt.next_action || '(none specified)'}`,
            next_steps: [
                {
                    tool: 'delimit_ledger_context',
                    reason: 'GOVERNANCE LOOP: check ledger for next action',
                    premium: false,
                    required: true,
                },
            ],
        };
    } catch (err) {
        return {
            status: 'error',
            message: `Failed to update receipt: ${err.message}`,
        };
    }
}

// --- Tool Dispatcher ---

function executeTool(name, args = {}) {
    switch (name) {
        case 'delimit_version':
            return handleDelimitVersion();
        case 'delimit_help':
            return handleDelimitHelp(args);
        case 'delimit_ledger_add':
            return handleLedgerAdd(args);
        case 'delimit_ledger_list':
            return handleLedgerList(args);
        case 'delimit_ledger_update':
            return handleLedgerUpdate(args);
        case 'delimit_ledger_done':
            return handleLedgerDone(args);
        case 'delimit_ledger_context':
            return handleLedgerContext(args);
        case 'delimit_handoff_create':
            return handleHandoffCreate(args);
        case 'delimit_handoff_list':
            return handleHandoffList(args);
        case 'delimit_handoff_acknowledge':
            return handleHandoffAcknowledge(args);
        default:
            throw new Error(`Unknown tool: ${name}`);
    }
}

// --- MCP JSON-RPC 2.0 stdio Server ---

function sendJson(msg) {
    process.stdout.write(JSON.stringify(msg) + '\n');
}

function sendResponse(id, result) {
    sendJson({ jsonrpc: '2.0', id, result });
}

function sendError(id, code, message) {
    sendJson({ jsonrpc: '2.0', id, error: { code, message } });
}

function startServer() {
    const rl = readline.createInterface({
        input: process.stdin,
        output: process.stdout,
        terminal: false,
    });

    rl.on('line', (line) => {
        const trimmed = line.trim();
        if (!trimmed) return;

        let msg;
        try {
            msg = JSON.parse(trimmed);
        } catch (e) {
            sendError(null, -32700, `Parse error: ${e.message}`);
            return;
        }

        const id = msg.id !== undefined ? msg.id : null;
        const method = msg.method;

        if (id === null && method && method.startsWith('notifications/')) {
            // Notification: no response required
            return;
        }

        switch (method) {
            case 'initialize': {
                sendResponse(id, {
                    protocolVersion: '2024-11-05',
                    capabilities: {
                        tools: {},
                    },
                    serverInfo: {
                        name: 'delimit',
                        version: '4.22.0',
                    },
                });
                break;
            }

            case 'ping': {
                sendResponse(id, {});
                break;
            }

            case 'tools/list': {
                sendResponse(id, {
                    tools: TOOL_DEFINITIONS,
                });
                break;
            }

            case 'tools/call': {
                const params = msg.params || {};
                const toolName = params.name;
                const toolArgs = params.arguments || {};

                try {
                    const result = executeTool(toolName, toolArgs);
                    sendResponse(id, {
                        content: [
                            {
                                type: 'text',
                                text: JSON.stringify(result, null, 2),
                            },
                        ],
                        structuredContent: result,
                        isError: false,
                    });
                } catch (err) {
                    sendResponse(id, {
                        content: [
                            {
                                type: 'text',
                                text: JSON.stringify({ error: err.message }),
                            },
                        ],
                        structuredContent: { error: err.message },
                        isError: true,
                    });
                }
                break;
            }

            default:
                if (id !== null) {
                    sendError(id, -32601, `Method not found: ${method}`);
                }
                break;
        }
    });

    rl.on('close', () => {
        process.exit(0);
    });
}

if (require.main === module) {
    startServer();
}

module.exports = {
    TOOL_DEFINITIONS,
    executeTool,
    getDelimitHome,
    resolveRepoRoot,
    resolveVenture,
    getLedgerDir,
    getLedgerFilePath,
    startServer,
    pyJsonDumps,
    escapePyString,
    ensureDir0700,
    writeFile0600,
    appendFile0600,
};
