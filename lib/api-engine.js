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
                // Capture stderr instead of echoing it: a raw Python
                // Traceback must never reach the user's terminal on its own.
                // It is surfaced through the thrown Error below.
                stdio: ['ignore', 'pipe', 'pipe'],
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
        // Lead with the actual exception line (the LAST non-empty stderr
        // line) so callers that show only the first line say something
        // useful instead of "Traceback (most recent call last):".
        if (stderr.trim()) {
            const lines = stderr.trim().split('\n').map(s => s.trim()).filter(Boolean);
            const headline = lines[lines.length - 1];
            throw new Error(`${headline}\n\n${stderr.trim()}`);
        }
        throw new Error(err.message || 'Gateway execution failed');
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

// ---------------------------------------------------------------------------
// Zero-config YAML (LED-4423).
//
// `delimit check` is advertised as zero-config: no `setup`, no venv. On a
// fresh Debian 12 host the system python3 has no `yaml` module and
// `pip install pyyaml` is refused (PEP 668). Every helper below used to start
// with `import json, yaml, sys`, so the gate could not even parse the spec.
//
// Strategy: probe ONCE whether the chosen python can `import yaml`. If it
// cannot, parse each YAML input in node with the bundled `js-yaml` and hand
// the helper a temp JSON file instead (JSON is valid YAML, so the same path
// also works when PyYAML IS present). The helper header installs a tiny
// JSON-backed `yaml` shim into sys.modules when PyYAML is missing, because
// the bundled gateway core (policy_engine.py) imports `yaml` at module level.
// ---------------------------------------------------------------------------

let _pyYamlAvailable = null;

/**
 * True when the selected python interpreter can `import yaml`. Cached.
 */
function pythonHasYaml() {
    if (_pyYamlAvailable !== null) return _pyYamlAvailable;
    try {
        execSync(`${PYTHON} -c "import yaml"`, {
            stdio: 'pipe',
            timeout: 10000,
            env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' },
        });
        _pyYamlAvailable = true;
    } catch {
        _pyYamlAvailable = false;
    }
    return _pyYamlAvailable;
}

/**
 * Parse a YAML/JSON spec in node and write it out as a temp JSON file.
 * Returns the temp path. Throws with a clear message on parse failure.
 */
function yamlToTempJson(specPath) {
    const jsYaml = require('js-yaml');
    const raw = fs.readFileSync(specPath, 'utf-8');
    let doc;
    try {
        doc = jsYaml.load(raw);
    } catch (e) {
        throw new Error(`Could not parse ${specPath} as YAML: ${e.message}`);
    }
    const tmp = path.join(
        os.tmpdir(),
        `delimit_spec_${process.pid}_${Date.now()}_${Math.random().toString(36).slice(2, 8)}.json`
    );
    fs.writeFileSync(tmp, JSON.stringify(doc === undefined ? null : doc));
    return tmp;
}

/**
 * Prepare spec inputs for the python helper. When PyYAML is missing the
 * YAML files are converted to temp JSON files; otherwise the originals are
 * passed through untouched. Always call `cleanup()` in a finally block.
 *
 * @param {string[]} paths
 * @returns {{ paths: string[], cleanup: () => void }}
 */
function prepareSpecInputs(paths) {
    if (pythonHasYaml()) {
        return { paths: paths.slice(), cleanup: () => {} };
    }
    const temps = [];
    const out = [];
    try {
        for (const p of paths) {
            if (p == null) { out.push(p); continue; }
            const t = yamlToTempJson(p);
            temps.push(t);
            out.push(t);
        }
    } catch (e) {
        for (const t of temps) { try { fs.unlinkSync(t); } catch (_) {} }
        throw e;
    }
    return {
        paths: out,
        cleanup: () => { for (const t of temps) { try { fs.unlinkSync(t); } catch (_) {} } },
    };
}

/**
 * Guarded helper header: `yaml` is optional. When PyYAML is missing a
 * JSON-backed shim is installed into sys.modules so the gateway core's own
 * `import yaml` succeeds, and `_load(path)` reads the (JSON) spec.
 */
// The shim first tries JSON (the pre-converted spec inputs). Anything else
// (e.g. the gateway's own policy presets under core/policies/*.yml) is
// parsed by handing the text to node + js-yaml, which is always present
// because this helper only ever runs underneath the node CLI.
function _jsYamlPath() {
    try { return require.resolve('js-yaml'); } catch { return null; }
}
const _NODE_YAML_TO_JSON = [
    'const fs=require("fs");',
    'const y=require(process.argv[1]);',
    'let s="";',
    'try{s=fs.readFileSync(0,"utf8")}catch(e){}',
    'const d=y.load(s);',
    'process.stdout.write(JSON.stringify(d===undefined?null:d));',
].join('');

const PY_HEADER = [
    'import json, sys',
    'try:',
    '    import yaml',
    'except ImportError:',
    '    yaml = None',
    'if yaml is None:',
    '    import types as _types, subprocess as _sp',
    `    _NODE = ${pyStr(process.execPath)}`,
    `    _JSYAML = ${pyStr(_jsYamlPath())}`,
    `    _NODE_SCRIPT = ${pyStr(_NODE_YAML_TO_JSON)}`,
    '    def _delimit_yaml_via_node(data):',
    '        if not _JSYAML:',
    '            raise RuntimeError("PyYAML is not installed and the built-in YAML reader (js-yaml) is unavailable.")',
    '        p = _sp.run([_NODE, "-e", _NODE_SCRIPT, _JSYAML], input=data.encode("utf-8"), stdout=_sp.PIPE, stderr=_sp.PIPE)',
    '        if p.returncode != 0:',
    '            err = p.stderr.decode("utf-8", "replace").strip().splitlines()',
    '            raise RuntimeError("YAML parse failed: " + (err[-1] if err else "unknown error"))',
    '        return json.loads(p.stdout.decode("utf-8"))',
    '    def _delimit_json_safe_load(stream):',
    '        data = stream if isinstance(stream, (str, bytes)) else stream.read()',
    '        if isinstance(data, bytes): data = data.decode("utf-8")',
    '        if not data.strip(): return None',
    '        try:',
    '            return json.loads(data)',
    '        except ValueError:',
    '            return _delimit_yaml_via_node(data)',
    '    yaml = _types.ModuleType("yaml")',
    '    yaml.safe_load = _delimit_json_safe_load',
    '    yaml.load = lambda stream, *a, **k: _delimit_json_safe_load(stream)',
    '    yaml.safe_dump = lambda data, *a, **k: json.dumps(data, indent=2)',
    '    yaml.dump = yaml.safe_dump',
    '    yaml.YAMLError = ValueError',
    '    yaml.__delimit_shim__ = True',
    '    sys.modules["yaml"] = yaml',
    'def _load(p):',
    '    with open(p, "r", encoding="utf-8") as f: return yaml.safe_load(f)',
    'sys.path.insert(0, ".")',
];

/**
 * delimit lint — diff + policy evaluation (primary command)
 */
function lint(oldSpec, newSpec, opts = {}) {
    // A policy file (a real path, not a preset name) is YAML too: convert it
    // alongside the specs so the no-PyYAML path can still load custom rules.
    const policyIsFile = !!(opts.policy && fs.existsSync(opts.policy) && fs.statSync(opts.policy).isFile());
    const inputs = prepareSpecInputs(policyIsFile ? [oldSpec, newSpec, opts.policy] : [oldSpec, newSpec]);
    try {
        const [oldPath, newPath, policyPath] = inputs.paths;
        const lines = [
            ...PY_HEADER,
            'from core.policy_engine import evaluate_with_policy',
            `old = _load(${pyStr(oldPath)})`,
            `new = _load(${pyStr(newPath)})`,
        ];
        const args = ['include_semver=True'];
        if (opts.policy) args.push(`policy_file=${pyStr(policyIsFile ? policyPath : opts.policy)}`);
        if (opts.version) args.push(`current_version=${pyStr(opts.version)}`);
        if (opts.name) args.push(`api_name=${pyStr(opts.name)}`);
        lines.push(`r = evaluate_with_policy(old, new, ${args.join(', ')})`);
        lines.push('print(json.dumps(r))');
        return runGateway(lines.join('\n'));
    } finally {
        inputs.cleanup();
    }
}

/**
 * delimit diff — pure diff, no policy
 */
function diff(oldSpec, newSpec) {
    const inputs = prepareSpecInputs([oldSpec, newSpec]);
    try {
        const [oldPath, newPath] = inputs.paths;
        return runGateway([
            ...PY_HEADER,
            'from core.diff_engine_v2 import OpenAPIDiffEngine',
            `old = _load(${pyStr(oldPath)})`,
            `new = _load(${pyStr(newPath)})`,
            'engine = OpenAPIDiffEngine()',
            'changes = engine.compare(old, new)',
            'breaking = [c for c in changes if c.is_breaking]',
            'r = {"total_changes": len(changes), "breaking_changes": len(breaking), "changes": [{"type": c.type.value, "path": c.path, "message": c.message, "is_breaking": c.is_breaking} for c in changes]}',
            'print(json.dumps(r))',
        ].join('\n'));
    } finally {
        inputs.cleanup();
    }
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

    const inputs = prepareSpecInputs([oldSpec, newSpec]);
    try {
        const [oldPath, newPath] = inputs.paths;
        return runGateway([
            ...PY_HEADER,
            'from core.diff_engine_v2 import OpenAPIDiffEngine',
            'from core.explainer import explain, TEMPLATES',
            `old = _load(${pyStr(oldPath)})`,
            `new = _load(${pyStr(newPath)})`,
            'engine = OpenAPIDiffEngine()',
            'changes = engine.compare(old, new)',
            `out = explain(changes, ${args.join(', ')})`,
            `print(json.dumps({"template": ${pyStr(template)}, "available_templates": TEMPLATES, "output": out}))`,
        ].join('\n'));
    } finally {
        inputs.cleanup();
    }
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

    const inputs = prepareSpecInputs([oldSpec, newSpec]);
    try {
        const [oldPath, newPath] = inputs.paths;
        return runGateway([
            ...PY_HEADER,
            'from core.diff_engine_v2 import OpenAPIDiffEngine',
            'from core.semver_classifier import classify_detailed, bump_version, classify',
            `old = _load(${pyStr(oldPath)})`,
            `new = _load(${pyStr(newPath)})`,
            'engine = OpenAPIDiffEngine()',
            'changes = engine.compare(old, new)',
            'r = classify_detailed(changes)',
            ...extraLines,
            'print(json.dumps(r))',
        ].join('\n'));
    } finally {
        inputs.cleanup();
    }
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

/**
 * Spec health score (used by `delimit scan`). Same zero-config YAML path as
 * the other helpers: works with or without PyYAML.
 */
function specHealth(specPath) {
    const inputs = prepareSpecInputs([specPath]);
    try {
        const [p] = inputs.paths;
        return runGateway([
            ...PY_HEADER,
            'from core.spec_health import score_spec',
            `spec = _load(${pyStr(p)})`,
            'print(json.dumps(score_spec(spec)))',
        ].join('\n'));
    } finally {
        inputs.cleanup();
    }
}

module.exports = { lint, diff, explain, semver, zeroSpec, specHealth, pythonHasYaml, GATEWAY_ROOT, PYTHON };
