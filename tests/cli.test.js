/**
 * Delimit CLI Tests
 *
 * Uses Node.js built-in test runner (node:test).
 * Run with: node --test tests/cli.test.js
 */

const { describe, it, before, after } = require('node:test');
const assert = require('node:assert/strict');
const { execSync } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');

const CLI = path.join(__dirname, '..', 'bin', 'delimit-cli.js');
const FIXTURES = path.join(__dirname, 'fixtures');
const SPEC_CLEAN = path.join(FIXTURES, 'openapi.yaml');
const SPEC_BREAKING = path.join(FIXTURES, 'openapi-changed.yaml');

/**
 * Run the CLI and return { stdout, stderr, exitCode }.
 * Does not throw on non-zero exit.
 */
function run(args, opts = {}) {
    const cwd = opts.cwd || os.tmpdir();
    try {
        const stdout = execSync(`node "${CLI}" ${args}`, {
            cwd,
            encoding: 'utf-8',
            timeout: 30000,
            env: { ...process.env, FORCE_COLOR: '0', NO_COLOR: '1' },
        });
        return { stdout, stderr: '', exitCode: 0 };
    } catch (err) {
        return {
            stdout: err.stdout || '',
            stderr: err.stderr || '',
            exitCode: err.status ?? 1,
        };
    }
}

// ---------------------------------------------------------------------------
// 1. CLI entry point loads without error
// ---------------------------------------------------------------------------
describe('CLI entry point', () => {
    it('loads the module without throwing', () => {
        assert.doesNotThrow(() => {
            execSync(`node --check "${CLI}"`, { encoding: 'utf-8' });
        });
    });
});

// ---------------------------------------------------------------------------
// 2. --version returns the version from package.json
// ---------------------------------------------------------------------------
describe('--version', () => {
    it('prints the version from package.json', () => {
        const pkg = require(path.join(__dirname, '..', 'package.json'));
        const { stdout } = run('--version');
        assert.equal(stdout.trim(), pkg.version);
    });
});

// ---------------------------------------------------------------------------
// 3. --help works at top level
// ---------------------------------------------------------------------------
describe('--help (top level)', () => {
    it('shows usage and lists commands', () => {
        const { stdout } = run('--help');
        assert.match(stdout, /Usage:/);
        assert.match(stdout, /init/);
        assert.match(stdout, /lint/);
        assert.match(stdout, /diff/);
        assert.match(stdout, /explain/);
        assert.match(stdout, /doctor/);
    });
});

// ---------------------------------------------------------------------------
// 4. Each command has help text
// ---------------------------------------------------------------------------
describe('subcommand --help', () => {
    for (const cmd of ['init', 'lint', 'diff', 'explain', 'doctor']) {
        it(`"help ${cmd}" shows description`, () => {
            const { stdout } = run(`help ${cmd}`);
            assert.match(stdout, /Usage:/);
            assert.match(stdout, new RegExp(cmd));
        });
    }
});

// ---------------------------------------------------------------------------
// 5. init creates .delimit/policies.yml with default preset
// ---------------------------------------------------------------------------
describe('init command', () => {
    let tmpDir;

    before(() => {
        tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-test-init-'));
    });

    after(() => {
        fs.rmSync(tmpDir, { recursive: true, force: true });
    });

    it('creates .delimit/policies.yml with default preset', () => {
        const { stdout, exitCode } = run('init', { cwd: tmpDir });
        assert.equal(exitCode, 0);
        const policyPath = path.join(tmpDir, '.delimit', 'policies.yml');
        assert.ok(fs.existsSync(policyPath), 'policies.yml should exist');
        const content = fs.readFileSync(policyPath, 'utf-8');
        assert.match(content, /Delimit Policy Preset: default/);
        assert.match(content, /override_defaults: false/);
    });

    it('reports already initialized on second run', () => {
        const { stdout, exitCode } = run('init', { cwd: tmpDir });
        assert.equal(exitCode, 0);
        assert.match(stdout, /Already initialized/);
    });
});

describe('init --preset strict', () => {
    let tmpDir;

    before(() => {
        tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-test-init-strict-'));
    });

    after(() => {
        fs.rmSync(tmpDir, { recursive: true, force: true });
    });

    it('creates policies.yml with strict preset', () => {
        const { stdout, exitCode } = run('init --preset strict', { cwd: tmpDir });
        assert.equal(exitCode, 0);
        const content = fs.readFileSync(
            path.join(tmpDir, '.delimit', 'policies.yml'),
            'utf-8'
        );
        assert.match(content, /Delimit Policy Preset: strict/);
        assert.match(content, /no_endpoint_removal/);
    });
});

describe('init --preset relaxed', () => {
    let tmpDir;

    before(() => {
        tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-test-init-relaxed-'));
    });

    after(() => {
        fs.rmSync(tmpDir, { recursive: true, force: true });
    });

    it('creates policies.yml with relaxed preset', () => {
        const { stdout, exitCode } = run('init --preset relaxed', { cwd: tmpDir });
        assert.equal(exitCode, 0);
        const content = fs.readFileSync(
            path.join(tmpDir, '.delimit', 'policies.yml'),
            'utf-8'
        );
        assert.match(content, /Delimit Policy Preset: relaxed/);
        assert.match(content, /warn_endpoint_removal/);
    });
});

// ---------------------------------------------------------------------------
// 6. lint with identical specs returns clean (exit 0)
// ---------------------------------------------------------------------------
describe('lint (clean)', () => {
    it('returns exit 0 with no violations for identical specs', () => {
        const { stdout, exitCode } = run(
            `lint "${SPEC_CLEAN}" "${SPEC_CLEAN}" --json`
        );
        assert.equal(exitCode, 0);
        const result = JSON.parse(stdout);
        assert.equal(result.decision, 'pass');
        assert.equal(result.exit_code, 0);
        assert.equal(result.summary.breaking_changes, 0);
        assert.equal(result.violations.length, 0);
    });
});

// ---------------------------------------------------------------------------
// 7. lint with breaking change returns exit 1
// ---------------------------------------------------------------------------
describe('lint (breaking)', () => {
    it('returns exit 1 when breaking changes are detected', () => {
        const { stdout, exitCode } = run(
            `lint "${SPEC_CLEAN}" "${SPEC_BREAKING}" --json`
        );
        assert.equal(exitCode, 1);
        const result = JSON.parse(stdout);
        assert.equal(result.decision, 'fail');
        assert.equal(result.exit_code, 1);
        assert.ok(result.summary.breaking_changes > 0, 'should have breaking changes');
        assert.ok(result.violations.length > 0, 'should have violations');
    });

    it('includes endpoint_removed in changes', () => {
        const { stdout } = run(
            `lint "${SPEC_CLEAN}" "${SPEC_BREAKING}" --json`
        );
        const result = JSON.parse(stdout);
        const types = result.all_changes.map(c => c.type);
        assert.ok(types.includes('endpoint_removed'), 'should detect endpoint removal');
    });

    it('includes semver bump classification', () => {
        const { stdout } = run(
            `lint "${SPEC_CLEAN}" "${SPEC_BREAKING}" --json`
        );
        const result = JSON.parse(stdout);
        assert.ok(result.semver, 'should have semver field');
        assert.equal(result.semver.bump, 'major');
    });
});

// ---------------------------------------------------------------------------
// 8. diff outputs change types
// ---------------------------------------------------------------------------
describe('diff command', () => {
    it('outputs changes between two specs', () => {
        const { stdout, exitCode } = run(
            `diff "${SPEC_CLEAN}" "${SPEC_BREAKING}" --json`
        );
        assert.equal(exitCode, 0);
        const result = JSON.parse(stdout);
        assert.ok(result.total_changes > 0, 'should have changes');
        assert.ok(result.breaking_changes > 0, 'should have breaking changes');
        assert.ok(Array.isArray(result.changes), 'changes should be an array');
    });

    it('reports change types correctly', () => {
        const { stdout } = run(
            `diff "${SPEC_CLEAN}" "${SPEC_BREAKING}" --json`
        );
        const result = JSON.parse(stdout);
        const types = result.changes.map(c => c.type);
        assert.ok(types.includes('endpoint_removed'));
        assert.ok(types.includes('type_changed'));
        assert.ok(types.includes('enum_value_removed'));
    });

    it('marks breaking changes with is_breaking flag', () => {
        const { stdout } = run(
            `diff "${SPEC_CLEAN}" "${SPEC_BREAKING}" --json`
        );
        const result = JSON.parse(stdout);
        const breakingChanges = result.changes.filter(c => c.is_breaking);
        assert.equal(breakingChanges.length, result.breaking_changes);
    });

    it('returns no changes for identical specs', () => {
        const { stdout, exitCode } = run(
            `diff "${SPEC_CLEAN}" "${SPEC_CLEAN}" --json`
        );
        assert.equal(exitCode, 0);
        const result = JSON.parse(stdout);
        assert.equal(result.total_changes, 0);
        assert.equal(result.breaking_changes, 0);
    });
});

// ---------------------------------------------------------------------------
// 9. explain command
// ---------------------------------------------------------------------------
describe('explain command', () => {
    it('generates human-readable explanation', () => {
        const { stdout, exitCode } = run(
            `explain "${SPEC_CLEAN}" "${SPEC_BREAKING}" --json`
        );
        assert.equal(exitCode, 0);
        const result = JSON.parse(stdout);
        assert.ok(result.output, 'should have output text');
        assert.ok(result.output.length > 0, 'output should not be empty');
        assert.ok(result.template, 'should report template used');
    });

    it('supports --template flag', () => {
        const { stdout, exitCode } = run(
            `explain "${SPEC_CLEAN}" "${SPEC_BREAKING}" --template migration --json`
        );
        assert.equal(exitCode, 0);
        const result = JSON.parse(stdout);
        assert.equal(result.template, 'migration');
    });
});

// ---------------------------------------------------------------------------
// 10. lint with --policy preset
// ---------------------------------------------------------------------------
describe('lint --policy', () => {
    it('accepts relaxed preset and does not fail on breaking changes', () => {
        // relaxed preset uses action:warn, so decision is "warn" not "fail"
        const { stdout, exitCode } = run(
            `lint "${SPEC_CLEAN}" "${SPEC_BREAKING}" --policy relaxed --json`
        );
        assert.equal(exitCode, 0, 'relaxed preset should exit 0');
        const result = JSON.parse(stdout);
        assert.notEqual(result.decision, 'fail', 'relaxed should not produce fail decision');
    });

    it('accepts strict preset and fails on breaking changes', () => {
        const { stdout, exitCode } = run(
            `lint "${SPEC_CLEAN}" "${SPEC_BREAKING}" --policy strict --json`
        );
        assert.equal(exitCode, 1);
        const result = JSON.parse(stdout);
        assert.equal(result.decision, 'fail');
        assert.ok(result.violations.length > 0);
    });
});

// ---------------------------------------------------------------------------
// 11. Error handling -- missing files
// ---------------------------------------------------------------------------
describe('error handling', () => {
    it('lint with nonexistent spec file reports error', () => {
        const { exitCode } = run('lint /nonexistent/old.yaml /nonexistent/new.yaml --json');
        assert.notEqual(exitCode, 0);
    });

    it('diff with nonexistent spec file reports error', () => {
        const { exitCode } = run('diff /nonexistent/old.yaml /nonexistent/new.yaml --json');
        assert.notEqual(exitCode, 0);
    });
});

// ---------------------------------------------------------------------------
// 12. api-engine module exports
// ---------------------------------------------------------------------------
describe('api-engine module', () => {
    it('exports lint, diff, explain, semver, zeroSpec functions', () => {
        const engine = require(path.join(__dirname, '..', 'lib', 'api-engine.js'));
        assert.equal(typeof engine.lint, 'function');
        assert.equal(typeof engine.diff, 'function');
        assert.equal(typeof engine.explain, 'function');
        assert.equal(typeof engine.semver, 'function');
        assert.equal(typeof engine.zeroSpec, 'function');
    });

    it('lint returns parsed JSON with decision field', () => {
        const engine = require(path.join(__dirname, '..', 'lib', 'api-engine.js'));
        const result = engine.lint(SPEC_CLEAN, SPEC_CLEAN);
        assert.ok(result.decision, 'should have decision field');
        assert.equal(result.decision, 'pass');
    });

    it('diff returns parsed JSON with changes array', () => {
        const engine = require(path.join(__dirname, '..', 'lib', 'api-engine.js'));
        const result = engine.diff(SPEC_CLEAN, SPEC_BREAKING);
        assert.ok(Array.isArray(result.changes), 'should have changes array');
        assert.ok(result.total_changes > 0);
    });
});
