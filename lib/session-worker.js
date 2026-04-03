const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');
const { resolveContinuityContext } = require('./continuity-resolver');

function readJson(filePath) {
    try {
        return JSON.parse(fs.readFileSync(filePath, 'utf-8'));
    } catch {
        return null;
    }
}

function writeJson(filePath, payload) {
    fs.mkdirSync(path.dirname(filePath), { recursive: true });
    fs.writeFileSync(filePath, JSON.stringify(payload, null, 2) + '\n');
}

function readJsonl(filePath) {
    if (!fs.existsSync(filePath)) {
        return [];
    }
    return fs.readFileSync(filePath, 'utf-8')
        .split('\n')
        .map(line => line.trim())
        .filter(Boolean)
        .map(line => {
            try {
                return JSON.parse(line);
            } catch {
                return null;
            }
        })
        .filter(Boolean);
}

function buildLedgerSnapshot(ledgerPath) {
    const entries = readJsonl(ledgerPath);
    const latestById = new Map();
    for (const entry of entries) {
        if (!entry || !entry.id) continue;
        const current = latestById.get(entry.id) || {};
        if (entry.type === 'update') {
            latestById.set(entry.id, {
                ...current,
                ...entry,
                id: current.id || entry.id,
                title: current.title || entry.title,
                description: current.description || entry.description,
                priority: current.priority || entry.priority,
            });
        } else {
            latestById.set(entry.id, {
                ...entry,
                ...current,
            });
        }
    }
    const priorityWeight = { P0: 0, P1: 1, P2: 2 };
    const open = Array.from(latestById.values())
        .filter(item => !['done', 'blocked'].includes(String(item.status || 'open')))
        .sort((a, b) => (priorityWeight[a.priority] ?? 9) - (priorityWeight[b.priority] ?? 9));
    return {
        openCount: open.length,
        nextItem: open[0] || null,
        openItems: open,
    };
}

function writeWorkerState(statePath, payload) {
    fs.mkdirSync(path.dirname(statePath), { recursive: true });
    fs.writeFileSync(statePath, JSON.stringify(payload, null, 2) + '\n');
}

function writeActivePointer(pointerPath, statePath) {
    fs.mkdirSync(path.dirname(pointerPath), { recursive: true });
    fs.writeFileSync(pointerPath, JSON.stringify({ statePath, updatedAt: new Date().toISOString() }, null, 2) + '\n');
}

function appendJsonl(filePath, payload) {
    fs.mkdirSync(path.dirname(filePath), { recursive: true });
    fs.appendFileSync(filePath, JSON.stringify(payload) + '\n');
}

function selectCurrentItem(executionState, ledger) {
    const currentId = executionState?.currentItem?.id;
    if (currentId) {
        const stillOpen = ledger.openItems.find(item => item.id === currentId);
        if (stillOpen) {
            return {
                id: stillOpen.id,
                title: stillOpen.title || '',
                priority: stillOpen.priority || '',
                status: stillOpen.status || 'open',
                description: stillOpen.description || '',
            };
        }
    }
    if (!ledger.nextItem) {
        return null;
    }
    return {
        id: ledger.nextItem.id,
        title: ledger.nextItem.title || '',
        priority: ledger.nextItem.priority || '',
        status: ledger.nextItem.status || 'open',
        description: ledger.nextItem.description || '',
    };
}

function loadExecutionState(executionPath) {
    return readJson(executionPath) || {};
}

function classifyItem(item) {
    const text = `${item?.title || ''} ${item?.description || ''}`.toLowerCase();
    if (/deploy|release|publish|production/.test(text)) return 'deploy';
    if (/ui|ux|dashboard|design|mobile/.test(text)) return 'product';
    if (/test|lint|ci|smoke|coverage/.test(text)) return 'verification';
    if (/docs|readme|guide|copy|content/.test(text)) return 'docs';
    if (/social|outreach|reddit|x |twitter|github issue|pr/.test(text)) return 'growth';
    return 'implementation';
}

function buildTaskBrief(context, currentItem, bootstrapState, phase) {
    if (!currentItem) {
        return {
            generatedAt: new Date().toISOString(),
            venture: context.venture,
            repo: context.repoRoot || process.cwd(),
            phase,
            summary: 'No active ledger item selected.',
            recommendedAction: 'Wait for a new open item or switch ventures.',
            guardrails: [
                'Do not mutate ledgers outside the current venture.',
                'Keep governance active before any deploy or publish action.',
            ],
        };
    }
    const category = classifyItem(currentItem);
    const recommendedActionByCategory = {
        deploy: 'Inspect repo state, tests, and governance before attempting any release step.',
        product: 'Inspect the relevant UI surface, validate responsiveness, and make the next controlled change.',
        verification: 'Run the smallest meaningful verification step first, then update the item with results.',
        docs: 'Open the relevant docs surface and prepare the next user-visible improvement.',
        growth: 'Inspect the target surface and prepare the next governed draft or outreach action.',
        implementation: 'Inspect the code area tied to this item and prepare the next governed execution step.',
    };
    return {
        generatedAt: new Date().toISOString(),
        venture: context.venture,
        repo: context.repoRoot || process.cwd(),
        phase,
        item: {
            id: currentItem.id,
            title: currentItem.title || '',
            priority: currentItem.priority || '',
            status: currentItem.status || 'open',
            description: currentItem.description || '',
        },
        category,
        summary: `${currentItem.id} ${currentItem.title || '(untitled)'}`.trim(),
        recommendedAction: recommendedActionByCategory[category] || recommendedActionByCategory.implementation,
        guardrails: [
            'Stay within the current venture and repo unless the ledger item explicitly requires cross-venture work.',
            'Do not deploy, publish, or use secrets without an explicit governed gate.',
            'Write outcomes back to ledger, session state, and memory before moving on.',
        ],
        latestSession: bootstrapState?.latestSession?.summary || null,
    };
}

function identifyOwnerActions(taskBrief, executionPlan) {
    if (!taskBrief?.item) {
        return [];
    }
    const actions = [];
    const category = taskBrief.category || 'implementation';
    if (['deploy', 'growth'].includes(category)) {
        actions.push({
            id: `${taskBrief.item.id}:owner-escalation`,
            itemId: taskBrief.item.id,
            title: `${taskBrief.item.id} requires owner review`,
            summary: taskBrief.recommendedAction,
            channels: ['dashboard', 'email', 'telegram'],
            status: 'open',
        });
    }
    if ((executionPlan?.repoSnapshot?.gitStatus || []).length > 0) {
        actions.push({
            id: `${taskBrief.item.id}:dirty-repo`,
            itemId: taskBrief.item.id,
            title: `${taskBrief.item.id} has existing dirty files in repo context`,
            summary: 'Proceed without blocking, but surface the dirty repo state to the owner and dashboard.',
            channels: ['dashboard', 'email', 'telegram'],
            status: 'open',
        });
    }
    return actions;
}

function safeExecFile(command, args, cwd) {
    try {
        return execFileSync(command, args, {
            cwd,
            encoding: 'utf-8',
            stdio: ['ignore', 'pipe', 'ignore'],
            timeout: 3000,
        }).trim();
    } catch {
        return '';
    }
}

function collectRepoSnapshot(context) {
    const repoRoot = context.repoRoot || process.cwd();
    const gitStatus = safeExecFile('git', ['status', '--short'], repoRoot)
        .split('\n')
        .map(line => line.trim())
        .filter(Boolean)
        .slice(0, 20);
    const topLevelEntries = fs.existsSync(repoRoot)
        ? fs.readdirSync(repoRoot, { withFileTypes: true })
            .filter((entry) => !entry.name.startsWith('.'))
            .slice(0, 30)
            .map((entry) => ({
                name: entry.name,
                type: entry.isDirectory() ? 'dir' : 'file',
            }))
        : [];
    const hasPackageJson = fs.existsSync(path.join(repoRoot, 'package.json'));
    const hasPyproject = fs.existsSync(path.join(repoRoot, 'pyproject.toml'));
    const hasCargo = fs.existsSync(path.join(repoRoot, 'Cargo.toml'));
    return {
        repoRoot,
        capturedAt: new Date().toISOString(),
        gitStatus,
        topLevelEntries,
        toolchain: {
            node: hasPackageJson,
            python: hasPyproject,
            rust: hasCargo,
        },
    };
}

function scoreEntryMatch(entryName, currentItem) {
    const haystack = `${currentItem?.title || ''} ${currentItem?.description || ''}`.toLowerCase();
    const tokens = entryName.toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
    return tokens.reduce((score, token) => (
        token.length >= 3 && haystack.includes(token) ? score + 1 : score
    ), 0);
}

function inferTargetAreas(currentItem, repoSnapshot) {
    const ranked = (repoSnapshot.topLevelEntries || [])
        .map((entry) => ({
            ...entry,
            score: scoreEntryMatch(entry.name, currentItem),
        }))
        .filter((entry) => entry.score > 0)
        .sort((a, b) => b.score - a.score)
        .slice(0, 5)
        .map((entry) => entry.name);
    if (ranked.length > 0) {
        return ranked;
    }
    const fallbacks = [];
    if (repoSnapshot.toolchain.node) fallbacks.push('package.json');
    if (repoSnapshot.toolchain.python) fallbacks.push('pyproject.toml');
    const commonDirs = repoSnapshot.topLevelEntries
        .filter((entry) => entry.type === 'dir' && ['app', 'src', 'pages', 'components', 'lib', 'tests'].includes(entry.name))
        .map((entry) => entry.name);
    return [...fallbacks, ...commonDirs].slice(0, 5);
}

function buildPlanSteps(category, repoSnapshot, targetAreas) {
    const base = [
        'Review the current ledger item and repo context.',
        'Inspect the highest-signal target areas before editing.',
    ];
    const categorySteps = {
        deploy: [
            'Confirm governance, repo cleanliness, and verification status before any release action.',
            'Prepare a release-safe checklist and stop for approval before deploy.',
        ],
        product: [
            'Inspect the relevant UI surface and responsive behavior first.',
            'Make one controlled change, then validate visually or with the smallest available test.',
        ],
        verification: [
            'Run the smallest meaningful verification command first.',
            'Capture the result and only then decide whether code changes are needed.',
        ],
        docs: [
            'Inspect the relevant content surface and supporting references.',
            'Make the next user-visible improvement and verify wording/links.',
        ],
        growth: [
            'Inspect the target content or thread before drafting.',
            'Prepare the next governed outreach action without posting automatically.',
        ],
        implementation: [
            'Inspect the relevant code path and adjacent tests.',
            'Prepare the smallest viable implementation slice before editing.',
        ],
    };
    const tail = repoSnapshot.gitStatus.length > 0
        ? ['Account for existing dirty files before making changes.']
        : ['Repo appears clean enough for a focused implementation slice.'];
    if (targetAreas.length > 0) {
        tail.unshift(`Start with: ${targetAreas.join(', ')}`);
    }
    return [...base, ...(categorySteps[category] || categorySteps.implementation), ...tail];
}

function buildExecutionPlan(context, currentItem, taskBrief, repoSnapshot, executionState) {
    if (!currentItem) {
        return null;
    }
    const targetAreas = inferTargetAreas(currentItem, repoSnapshot);
    const planItemId = executionState?.plan?.itemId;
    const reuseExisting = planItemId === currentItem.id && executionState?.plan?.steps?.length;
    const category = taskBrief.category || classifyItem(currentItem);
    const steps = reuseExisting
        ? executionState.plan.steps
        : buildPlanSteps(category, repoSnapshot, targetAreas);
    return {
        generatedAt: new Date().toISOString(),
        venture: context.venture,
        repo: context.repoRoot || process.cwd(),
        itemId: currentItem.id,
        category,
        phase: currentItem ? 'planned' : 'idle',
        targetAreas,
        repoSnapshot,
        summary: taskBrief.summary,
        recommendedAction: taskBrief.recommendedAction,
        steps,
    };
}

function createWorkerPayload(context, executionState, status = 'running') {
    const ledgerPath = path.join(context.ledgerRoot, 'operations.jsonl');
    const ledger = buildLedgerSnapshot(ledgerPath);
    const bootstrapPath = path.join(context.continuityRoot, 'bootstrap-state.json');
    const bootstrapState = readJson(bootstrapPath);
    const currentItem = selectCurrentItem(executionState, ledger);
    const phase = currentItem ? (executionState?.phase || 'ready') : 'idle';
    return {
        pid: process.pid,
        status,
        phase,
        updatedAt: new Date().toISOString(),
        startedAt: process.env.DELIMIT_WORKER_STARTED_AT || new Date().toISOString(),
        actor: context.actor,
        venture: context.venture,
        repo: context.repoRoot || process.cwd(),
        ledgerRoot: context.ledgerRoot,
        ledgerScope: context.ledgerScope,
        continuityRoot: context.continuityRoot,
        bootstrapPath,
        bootstrapState,
        openItemCount: ledger.openCount,
        currentItem,
        nextItem: ledger.nextItem ? {
            id: ledger.nextItem.id,
            title: ledger.nextItem.title || '',
            priority: ledger.nextItem.priority || '',
            status: ledger.nextItem.status || 'open',
        } : null,
    };
}

function runWorkerLoop(options = {}) {
    const context = resolveContinuityContext({ cwd: options.cwd || process.cwd() });
    const runId = process.env.DELIMIT_WORKER_RUN_ID || String(Date.now());
    const statePath = path.join(context.continuityRoot, `worker-state-${runId}.json`);
    const pointerPath = path.join(context.continuityRoot, 'active-worker.json');
    const executionPath = path.join(context.continuityRoot, 'execution-state.json');
    const startedAt = new Date().toISOString();
    process.env.DELIMIT_WORKER_STARTED_AT = startedAt;
    let lastSignature = '';

    const update = (status = 'running') => {
        const executionState = loadExecutionState(executionPath);
        const payload = createWorkerPayload(context, executionState, status);
        const taskBriefPath = path.join(context.continuityRoot, 'task-brief.json');
        const taskBrief = buildTaskBrief(context, payload.currentItem, payload.bootstrapState, payload.phase);
        const repoSnapshot = collectRepoSnapshot(context);
        const executionPlanPath = path.join(context.continuityRoot, 'execution-plan.json');
        const executionPlan = buildExecutionPlan(context, payload.currentItem, taskBrief, repoSnapshot, executionState);
        const ownerActionsPath = path.join(context.continuityRoot, 'owner-actions.json');
        const ownerActions = identifyOwnerActions(taskBrief, executionPlan);
        const effectivePhase = payload.currentItem ? (executionPlan ? 'planned' : 'ready') : 'idle';
        writeJson(executionPath, {
            ...executionState,
            venture: context.venture,
            repo: context.repoRoot || process.cwd(),
            ledgerRoot: context.ledgerRoot,
            phase: effectivePhase,
            currentItem: payload.currentItem,
            nextItem: payload.nextItem,
            taskBriefPath,
            executionPlanPath,
            ownerActionsPath,
            plan: executionPlan ? {
                itemId: executionPlan.itemId,
                category: executionPlan.category,
                targetAreas: executionPlan.targetAreas,
                steps: executionPlan.steps,
            } : null,
            ownerActions,
            updatedAt: payload.updatedAt,
        });
        writeJson(taskBriefPath, taskBrief);
        if (executionPlan) {
            writeJson(executionPlanPath, executionPlan);
        }
        writeJson(ownerActionsPath, {
            generatedAt: payload.updatedAt,
            venture: context.venture,
            repo: context.repoRoot || process.cwd(),
            itemId: payload.currentItem?.id || null,
            actions: ownerActions,
            nonBlocking: true,
        });
        writeWorkerState(statePath, {
            ...payload,
            phase: effectivePhase,
            ownerActionCount: ownerActions.length,
        });
        writeActivePointer(pointerPath, statePath);
        const signature = JSON.stringify({
            phase: effectivePhase,
            currentItem: payload.currentItem?.id || null,
            nextItem: payload.nextItem?.id || null,
            status,
        });
        if (signature !== lastSignature) {
            appendJsonl(path.join(context.continuityRoot, 'execution-log.jsonl'), {
                timestamp: payload.updatedAt,
                venture: context.venture,
                repo: context.repoRoot || process.cwd(),
                phase: effectivePhase,
                status,
                currentItem: payload.currentItem ? {
                    id: payload.currentItem.id,
                    title: payload.currentItem.title || '',
                } : null,
                nextItem: payload.nextItem ? {
                    id: payload.nextItem.id,
                    title: payload.nextItem.title || '',
                } : null,
            });
            lastSignature = signature;
        }
    };

    update('running');
    const interval = setInterval(() => update('running'), 15000);

    const shutdown = (status) => {
        clearInterval(interval);
        update(status);
        process.exit(0);
    };

    process.on('SIGTERM', () => shutdown('stopped'));
    process.on('SIGINT', () => shutdown('stopped'));
}

if (require.main === module) {
    runWorkerLoop();
}

module.exports = {
    runWorkerLoop,
};
