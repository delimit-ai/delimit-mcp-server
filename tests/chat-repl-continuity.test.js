'use strict';

const { test, describe, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { DelimitChatREPL } = require('../lib/chat-repl');

describe('LED-4057 delimit chat project-bound continuity', () => {
    let backendRoot;

    beforeEach(() => {
        backendRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-phoenix-'));
        fs.mkdirSync(path.join(backendRoot, 'ai'), { recursive: true });
        fs.writeFileSync(path.join(backendRoot, 'ai', 'session_phoenix.py'), '# fixture\n');
    });

    afterEach(() => {
        fs.rmSync(backendRoot, { recursive: true, force: true });
    });

    function replReturning(payload, options = {}) {
        const calls = [];
        const repl = new DelimitChatREPL({
            chatRunId: 'run-led-4057',
            sessionPhoenixRoot: backendRoot,
            continuityHome: backendRoot,
            spawnSync: (command, args, spawnOptions) => {
                calls.push({ command, args, spawnOptions });
                return {
                    status: 0,
                    stdout: JSON.stringify(payload) + '\n',
                    stderr: '',
                };
            },
            ...options,
        });
        return { repl, calls };
    }

    test('one stable chatRunId is reused by capture, revive, and child env', () => {
        const { repl, calls } = replReturning({ status: 'captured', soul_id: 's1' });

        const capture = repl.captureSoulForMigration(
            'claude',
            'codex',
            { trigger: 'launcher-failure', reason: 'exit-1' },
        );
        assert.strictEqual(capture.status, 'captured');

        const sent = JSON.parse(calls[0].args[2]);
        assert.strictEqual(sent.chat_run_id, 'run-led-4057');
        assert.strictEqual(sent.source_model, 'claude');
        assert.strictEqual(sent.to_model, 'codex');
        assert.strictEqual(sent.trigger, 'launcher-failure');
        assert.strictEqual(sent.reason, 'exit-1');
        assert.strictEqual(calls[0].spawnOptions.env.DELIMIT_CHAT_RUN_ID, 'run-led-4057');

        repl.reviveSoulForLaunch();
        const revivePayload = JSON.parse(calls[1].args[2]);
        assert.strictEqual(revivePayload.chat_run_id, 'run-led-4057');
        assert.strictEqual(revivePayload.allow_global_fallback, false);
        assert.strictEqual(repl.chatRunId, 'run-led-4057');
    });

    test('capture success is based on structured backend status, not process exit', () => {
        for (const status of ['captured', 'updated', 'deduplicated', 'finalized', 'already_captured', 'noop']) {
            const { repl } = replReturning({ status, soul_id: 'exact-soul' });
            assert.strictEqual(repl.captureSucceeded(repl.captureSoulForMigration('claude')), true);
            assert.strictEqual(repl.captureSucceeded({ status }), false, 'bare status is not durable evidence');
        }

        for (const status of ['ambiguous', 'blocked_ambiguity', 'unavailable', 'error', 'skipped']) {
            const { repl } = replReturning({ status });
            assert.strictEqual(repl.captureSucceeded(repl.captureSoulForMigration('claude')), false);
        }
    });

    test('revive returns ambiguity without converting it into injectable context', () => {
        const { repl } = replReturning({
            status: 'ambiguous',
            candidates: ['delimit-mcp', 'wire-report'],
            context: 'must not inject',
        });
        const result = repl.reviveSoulForLaunch();
        assert.strictEqual(result.status, 'ambiguous');
        assert.deepStrictEqual(result.candidates, ['delimit-mcp', 'wire-report']);
        assert.strictEqual(repl.captureSucceeded(result), false);
        assert.strictEqual(
            repl.formatContinuityCandidates([
                { venture: 'delimit-mcp', project_path: '/home/delimit/delimit-gateway' },
                { name: 'Wire Report', project_path: '/home/jamsons/ventures/wire-report' },
            ]),
            'delimit-mcp, Wire Report',
        );
    });

    test('a Codex revival is acknowledged only after confirmed delivery', () => {
        let acknowledged = false;
        const calls = [];
        const repl = new DelimitChatREPL({
            chatRunId: 'run-led-4057',
            sessionPhoenixRoot: backendRoot,
            spawnSync: (command, args) => {
                const functionName = args[1].match(
                    /getattr\(backend, "([^"]+)", None\)/,
                )?.[1];
                calls.push(functionName);
                if (functionName === 'revive') {
                    return {
                        status: 0,
                        stdout: JSON.stringify(
                            acknowledged
                                ? { status: 'no_active_project' }
                                : {
                                    status: 'revived',
                                    context: 'EXACT CODEX CONTEXT',
                                    delivery_ack: {
                                        capture_key: 'capture-1',
                                        write_id: 'generation-1',
                                        transcript_path: '/portfolio/session.jsonl',
                                        logical_session_id: 'logical-1',
                                        launcher_run_id: 'run-led-4057',
                                    },
                                },
                        ) + '\n',
                    };
                }
                if (functionName === 'acknowledge_revival') {
                    acknowledged = true;
                    return {
                        status: 0,
                        stdout: '{"status":"acknowledged"}\n',
                    };
                }
                throw new Error(`unexpected backend function: ${functionName}`);
            },
        });

        const delivered = repl.reviveSoulForLaunch();
        assert.strictEqual(delivered.status, 'revived');
        assert.strictEqual(
            repl.acknowledgeCodexRevival(delivered, { status: 0 }).status,
            'acknowledged',
        );
        assert.strictEqual(
            repl.reviveSoulForLaunch().status,
            'no_active_project',
            'an acknowledged generation is not reinjected',
        );
        assert.deepStrictEqual(calls, [
            'revive',
            'acknowledge_revival',
            'revive',
        ]);
    });

    test('a failed or aborted Codex delivery remains retryable', () => {
        for (const childResult of [
            { status: 1 },
            { status: null, signal: 'SIGINT' },
            { status: null, error: new Error('spawn failed') },
        ]) {
            let acknowledgeCalls = 0;
            const repl = new DelimitChatREPL({
                chatRunId: 'run-led-4057',
                sessionPhoenixRoot: backendRoot,
                spawnSync: (command, args) => {
                    const functionName = args[1].match(
                        /getattr\(backend, "([^"]+)", None\)/,
                    )?.[1];
                    if (functionName === 'acknowledge_revival') {
                        acknowledgeCalls += 1;
                    }
                    return {
                        status: 0,
                        stdout: JSON.stringify({
                            status: 'revived',
                            context: 'RETRYABLE CODEX CONTEXT',
                            delivery_ack: {
                                capture_key: 'capture-2',
                                write_id: 'generation-2',
                            },
                        }) + '\n',
                    };
                },
            });

            const first = repl.reviveSoulForLaunch();
            assert.strictEqual(
                repl.acknowledgeCodexRevival(first, childResult).status,
                'retryable',
            );
            assert.strictEqual(acknowledgeCalls, 0);
            assert.strictEqual(
                repl.reviveSoulForLaunch().status,
                'revived',
                'an unacknowledged generation stays eligible for retry',
            );
        }
    });

    test('backend absence and malformed output fail closed', () => {
        const missing = new DelimitChatREPL({
            chatRunId: 'run-led-4057',
            sessionPhoenixRoot: path.join(backendRoot, 'missing'),
            spawnSync: () => { throw new Error('must not spawn'); },
        });
        assert.deepStrictEqual(
            missing.captureSoulForMigration('claude'),
            { status: 'unavailable', reason: 'backend_missing' },
        );

        const malformed = replReturning({ status: 'captured' });
        malformed.repl._spawnSync = () => ({ status: 0, stdout: 'not-json\n', stderr: '' });
        assert.deepStrictEqual(
            malformed.repl.captureSoulForMigration('claude'),
            { status: 'unavailable', reason: 'backend_call_failed' },
        );
    });

    test('namespace-only installs resolve Session Phoenix from that namespace', () => {
        const savedHome = process.env.DELIMIT_HOME;
        const savedNamespace = process.env.DELIMIT_NAMESPACE_ROOT;
        const namespacedServer = path.join(backendRoot, 'server', 'ai');
        fs.mkdirSync(namespacedServer, { recursive: true });
        fs.writeFileSync(
            path.join(namespacedServer, 'session_phoenix.py'),
            'CONTINUITY_PROTOCOL_VERSION = 2\n',
        );
        try {
            delete process.env.DELIMIT_HOME;
            process.env.DELIMIT_NAMESPACE_ROOT = backendRoot;
            const repl = new DelimitChatREPL({
                chatRunId: 'namespace-run',
                spawnSync: () => ({
                    status: 0,
                    stdout: '{"status":"noop"}\n',
                    stderr: '',
                }),
            });
            assert.strictEqual(
                repl.sessionPhoenixRoot,
                path.join(backendRoot, 'server'),
            );
        } finally {
            if (savedHome === undefined) delete process.env.DELIMIT_HOME;
            else process.env.DELIMIT_HOME = savedHome;
            if (savedNamespace === undefined) delete process.env.DELIMIT_NAMESPACE_ROOT;
            else process.env.DELIMIT_NAMESPACE_ROOT = savedNamespace;
        }
    });

    test('clean-exit request carries finalize intent but no guessed transcript', () => {
        const { repl, calls } = replReturning({ status: 'unavailable', reason: 'no_bound_session' });
        const result = repl.captureSoulForMigration(
            'codex',
            '',
            { trigger: 'launcher-clean-exit', reason: 'clean-exit', finalize: true },
        );
        const payload = JSON.parse(calls[0].args[2]);
        assert.strictEqual(payload.finalize, true);
        assert.strictEqual(payload.trigger, 'launcher-clean-exit');
        assert.strictEqual(payload.transcript_path, '');
        assert.strictEqual(repl.captureSucceeded(result), false);
    });

    test('compiled backend layout is not rejected for lacking Python source', () => {
        fs.unlinkSync(path.join(backendRoot, 'ai', 'session_phoenix.py'));
        fs.writeFileSync(path.join(backendRoot, 'ai', 'session_phoenix.so'), 'fixture');
        const { repl, calls } = replReturning({ status: 'finalized', soul_id: 'compiled-soul' });
        assert.strictEqual(repl.finalizeSession('codex').saved, true);
        assert.strictEqual(calls.length, 1);
    });

    test('uses the installed backend interpreter when executable', () => {
        const python = path.join(backendRoot, 'venv', 'bin', 'python');
        fs.mkdirSync(path.dirname(python), { recursive: true });
        fs.writeFileSync(python, '#!/bin/sh\nexit 0\n', { mode: 0o700 });
        const { repl, calls } = replReturning({ status: 'finalized', soul_id: 's1' });
        repl.finalizeSession('claude');
        assert.strictEqual(calls[0].command, python);
    });

    test('unbound exit records honest recovery metadata without replacing saved context', () => {
        const sessions = path.join(backendRoot, 'sessions');
        fs.mkdirSync(sessions);
        const prior = path.join(sessions, 'existing-rich-handoff.json');
        fs.writeFileSync(prior, '{"summary":"rich context must survive"}');
        const { repl, calls } = replReturning({ status: 'not_found', reason: 'no_event_bound_floor' });
        const result = repl.finalizeSession('codex');
        assert.strictEqual(result.saved, false);
        const receipt = JSON.parse(fs.readFileSync(result.receipt, 'utf8'));
        assert.strictEqual(receipt.context_captured, false);
        assert.strictEqual(receipt.chat_run_id, 'run-led-4057');
        assert.strictEqual(receipt.reason, 'no_event_bound_floor');
        assert.strictEqual(fs.statSync(result.receipt).mode & 0o777, 0o600);
        assert.strictEqual(fs.readFileSync(prior, 'utf8'), '{"summary":"rich context must survive"}');
        assert.strictEqual(calls.length, 1, 'never retries with a guessed project or empty soul');
        const payload = JSON.parse(calls[0].args[2]);
        assert.strictEqual(payload.project_path, '');
        assert.strictEqual(payload.transcript_path, '');
        assert.match(repl.formatSessionExit(result), /no_event_bound_floor/);
        assert.doesNotMatch(repl.formatSessionExit(result), /Session saved/);
    });

    test('backend errors and unsafe receipt paths remain honest, without leaking raw errors', () => {
        const other = path.join(backendRoot, 'other');
        fs.mkdirSync(other, { mode: 0o700 });
        fs.chmodSync(other, 0o700);
        fs.symlinkSync(other, path.join(backendRoot, 'sessions'));
        const { repl } = replReturning({ status: 'error', error: 'SECRET private exception text' });
        const result = repl.finalizeSession('claude');
        assert.strictEqual(result.receipt_error, undefined);
        assert.strictEqual(result.reason, 'backend_error');
        assert.strictEqual(fs.readdirSync(other).length, 1);
        assert.doesNotMatch(repl.formatSessionExit(result), /SECRET/);
    });

    test('a symlinked sessions directory whose target is group/world-writable is refused', () => {
        const shared = path.join(backendRoot, 'shared');
        fs.mkdirSync(shared);
        fs.chmodSync(shared, 0o777);
        fs.symlinkSync(shared, path.join(backendRoot, 'sessions'));
        const { repl } = replReturning({ status: 'error', error: 'SECRET private exception text' });
        const result = repl.finalizeSession('claude');
        assert.strictEqual(result.receipt_error, 'recovery_receipt_unavailable');
        assert.strictEqual(result.receipt, undefined);
        assert.deepStrictEqual(fs.readdirSync(shared), []);
        assert.doesNotMatch(repl.formatSessionExit(result), /SECRET/);
    });

    test('real Python bridge finalizes synthetic evidence without inference', () => {
        fs.writeFileSync(path.join(backendRoot, 'ai', '__init__.py'), '');
        fs.writeFileSync(path.join(backendRoot, 'ai', 'session_phoenix.py'), [
            'CONTINUITY_PROTOCOL_VERSION = 2',
            'def capture_floor_from_transcript(chat_run_id="", finalize=False):',
            '    assert chat_run_id == "synthetic-run" and finalize',
            '    return {"status": "finalized", "soul_id": "synthetic-only"}',
        ].join('\n'));
        const repl = new DelimitChatREPL({
            sessionPhoenixRoot: backendRoot, continuityHome: backendRoot, chatRunId: 'synthetic-run',
        });
        assert.strictEqual(repl.finalizeSession('codex').saved, true);
        assert.strictEqual(fs.existsSync(path.join(backendRoot, 'sessions')), false);
    });
});
