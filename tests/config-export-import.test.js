const { describe, it, before, after } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');
const { execSync } = require('child_process');

const CLI = path.join(__dirname, '..', 'bin', 'delimit-cli.js');

// Create a temp directory for each test run
function makeTempDir() {
    return fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-config-test-'));
}

// ---------------------------------------------------------------------------
// buildConfigBundle (unit-level, extracted from CLI)
// ---------------------------------------------------------------------------

describe('buildConfigBundle', () => {
    it('returns policies from delimit.yml', () => {
        const dir = makeTempDir();
        const policyContent = 'override_defaults: false\nrules: []\n';
        fs.writeFileSync(path.join(dir, 'delimit.yml'), policyContent);

        // We invoke the CLI with --help just to load it, but we test the
        // function by requiring the module logic directly.  Since the CLI
        // is not structured as a library, we test via subprocess.
        const result = execSync(
            `node ${CLI} export`,
            { cwd: dir, encoding: 'utf-8' }
        );
        const bundle = JSON.parse(result);

        assert.strictEqual(bundle.delimit_config_version, 1);
        assert.ok(bundle.created_at);
        assert.strictEqual(bundle.policies.path, 'delimit.yml');
        assert.strictEqual(bundle.policies.content, policyContent);
        assert.strictEqual(bundle.workflow, null);

        fs.rmSync(dir, { recursive: true });
    });

    it('returns policies from .delimit/policies.yml', () => {
        const dir = makeTempDir();
        const configDir = path.join(dir, '.delimit');
        fs.mkdirSync(configDir);
        const policyContent = 'override_defaults: true\nrules: []\n';
        fs.writeFileSync(path.join(configDir, 'policies.yml'), policyContent);

        const result = execSync(
            `node ${CLI} export`,
            { cwd: dir, encoding: 'utf-8' }
        );
        const bundle = JSON.parse(result);

        assert.strictEqual(bundle.policies.path, path.join('.delimit', 'policies.yml'));
        assert.strictEqual(bundle.policies.content, policyContent);

        fs.rmSync(dir, { recursive: true });
    });

    it('includes workflow when present', () => {
        const dir = makeTempDir();
        fs.writeFileSync(path.join(dir, 'delimit.yml'), 'rules: []\n');
        const wfDir = path.join(dir, '.github', 'workflows');
        fs.mkdirSync(wfDir, { recursive: true });
        const wfContent = 'name: API Governance\non: pull_request\n';
        fs.writeFileSync(path.join(wfDir, 'api-governance.yml'), wfContent);

        const result = execSync(
            `node ${CLI} export`,
            { cwd: dir, encoding: 'utf-8' }
        );
        const bundle = JSON.parse(result);

        assert.strictEqual(bundle.workflow.path, '.github/workflows/api-governance.yml');
        assert.strictEqual(bundle.workflow.content, wfContent);

        fs.rmSync(dir, { recursive: true });
    });

    it('fails when no config exists', () => {
        const dir = makeTempDir();

        try {
            execSync(`node ${CLI} export`, { cwd: dir, encoding: 'utf-8', stdio: 'pipe' });
            assert.fail('should have exited with error');
        } catch (err) {
            assert.ok(err.stderr.includes('No governance config found') || err.status !== 0);
        }

        fs.rmSync(dir, { recursive: true });
    });
});

// ---------------------------------------------------------------------------
// export --output
// ---------------------------------------------------------------------------

describe('export --output', () => {
    it('writes config to a file', () => {
        const dir = makeTempDir();
        fs.writeFileSync(path.join(dir, 'delimit.yml'), 'rules: []\n');
        const outFile = path.join(dir, 'exported.json');

        execSync(`node ${CLI} export --output ${outFile}`, { cwd: dir });

        assert.ok(fs.existsSync(outFile));
        const bundle = JSON.parse(fs.readFileSync(outFile, 'utf-8'));
        assert.strictEqual(bundle.delimit_config_version, 1);

        fs.rmSync(dir, { recursive: true });
    });
});

// ---------------------------------------------------------------------------
// export --url
// ---------------------------------------------------------------------------

describe('export --url', () => {
    it('produces a base64-encoded share URL', () => {
        const dir = makeTempDir();
        fs.writeFileSync(path.join(dir, 'delimit.yml'), 'rules: []\n');

        const result = execSync(
            `node ${CLI} export --url`,
            { cwd: dir, encoding: 'utf-8' }
        );

        assert.ok(result.includes('https://delimit.ai/import?config='));

        // Extract and decode the base64
        const match = result.match(/config=([A-Za-z0-9+/=]+)/);
        assert.ok(match);
        const decoded = JSON.parse(Buffer.from(match[1], 'base64').toString('utf-8'));
        assert.strictEqual(decoded.delimit_config_version, 1);

        fs.rmSync(dir, { recursive: true });
    });
});

// ---------------------------------------------------------------------------
// import from file
// ---------------------------------------------------------------------------

describe('import from file', () => {
    it('writes policy file from exported JSON', () => {
        // Create source project and export
        const srcDir = makeTempDir();
        const policyContent = '# test policy\nrules: []\n';
        fs.writeFileSync(path.join(srcDir, 'delimit.yml'), policyContent);

        const exported = execSync(
            `node ${CLI} export`,
            { cwd: srcDir, encoding: 'utf-8' }
        );
        const exportFile = path.join(srcDir, 'config.json');
        fs.writeFileSync(exportFile, exported);

        // Import into a fresh directory
        const destDir = makeTempDir();
        execSync(
            `node ${CLI} import ${exportFile} --yes`,
            { cwd: destDir, encoding: 'utf-8' }
        );

        assert.ok(fs.existsSync(path.join(destDir, 'delimit.yml')));
        assert.strictEqual(
            fs.readFileSync(path.join(destDir, 'delimit.yml'), 'utf-8'),
            policyContent
        );

        fs.rmSync(srcDir, { recursive: true });
        fs.rmSync(destDir, { recursive: true });
    });

    it('writes workflow with --action flag', () => {
        const srcDir = makeTempDir();
        fs.writeFileSync(path.join(srcDir, 'delimit.yml'), 'rules: []\n');
        const wfDir = path.join(srcDir, '.github', 'workflows');
        fs.mkdirSync(wfDir, { recursive: true });
        fs.writeFileSync(path.join(wfDir, 'api-governance.yml'), 'name: Test\n');

        const exported = execSync(
            `node ${CLI} export`,
            { cwd: srcDir, encoding: 'utf-8' }
        );
        const exportFile = path.join(srcDir, 'config.json');
        fs.writeFileSync(exportFile, exported);

        const destDir = makeTempDir();
        execSync(
            `node ${CLI} import ${exportFile} --yes --action`,
            { cwd: destDir, encoding: 'utf-8' }
        );

        assert.ok(fs.existsSync(path.join(destDir, '.github', 'workflows', 'api-governance.yml')));

        fs.rmSync(srcDir, { recursive: true });
        fs.rmSync(destDir, { recursive: true });
    });
});

// ---------------------------------------------------------------------------
// import from base64
// ---------------------------------------------------------------------------

describe('import from base64', () => {
    it('decodes base64 config and writes policy', () => {
        const bundle = {
            delimit_config_version: 1,
            created_at: new Date().toISOString(),
            project: 'b64-test',
            policies: { path: 'delimit.yml', content: '# b64 test\nrules: []\n' },
            workflow: null,
        };
        const b64 = Buffer.from(JSON.stringify(bundle)).toString('base64');

        const destDir = makeTempDir();
        execSync(
            `node ${CLI} import ${b64} --yes`,
            { cwd: destDir, encoding: 'utf-8' }
        );

        assert.ok(fs.existsSync(path.join(destDir, 'delimit.yml')));
        assert.strictEqual(
            fs.readFileSync(path.join(destDir, 'delimit.yml'), 'utf-8'),
            '# b64 test\nrules: []\n'
        );

        fs.rmSync(destDir, { recursive: true });
    });
});

// ---------------------------------------------------------------------------
// round-trip: export -> import preserves content
// ---------------------------------------------------------------------------

describe('round-trip', () => {
    it('export then import preserves policies exactly', () => {
        const srcDir = makeTempDir();
        const originalPolicy = `# Delimit strict preset
override_defaults: true

rules:
  - id: no_endpoint_removal
    name: Forbid Endpoint Removal
    change_types: [endpoint_removed]
    severity: error
    action: forbid
`;
        fs.writeFileSync(path.join(srcDir, 'delimit.yml'), originalPolicy);

        // Export
        const exported = execSync(
            `node ${CLI} export`,
            { cwd: srcDir, encoding: 'utf-8' }
        );

        // Import into fresh dir
        const destDir = makeTempDir();
        const exportFile = path.join(srcDir, 'bundle.json');
        fs.writeFileSync(exportFile, exported);
        execSync(
            `node ${CLI} import ${exportFile} --yes`,
            { cwd: destDir, encoding: 'utf-8' }
        );

        const imported = fs.readFileSync(path.join(destDir, 'delimit.yml'), 'utf-8');
        assert.strictEqual(imported, originalPolicy);

        fs.rmSync(srcDir, { recursive: true });
        fs.rmSync(destDir, { recursive: true });
    });
});
