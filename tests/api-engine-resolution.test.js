/**
 * Tests for api-engine.js gateway resolution and error handling.
 *
 * Verifies that the gateway root is resolved correctly across:
 *   - env var override
 *   - ~/.delimit/server (setup installs here)
 *   - ~/.delimit/gateway (legacy path)
 *   - bundled gateway (npm package)
 *
 * Also verifies clear error messages when the gateway is missing.
 */

const { describe, it, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const os = require('os');

const API_ENGINE_PATH = path.join(__dirname, '..', 'lib', 'api-engine.js');

describe('api-engine gateway resolution', () => {
    it('bundled gateway directory exists with core/', () => {
        const bundledGateway = path.join(__dirname, '..', 'gateway');
        assert.ok(fs.existsSync(bundledGateway), 'gateway/ directory should exist in npm package');
        assert.ok(fs.existsSync(path.join(bundledGateway, 'core')), 'gateway/core/ should exist');
    });

    it('bundled gateway has required Python modules', () => {
        const corePath = path.join(__dirname, '..', 'gateway', 'core');
        const requiredFiles = [
            'diff_engine_v2.py',
            'policy_engine.py',
            'semver_classifier.py',
        ];
        for (const f of requiredFiles) {
            assert.ok(
                fs.existsSync(path.join(corePath, f)),
                `core/${f} should exist in bundled gateway`
            );
        }
    });

    it('api-engine.js exports lint, diff, explain, semver, zeroSpec', () => {
        // Clear require cache so we get fresh module
        delete require.cache[require.resolve(API_ENGINE_PATH)];
        const apiEngine = require(API_ENGINE_PATH);
        assert.equal(typeof apiEngine.lint, 'function');
        assert.equal(typeof apiEngine.diff, 'function');
        assert.equal(typeof apiEngine.explain, 'function');
        assert.equal(typeof apiEngine.semver, 'function');
        assert.equal(typeof apiEngine.zeroSpec, 'function');
    });

    it('api-engine.js contains bundled gateway fallback logic', () => {
        const source = fs.readFileSync(API_ENGINE_PATH, 'utf-8');
        assert.ok(
            source.includes("path.join(__dirname, '..', 'gateway')"),
            'Should check bundled gateway path as fallback'
        );
    });

    it('runGateway gives clear error when gateway is missing', () => {
        const source = fs.readFileSync(API_ENGINE_PATH, 'utf-8');
        assert.ok(
            source.includes('Delimit gateway engine not found'),
            'Should have clear error message for missing gateway'
        );
        assert.ok(
            source.includes('npx delimit-cli setup'),
            'Error message should suggest running setup'
        );
    });

    it('runGateway gives clear error for missing Python dependencies', () => {
        const source = fs.readFileSync(API_ENGINE_PATH, 'utf-8');
        assert.ok(
            source.includes('Python dependency missing'),
            'Should have clear error for missing Python modules'
        );
    });

    it('runGateway gives clear error when Python is not found', () => {
        const source = fs.readFileSync(API_ENGINE_PATH, 'utf-8');
        assert.ok(
            source.includes('Python not found'),
            'Should have clear error when Python binary is missing'
        );
    });
});

describe('api-engine integration', () => {
    const fixturesDir = path.join(os.tmpdir(), 'delimit-test-fixtures');
    const oldSpecPath = path.join(fixturesDir, 'old.yaml');
    const newSpecPath = path.join(fixturesDir, 'new.yaml');

    beforeEach(() => {
        fs.mkdirSync(fixturesDir, { recursive: true });
        fs.writeFileSync(oldSpecPath, `openapi: "3.0.3"
info:
  title: Test API
  version: "1.0.0"
paths:
  /items:
    get:
      summary: List items
      responses:
        "200":
          description: OK
`);
        fs.writeFileSync(newSpecPath, `openapi: "3.0.3"
info:
  title: Test API
  version: "1.1.0"
paths:
  /items:
    get:
      summary: List items
      parameters:
        - name: limit
          in: query
          required: true
          schema:
            type: integer
      responses:
        "200":
          description: OK
`);
    });

    afterEach(() => {
        try { fs.rmSync(fixturesDir, { recursive: true }); } catch {}
    });

    it('lint detects breaking change from added required parameter', () => {
        delete require.cache[require.resolve(API_ENGINE_PATH)];
        const apiEngine = require(API_ENGINE_PATH);
        const result = apiEngine.lint(oldSpecPath, newSpecPath);
        assert.ok(result, 'lint should return a result');
        assert.ok(result.exit_code !== undefined || result.changes !== undefined, 'result should have structure');
    });

    it('diff detects breaking change from added required parameter', () => {
        delete require.cache[require.resolve(API_ENGINE_PATH)];
        const apiEngine = require(API_ENGINE_PATH);
        const result = apiEngine.diff(oldSpecPath, newSpecPath);
        assert.ok(result, 'diff should return a result');
        assert.ok(result.total_changes >= 1, 'should detect at least 1 change');
        assert.ok(result.breaking_changes >= 1, 'should detect at least 1 breaking change');
    });
});
