const { describe, it } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');
const { spawnSync } = require('child_process');

/**
 * LED-4391 finding 1: `delimit deliberate` never deliberated. It looked for
 * a `deliberation.py` SOURCE file (customers only ever get the compiled
 * `deliberation.cpython-*.so` from the core-engine tarball) and imported a
 * `run_deliberation` function that does not exist, so every customer run
 * fell through to "use the MCP tool" + pending.json.
 *
 * These tests pin the new contract: the command resolves the installed
 * engine as a MODULE, invokes `ai.deliberation.deliberate`, surfaces the
 * hosted-tier sign-in requirement, and only parks the question when it
 * genuinely could not run.
 */

const CLI = fs.readFileSync(path.join(__dirname, '..', 'bin', 'delimit-cli.js'), 'utf-8');

describe('delimit deliberate: engine resolution and invocation (LED-4391 #1)', () => {
    it('no longer depends on a deliberation.py source file or run_deliberation', () => {
        const start = CLI.indexOf(".command('deliberate [question...]')");
        assert.ok(start > 0, 'deliberate command not found');
        const handler = CLI.slice(start, CLI.indexOf(".command('", start + 10));
        assert.doesNotMatch(handler, /deliberation\.py/, 'must not look for the proprietary SOURCE file');
        assert.doesNotMatch(handler, /run_deliberation/, 'that function does not exist in the engine');
        assert.match(handler, /resolveDeliberationEngine\(\)/);
        assert.match(handler, /runEngineDeliberation\(/);
    });

    it('invokes ai.deliberation.deliberate with the question, mode and max_rounds, emitting JSON', () => {
        const start = CLI.indexOf('function runEngineDeliberation(');
        assert.ok(start > 0, 'runEngineDeliberation helper missing');
        const body = CLI.slice(start, start + 4000);
        assert.match(body, /from ai\.deliberation import deliberate/);
        assert.match(body, /deliberate\(\s*question=/);
        assert.match(body, /mode=/);
        assert.match(body, /max_rounds=/);
        assert.match(body, /json\.dumps/);
    });

    it('resolves the engine from the installed compiled module OR a source file, using the setup venv python when present', () => {
        const start = CLI.indexOf('function resolveDeliberationEngine(');
        assert.ok(start > 0, 'resolveDeliberationEngine helper missing');
        const body = CLI.slice(start, start + 2500);
        assert.match(body, /deliberation\\?\.cpython-/, 'compiled engine module must be recognised');
        assert.match(body, /venv/, 'must prefer the venv python that `delimit setup` created');
    });

    it('renders the hosted sign-in requirement instead of a generic failure', () => {
        const start = CLI.indexOf('function renderDeliberationOutcome(');
        assert.ok(start > 0, 'renderDeliberationOutcome helper missing');
        const body = CLI.slice(start, start + 3000);
        assert.match(body, /oauth_required/);
        assert.match(body, /delimit signin/);
        assert.match(body, /final_verdict/);
    });

    it('end-to-end against a stub engine: runs, parses JSON, and reports the verdict', () => {
        // Build a fake ~/.delimit with a python "engine" whose deliberate()
        // returns a canned transcript, then drive the real CLI against it.
        const home = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-delib-'));
        const serverAi = path.join(home, '.delimit', 'server', 'ai');
        fs.mkdirSync(serverAi, { recursive: true });
        fs.writeFileSync(path.join(serverAi, '__init__.py'), '');
        fs.writeFileSync(path.join(serverAi, 'deliberation.py'), [
            'def deliberate(question, context="", max_rounds=3, mode="dialogue", **kw):',
            '    return {"final_verdict": "UNANIMOUS AGREEMENT", "unanimous": True, "rounds": [1, 2],',
            '            "agreed_at_round": 2, "models_used": ["a", "b", "c"], "question": question, "mode": mode}',
        ].join('\n'));
        const r = spawnSync(process.execPath, [path.join(__dirname, '..', 'bin', 'delimit-cli.js'), 'deliberate', 'Is', 'this', 'wired?'], {
            env: { ...process.env, HOME: home, DELIMIT_HOME: path.join(home, '.delimit'), CI: '1', NO_COLOR: '1' },
            encoding: 'utf-8', timeout: 60000,
        });
        try {
            assert.strictEqual(r.status, 0, r.stderr);
            assert.match(r.stdout, /UNANIMOUS AGREEMENT/);
            assert.match(r.stdout, /3 models/);
            assert.doesNotMatch(r.stdout, /pending\.json/, 'a deliberation that ran must not be parked as pending');
        } finally {
            fs.rmSync(home, { recursive: true, force: true });
        }
    });
});
