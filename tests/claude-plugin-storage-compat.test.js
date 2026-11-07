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
const crypto = require('node:crypto');
const { spawn, spawnSync } = require('node:child_process');
const pkg = require('../package.json');
const { copyDir, writeInstalledVersionMarker } = require('../bin/delimit-setup');
const { pyJsonDumps, ensureDir0700, writeFile0600, appendFile0600 } = require('../lib/claude-plugin-server.cjs');

const REPO_ROOT = path.resolve(__dirname, '..');
const CLI_PATH = path.join(REPO_ROOT, 'bin', 'delimit-cli.js');
const BUNDLED_PATH = path.join(REPO_ROOT, 'claude-plugin', 'server.cjs');

function getFastmcpUnavailableReason() {
    try {
        const res = spawnSync('python3', ['-c', 'import fastmcp'], { encoding: 'utf8' });
        if (res.status !== 0) {
            return 'Python server dependencies unavailable (fastmcp not installed)';
        }
        return false;
    } catch (err) {
        return `Python server dependencies unavailable (${err.message})`;
    }
}
const FASTMCP_UNAVAILABLE = getFastmcpUnavailableReason();

function setupTempHome(t) {
    const tempHome = fs.mkdtempSync(path.join('/tmp', 'delimit-compat-test-'));
    t.after(() => {
        try {
            fs.rmSync(tempHome, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
        } catch {}
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

async function startServer(cmd, args, env, t) {
    const proc = spawn(cmd, args, { env, stdio: ['pipe', 'pipe', 'inherit'] });
    if (t && typeof t.after === 'function') {
        t.after(() => {
            try { proc.kill(); } catch {}
        });
    }
    await runRpc(proc, 1, 'initialize', {
        protocolVersion: '2024-11-05',
        capabilities: {},
        clientInfo: { name: 'test-compat', version: '1.0' },
    });
    proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized', params: {} }) + '\n');
    return proc;
}

test('tools/list schema parity between CLI records server and bundled server', {
    skip: FASTMCP_UNAVAILABLE || false,
}, async (t) => {
    if (FASTMCP_UNAVAILABLE) {
        t.skip(FASTMCP_UNAVAILABLE);
        return;
    }
    const { env } = setupTempHome(t);

    const cliProc = await startServer(process.execPath, [CLI_PATH, 'mcp', '--toolset', 'records'], env, t);
    const cliRes = await runRpc(cliProc, 2, 'tools/list', {});
    cliProc.kill();

    const bundledProc = await startServer(process.execPath, [BUNDLED_PATH], env, t);
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

test('bidirectional storage compatibility: CLI writes, bundled reads, then bundled writes, CLI reads', {
    skip: FASTMCP_UNAVAILABLE || false,
}, async (t) => {
    if (FASTMCP_UNAVAILABLE) {
        t.skip(FASTMCP_UNAVAILABLE);
        return;
    }
    const { tempHome, env } = setupTempHome(t);

    const cliTitle = 'CLI Task LED-001: Don\u010di\u0107 caf\u00e9 na\u00efve';
    const cliDesc = 'CLI Created Item with CJK: \u65e5\u672c\u8a9e and emoji: \ud83d\ude80 and controls: \u0001\u001f\u007f\t';
    const cliUpdateNote = 'CLI updated note with \u0161tra\u00dfe, CJK: \u7e41\u9ad4\u4e2d\u6587, emoji: \ud83c\udf89, controls: \u0002\u007f';
    const cliDoneNote = 'CLI marked done with \u010d\u0107\u017e\u0161\u0111, emoji: \ud83d\udc68\u200d\ud83d\udc69\u200d\ud83d\udc67\u200d\ud83d\udc66, controls: \u0003\t';

    // --- Phase 1: CLI writes ---
    const cliProc1 = await startServer(process.execPath, [CLI_PATH, 'mcp', '--toolset', 'records'], env, t);

    const cliAdd = await runRpc(cliProc1, 2, 'tools/call', {
        name: 'delimit_ledger_add',
        arguments: { title: cliTitle, priority: 'P1', description: cliDesc },
    });
    assert.ok(cliAdd && cliAdd.content, 'CLI add returned response');

    await runRpc(cliProc1, 3, 'tools/call', {
        name: 'delimit_ledger_update',
        arguments: { item_id: 'LED-001', note: cliUpdateNote, priority: 'P0' },
    });

    await runRpc(cliProc1, 4, 'tools/call', {
        name: 'delimit_ledger_done',
        arguments: { item_id: 'LED-001', note: cliDoneNote },
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
    const bundledProc = await startServer(process.execPath, [BUNDLED_PATH], env, t);

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
    assert.equal(bItem1.title, cliTitle, 'Bundled must preserve accented Latin title from CLI');
    assert.equal(bItem1.description, cliDesc, 'Bundled must preserve CJK, emoji, and control chars in description from CLI');
    assert.equal(bItem1.last_note, cliDoneNote, 'Bundled must preserve astral emoji and control chars in note from CLI');

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
    const bTitle = 'Bundled Task LED-002: \u0141\u00f3d\u017a & Don\u010di\u0107';
    const bDesc = 'Bundled Created Item with CJK: \ud55c\uad6d\uc5b4 \u4e2d\u6587 and emoji: \ud83c\udf1f \ud83d\udd25 and controls: \u0004\u001e\u007f\t';
    const bUpdateNote = 'Bundled updated note with \u00e0\u00e8\u00e9\u00ec\u00f2\u00f9, CJK: \u65e5\u672c\u8a9e, emoji: \ud83d\udef8, controls: \u0005\u007f';
    const bDoneNote = 'Bundled marked done with \u00fc\u00f6\u00e4, emoji: \ud83c\udfc6, controls: \u0006\t';

    const bAdd = await runRpc(bundledProc, 10, 'tools/call', {
        name: 'delimit_ledger_add',
        arguments: { title: bTitle, priority: 'P1', description: bDesc },
    });
    const bAddData = JSON.parse(bAdd.content[0].text);
    assert.equal(bAddData.added.id, 'LED-002');

    await runRpc(bundledProc, 11, 'tools/call', {
        name: 'delimit_ledger_update',
        arguments: { item_id: 'LED-002', note: bUpdateNote, priority: 'P0' },
    });

    await runRpc(bundledProc, 12, 'tools/call', {
        name: 'delimit_ledger_done',
        arguments: { item_id: 'LED-002', note: bDoneNote },
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
    const cliProc2 = await startServer(process.execPath, [CLI_PATH, 'mcp', '--toolset', 'records'], env, t);

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
    assert.equal(cliItem2.title, bTitle, 'CLI must preserve accented Latin title written by bundled');
    assert.equal(cliItem2.description, bDesc, 'CLI must preserve CJK, emoji, and control chars written by bundled');
    assert.equal(cliItem2.last_note, bDoneNote, 'CLI must preserve astral emoji and control chars written by bundled');

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

    // --- Phase 5: On-disk file validation & bidirectional hash-chain parity ---
    const opsFile = path.join(tempHome, '.delimit', 'ledger', 'operations.jsonl');
    assert.ok(fs.existsSync(opsFile), 'operations.jsonl must exist on disk under ~/.delimit/ledger');

    const opsLines = fs.readFileSync(opsFile, 'utf8').trim().split('\n').map((l) => JSON.parse(l));
    assert.equal(opsLines.length, 6, 'Expected 3 events from CLI + 3 events from Bundled = 6 lines');

    let prevHash = 'genesis';
    for (let i = 0; i < opsLines.length; i++) {
        const line = opsLines[i];
        assert.match(line.id, /^LED-\d{3}$/, 'id format must be LED-XXX');
        assert.match(line.hash, /^[a-f0-9]{16}$/, 'hash format must be 16-character hex');
        assert.match(line.created_at, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/, 'created_at must be ISO UTC without ms');
        if (line.type === 'update') {
            assert.match(line.updated_at, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/, 'updated_at must be ISO UTC without ms');
        }

        // Verify the hash chain using Node's pyJsonDumps canonical serializer
        const unsealed = { ...line };
        delete unsealed.hash;
        delete unsealed.created_at;
        const serialized = pyJsonDumps(unsealed);
        const expectedHash = crypto.createHash('sha256').update(prevHash + serialized).digest('hex').slice(0, 16);
        assert.equal(
            line.hash,
            expectedHash,
            `Hash chain broken at event index ${i} (${line.id}): stored=${line.hash} expected=${expectedHash} prevHash=${prevHash}`
        );
        prevHash = line.hash;
    }

    // Drive the real Python CLI environment to re-verify the identical hash chain using Python's json.dumps(sort_keys=True)
    const pythonVerifyScript = `
import json, hashlib, sys

with open(sys.argv[1], 'r', encoding='utf-8') as f:
    lines = [json.loads(line) for line in f if line.strip()]

assert len(lines) == 6, f"Expected 6 lines, got {len(lines)}"

prev_hash = "genesis"
for i, entry in enumerate(lines):
    stored_hash = entry["hash"]
    unsealed = {k: v for k, v in entry.items() if k not in ("hash", "created_at")}
    serialized = json.dumps(unsealed, sort_keys=True)
    computed_hash = hashlib.sha256(f"{prev_hash}{serialized}".encode()).hexdigest()[:16]
    if stored_hash != computed_hash:
        print(f"PYTHON_HASH_MISMATCH at index {i} id={entry.get('id')}: stored={stored_hash} computed={computed_hash}", file=sys.stderr)
        sys.exit(1)
    prev_hash = stored_hash

print("PYTHON_HASH_OK")
`;
    const pyRes = spawnSync('python3', ['-c', pythonVerifyScript, opsFile], { encoding: 'utf8' });
    assert.equal(pyRes.status, 0, `Python ledger verification failed: ${pyRes.stderr}`);
    assert.match(pyRes.stdout, /PYTHON_HASH_OK/, 'Python must verify all records in operations.jsonl');

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

test('canonical serializer pyJsonDumps byte-identity matches Python json.dumps fixtures', () => {
    const fixturePath = path.join(__dirname, 'fixtures', 'claude-plugin-canonical-serializer-vectors.json');
    assert.ok(fs.existsSync(fixturePath), 'canonical serializer fixture file must exist');
    const fixtures = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));

    // 1. Verify unit test vectors (accents, CJK, emoji, controls, escapes, nesting, primitives)
    for (const { input, expected } of fixtures.unit_vectors) {
        const actual = pyJsonDumps(input);
        assert.equal(actual, expected, `Mismatch in unit vector for input: ${JSON.stringify(input)}`);
    }

    // 2. Verify hash chain vectors byte-identity and SHA-256 seal integrity
    for (let i = 0; i < fixtures.chain_vectors.length; i++) {
        const { unsealed, serialized, prev_hash, expected_hash } = fixtures.chain_vectors[i];
        const actual = pyJsonDumps(unsealed);
        assert.equal(actual, serialized, `Serialized string mismatch at chain vector index ${i}`);
        const computedHash = crypto.createHash('sha256').update(prev_hash + actual).digest('hex').slice(0, 16);
        assert.equal(computedHash, expected_hash, `Hash mismatch at chain vector index ${i}`);
    }
});

test('permissions invariants under permissive umask: dirs 0700, files 0600 (new and rewritten), pre-existing 0644 preserved', async (t) => {
    const oldUmask = process.umask(0o022);
    t.after(() => {
        try {
            process.umask(oldUmask);
        } catch {}
    });

    const testDir = fs.mkdtempSync(path.join('/tmp', 'delimit-perm-unit-test-'));
    t.after(() => {
        try {
            fs.rmSync(testDir, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
        } catch {}
    });

    // 1. Newly created directories get mode 0700
    const nestedDir = path.join(testDir, 'created_parent', 'created_child');
    ensureDir0700(nestedDir);
    if (process.platform !== 'win32') {
        const parentMode = fs.statSync(path.join(testDir, 'created_parent')).mode & 0o777;
        const childMode = fs.statSync(nestedDir).mode & 0o777;
        assert.equal(parentMode, 0o700, `expected parent dir mode 0700, got 0o${parentMode.toString(8)}`);
        assert.equal(childMode, 0o700, `expected child dir mode 0700, got 0o${childMode.toString(8)}`);
    }

    // 2. Pre-existing 0755 directory is not chmoded (backward compatibility)
    const preDir = path.join(testDir, 'pre_existing_dir_0755');
    fs.mkdirSync(preDir, { mode: 0o755 });
    if (process.platform !== 'win32') {
        fs.chmodSync(preDir, 0o755);
        assert.equal(fs.statSync(preDir).mode & 0o777, 0o755);
    }
    ensureDir0700(preDir);
    if (process.platform !== 'win32') {
        assert.equal(fs.statSync(preDir).mode & 0o777, 0o755, 'pre-existing dir mode must be preserved');
    }

    // 3. Newly created files get mode 0600
    const newFile = path.join(nestedDir, 'new_file.json');
    writeFile0600(newFile, '{"created": true}\n');
    if (process.platform !== 'win32') {
        const fileMode = fs.statSync(newFile).mode & 0o777;
        assert.equal(fileMode, 0o600, `expected new file mode 0600, got 0o${fileMode.toString(8)}`);
    }

    // 4. Atomic rewrite of newly created file preserves 0600
    writeFile0600(newFile, '{"rewritten": true}\n');
    if (process.platform !== 'win32') {
        const rewrittenMode = fs.statSync(newFile).mode & 0o777;
        assert.equal(rewrittenMode, 0o600, `expected rewritten file mode 0600, got 0o${rewrittenMode.toString(8)}`);
    }

    // 5. Pre-existing 0644 file keeps its mode on rewrite (backward compatibility)
    const preFile = path.join(testDir, 'pre_existing_file_0644.json');
    fs.writeFileSync(preFile, '{"initial": 1}\n', { mode: 0o644 });
    if (process.platform !== 'win32') {
        fs.chmodSync(preFile, 0o644);
        assert.equal(fs.statSync(preFile).mode & 0o777, 0o644);
    }
    writeFile0600(preFile, '{"updated": 2}\n');
    if (process.platform !== 'win32') {
        const keptMode = fs.statSync(preFile).mode & 0o777;
        assert.equal(keptMode, 0o644, `expected pre-existing 0644 file to keep mode 0644, got 0o${keptMode.toString(8)}`);
    }

    // 6. Newly created append file gets mode 0600, and subsequent append keeps 0600
    const newAppendFile = path.join(nestedDir, 'append.jsonl');
    appendFile0600(newAppendFile, '{"line": 1}\n');
    if (process.platform !== 'win32') {
        const appendMode1 = fs.statSync(newAppendFile).mode & 0o777;
        assert.equal(appendMode1, 0o600, `expected initial append file mode 0600, got 0o${appendMode1.toString(8)}`);
    }
    appendFile0600(newAppendFile, '{"line": 2}\n');
    if (process.platform !== 'win32') {
        const appendMode2 = fs.statSync(newAppendFile).mode & 0o777;
        assert.equal(appendMode2, 0o600, `expected subsequent append file mode 0600, got 0o${appendMode2.toString(8)}`);
    }

    // 7. Pre-existing 0644 append file keeps its mode on append
    const preAppendFile = path.join(testDir, 'pre_existing_append_0644.jsonl');
    fs.writeFileSync(preAppendFile, '{"line": 0}\n', { mode: 0o644 });
    if (process.platform !== 'win32') {
        fs.chmodSync(preAppendFile, 0o644);
        assert.equal(fs.statSync(preAppendFile).mode & 0o777, 0o644);
    }
    appendFile0600(preAppendFile, '{"line": 1}\n');
    if (process.platform !== 'win32') {
        const keptAppendMode = fs.statSync(preAppendFile).mode & 0o777;
        assert.equal(keptAppendMode, 0o644, `expected pre-existing 0644 append file to keep mode 0644, got 0o${keptAppendMode.toString(8)}`);
    }
});

test('bundled MCP server under permissive umask creates 0700 dirs, 0600 files (rewrites preserve 0600)', async (t) => {
    const oldUmask = process.umask(0o022);
    t.after(() => {
        try {
            process.umask(oldUmask);
        } catch {}
    });

    const tempHome = fs.mkdtempSync(path.join('/tmp', 'delimit-bundled-server-mode-test-'));
    t.after(() => {
        try {
            fs.rmSync(tempHome, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
        } catch {}
    });

    const env = {
        ...process.env,
        HOME: tempHome,
        DELIMIT_HOME: path.join(tempHome, '.delimit'),
        DELIMIT_NON_INTERACTIVE: '1',
    };

    const proc = await startServer(process.execPath, [BUNDLED_PATH], env, t);

    // 1. Create ledger item: triggers ledgerDir (0700) and operations.jsonl (0600)
    const addRes = await runRpc(proc, 2, 'tools/call', {
        name: 'delimit_ledger_add',
        arguments: { title: 'Umask test task', priority: 'P1', description: 'Testing mode creation' },
    });
    assert.ok(addRes && addRes.content, 'Ledger add response returned');

    // 2. Update ledger item: appends to operations.jsonl (must remain 0600)
    await runRpc(proc, 3, 'tools/call', {
        name: 'delimit_ledger_update',
        arguments: { item_id: 'LED-001', note: 'Update note', priority: 'P0' },
    });

    // 3. Create handoff receipt: creates handoff dir (0700), receipt.json (0600), index.json (0600)
    const createRes = await runRpc(proc, 4, 'tools/call', {
        name: 'delimit_handoff_create',
        arguments: { task_description: 'Umask Handoff Task', next_action: 'Ack receipt' },
    });
    const receiptData = JSON.parse(createRes.content[0].text);
    const receiptId = receiptData.receipt_id;
    assert.match(receiptId, /^[a-f0-9]{8}$/);

    // 4. Acknowledge receipt: rewrites receipt file AND index.json (must remain 0600)
    await runRpc(proc, 5, 'tools/call', {
        name: 'delimit_handoff_acknowledge',
        arguments: { receipt_id: receiptId, notes: 'Acknowledged under umask' },
    });

    proc.kill();

    if (process.platform !== 'win32') {
        const ledgerDir = path.join(tempHome, '.delimit', 'ledger');
        const opsFile = path.join(ledgerDir, 'operations.jsonl');
        assert.equal(fs.statSync(ledgerDir).mode & 0o777, 0o700, 'ledger dir created by bundled server must be 0700');
        assert.equal(fs.statSync(opsFile).mode & 0o777, 0o600, 'operations.jsonl created by bundled server must be 0600');

        const handoffBase = path.join(tempHome, '.delimit', 'handoff_receipts');
        assert.equal(fs.statSync(handoffBase).mode & 0o777, 0o700, 'handoff_receipts base dir must be 0700');

        const hashDirs = fs.readdirSync(handoffBase);
        assert.equal(hashDirs.length, 1);
        const projDir = path.join(handoffBase, hashDirs[0]);
        assert.equal(fs.statSync(projDir).mode & 0o777, 0o700, 'project handoff dir must be 0700');

        const indexJson = path.join(projDir, 'index.json');
        const rcptJson = path.join(projDir, `${receiptId}.json`);
        assert.equal(fs.statSync(indexJson).mode & 0o777, 0o600, 'index.json must be 0600 after rewrite');
        assert.equal(fs.statSync(rcptJson).mode & 0o777, 0o600, 'receipt file must be 0600 after rewrite');
    }
});

test('bundled MCP server preserves pre-existing 0755 dirs and 0644 files under permissive umask', async (t) => {
    const oldUmask = process.umask(0o022);
    t.after(() => {
        try {
            process.umask(oldUmask);
        } catch {}
    });

    const tempHome = fs.mkdtempSync(path.join('/tmp', 'delimit-bundled-server-preserve-test-'));
    t.after(() => {
        try {
            fs.rmSync(tempHome, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
        } catch {}
    });

    const env = {
        ...process.env,
        HOME: tempHome,
        DELIMIT_HOME: path.join(tempHome, '.delimit'),
        DELIMIT_NON_INTERACTIVE: '1',
    };

    // Pre-seed a pre-existing 0755 ledger dir and pre-existing 0644 operations.jsonl
    const preLedgerDir = path.join(tempHome, '.delimit', 'ledger');
    fs.mkdirSync(preLedgerDir, { recursive: true, mode: 0o755 });
    if (process.platform !== 'win32') fs.chmodSync(preLedgerDir, 0o755);

    const preOpsFile = path.join(preLedgerDir, 'operations.jsonl');
    fs.writeFileSync(preOpsFile, '', { mode: 0o644 });
    if (process.platform !== 'win32') fs.chmodSync(preOpsFile, 0o644);

    const proc = await startServer(process.execPath, [BUNDLED_PATH], env, t);

    // Add item (appends to pre-existing 0644 operations.jsonl)
    await runRpc(proc, 2, 'tools/call', {
        name: 'delimit_ledger_add',
        arguments: { title: 'Pre-existing mode test', priority: 'P1' },
    });

    proc.kill();

    if (process.platform !== 'win32') {
        assert.equal(fs.statSync(preLedgerDir).mode & 0o777, 0o755, 'pre-existing ledger dir must retain 0755');
        assert.equal(fs.statSync(preOpsFile).mode & 0o777, 0o644, 'pre-existing operations.jsonl must retain 0644');
    }
});
