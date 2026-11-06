'use strict';

/**
 * Storage compatibility test suite between Delimit CLI MCP records toolset
 * (`delimit-cli mcp --toolset records`) and the bundled Claude plugin server
 * (`claude-plugin/server.cjs`).
 *
 * Requirements:
 * - Temporary HOME for each test run (cleaned up on completion)
 * - Hermetic environment with NO network calls (offline proxy traps)
 * - Bidirectional storage parity: CLI writes -> Bundled reads, Bundled writes -> CLI reads
 * - Disk format parity (paths, JSON/JSONL schema, field names, timestamp format, ID format)
 * - Schema parity across all 10 records tools (tools/list inputSchema properties and required fields)
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawn, spawnSync } = require('node:child_process');
const pkg = require('../package.json');
const { copyDir, writeInstalledVersionMarker } = require('../bin/delimit-setup');

const REPO_ROOT = path.resolve(__dirname, '..');
const CLI_PATH = path.join(REPO_ROOT, 'bin', 'delimit-cli.js');
const BUNDLED_PATH = path.join(REPO_ROOT, 'claude-plugin', 'server.cjs');

function setupTempHome(t) {
    const tempHome = fs.mkdtempSync(path.join('/tmp', 'delimit-compat-test-'));
    t.after(() => {
        fs.rmSync(tempHome, { recursive: true, force: true });
    });

    // Hermetically seed the plugin server directory and venv with system packages
    // so ensureMcpInstall() never triggers pip or network calls.
    const serverDir = path.join(tempHome, '.delimit', 'plugin-server', pkg.version);
    const venv = path.join(serverDir, 'venv');
    fs.mkdirSync(serverDir, { recursive: true });
    copyDir(path.join(REPO_ROOT, 'gateway'), serverDir);
    writeInstalledVersionMarker(serverDir, pkg.version);

    fs.mkdirSync(path.join(venv, 'bin'), { recursive: true });
    spawnSync('python3', ['-m', 'venv', '--system-site-packages', venv]);
    fs.writeFileSync(path.join(venv, '.delimit-deps-ok'), `${pkg.version}\n`);

    const env = {
        ...process.env,
        HOME: tempHome,
        DELIMIT_HOME: path.join(tempHome, '.delimit'),
        DELIMIT_NON_INTERACTIVE: '1',
        http_proxy: 'http://127.0.0.1:9',
        https_proxy: 'http://127.0.0.1:9',
        all_proxy: 'http://127.0.0.1:9',
    };

    return { tempHome, env };
}

function runRpc(proc, id, method, params) {
    return new Promise((resolve, reject) => {
        const timeout = setTimeout(() => {
            reject(new Error(`RPC timeout for id=${id} method=${method}`));
        }, 15000);

        function onData(chunk) {
            for (const line of chunk.toString().split('\n')) {
                if (!line.trim()) continue;
                try {
                    const msg = JSON.parse(line);
                    if (msg.id === id) {
                        clearTimeout(timeout);
                        proc.stdout.off('data', onData);
                        resolve(msg.result);
                    }
                } catch {
                    // Ignore non-JSON output on stdout
                }
            }
        }
        proc.stdout.on('data', onData);
        proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
    });
}

async function startServer(cmd, args, env) {
    const proc = spawn(cmd, args, { env, stdio: ['pipe', 'pipe', 'inherit'] });
    await runRpc(proc, 1, 'initialize', {
        protocolVersion: '2024-11-05',
        capabilities: {},
        clientInfo: { name: 'test-compat', version: '1.0' },
    });
    proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized', params: {} }) + '\n');
    return proc;
}

test('tools/list schema parity between CLI records server and bundled server', async (t) => {
    const { env } = setupTempHome(t);

    const cliProc = await startServer(process.execPath, [CLI_PATH, 'mcp', '--toolset', 'records'], env);
    const cliRes = await runRpc(cliProc, 2, 'tools/list', {});
    cliProc.kill();

    const bundledProc = await startServer(process.execPath, [BUNDLED_PATH], env);
    const bundledRes = await runRpc(bundledProc, 3, 'tools/list', {});
    bundledProc.kill();

    const expectedTools = [
        'delimit_version',
        'delimit_help',
        'delimit_ledger_add',
        'delimit_ledger_list',
        'delimit_ledger_update',
        'delimit_ledger_done',
        'delimit_ledger_context',
        'delimit_handoff_create',
        'delimit_handoff_list',
        'delimit_handoff_acknowledge',
    ];

    assert.ok(cliRes && Array.isArray(cliRes.tools), 'CLI must return a tools array');
    assert.ok(bundledRes && Array.isArray(bundledRes.tools), 'Bundled must return a tools array');

    for (const toolName of expectedTools) {
        const cliTool = cliRes.tools.find((t) => t.name === toolName);
        const bundledTool = bundledRes.tools.find((t) => t.name === toolName);

        assert.ok(cliTool, `CLI server missing expected tool: ${toolName}`);
        assert.ok(bundledTool, `Bundled server missing expected tool: ${toolName}`);

        const cliProps = Object.keys(cliTool.inputSchema?.properties || {}).sort();
        const bundledProps = Object.keys(bundledTool.inputSchema?.properties || {}).sort();
        assert.deepEqual(
            bundledProps,
            cliProps,
            `Parameter mismatch in ${toolName}: bundled=${JSON.stringify(bundledProps)} cli=${JSON.stringify(cliProps)}`
        );

        const cliReq = (cliTool.inputSchema?.required || []).slice().sort();
        const bundledReq = (bundledTool.inputSchema?.required || []).slice().sort();
        assert.deepEqual(
            bundledReq,
            cliReq,
            `Required parameters mismatch in ${toolName}: bundled=${JSON.stringify(bundledReq)} cli=${JSON.stringify(cliReq)}`
        );
    }
});

test('bidirectional storage compatibility: CLI writes, bundled reads, then bundled writes, CLI reads', async (t) => {
    const { tempHome, env } = setupTempHome(t);

    // --- Phase 1: CLI writes ---
    const cliProc1 = await startServer(process.execPath, [CLI_PATH, 'mcp', '--toolset', 'records'], env);

    const cliAdd = await runRpc(cliProc1, 2, 'tools/call', {
        name: 'delimit_ledger_add',
        arguments: { title: 'CLI Task LED-001', priority: 'P1', description: 'CLI Created Item' },
    });
    assert.ok(cliAdd && cliAdd.content, 'CLI add returned response');

    await runRpc(cliProc1, 3, 'tools/call', {
        name: 'delimit_ledger_update',
        arguments: { item_id: 'LED-001', note: 'CLI updated note', priority: 'P0' },
    });

    await runRpc(cliProc1, 4, 'tools/call', {
        name: 'delimit_ledger_done',
        arguments: { item_id: 'LED-001', note: 'CLI marked done' },
    });

    const cliHandoff = await runRpc(cliProc1, 5, 'tools/call', {
        name: 'delimit_handoff_create',
        arguments: { task_description: 'CLI Handoff Receipt', next_action: 'Step for bundled' },
    });
    const cliHandoffData = JSON.parse(cliHandoff.content[0].text);
    const cliRcptId = cliHandoffData.receipt_id;
    assert.match(cliRcptId, /^[a-f0-9]{8}$/, 'CLI receipt ID must be 8 hex characters');

    await runRpc(cliProc1, 6, 'tools/call', {
        name: 'delimit_handoff_acknowledge',
        arguments: { receipt_id: cliRcptId, notes: 'CLI Ack Receipt' },
    });
    cliProc1.kill();

    // --- Phase 2: Bundled reads CLI data on SAME HOME ---
    const bundledProc = await startServer(process.execPath, [BUNDLED_PATH], env);

    const bListRes = await runRpc(bundledProc, 7, 'tools/call', {
        name: 'delimit_ledger_list',
        arguments: {},
    });
    const bListData = JSON.parse(bListRes.content[0].text);
    assert.equal(bListData.summary.done, 1);
    assert.equal(bListData.summary.open, 0);
    const bItem1 = bListData.items.ops.find((i) => i.id === 'LED-001');
    assert.ok(bItem1, 'Bundled server must read item LED-001 created by CLI');
    assert.equal(bItem1.status, 'done');
    assert.equal(bItem1.priority, 'P0');

    const bCtxRes = await runRpc(bundledProc, 8, 'tools/call', {
        name: 'delimit_ledger_context',
        arguments: {},
    });
    const bCtxData = JSON.parse(bCtxRes.content[0].text);
    assert.equal(bCtxData.open_items, 0);
    assert.equal(bCtxData.summary.done, 1);

    const bHandoffRes = await runRpc(bundledProc, 9, 'tools/call', {
        name: 'delimit_handoff_list',
        arguments: { status: 'all' },
    });
    const bHandoffData = JSON.parse(bHandoffRes.content[0].text);
    assert.equal(bHandoffData.count, 1);
    assert.equal(bHandoffData.receipts[0].receipt_id, cliRcptId);
    assert.equal(bHandoffData.receipts[0].acknowledged, true);

    // --- Phase 3: Bundled writes ---
    const bAdd = await runRpc(bundledProc, 10, 'tools/call', {
        name: 'delimit_ledger_add',
        arguments: { title: 'Bundled Task LED-002', priority: 'P1', description: 'Bundled Created Item' },
    });
    const bAddData = JSON.parse(bAdd.content[0].text);
    assert.equal(bAddData.added.id, 'LED-002');

    await runRpc(bundledProc, 11, 'tools/call', {
        name: 'delimit_ledger_update',
        arguments: { item_id: 'LED-002', note: 'Bundled updated note', priority: 'P0' },
    });

    await runRpc(bundledProc, 12, 'tools/call', {
        name: 'delimit_ledger_done',
        arguments: { item_id: 'LED-002', note: 'Bundled marked done' },
    });

    const bHandoff = await runRpc(bundledProc, 13, 'tools/call', {
        name: 'delimit_handoff_create',
        arguments: { task_description: 'Bundled Handoff Receipt', next_action: 'Step for CLI' },
    });
    const bHandoffCreated = JSON.parse(bHandoff.content[0].text);
    const bRcptId = bHandoffCreated.receipt_id;
    assert.match(bRcptId, /^[a-f0-9]{8}$/, 'Bundled receipt ID must be 8 hex characters');

    await runRpc(bundledProc, 14, 'tools/call', {
        name: 'delimit_handoff_acknowledge',
        arguments: { receipt_id: bRcptId, notes: 'Bundled Ack Receipt' },
    });
    bundledProc.kill();

    // --- Phase 4: CLI reads Bundled data on SAME HOME ---
    const cliProc2 = await startServer(process.execPath, [CLI_PATH, 'mcp', '--toolset', 'records'], env);

    const cliListRes = await runRpc(cliProc2, 15, 'tools/call', {
        name: 'delimit_ledger_list',
        arguments: {},
    });
    const cliListData = JSON.parse(cliListRes.content[0].text);
    assert.equal(cliListData.summary.done, 2);
    assert.equal(cliListData.summary.open, 0);
    const cliItem2 = cliListData.items.ops.find((i) => i.id === 'LED-002');
    assert.ok(cliItem2, 'CLI must read item LED-002 written by bundled server');
    assert.equal(cliItem2.status, 'done');
    assert.equal(cliItem2.priority, 'P0');

    const cliCtxRes = await runRpc(cliProc2, 16, 'tools/call', {
        name: 'delimit_ledger_context',
        arguments: {},
    });
    const cliCtxData = JSON.parse(cliCtxRes.content[0].text);
    assert.equal(cliCtxData.open_items, 0);
    assert.equal(cliCtxData.summary.done, 2);

    const cliHandoffRes = await runRpc(cliProc2, 17, 'tools/call', {
        name: 'delimit_handoff_list',
        arguments: { status: 'all' },
    });
    const cliHandoffList = JSON.parse(cliHandoffRes.content[0].text);
    assert.equal(cliHandoffList.count, 2);
    const cliFoundR2 = cliHandoffList.receipts.find((r) => r.receipt_id === bRcptId);
    assert.ok(cliFoundR2, 'CLI must read receipt created by bundled server');
    assert.equal(cliFoundR2.acknowledged, true);

    cliProc2.kill();

    // --- Phase 5: On-disk file validation ---
    const opsFile = path.join(tempHome, '.delimit', 'ledger', 'operations.jsonl');
    assert.ok(fs.existsSync(opsFile), 'operations.jsonl must exist on disk under ~/.delimit/ledger');

    const opsLines = fs.readFileSync(opsFile, 'utf8').trim().split('\n').map((l) => JSON.parse(l));
    assert.equal(opsLines.length, 6, 'Expected 3 events from CLI + 3 events from Bundled = 6 lines');

    for (const line of opsLines) {
        assert.match(line.id, /^LED-\d{3}$/, 'id format must be LED-XXX');
        assert.match(line.hash, /^[a-f0-9]{16}$/, 'hash format must be 16-character hex');
        assert.match(line.created_at, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/, 'created_at must be ISO UTC without ms');
        if (line.type === 'update') {
            assert.match(line.updated_at, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/, 'updated_at must be ISO UTC without ms');
        }
    }

    const handoffBase = path.join(tempHome, '.delimit', 'handoff_receipts');
    assert.ok(fs.existsSync(handoffBase), 'handoff_receipts directory must exist');

    const projectDirs = fs.readdirSync(handoffBase).map((d) => path.join(handoffBase, d));
    assert.equal(projectDirs.length, 1, 'Expected exactly one project hash directory');

    const projectDir = projectDirs[0];
    const indexFile = path.join(projectDir, 'index.json');
    assert.ok(fs.existsSync(indexFile), 'index.json must exist');

    const indexData = JSON.parse(fs.readFileSync(indexFile, 'utf8'));
    assert.ok(Array.isArray(indexData.receipts), 'index.json receipts must be an array');
    assert.equal(indexData.receipts.length, 2, 'Expected 2 receipts in index.json');

    for (const entry of indexData.receipts) {
        assert.match(entry.receipt_id, /^[a-f0-9]{8}$/, 'receipt_id must be 8 hex characters');
        assert.equal(entry.acknowledged, true, 'acknowledged must be true');
        const rcptFile = path.join(projectDir, `${entry.receipt_id}.json`);
        assert.ok(fs.existsSync(rcptFile), `Receipt file ${entry.receipt_id}.json must exist`);
        const rcptData = JSON.parse(fs.readFileSync(rcptFile, 'utf8'));
        assert.equal(rcptData.receipt_id, entry.receipt_id);
        assert.equal(rcptData.acknowledged, true);
        assert.match(rcptData.content_digest, /^[a-f0-9]{64}$/, 'content_digest must be 64-char sha256 hex');
        assert.equal(rcptData.integrity, 'ok');
    }
});
