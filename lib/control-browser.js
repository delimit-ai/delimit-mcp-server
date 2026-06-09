// `delimit control` — interactive browser over the LED-1709 unified control
// plane. Dependency-light: uses the package's existing `inquirer` + `chalk`
// (same as the chat REPL); NO blessed/ink.
//
// It consumes the SHARED backend (ai.control_plane) via a python3 shim,
// resolving the server path exactly the way chat-repl's
// captureSoulForMigration does (bundled gateway first, then the installed
// ~/.delimit/server). It does NOT reimplement aggregation — the same
// build_queue/get_item/approve/reject the web dashboard hits via
// /api/mcp -> delimit_control. One shared backend, two surfaces.

const path = require('path');
const os = require('os');
const fs = require('fs');
const { spawnSync } = require('child_process');
const chalk = require('chalk');
const inquirer = require('inquirer');

const PKG_VERSION = (() => {
    try { return require('../package.json').version; } catch (e) { return ''; }
})();

const CLASSES = ['attestation', 'approval', 'sensing', 'ops'];

// Resolve the gateway base that holds ai/control_plane.py. Bundled gateway
// first (shipped with the npm package), then the installed MCP server.
function resolveBase() {
    const candidates = [
        path.join(__dirname, '..', 'gateway'),
        path.join(os.homedir(), '.delimit', 'server'),
    ];
    for (const base of candidates) {
        if (fs.existsSync(path.join(base, 'ai', 'control_plane.py'))) return base;
    }
    return null;
}

// Run a one-liner against ai.control_plane and parse its JSON stdout.
// `expr` is a python expression returning a JSON-serializable value.
function callBackend(base, expr) {
    const py = `import sys, json; sys.path.insert(0, ${JSON.stringify(base)}); `
        + `from ai import control_plane as cp; `
        + `print(json.dumps(${expr}))`;
    const r = spawnSync('python3', ['-c', py], { encoding: 'utf-8' });
    if (r.status !== 0) {
        const err = (r.stderr || r.error || 'unknown error').toString().trim();
        throw new Error(err.split('\n').pop() || 'backend call failed');
    }
    try {
        return JSON.parse((r.stdout || '').trim());
    } catch (e) {
        throw new Error('could not parse backend output: ' + e.message);
    }
}

function buildQueue(base, classFilter, limit) {
    const cf = JSON.stringify(classFilter || '');
    const lim = parseInt(limit, 10) || 100;
    return callBackend(base, `cp.build_queue(class_filter=${cf}, state_filter="", limit=${lim})`);
}

function getItem(base, itemId) {
    return callBackend(base, `cp.get_item(${JSON.stringify(itemId)})`);
}

function act(base, verb, itemId, note) {
    // verb is "approve" or "reject"
    const fn = verb === 'approve' ? 'approve' : 'reject';
    return callBackend(base, `cp.${fn}(${JSON.stringify(itemId)}, note=${JSON.stringify(note || '')})`);
}

function banner() {
    console.log(chalk.magenta.bold('\n  ┌──────────────────────────────────────────┐'));
    console.log(chalk.magenta.bold('  │  ') + chalk.magenta('Delimit Control Plane') + chalk.gray('  ·  v' + PKG_VERSION)
        + chalk.magenta.bold('          │'));
    console.log(chalk.magenta.bold('  └──────────────────────────────────────────┘'));
    console.log(chalk.gray('  The merge gate for AI-written code · unified queue\n'));
}

function classColor(cls) {
    switch (cls) {
        case 'attestation': return chalk.cyan(cls);
        case 'approval': return chalk.yellow(cls);
        case 'sensing': return chalk.blue(cls);
        case 'ops': return chalk.green(cls);
        default: return chalk.gray(cls || 'unknown');
    }
}

function renderCounts(queue) {
    const byClass = {};
    for (const it of queue) {
        const c = it.class || 'unknown';
        byClass[c] = (byClass[c] || 0) + 1;
    }
    const parts = CLASSES.map(c => `${classColor(c)} ${chalk.bold(byClass[c] || 0)}`);
    console.log('  Lanes: ' + parts.join(chalk.gray('  ·  ')) + chalk.gray(`   (total ${queue.length})`));
}

async function pickLane() {
    const { lane } = await inquirer.prompt([{
        type: 'list',
        name: 'lane',
        message: 'Pick a lane to browse:',
        choices: [
            { name: 'all (balanced)', value: '' },
            { name: 'attestation (signed evidence)', value: 'attestation' },
            { name: 'approval (founder directives awaiting ack)', value: 'approval' },
            { name: 'sensing (STR-*)', value: 'sensing' },
            { name: 'ops (LED-* / work-orders)', value: 'ops' },
            new inquirer.Separator(),
            { name: 'quit', value: '__quit__' },
        ],
        loop: false,
    }]);
    return lane;
}

function itemLabel(it) {
    const id = chalk.bold((it.id || '?').padEnd(22).slice(0, 22));
    const state = chalk.gray((it.state || '').padEnd(18).slice(0, 18));
    const title = it.title || '(untitled)';
    return `${id} ${state} ${title}`;
}

async function pickItem(queue) {
    if (!queue.length) {
        console.log(chalk.gray('\n  (no items in this lane)\n'));
        return '__back__';
    }
    const choices = queue.map(it => ({ name: itemLabel(it), value: it.id }));
    choices.push(new inquirer.Separator());
    choices.push({ name: 'back', value: '__back__' });
    const { id } = await inquirer.prompt([{
        type: 'list',
        name: 'id',
        message: 'Select an item:',
        choices,
        pageSize: 15,
        loop: false,
    }]);
    return id;
}

function renderDetail(item) {
    console.log(chalk.magenta('\n  ─── item detail ───────────────────────────'));
    console.log('  id:      ' + chalk.bold(item.id));
    console.log('  class:   ' + classColor(item.class));
    console.log('  state:   ' + chalk.bold(item.state || ''));
    console.log('  title:   ' + (item.title || ''));
    console.log('  source:  ' + chalk.gray(item.source || ''));
    console.log('  created: ' + chalk.gray(item.created || ''));
    if (item.summary) console.log('  summary: ' + item.summary);
    if (item.links && Object.keys(item.links).length) {
        console.log('  links:   ' + chalk.gray(JSON.stringify(item.links)));
    }
    if (item.raw !== undefined) {
        const rawStr = JSON.stringify(item.raw, null, 2)
            .split('\n').map(l => '    ' + l).join('\n');
        console.log(chalk.gray('  raw:'));
        console.log(chalk.gray(rawStr));
    }
    console.log(chalk.magenta('  ───────────────────────────────────────────\n'));
}

// Returns true if the caller should refresh the lane (state changed).
async function handleItem(base, itemId) {
    let item;
    try {
        item = getItem(base, itemId);
    } catch (e) {
        console.log(chalk.red('\n  Could not load item: ' + e.message + '\n'));
        return false;
    }
    if (!item) {
        console.log(chalk.gray('\n  (item no longer in the queue)\n'));
        return true;
    }
    renderDetail(item);

    // Read-only browsing always works. Only approval-class items offer
    // approve/reject; everything else is back-only.
    if (item.class !== 'approval') {
        await inquirer.prompt([{
            type: 'list', name: 'next', message: 'Action:',
            choices: [{ name: 'back', value: '__back__' }], loop: false,
        }]);
        return false;
    }

    const { choice } = await inquirer.prompt([{
        type: 'list',
        name: 'choice',
        message: 'This is a founder-approval item — approve mirrors the email "ship it" ack:',
        choices: [
            { name: chalk.green('Approve'), value: 'approve' },
            { name: chalk.red('Reject'), value: 'reject' },
            new inquirer.Separator(),
            { name: 'Back (read-only, no change)', value: '__back__' },
        ],
        loop: false,
    }]);

    if (choice === '__back__') return false;

    const { note } = await inquirer.prompt([{
        type: 'input',
        name: 'note',
        message: `Optional note for this ${choice} (enter to skip):`,
    }]);

    try {
        const res = act(base, choice, itemId, note);
        const status = res && res.status;
        if (status === 'approved') {
            console.log(chalk.green(`\n  ✓ Approved ${itemId} — directive-completed ack written to the inbox approval store.\n`));
        } else if (status === 'rejected') {
            console.log(chalk.yellow(`\n  ✓ Rejected ${itemId} — directive-completed ack (disposition=rejected) written.\n`));
        } else if (status === 'noop') {
            console.log(chalk.gray(`\n  • ${itemId} was already acked — no change.\n`));
        } else {
            console.log(chalk.gray('\n  ' + JSON.stringify(res) + '\n'));
        }
    } catch (e) {
        console.log(chalk.red('\n  Action failed: ' + e.message + '\n'));
        return false;
    }
    return true;
}

async function run() {
    banner();

    const base = resolveBase();
    if (!base) {
        console.log(chalk.red('  Could not locate the Delimit control-plane backend'
            + ' (ai/control_plane.py) in the bundled gateway or ~/.delimit/server.'));
        console.log(chalk.gray('  Run `delimit install` to set up the MCP server.\n'));
        process.exitCode = 1;
        return;
    }

    // Clean exit on Ctrl-C.
    process.on('SIGINT', () => {
        console.log(chalk.gray('\n  Bye.\n'));
        process.exit(0);
    });

    // Outer loop: pick a lane until quit.
    // eslint-disable-next-line no-constant-condition
    while (true) {
        let lane;
        try {
            lane = await pickLane();
        } catch (e) {
            // inquirer throws on Ctrl-C/closed TTY — exit cleanly.
            console.log(chalk.gray('\n  Bye.\n'));
            return;
        }
        if (lane === '__quit__') {
            console.log(chalk.gray('\n  Bye.\n'));
            return;
        }

        // Inner loop: browse items in the lane until back.
        // eslint-disable-next-line no-constant-condition
        while (true) {
            let queue;
            try {
                queue = buildQueue(base, lane, 100);
            } catch (e) {
                console.log(chalk.red('\n  Failed to load queue: ' + e.message + '\n'));
                break;
            }
            renderCounts(queue);
            let itemId;
            try {
                itemId = await pickItem(queue);
            } catch (e) {
                console.log(chalk.gray('\n  Bye.\n'));
                return;
            }
            if (itemId === '__back__') break;
            await handleItem(base, itemId);
            // loop back to the (refreshed) item list
        }
    }
}

module.exports = {
    run,
    // exported for testing / reuse
    resolveBase,
    buildQueue,
    getItem,
    act,
};
