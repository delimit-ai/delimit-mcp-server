const fs = require('fs');
const path = require('path');
const readline = require('readline');
const { spawn } = require('child_process');
const {
    resolveContinuityContext,
    saveActiveVenture,
} = require('./continuity-resolver');
const { hookBootstrap } = require('./cross-model-hooks');

function readJson(filePath) {
    try {
        return JSON.parse(fs.readFileSync(filePath, 'utf-8'));
    } catch {
        return null;
    }
}

function appendJsonl(filePath, payload) {
    fs.mkdirSync(path.dirname(filePath), { recursive: true });
    fs.appendFileSync(filePath, JSON.stringify(payload) + '\n');
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

function readLatestSession(sessionDir) {
    if (!fs.existsSync(sessionDir)) {
        return null;
    }
    const files = fs.readdirSync(sessionDir)
        .filter(name => name.endsWith('.json'))
        .map(name => path.join(sessionDir, name))
        .sort((a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs);
    return files.length > 0 ? readJson(files[0]) : null;
}

function resolvePortfolioVenture(context, rawName) {
    const normalized = String(rawName || '').trim().toLowerCase();
    if (!normalized) {
        return null;
    }
    const ventures = context.ventureLedgers || [];
    return ventures.find((entry) => entry.scope === 'repo' && (
        entry.venture.toLowerCase() === normalized
        || path.basename(entry.repoRoot || '').toLowerCase() === normalized
        || entry.venture.toLowerCase().includes(normalized)
        || path.basename(entry.repoRoot || '').toLowerCase().includes(normalized)
    )) || null;
}

function rememberActiveVenture(target) {
    if (target?.repoRoot) {
        saveActiveVenture({
            venture: target.venture,
            repoRoot: target.repoRoot,
        });
    }
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
        openItems: open.slice(0, 5),
    };
}

function buildPortfolioSnapshot(context) {
    const ledgers = (context.ventureLedgers || []).map((entry) => {
        const snapshot = buildLedgerSnapshot(path.join(entry.ledgerRoot, 'operations.jsonl'));
        return {
            venture: entry.venture,
            scope: entry.scope,
            repoRoot: entry.repoRoot,
            ledgerRoot: entry.ledgerRoot,
            openCount: snapshot.openCount,
            nextItem: snapshot.nextItem,
        };
    });
    const active = ledgers
        .filter(item => item.openCount > 0)
        .sort((a, b) => {
            const prio = { P0: 0, P1: 1, P2: 2 };
            const aPrio = prio[a.nextItem?.priority] ?? 9;
            const bPrio = prio[b.nextItem?.priority] ?? 9;
            if (aPrio !== bPrio) return aPrio - bPrio;
            return b.openCount - a.openCount;
        });
    return {
        openCount: active.reduce((sum, item) => sum + item.openCount, 0),
        nextItem: active[0]?.nextItem || null,
        nextVenture: active[0]?.venture || null,
        ventures: ledgers,
        active,
    };
}

function getBootstrapState(context) {
    const statePath = path.join(context.continuityRoot, 'bootstrap-state.json');
    return {
        statePath,
        state: readJson(statePath),
    };
}

function getTaskBrief(context) {
    const taskBriefPath = path.join(context.continuityRoot, 'task-brief.json');
    return {
        taskBriefPath,
        brief: readJson(taskBriefPath),
    };
}

function getExecutionPlan(context) {
    const executionPlanPath = path.join(context.continuityRoot, 'execution-plan.json');
    return {
        executionPlanPath,
        plan: readJson(executionPlanPath),
    };
}

function getOwnerActions(context) {
    const ownerActionsPath = path.join(context.continuityRoot, 'owner-actions.json');
    return {
        ownerActionsPath,
        state: readJson(ownerActionsPath),
    };
}

function getExecutionState(context) {
    const statePath = path.join(context.continuityRoot, 'execution-state.json');
    return {
        statePath,
        state: readJson(statePath),
    };
}

function setExecutionState(context, nextState) {
    const statePath = path.join(context.continuityRoot, 'execution-state.json');
    fs.mkdirSync(path.dirname(statePath), { recursive: true });
    fs.writeFileSync(statePath, JSON.stringify(nextState, null, 2) + '\n');
}

function getWorkerState(context) {
    const pointerPath = path.join(context.continuityRoot, 'active-worker.json');
    const pointer = readJson(pointerPath);
    const statePath = pointer?.statePath || path.join(context.continuityRoot, 'worker-state.json');
    const workerState = readJson(statePath);
    const executionState = getExecutionState(context).state;
    return {
        statePath,
        state: workerState ? {
            ...workerState,
            phase: executionState?.phase ?? workerState.phase ?? null,
            currentItem: executionState?.currentItem ?? workerState.currentItem ?? null,
            nextItem: executionState?.nextItem ?? workerState.nextItem ?? null,
        } : null,
    };
}

function pidIsAlive(pid) {
    if (!pid || !Number.isInteger(pid)) {
        return false;
    }
    try {
        process.kill(pid, 0);
        return true;
    } catch {
        return false;
    }
}

function ensureWorker(context) {
    const worker = getWorkerState(context);
    const workerMatchesContext = worker.state
        && worker.state.repo === (context.repoRoot || process.cwd())
        && worker.state.ledgerRoot === context.ledgerRoot
        && worker.state.venture === context.venture;
    if (worker.state && pidIsAlive(worker.state.pid) && workerMatchesContext) {
        return { started: false, worker, restarted: false };
    }

    fs.mkdirSync(context.continuityRoot, { recursive: true });
    const workerPath = path.join(__dirname, 'session-worker.js');
    const runId = String(Date.now());
    const child = spawn(process.execPath, [workerPath], {
        detached: true,
        stdio: 'ignore',
        cwd: context.repoRoot || process.cwd(),
        env: {
            ...process.env,
            DELIMIT_WORKER_RUN_ID: runId,
            DELIMIT_HOME: context.delimitHome,
            DELIMIT_CONTINUITY_ROOT: context.continuityRoot,
            DELIMIT_REPO_GOVERNANCE_ROOT: context.repoGovernanceRoot || '',
            DELIMIT_RESOLVED_VENTURE: context.venture,
            DELIMIT_RESOLVED_ACTOR: context.actor,
        },
    });
    child.unref();
    const startedAt = new Date().toISOString();
    const statePath = path.join(context.continuityRoot, `worker-state-${runId}.json`);
    fs.writeFileSync(statePath, JSON.stringify({
        pid: child.pid,
        status: 'starting',
        phase: 'starting',
        updatedAt: startedAt,
        startedAt,
        actor: context.actor,
        venture: context.venture,
        repo: context.repoRoot || process.cwd(),
        ledgerRoot: context.ledgerRoot,
        ledgerScope: context.ledgerScope,
        continuityRoot: context.continuityRoot,
    }, null, 2) + '\n');
    fs.writeFileSync(path.join(context.continuityRoot, 'active-worker.json'), JSON.stringify({
        statePath,
        updatedAt: startedAt,
    }, null, 2) + '\n');
    return {
        started: true,
        restarted: Boolean(worker.state),
        worker: {
            statePath,
            state: null,
        },
    };
}

async function waitForWorkerState(context, timeoutMs = 1500) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
        const worker = getWorkerState(context);
        if (worker.state && pidIsAlive(worker.state.pid)) {
            return worker;
        }
        await new Promise(resolve => setTimeout(resolve, 100));
    }
    return getWorkerState(context);
}

function renderSummary(context) {
    const sessionDir = path.join(context.delimitHome, 'sessions');
    const ledger = context.ledgerScope === 'all'
        ? buildPortfolioSnapshot(context)
        : buildLedgerSnapshot(path.join(context.ledgerRoot, 'operations.jsonl'));
    const latestSession = readLatestSession(sessionDir);
    const bootstrap = getBootstrapState(context);
    const worker = getWorkerState(context);

    const workerStatus = worker.state
        ? (pidIsAlive(worker.state.pid) ? worker.state.status || 'running' : 'stale')
        : 'not running';
    const workerPhase = context.ledgerScope === 'all'
        ? null
        : (worker.state?.phase || (worker.state ? 'ready' : 'idle'));
    const lines = [];
    lines.push('Delimit');
    lines.push(`${context.venture}  ${context.repoRoot || process.cwd()}`);
    lines.push('');
    const currentItem = context.ledgerScope !== 'all' ? worker.state?.currentItem : null;
    if (currentItem) {
        lines.push(`Current: ${currentItem.id}  ${currentItem.title || '(untitled)'}  [${currentItem.priority || 'P?'}]`);
    }
    if (ledger.nextItem) {
        const prefix = context.ledgerScope === 'all' && ledger.nextVenture ? `${ledger.nextVenture} :: ` : '';
        lines.push(`Next: ${prefix}${ledger.nextItem.id}  ${ledger.nextItem.title || '(untitled)'}  [${ledger.nextItem.priority || 'P?'}]`);
    } else {
        lines.push('Next: none');
    }
    lines.push(`Queue: ${ledger.openCount} open`);
    lines.push(`Scope: ${context.ledgerScope}`);
    lines.push(`Worker: ${context.ledgerScope === 'all' ? 'portfolio' : workerStatus}`);
    if (workerPhase) {
        lines.push(`State: ${workerPhase}`);
    }
    if (context.ledgerScope !== 'all' && worker.state?.ownerActionCount) {
        lines.push(`Owner actions: ${worker.state.ownerActionCount} queued`);
    }
    lines.push(`Inbox: ${bootstrap.state?.daemons?.inbox?.active || 'unknown'}`);
    lines.push(`Social: ${bootstrap.state?.daemons?.social?.active || 'unknown'}`);
    if (context.ledgerScope === 'all' && ledger.active.length > 0) {
        lines.push('');
        lines.push('Active ventures:');
        for (const item of ledger.active.slice(0, 3)) {
            const next = item.nextItem ? `${item.nextItem.id} ${item.nextItem.title || '(untitled)'}` : 'no open items';
            lines.push(`- ${item.venture}: ${item.openCount} open | ${next}`);
        }
    }
    if (latestSession && latestSession.summary) {
        lines.push('');
        lines.push(`Recent: ${latestSession.summary}`);
    }
    return {
        ledger,
        latestSession,
        bootstrap,
        worker,
        workerStatus,
        text: lines.join('\n'),
    };
}

async function refreshBootstrap(context, mode = 'inspect') {
    return hookBootstrap(mode, {
        silent: true,
        scope: context.ledgerScope === 'all' ? 'all' : undefined,
        cwd: context.repoRoot || process.cwd(),
    });
}

function printHelp(context) {
    const commands = ['home', 'next', 'recent', 'details', 'help', 'exit'];
    if (context.ledgerScope === 'all') {
        commands.splice(3, 0, 'ventures', 'open <venture>', 'build <venture>', 'worker');
    } else {
        commands.splice(3, 0, 'done', 'worker', 'switch <venture>', 'portfolio');
    }
    console.log(`Commands: ${commands.join(', ')}`);
}

function markCurrentItemDone(context, note = '') {
    if (context.ledgerScope === 'all') {
        return { ok: false, error: 'Portfolio mode cannot mark items done. Open a venture first.' };
    }
    const execution = getExecutionState(context).state || {};
    const current = execution.currentItem;
    if (!current?.id) {
        return { ok: false, error: 'No current item selected.' };
    }
    const ledgerPath = path.join(context.ledgerRoot, 'operations.jsonl');
    const timestamp = new Date().toISOString();
    appendJsonl(ledgerPath, {
        id: current.id,
        type: 'update',
        updated_at: timestamp,
        status: 'done',
        note: note || 'Marked done from native Delimit session.',
        created_at: timestamp,
    });
    const refreshed = buildLedgerSnapshot(ledgerPath);
    setExecutionState(context, {
        ...execution,
        phase: refreshed.nextItem ? 'ready' : 'idle',
        currentItem: refreshed.nextItem ? {
            id: refreshed.nextItem.id,
            title: refreshed.nextItem.title || '',
            priority: refreshed.nextItem.priority || '',
            status: refreshed.nextItem.status || 'open',
            description: refreshed.nextItem.description || '',
        } : null,
        nextItem: refreshed.nextItem ? {
            id: refreshed.nextItem.id,
            title: refreshed.nextItem.title || '',
            priority: refreshed.nextItem.priority || '',
            status: refreshed.nextItem.status || 'open',
        } : null,
        updatedAt: timestamp,
    });
    return {
        ok: true,
        completed: current,
        next: refreshed.nextItem || null,
    };
}

async function runInteractiveSession(options = {}) {
    const mode = options.build ? 'execute' : 'inspect';
    const context = resolveContinuityContext({ cwd: options.cwd || process.cwd(), scope: options.scope });
    await refreshBootstrap(context, mode);

    let workerAction = null;
    if (options.build && context.ledgerScope !== 'all') {
        workerAction = ensureWorker(context);
        if (workerAction.started) {
            await waitForWorkerState(context);
        }
    }
    const summary = renderSummary(context);
    console.log(summary.text);
    if (workerAction && workerAction.started) {
        console.log('');
        console.log(workerAction.restarted ? 'Build session refreshed.' : 'Build session resumed.');
    }
    console.log('');
    printHelp(context);

    const rl = readline.createInterface({
        input: process.stdin,
        output: process.stdout,
        prompt: `delimit:${context.venture}> `,
    });
    let transitioning = false;

    rl.prompt();
    rl.on('line', async (line) => {
        const raw = line.trim();
        const command = raw.toLowerCase();
        if (!command || command === 'home' || command === 'status') {
            await refreshBootstrap(context, mode);
            console.log(renderSummary(context).text);
        } else if (command === 'next') {
            await refreshBootstrap(context, mode);
            const refreshed = renderSummary(context);
            const current = refreshed.ledger.nextItem;
            if (current) {
                const prefix = context.ledgerScope === 'all' && refreshed.ledger.nextVenture ? `${refreshed.ledger.nextVenture} :: ` : '';
                console.log(`${prefix}${current.id}  ${current.title || '(untitled)'}  [${current.priority || 'P?'}]`);
                if (current.description) {
                    console.log('');
                    console.log(current.description);
                }
            } else {
                console.log('No open ledger items.');
            }
        } else if (command === 'ventures') {
            if (context.ledgerScope !== 'all') {
                console.log('Portfolio view: use `portfolio`.');
            } else {
                await refreshBootstrap(context, mode);
                const refreshed = renderSummary(context);
                const lines = refreshed.ledger.active.map(item => {
                    const next = item.nextItem ? `${item.nextItem.id} ${item.nextItem.title || '(untitled)'}` : 'no open items';
                    return `${item.venture}: ${item.openCount} open | ${next}`;
                });
                console.log(lines.length ? lines.join('\n') : 'No open items across ventures.');
            }
        } else if (command.startsWith('open ')) {
            if (context.ledgerScope !== 'all') {
                console.log('`open` is available from portfolio view.');
            } else {
                const target = resolvePortfolioVenture(context, raw.slice(5));
                if (!target?.repoRoot) {
                    console.log(`Unknown venture: ${raw.slice(5).trim()}`);
                } else {
                    transitioning = true;
                    rememberActiveVenture(target);
                    rl.close();
                    await runInteractiveSession({ cwd: target.repoRoot, build: false });
                    return;
                }
            }
        } else if (command.startsWith('build ')) {
            if (context.ledgerScope !== 'all') {
                console.log('`build <venture>` is available from portfolio view.');
            } else {
                const target = resolvePortfolioVenture(context, raw.slice(6));
                if (!target?.repoRoot) {
                    console.log(`Unknown venture: ${raw.slice(6).trim()}`);
                } else {
                    transitioning = true;
                    rememberActiveVenture(target);
                    rl.close();
                    await runInteractiveSession({ cwd: target.repoRoot, build: true });
                    return;
                }
            }
        } else if (command === 'portfolio') {
            if (context.ledgerScope === 'all') {
                console.log('Already in portfolio view.');
            } else {
                transitioning = true;
                rl.close();
                await runInteractiveSession({ cwd: process.cwd(), scope: 'all' });
                return;
            }
        } else if (command.startsWith('switch ')) {
            if (context.ledgerScope === 'all') {
                console.log('Use `open <venture>` or `build <venture>` from portfolio view.');
            } else {
                const portfolio = resolveContinuityContext({ cwd: process.cwd(), scope: 'all' });
                const target = resolvePortfolioVenture(portfolio, raw.slice(7));
                if (!target?.repoRoot) {
                    console.log(`Unknown venture: ${raw.slice(7).trim()}`);
                } else {
                    transitioning = true;
                    rememberActiveVenture(target);
                    rl.close();
                    await runInteractiveSession({ cwd: target.repoRoot, build: false });
                    return;
                }
            }
        } else if (command === 'recent') {
            await refreshBootstrap(context, 'inspect');
            const latest = renderSummary(context).latestSession;
            console.log(latest?.summary || 'No saved session state.');
            if (latest?.blockers?.length) {
                console.log('');
                console.log(`Blockers: ${latest.blockers.join('; ')}`);
            }
        } else if (command === 'details' || command === 'bootstrap') {
            await refreshBootstrap(context, mode);
            const taskBrief = getTaskBrief(context);
            const executionPlan = getExecutionPlan(context);
            const ownerActions = getOwnerActions(context);
            if (taskBrief.brief?.item) {
                console.log(`Task: ${taskBrief.brief.summary}`);
                if (taskBrief.brief.item.description) {
                    console.log('');
                    console.log(taskBrief.brief.item.description);
                }
                if (taskBrief.brief.recommendedAction) {
                    console.log('');
                    console.log(`Next action: ${taskBrief.brief.recommendedAction}`);
                }
                if (Array.isArray(taskBrief.brief.guardrails) && taskBrief.brief.guardrails.length > 0) {
                    console.log('');
                    console.log('Guardrails:');
                    for (const line of taskBrief.brief.guardrails) {
                        console.log(`- ${line}`);
                    }
                }
                if (executionPlan.plan?.targetAreas?.length) {
                    console.log('');
                    console.log(`Target areas: ${executionPlan.plan.targetAreas.join(', ')}`);
                }
                if (executionPlan.plan?.steps?.length) {
                    console.log('');
                    console.log('Plan:');
                    for (const step of executionPlan.plan.steps) {
                        console.log(`- ${step}`);
                    }
                }
                if (ownerActions.state?.actions?.length) {
                    console.log('');
                    console.log('Owner actions (non-blocking):');
                    for (const action of ownerActions.state.actions) {
                        console.log(`- ${action.title} [${(action.channels || []).join(', ')}]`);
                    }
                }
            } else {
                const state = getBootstrapState(context);
                console.log(state.state ? JSON.stringify(state.state, null, 2) : 'No bootstrap state written yet.');
            }
        } else if (command === 'done') {
            const result = markCurrentItemDone(context);
            if (!result.ok) {
                console.log(result.error);
            } else {
                console.log(`Completed: ${result.completed.id} ${result.completed.title || ''}`);
                if (result.next) {
                    console.log(`Next: ${result.next.id} ${result.next.title || ''}`);
                } else {
                    console.log('Queue empty.');
                }
            }
        } else if (command === 'worker') {
            if (context.ledgerScope === 'all') {
                console.log('Portfolio view does not run a single worker. Use a repo session to build.');
                console.log('Try: build <venture>');
                console.log('');
                rl.prompt();
                return;
            }
            await refreshBootstrap(context, mode);
            const worker = getWorkerState(context);
            if (worker.state) {
                const status = pidIsAlive(worker.state.pid) ? worker.state.status || 'running' : 'stale';
                console.log(`Worker: ${status}`);
                if (worker.state.phase) {
                    console.log(`State: ${worker.state.phase}`);
                }
                console.log(`PID: ${worker.state.pid}`);
                if (worker.state.currentItem) {
                    console.log(`Current: ${worker.state.currentItem.id} ${worker.state.currentItem.title || ''}`);
                }
                if (worker.state.nextItem) {
                    console.log(`Next: ${worker.state.nextItem.id} ${worker.state.nextItem.title || ''}`);
                }
                const taskBrief = getTaskBrief(context);
                if (taskBrief.brief?.recommendedAction) {
                    console.log(`Action: ${taskBrief.brief.recommendedAction}`);
                }
                const executionPlan = getExecutionPlan(context);
                if (executionPlan.plan?.targetAreas?.length) {
                    console.log(`Targets: ${executionPlan.plan.targetAreas.join(', ')}`);
                }
                const ownerActions = getOwnerActions(context);
                if (ownerActions.state?.actions?.length) {
                    console.log(`Owner actions: ${ownerActions.state.actions.length} queued (non-blocking)`);
                }
            } else {
                console.log('No worker state written yet.');
            }
        } else if (command === 'help') {
            printHelp(context);
        } else if (command === 'exit' || command === 'quit') {
            rl.close();
            return;
        } else {
            console.log('Unknown command. Use `help` for available commands.');
        }
        console.log('');
        rl.prompt();
    });

    rl.on('close', () => {
        if (!transitioning) {
            console.log('Session closed.');
        }
    });
}

module.exports = {
    runInteractiveSession,
    renderSummary,
    ensureWorker,
    waitForWorkerState,
    getWorkerState,
    getTaskBrief,
    getExecutionPlan,
    getOwnerActions,
    pidIsAlive,
};
