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
    const repo = resolveRepoRoot(cwd);
    if (repo) return path.basename(repo);
    const base = path.basename(cwd);
    return base || 'unsorted';
}

function getLedgerDir(cwd = process.cwd()) {
    const repo = resolveRepoRoot(cwd);
    if (repo && fs.existsSync(path.join(repo, '.delimit', 'ledger'))) {
        return path.join(repo, '.delimit', 'ledger');
    }
    return path.join(getDelimitHome(), 'ledger');
}

function getLedgerFilePath(cwd = process.cwd(), ledger = 'ops') {
    const ledgerDir = getLedgerDir(cwd);
    fs.mkdirSync(ledgerDir, { recursive: true });
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

function appendJsonl(filePath, obj) {
    fs.mkdirSync(path.dirname(filePath), { recursive: true });
    fs.appendFileSync(filePath, JSON.stringify(obj) + '\n', 'utf8');
}

function buildLedgerSnapshot(filePath) {
    const entries = readJsonl(filePath);
    const latestById = new Map();

    for (const entry of entries) {
        if (!entry || !entry.id) continue;
        const current = latestById.get(entry.id) || {};
        if (entry.type === 'update') {
            latestById.set(entry.id, {
                ...current,
                ...entry,
                id: current.id || entry.id,
                title: entry.title !== undefined ? entry.title : current.title,
                description: entry.description !== undefined ? entry.description : current.description,
                priority: entry.priority !== undefined ? entry.priority : current.priority,
                status: entry.status !== undefined ? entry.status : (current.status || 'open'),
            });
        } else {
            latestById.set(entry.id, {
                ...entry,
                status: entry.status || 'open',
                ...current,
            });
        }
    }

    const priorityWeight = { P0: 0, P1: 1, P2: 2 };
    const all = Array.from(latestById.values());
    const open = all
        .filter(item => !['done', 'blocked', 'archived'].includes(String(item.status || 'open').toLowerCase()))
        .sort((a, b) => (priorityWeight[a.priority] ?? 9) - (priorityWeight[b.priority] ?? 9));

    return { all, open, latestById };
}

function nextLedgerId(filePath) {
    const entries = readJsonl(filePath);
    let max = 0;
    for (const e of entries) {
        if (e && typeof e.id === 'string') {
            const m = e.id.match(/^LED-(\d+)$/);
            if (m) {
                const n = parseInt(m[1], 10);
                if (n > max) max = n;
            }
        }
    }
    return `LED-${String(max + 1).padStart(3, '0')}`;
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
    fs.mkdirSync(handoffDir, { recursive: true });
    fs.writeFileSync(path.join(handoffDir, 'index.json'), JSON.stringify(indexData, null, 2), 'utf8');
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
        description: 'Return the running Delimit server version and runtime information.',
        inputSchema: {
            type: 'object',
            properties: {},
            additionalProperties: false,
        },
    },
    {
        name: 'delimit_help',
        description: 'Delimit workflows overview and available command suggestions.',
        inputSchema: {
            type: 'object',
            properties: {
                tool_name: {
                    type: 'string',
                    description: 'Tool name (e.g. "delimit_ledger_add"). Empty returns the workflows overview.',
                },
            },
        },
    },
    {
        name: 'delimit_ledger_add',
        description: 'Add a new item to a project\'s ledger (tasks, bugs, features, decisions).',
        inputSchema: {
            type: 'object',
            properties: {
                title: { type: 'string', description: 'What needs to be done. Required.' },
                venture: { type: 'string', description: 'Project name or path. Empty = auto-detect from cwd.' },
                ledger: { type: 'string', description: '"ops" (tasks, bugs, features) or "strategy" (decisions).' },
                item_type: { type: 'string', description: 'task, fix, feat, strategy, consensus.' },
                priority: { type: 'string', description: 'P0 (urgent), P1 (important), P2 (nice to have).' },
                description: { type: 'string', description: 'Details.' },
                source: { type: 'string', description: 'Where this came from (session, consensus, etc).' },
                tags: { type: ['string', 'array'], description: 'Labels/tags (comma-separated or array).' },
                acceptance_criteria: { type: ['string', 'array'], description: 'List of testable "done when" conditions.' },
                context: { type: 'string', description: 'Background info an AI agent needs.' },
                tools_needed: { type: ['string', 'array'], description: 'Tools needed.' },
                estimated_complexity: { type: 'string', description: 'small, medium, or large.' },
                worked_by: { type: 'string', description: 'Which AI model is working on this.' },
            },
            required: ['title'],
        },
    },
    {
        name: 'delimit_ledger_list',
        description: 'List items from the project ledger with optional status or priority filtering.',
        inputSchema: {
            type: 'object',
            properties: {
                venture: { type: 'string', description: 'Project name or path. Empty = auto-detect.' },
                ledger: { type: 'string', description: '"ops" or "strategy".' },
                status: { type: 'string', description: 'Filter by status: open, done, in_progress, blocked, archived.' },
                priority: { type: 'string', description: 'Filter by priority: P0, P1, P2.' },
                limit: { type: 'integer', description: 'Maximum items to return (default 50).' },
            },
        },
    },
    {
        name: 'delimit_ledger_update',
        description: 'Update an existing ledger item (status, priority, title, description, or progress note).',
        inputSchema: {
            type: 'object',
            properties: {
                item_id: { type: 'string', description: 'Item ID to update (e.g. "LED-001"). Required.' },
                title: { type: 'string', description: 'Updated title.' },
                status: { type: 'string', description: 'open, in_progress, done, blocked, archived.' },
                priority: { type: 'string', description: 'P0, P1, P2.' },
                description: { type: 'string', description: 'Updated description.' },
                note: { type: 'string', description: 'Progress note or reason for change.' },
                venture: { type: 'string', description: 'Project name or path.' },
                ledger: { type: 'string', description: '"ops" or "strategy".' },
            },
            required: ['item_id'],
        },
    },
    {
        name: 'delimit_ledger_done',
        description: 'Mark a ledger item as done with an optional closing note.',
        inputSchema: {
            type: 'object',
            properties: {
                item_id: { type: 'string', description: 'Item ID to mark done (e.g. "LED-001"). Required.' },
                note: { type: 'string', description: 'Closing note explaining what was completed.' },
                venture: { type: 'string', description: 'Project name or path.' },
                ledger: { type: 'string', description: '"ops" or "strategy".' },
            },
            required: ['item_id'],
        },
    },
    {
        name: 'delimit_ledger_context',
        description: 'Read the active ledger context (open tasks, priority items, summary counts) to resume work.',
        inputSchema: {
            type: 'object',
            properties: {
                venture: { type: 'string', description: 'Project name or path. Empty = auto-detect from cwd.' },
            },
        },
    },
    {
        name: 'delimit_handoff_create',
        description: 'Create a structured handoff receipt when pausing or handing off a session.',
        inputSchema: {
            type: 'object',
            properties: {
                task_description: { type: 'string', description: 'What this piece of work is. Required.' },
                completed: { type: ['string', 'array'], description: 'What was done (e.g. tests passed).' },
                not_completed: { type: ['string', 'array'], description: 'Unfinished work.' },
                next_action: { type: 'string', description: 'The single next step.' },
                priority: { type: 'string', description: 'P0, P1, P2.' },
                from_model: { type: 'string', description: 'Model creating this handoff (default: claude).' },
                to_model: { type: 'string', description: 'Target model (default: any).' },
                assumptions: { type: ['string', 'array'], description: 'Assumptions made.' },
                blockers: { type: ['string', 'array'], description: 'Blockers encountered.' },
                files_modified: { type: ['string', 'array'], description: 'Files touched.' },
            },
            required: ['task_description'],
        },
    },
    {
        name: 'delimit_handoff_list',
        description: 'List pending or acknowledged handoff receipts.',
        inputSchema: {
            type: 'object',
            properties: {
                filter: { type: 'string', description: 'pending (default), all, acknowledged.' },
                limit: { type: 'integer', description: 'Max receipts to return (default 50).' },
            },
        },
    },
    {
        name: 'delimit_handoff_acknowledge',
        description: 'Acknowledge a handoff receipt before beginning work.',
        inputSchema: {
            type: 'object',
            properties: {
                receipt_id: { type: 'string', description: 'Receipt ID to acknowledge. Required.' },
                model: { type: 'string', description: 'Acknowledging model name.' },
                note: { type: 'string', description: 'Optional acknowledgement note.' },
            },
            required: ['receipt_id'],
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

function handleLedgerAdd(args) {
    const cwd = process.cwd();
    const title = String(args.title || '').trim();
    if (!title) throw new Error('title is required');

    const venture = resolveVenture(args.venture, cwd);
    const ledger = (args.ledger || 'ops').toLowerCase() === 'strategy' ? 'strategy' : 'ops';
    const filePath = getLedgerFilePath(cwd, ledger);
    const id = nextLedgerId(filePath);
    const now = new Date().toISOString();

    const tags = coerceList(args.tags);
    const criteria = coerceList(args.acceptance_criteria);
    const tools = coerceList(args.tools_needed);

    const hashInput = `${id}|${title}|${args.description || ''}|${args.priority || 'P1'}|${now}`;
    const hash = crypto.createHash('sha256').update(hashInput).digest('hex').slice(0, 16);

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
        acceptance_criteria: criteria,
        context: args.context || '',
        tools_needed: tools,
        estimated_complexity: args.estimated_complexity || '',
        worked_by: args.worked_by || 'unknown',
        verified_states: [],
        hash,
        created_at: now,
    };

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
    const ledger = (args.ledger || 'ops').toLowerCase() === 'strategy' ? 'strategy' : 'ops';
    const filePath = getLedgerFilePath(cwd, ledger);
    const snapshot = buildLedgerSnapshot(filePath);

    let items = snapshot.all;
    if (args.status) {
        const s = String(args.status).toLowerCase();
        items = items.filter(i => String(i.status || '').toLowerCase() === s);
    }
    if (args.priority) {
        const p = String(args.priority).toUpperCase();
        items = items.filter(i => String(i.priority || '').toUpperCase() === p);
    }

    const limit = Math.max(1, parseInt(args.limit, 10) || 50);
    items = items.slice(0, limit);

    return {
        venture,
        ledger,
        items,
        count: items.length,
        next_steps: [],
    };
}

function handleLedgerUpdate(args) {
    const cwd = process.cwd();
    const itemId = String(args.item_id || '').trim();
    if (!itemId) throw new Error('item_id is required');

    const venture = resolveVenture(args.venture, cwd);
    const ledger = (args.ledger || 'ops').toLowerCase() === 'strategy' ? 'strategy' : 'ops';
    const filePath = getLedgerFilePath(cwd, ledger);
    const snapshot = buildLedgerSnapshot(filePath);

    const existing = snapshot.latestById.get(itemId);
    if (!existing) {
        return {
            status: 'not_found',
            error: `Item "${itemId}" not found in ledger "${ledger}".`,
            item_id: itemId,
            next_steps: [],
        };
    }

    const now = new Date().toISOString();
    const updateRecord = {
        id: itemId,
        type: 'update',
        updated_at: now,
    };
    if (args.title !== undefined) updateRecord.title = String(args.title);
    if (args.status !== undefined) updateRecord.status = String(args.status);
    if (args.priority !== undefined) updateRecord.priority = String(args.priority);
    if (args.description !== undefined) updateRecord.description = String(args.description);
    if (args.note !== undefined) updateRecord.note = String(args.note);

    appendJsonl(filePath, updateRecord);

    const updated = {
        ...existing,
        ...updateRecord,
    };

    return {
        status: 'updated',
        updated,
        next_steps: [],
    };
}

function handleLedgerDone(args) {
    const cwd = process.cwd();
    const itemId = String(args.item_id || '').trim();
    if (!itemId) throw new Error('item_id is required');

    const venture = resolveVenture(args.venture, cwd);
    const ledger = (args.ledger || 'ops').toLowerCase() === 'strategy' ? 'strategy' : 'ops';
    const filePath = getLedgerFilePath(cwd, ledger);
    const snapshot = buildLedgerSnapshot(filePath);

    const existing = snapshot.latestById.get(itemId);
    const now = new Date().toISOString();
    const closeRecord = {
        id: itemId,
        type: 'update',
        status: 'done',
        updated_at: now,
        note: args.note || 'Marked done',
    };

    appendJsonl(filePath, closeRecord);

    return {
        status: 'closed',
        closed: {
            ...(existing || { id: itemId }),
            ...closeRecord,
        },
        next_steps: [],
    };
}

function handleLedgerContext(args) {
    const cwd = process.cwd();
    const venture = resolveVenture(args.venture, cwd);
    const opsPath = getLedgerFilePath(cwd, 'ops');
    const snapshot = buildLedgerSnapshot(opsPath);

    const all = snapshot.all;
    const open = snapshot.open;

    let done = 0;
    let inProgress = 0;
    let blocked = 0;
    let archived = 0;

    for (const item of all) {
        const s = String(item.status || 'open').toLowerCase();
        if (s === 'done') done++;
        else if (s === 'in_progress') inProgress++;
        else if (s === 'blocked') blocked++;
        else if (s === 'archived') archived++;
    }

    const nextUp = open.map(item => ({
        id: item.id,
        title: item.title || '',
        priority: item.priority || 'P1',
        truth_label: 'legacy/unclassified',
        verified_states: [],
    }));

    return {
        venture,
        open_items: open.length,
        next_up: nextUp,
        summary: {
            total: all.length,
            open: open.length,
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
    if (!taskDesc) throw new Error('task_description is required');

    const completed = coerceList(args.completed);
    const notCompleted = coerceList(args.not_completed);
    const assumptions = coerceList(args.assumptions);
    const blockers = coerceList(args.blockers);
    const files = coerceList(args.files_modified);

    const receiptId = crypto.randomBytes(4).toString('hex');
    const createdAt = new Date().toISOString();
    const fromModel = args.from_model || 'claude';
    const toModel = args.to_model || 'any';
    const priority = args.priority || 'P1';
    const nextAction = args.next_action || '';

    const handoffDir = getHandoffDir(cwd);
    fs.mkdirSync(handoffDir, { recursive: true });

    const formattedLines = [
        '=== HANDOFF RECEIPT ===',
        `ID: ${receiptId}`,
        `From: ${fromModel} | To: ${toModel}`,
        `Task: ${taskDesc}`,
        `Priority: ${priority}`,
        `Created: ${createdAt}`,
        'Integrity: ok | Truth: ASSERTED | Stale: no | Owner-backed: false',
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
        next_action: nextAction,
        priority,
        acknowledged: false,
        acknowledged_at: null,
        acknowledged_by: null,
        formatted,
    };

    fs.writeFileSync(path.join(handoffDir, `${receiptId}.json`), JSON.stringify(receipt, null, 2), 'utf8');

    const index = loadHandoffIndex(handoffDir);
    index.receipts.unshift({
        receipt_id: receiptId,
        project_path: cwd,
        created_at: createdAt,
        task_description: taskDesc,
        from_model: fromModel,
        to_model: toModel,
        priority,
        acknowledged: false,
        completed_count: completed.length,
        not_completed_count: notCompleted.length,
        next_action: nextAction,
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
                reason: 'check ledger for next action',
                premium: false,
            },
        ],
    };
}

function handleHandoffList(args) {
    const cwd = process.cwd();
    const filter = String(args.filter || 'pending').toLowerCase();
    const limit = Math.max(1, parseInt(args.limit, 10) || 50);

    const baseDir = path.join(getDelimitHome(), 'handoff_receipts');
    const handoffDir = getHandoffDir(cwd);

    let candidateDirs = [handoffDir];
    if (fs.existsSync(baseDir)) {
        try {
            const allDirs = fs.readdirSync(baseDir)
                .map(d => path.join(baseDir, d))
                .filter(d => fs.statSync(d).isDirectory());
            for (const d of allDirs) {
                if (!candidateDirs.includes(d)) candidateDirs.push(d);
            }
        } catch { /* ignore */ }
    }

    let allReceipts = [];
    for (const dir of candidateDirs) {
        const index = loadHandoffIndex(dir);
        for (const item of index.receipts) {
            allReceipts.push(item);
        }
    }

    // Filter
    if (filter === 'pending') {
        allReceipts = allReceipts.filter(r => !r.acknowledged);
    } else if (filter === 'acknowledged') {
        allReceipts = allReceipts.filter(r => r.acknowledged);
    }

    allReceipts.sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')));
    const list = allReceipts.slice(0, limit);

    return {
        status: 'ok',
        filter,
        project_path: cwd,
        limit,
        count: list.length,
        total_count: allReceipts.length,
        truncated: allReceipts.length > limit,
        receipts: list,
        message: `${list.length} pending receipt(s) found.`,
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
    if (!receiptId) throw new Error('receipt_id is required');
    const model = args.model || 'claude';
    const cwd = process.cwd();

    const baseDir = path.join(getDelimitHome(), 'handoff_receipts');
    let foundDir = null;
    let foundPath = null;

    if (fs.existsSync(baseDir)) {
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
        } catch { /* ignore */ }
    }

    if (!foundPath) {
        return {
            status: 'not_found',
            receipt_id: receiptId,
            message: `Receipt "${receiptId}" not found.`,
        };
    }

    try {
        const receipt = JSON.parse(fs.readFileSync(foundPath, 'utf8'));
        receipt.acknowledged = true;
        receipt.acknowledged_at = new Date().toISOString();
        receipt.acknowledged_by = model;
        if (args.note) receipt.acknowledgement_note = String(args.note);
        fs.writeFileSync(foundPath, JSON.stringify(receipt, null, 2), 'utf8');

        const index = loadHandoffIndex(foundDir);
        for (const item of index.receipts) {
            if (item.receipt_id === receiptId) {
                item.acknowledged = true;
                item.acknowledged_at = receipt.acknowledged_at;
                item.acknowledged_by = model;
                break;
            }
        }
        saveHandoffIndex(foundDir, index);
    } catch (err) {
        throw new Error(`Failed to update receipt: ${err.message}`);
    }

    return {
        status: 'acknowledged',
        receipt_id: receiptId,
        acknowledged_by: model,
        message: `Receipt ${receiptId} acknowledged.`,
    };
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
                    protocolVersion: '2025-06-18',
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
                    });
                } catch (err) {
                    sendResponse(id, {
                        content: [
                            {
                                type: 'text',
                                text: JSON.stringify({ error: err.message }),
                            },
                        ],
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
};
