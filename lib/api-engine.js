/**
 * API Engine — Bridge from npm CLI to the Python gateway core.
 *
 * Invokes the delimit-gateway Python engine for:
 *   - lint (diff + policy)
 *   - diff (pure diff)
 *   - explain (human-readable templates)
 *   - semver (version classification)
 *
 * The gateway is the single implementation authority.
 * This module is a pure translation layer.
 */

const { execSync } = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');

// Gateway root — the Python engine lives here
// Resolution order: env var > ~/.delimit/server > ~/.delimit/gateway > bundled gateway
const DELIMIT_HOME = process.env.DELIMIT_HOME || path.join(os.homedir(), '.delimit');
const GATEWAY_ROOT = (() => {
    if (process.env.DELIMIT_GATEWAY_ROOT) return process.env.DELIMIT_GATEWAY_ROOT;
    // Check ~/.delimit/server (where `delimit setup` installs)
    const serverPath = path.join(DELIMIT_HOME, 'server');
    if (fs.existsSync(path.join(serverPath, 'core'))) return serverPath;
    // Check ~/.delimit/gateway (legacy path)
    const gatewayPath = path.join(DELIMIT_HOME, 'gateway');
    if (fs.existsSync(path.join(gatewayPath, 'core'))) return gatewayPath;
    // Check bundled gateway inside the npm package
    const bundledPath = path.join(__dirname, '..', 'gateway');
    if (fs.existsSync(path.join(bundledPath, 'core'))) return bundledPath;
    // Fallback — will fail with a clear error in runGateway()
    return gatewayPath;
})();

// Python executable — prefer venv if available
const PYTHON = (() => {
    const venvPy = path.join(DELIMIT_HOME, 'venv', 'bin', 'python');
    if (fs.existsSync(venvPy)) return venvPy;
    // Check common python locations
    for (const cmd of ['python3', 'python']) {
        try {
            execSync(`${cmd} --version`, { stdio: 'pipe' });
            return cmd;
        } catch {}
    }
    return 'python3';
})();

/**
 * Run a Python script against the gateway core.
 * Writes to a temp file to avoid shell escaping issues.
 * Returns parsed JSON or throws.
 */
function runGateway(pythonCode, timeoutMs = 30000) {
    // Check that the gateway core exists before trying to run
    if (!fs.existsSync(path.join(GATEWAY_ROOT, 'core'))) {
        const msg = [
            'Delimit gateway engine not found.',
            '',
            'Run one of:',
            '  npx delimit-cli setup     # full install with MCP server',
            '  delimit setup             # if globally installed',
            '',
            'Or set DELIMIT_GATEWAY_ROOT to your gateway directory.',
        ].join('\n');
        throw new Error(msg);
    }

    const tmpFile = path.join(os.tmpdir(), `delimit_${process.pid}_${Date.now()}.py`);
    try {
        fs.writeFileSync(tmpFile, pythonCode);
        const result = execSync(
            `${PYTHON} "${tmpFile}"`,
            {
                cwd: GATEWAY_ROOT,
                timeout: timeoutMs,
                encoding: 'utf-8',
                env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' },
            }
        );
        return JSON.parse(result.trim());
    } catch (err) {
        if (err.stdout) {
            try { return JSON.parse(err.stdout.trim()); } catch (_) {}
        }
        // Improve error messages for common failures
        const stderr = err.stderr || '';
        if (stderr.includes('No module named') || stderr.includes('ModuleNotFoundError')) {
            throw new Error(
                `Python dependency missing. Run: npx delimit-cli setup\n\nDetails: ${stderr.trim()}`
            );
        }
        if (err.message && err.message.includes('ENOENT')) {
            throw new Error(
                `Python not found. Install Python 3.9+ and try again.\n\nDetails: ${err.message}`
            );
        }
        throw new Error(err.stderr || err.message || 'Gateway execution failed');
    } finally {
        try { fs.unlinkSync(tmpFile); } catch (_) {}
    }
}

/**
 * Escape a string for safe embedding in Python source.
 */
function pyStr(s) {
    if (s == null) return 'None';
    return JSON.stringify(s);  // JSON strings are valid Python strings
}

/**
 * delimit lint — diff + policy evaluation (primary command)
 */
function lint(oldSpec, newSpec, opts = {}) {
    const lines = [
        'import json, yaml, sys',
        'sys.path.insert(0, ".")',
        'from core.policy_engine import evaluate_with_policy',
        `with open(${pyStr(oldSpec)}) as f: old = yaml.safe_load(f)`,
        `with open(${pyStr(newSpec)}) as f: new = yaml.safe_load(f)`,
        `r = evaluate_with_policy(old, new`,
    ];
    const args = ['include_semver=True'];
    if (opts.policy) args.push(`policy_file=${pyStr(opts.policy)}`);
    if (opts.version) args.push(`current_version=${pyStr(opts.version)}`);
    if (opts.name) args.push(`api_name=${pyStr(opts.name)}`);
    // Close the function call
    lines[lines.length - 1] = `r = evaluate_with_policy(old, new, ${args.join(', ')})`;
    lines.push('print(json.dumps(r))');
    return runGateway(lines.join('\n'));
}

/**
 * delimit diff — pure diff, no policy
 */
function diff(oldSpec, newSpec) {
    return runGateway([
        'import json, yaml, sys',
        'sys.path.insert(0, ".")',
        'from core.diff_engine_v2 import OpenAPIDiffEngine',
        `with open(${pyStr(oldSpec)}) as f: old = yaml.safe_load(f)`,
        `with open(${pyStr(newSpec)}) as f: new = yaml.safe_load(f)`,
        'engine = OpenAPIDiffEngine()',
        'changes = engine.compare(old, new)',
        'breaking = [c for c in changes if c.is_breaking]',
        'r = {"total_changes": len(changes), "breaking_changes": len(breaking), "changes": [{"type": c.type.value, "path": c.path, "message": c.message, "is_breaking": c.is_breaking} for c in changes]}',
        'print(json.dumps(r))',
    ].join('\n'));
}

/**
 * delimit explain — human-readable explanation
 */
function explain(oldSpec, newSpec, opts = {}) {
    const template = opts.template || 'developer';
    const args = [`template=${pyStr(template)}`];
    if (opts.oldVersion) args.push(`old_version=${pyStr(opts.oldVersion)}`);
    if (opts.newVersion) args.push(`new_version=${pyStr(opts.newVersion)}`);
    if (opts.name) args.push(`api_name=${pyStr(opts.name)}`);

    return runGateway([
        'import json, yaml, sys',
        'sys.path.insert(0, ".")',
        'from core.diff_engine_v2 import OpenAPIDiffEngine',
        'from core.explainer import explain, TEMPLATES',
        `with open(${pyStr(oldSpec)}) as f: old = yaml.safe_load(f)`,
        `with open(${pyStr(newSpec)}) as f: new = yaml.safe_load(f)`,
        'engine = OpenAPIDiffEngine()',
        'changes = engine.compare(old, new)',
        `out = explain(changes, ${args.join(', ')})`,
        `print(json.dumps({"template": ${pyStr(template)}, "available_templates": TEMPLATES, "output": out}))`,
    ].join('\n'));
}

/**
 * delimit semver — classify version bump
 */
function semver(oldSpec, newSpec, currentVersion) {
    const extraLines = currentVersion
        ? [
            `r["current_version"] = ${pyStr(currentVersion)}`,
            `r["next_version"] = bump_version(${pyStr(currentVersion)}, classify(changes))`,
          ]
        : [];

    return runGateway([
        'import json, yaml, sys',
        'sys.path.insert(0, ".")',
        'from core.diff_engine_v2 import OpenAPIDiffEngine',
        'from core.semver_classifier import classify_detailed, bump_version, classify',
        `with open(${pyStr(oldSpec)}) as f: old = yaml.safe_load(f)`,
        `with open(${pyStr(newSpec)}) as f: new = yaml.safe_load(f)`,
        'engine = OpenAPIDiffEngine()',
        'changes = engine.compare(old, new)',
        'r = classify_detailed(changes)',
        ...extraLines,
        'print(json.dumps(r))',
    ].join('\n'));
}

/**
 * delimit zero-spec — extract OpenAPI from framework source code
 */
function zeroSpec(projectDir, opts = {}) {
    const args = [];
    if (opts.pythonBin) args.push(`python_bin=${pyStr(opts.pythonBin)}`);

    return runGateway([
        'import json, sys',
        'sys.path.insert(0, ".")',
        'from core.zero_spec.detector import detect_framework, Framework',
        'from core.zero_spec.fastapi_extractor import extract_fastapi_spec',
        'from core.zero_spec.nestjs_extractor import extract_nestjs_spec',
        'from core.zero_spec.express_extractor import extract_express_spec',
        `info = detect_framework(${pyStr(projectDir)})`,
        'r = {"framework": info.framework.value, "confidence": info.confidence, "message": info.message}',
        'if info.framework == Framework.FASTAPI:',
        `    ext = extract_fastapi_spec(info, ${pyStr(projectDir)}${opts.pythonBin ? `, python_bin=${pyStr(opts.pythonBin)}` : ''})`,
        '    r.update(ext)',
        '    if ext.get("success") and info.app_locations:',
        '        r["app_file"] = info.app_locations[0].file',
        'elif info.framework == Framework.NESTJS:',
        `    ext = extract_nestjs_spec(info, ${pyStr(projectDir)})`,
        '    r.update(ext)',
        '    if ext.get("success") and info.app_locations:',
        '        r["app_file"] = info.app_locations[0].file',
        'elif info.framework == Framework.EXPRESS:',
        `    ext = extract_express_spec(info, ${pyStr(projectDir)})`,
        '    r.update(ext)',
        '    if ext.get("success") and info.app_locations:',
        '        r["app_file"] = info.app_locations[0].file',
        'else:',
        '    r["success"] = False',
        '    r["error"] = "No supported API framework detected"',
        'print(json.dumps(r, default=str))',
    ].join('\n'));
}

module.exports = { lint, diff, explain, semver, zeroSpec, GATEWAY_ROOT };
