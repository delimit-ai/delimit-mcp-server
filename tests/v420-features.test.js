/**
 * v4.20 feature tests — validates new commands:
 * doctor, status, simulate, report, remember, recall
 *
 * Uses Node.js built-in test runner (node:test) and assert module.
 */

const { describe, it, before, after } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');
const { execSync } = require('child_process');

const CLI = path.join(__dirname, '..', 'bin', 'delimit-cli.js');
const CLI_CWD = path.join(__dirname, '..');

const SKIP_IN_CI = process.env.CI ? 'requires full CLI stack (not available in CI)' : false;

function run(args, opts = {}) {
    return execSync(`node ${CLI} ${args}`, {
        timeout: 30000,
        encoding: 'utf-8',
        cwd: opts.cwd || CLI_CWD,
        env: { ...process.env, FORCE_COLOR: '0', ...(opts.env || {}) },
        ...(opts.input !== undefined ? { input: opts.input } : {}),
    });
}

// ---------------------------------------------------------------------------
// doctor
// ---------------------------------------------------------------------------
describe('v4.20: doctor command', () => {
    it('runs without error', { skip: SKIP_IN_CI }, () => {
        const output = run('doctor');
        assert.ok(output.length > 0, 'Should produce output');
    });

    it('--ci outputs valid JSON with required fields', { skip: SKIP_IN_CI }, () => {
        // doctor --ci may set exitCode=1 on failures but still prints JSON to stdout
        let output;
        try {
            output = run('doctor --ci');
        } catch (e) {
            // Non-zero exit is expected when there are failures
            output = e.stdout;
        }
        assert.ok(output, 'Should produce output even on non-zero exit');

        const data = JSON.parse(output);
        assert.ok(data.version !== undefined, 'JSON should have version field');
        assert.ok(data.health_score !== undefined, 'JSON should have health_score field');
        assert.ok(data.summary !== undefined, 'JSON should have summary field');
        assert.ok(data.checks !== undefined, 'JSON should have checks field');
    });

    it('--ci JSON has checks as array with name, status, message', { skip: SKIP_IN_CI }, () => {
        let output;
        try {
            output = run('doctor --ci');
        } catch (e) {
            output = e.stdout;
        }
        const data = JSON.parse(output);

        assert.ok(Array.isArray(data.checks), 'checks should be an array');
        assert.ok(data.checks.length > 0, 'checks should not be empty');

        for (const check of data.checks) {
            assert.ok(typeof check.name === 'string', `check should have name (got ${JSON.stringify(check)})`);
            assert.ok(typeof check.status === 'string', `check should have status (got ${JSON.stringify(check)})`);
            assert.ok(['pass', 'warn', 'fail'].includes(check.status), `status should be pass/warn/fail (got ${check.status})`);
            assert.ok(typeof check.message === 'string', `check should have message (got ${JSON.stringify(check)})`);
        }
    });

    it('--ci health_score format is N/10', { skip: SKIP_IN_CI }, () => {
        let output;
        try {
            output = run('doctor --ci');
        } catch (e) {
            output = e.stdout;
        }
        const data = JSON.parse(output);
        assert.match(data.health_score, /^\d+\/10$/, `health_score should match N/10 (got ${data.health_score})`);
    });
});

// ---------------------------------------------------------------------------
// status
// ---------------------------------------------------------------------------
describe('v4.20: status command', () => {
    it('runs without error', { skip: SKIP_IN_CI }, () => {
        const output = run('status');
        assert.ok(output.length > 0, 'Should produce output');
    });

    it('--json outputs valid JSON with required fields', { skip: SKIP_IN_CI }, () => {
        const output = run('status --json');
        const data = JSON.parse(output);

        assert.ok(data.memory !== undefined, 'JSON should have memory field');
        assert.ok(data.governance !== undefined, 'JSON should have governance field');
        assert.ok(data.mcp !== undefined, 'JSON should have mcp field');
        assert.ok(data.models !== undefined, 'JSON should have models field');
        assert.ok(data.readiness !== undefined, 'JSON should have readiness field');
    });

    it('--json readiness has score and total fields', { skip: SKIP_IN_CI }, () => {
        const output = run('status --json');
        const data = JSON.parse(output);

        assert.ok(typeof data.readiness.score === 'number', 'readiness.score should be a number');
        assert.ok(typeof data.readiness.total === 'number', 'readiness.total should be a number');
        assert.ok(data.readiness.total > 0, 'readiness.total should be positive');
        assert.ok(data.readiness.score >= 0 && data.readiness.score <= data.readiness.total,
            'readiness.score should be between 0 and total');
    });
});

// ---------------------------------------------------------------------------
// simulate
// ---------------------------------------------------------------------------
describe('v4.20: simulate command', () => {
    it('runs without error', { skip: SKIP_IN_CI }, () => {
        const output = run('simulate');
        assert.ok(output.length > 0, 'Should produce output');
    });

    it('output contains Simulate and Dry Run', { skip: SKIP_IN_CI }, () => {
        const output = run('simulate');
        assert.ok(output.includes('Simulate'), 'Output should contain "Simulate"');
        assert.ok(output.includes('Dry Run'), 'Output should contain "Dry Run"');
    });

    it('output contains Verdict', { skip: SKIP_IN_CI }, () => {
        const output = run('simulate');
        assert.ok(output.includes('Verdict'), 'Output should contain "Verdict"');
    });
});

// ---------------------------------------------------------------------------
// report
// ---------------------------------------------------------------------------
describe('v4.20: report command', () => {
    it('--since 7d runs without error', { skip: SKIP_IN_CI }, () => {
        const output = run('report --since 7d');
        assert.ok(output.length > 0, 'Should produce output');
    });

    it('--format json outputs valid JSON with period and summary', { skip: SKIP_IN_CI }, () => {
        const output = run('report --format json');
        const data = JSON.parse(output);

        assert.ok(data.period !== undefined, 'JSON should have period field');
        assert.ok(data.summary !== undefined, 'JSON should have summary field');
    });

    it('--format md output starts with # Delimit Governance Report', { skip: SKIP_IN_CI }, () => {
        const output = run('report --format md');
        assert.ok(output.trimStart().startsWith('# Delimit Governance Report'),
            'Markdown output should start with "# Delimit Governance Report"');
    });

    it('invalid format exits with error', { skip: SKIP_IN_CI }, () => {
        assert.throws(() => {
            run('report --format banana');
        }, (err) => {
            // process.exit(1) causes a non-zero exit code
            return err.status !== 0;
        }, 'Invalid format should exit with error');
    });
});

// ---------------------------------------------------------------------------
// remember + recall
// ---------------------------------------------------------------------------
describe('v4.20: remember and recall commands', () => {
    let origMemoryDir;
    let tmpHome;
    let tmpMemoryDir;

    before(() => {
        // Use a temp HOME so we don't pollute the real memory store
        tmpHome = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-mem-test-'));
        tmpMemoryDir = path.join(tmpHome, '.delimit', 'memory');
        // Save original for reference
        origMemoryDir = path.join(os.homedir(), '.delimit', 'memory');
    });

    after(() => {
        try { fs.rmSync(tmpHome, { recursive: true }); } catch {}
    });

    it('remember stores a memory file with hash field', { skip: SKIP_IN_CI }, () => {
        const testText = 'Test memory for v420 hash verification';
        run(`remember ${testText}`, { env: { HOME: tmpHome } });

        // Find the created memory file
        assert.ok(fs.existsSync(tmpMemoryDir), 'Memory directory should be created');
        const files = fs.readdirSync(tmpMemoryDir).filter(f => f.startsWith('mem-') && f.endsWith('.json'));
        assert.ok(files.length > 0, 'Should create at least one memory file');

        const memData = JSON.parse(fs.readFileSync(path.join(tmpMemoryDir, files[0]), 'utf-8'));
        assert.ok(typeof memData.hash === 'string', 'Memory should have hash field');
        assert.ok(memData.hash.length > 0, 'hash should not be empty');
    });

    it('remember stores with source_model field', { skip: SKIP_IN_CI }, () => {
        const testText = 'Test memory for v420 source_model check';
        run(`remember ${testText}`, { env: { HOME: tmpHome, DELIMIT_MODEL: 'test-model' } });

        const files = fs.readdirSync(tmpMemoryDir).filter(f => f.startsWith('mem-') && f.endsWith('.json'));
        // Find the file containing our text
        let found = false;
        for (const f of files) {
            const data = JSON.parse(fs.readFileSync(path.join(tmpMemoryDir, f), 'utf-8'));
            if (data.content && data.content.includes('source_model check')) {
                assert.ok(typeof data.source_model === 'string', 'Memory should have source_model field');
                assert.strictEqual(data.source_model, 'test-model', 'source_model should match DELIMIT_MODEL env');
                found = true;
                break;
            }
        }
        assert.ok(found, 'Should find the memory with source_model');
    });

    it('recall displays integrity badge', { skip: SKIP_IN_CI }, () => {
        // First store a memory so there is something to recall
        run(`remember "Integrity badge test memory"`, { env: { HOME: tmpHome } });

        const output = run('recall', { env: { HOME: tmpHome } });
        // The integrity badge is a checkmark character when hash matches
        // displayMemory shows either green checkmark or red X for tampered
        assert.ok(output.length > 0, 'recall should produce output');
        // The output should contain memory text and some integrity indicator
        // The verified checkmark is unicode \u2713
        assert.ok(
            output.includes('\u2713') || output.includes('tampered') || output.includes('Memories'),
            'recall should display memories with integrity information'
        );
    });
});
