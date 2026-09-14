const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const { DelimitChatREPL } = require('../lib/chat-repl');

const fixture = {
    claude: { auth_mode: 'chat_login' },
    codex: { auth_mode: 'chat_login' },
    grok: { api_key: 'synthetic-unused' },
    fallbacks: { default: ['claude', 'codex'] },
};

function makeRepl(options) {
    // No account/configuration reads while testing the launcher contract.
    const oldModels = DelimitChatREPL.prototype.loadModels;
    const oldRoutes = DelimitChatREPL.prototype.loadRoutes;
    DelimitChatREPL.prototype.loadModels = () => structuredClone(fixture);
    DelimitChatREPL.prototype.loadRoutes = () => ({});
    try {
        return new DelimitChatREPL(options);
    } finally {
        DelimitChatREPL.prototype.loadModels = oldModels;
        DelimitChatREPL.prototype.loadRoutes = oldRoutes;
    }
}

function startWithoutProcessExit(repl) {
    const oldCode = process.exitCode;
    const oldError = console.error;
    const errors = [];
    console.error = (...args) => errors.push(args.join(' '));
    try {
        const returned = repl.start();
        return { returned, exitCode: process.exitCode, errors };
    } finally {
        process.exitCode = oldCode;
        console.error = oldError;
    }
}

describe('explicit Copilot/Muse chat harnesses', () => {
    for (const model of ['copilot', 'muse']) {
        it(`${model} needs no models registry entry and has no fallback chain`, () => {
            const repl = makeRepl({ model });
            const before = JSON.stringify(repl.modelsConfig);
            assert.deepEqual(repl.getActiveChain(), [{ id: model, type: 'harness' }]);
            assert.equal(JSON.stringify(repl.modelsConfig), before);
            assert.equal(repl.modelsConfig[model], undefined);
        });

        for (const status of [0, 1, 127, 130, 143]) {
            it(`${model} returns native exit ${status} without model fallback or context probes`, () => {
                const calls = [];
                const repl = makeRepl({ model, chatRunId: 'synthetic-chat-run',
                    launchExplicitHarness: (id, options) => { calls.push([id, options]); return status; },
                });
                for (const method of ['getActiveChain', 'probeModelHealth', 'printLaunchPrebrief',
                    'reviveSoulForLaunch', 'captureSoulForMigration', 'preflightHandoff']) {
                    repl[method] = () => { throw new Error(`legacy ${method} must not run`); };
                }
                const result = startWithoutProcessExit(repl);
                assert.equal(result.returned, status);
                assert.equal(result.exitCode, status);
                assert.equal(result.errors.length, 0);
                assert.equal(calls.length, 1);
                assert.equal(calls[0][0], model);
                assert.deepEqual(calls[0][1].args, []); // no Claude -p probe or fabricated model flag
                assert.equal(calls[0][1].cwd, process.cwd());
                assert.equal(calls[0][1].chatRunId, 'synthetic-chat-run');
                assert.deepEqual(repl.modelsConfig.fallbacks.default, ['claude', 'codex']);
            });
        }

        it(`${model} thrown launch failure stays local and does not expose raw error`, () => {
            let calls = 0;
            const repl = makeRepl({ model, apiFallback: true,
                launchExplicitHarness: () => { calls += 1; throw new Error('secret-runtime-detail'); },
            });
            const result = startWithoutProcessExit(repl);
            assert.equal(calls, 1);
            assert.equal(result.exitCode, 1);
            assert.match(result.errors.join(' '), /no fallback/);
            assert.doesNotMatch(result.errors.join(' '), /secret-runtime-detail/);
        });
    }

    it('invalid launcher results do not become a successful exit', () => {
        for (const code of [undefined, null, -1, 256, '0', { status: 0 }]) {
            const repl = makeRepl({ model: 'muse', launchExplicitHarness: () => code });
            assert.equal(startWithoutProcessExit(repl).exitCode, 1);
        }
    });

    it('the existing default and explicit Codex chain remain unchanged', () => {
        assert.deepEqual(makeRepl({}).getActiveChain().map(row => row.id), ['claude', 'codex']);
        assert.deepEqual(makeRepl({ model: 'codex' }).getActiveChain().map(row => row.id), ['codex', 'claude']);
    });

    it('setup uses the dedicated shim writer, not the legacy tool template', () => {
        const source = fs.readFileSync(path.join(__dirname, '..', 'bin', 'delimit-setup.js'), 'utf8');
        assert.match(source, /require\('\.\.\/lib\/harness-launch'\)\.installHarnessShims\(/);
        assert.doesNotMatch(source, /\['(?:muse|copilot)',/);
    });
});
