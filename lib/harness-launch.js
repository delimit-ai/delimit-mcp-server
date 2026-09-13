'use strict';

// Explicit interactive client compatibility, not an autonomous executor/router.
const fs = require('fs');
const path = require('path');
const os = require('os');
const cp = require('child_process');

const HARNESS_IDS = ['copilot', 'muse'];
const MAX_BOOTSTRAP_BYTES = 16384;
const SHIM_MARKER = '// Delimit explicit harness shim';
const GIT_ROUTING_ENV = ['GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE',
    'GIT_OBJECT_DIRECTORY', 'GIT_COMMON_DIR', 'GIT_QUARANTINE_PATH'];
class HarnessLaunchError extends Error {}

function isHarnessShim(candidate, env) {
    const home = env.HOME || os.homedir();
    for (const root of [env.DELIMIT_HOME, path.join(home, '.delimit')].filter(Boolean)) {
        for (const id of HARNESS_IDS) {
            try {
                if (fs.realpathSync(path.join(root, 'shims', id)) === candidate) return true;
            } catch (_) {}
        }
    }
    // Also reject a copied shim outside the normal directory. Read only its
    // short header, never dump executable/config contents into an error.
    const fd = fs.openSync(candidate, 'r');
    try {
        const header = Buffer.alloc(160);
        const count = fs.readSync(fd, header, 0, header.length, 0);
        return header.subarray(0, count).toString('utf8').startsWith('#!/usr/bin/env node\n' + SHIM_MARKER);
    } finally { fs.closeSync(fd); }
}

function resolveBinary(id, env) {
    const home = env.HOME || os.homedir();
    // Never resolve through PATH: the first entry may be this shim itself.
    const candidates = id === 'muse'
        ? fs.readdirSync(path.join(home, '.local', 'bin')).filter(n => /^muse-bin-\d+\.\d+\.\d+-R[\d.]+$/.test(n))
            .sort((a, b) => b.localeCompare(a, undefined, {numeric: true}))
            .map(n => path.join(home, '.local', 'bin', n))
        : [path.join(home, '.local', 'bin', id), `/usr/local/bin/${id}-real`, `/usr/bin/${id}-real`,
            `/usr/local/bin/${id}`, `/usr/bin/${id}`];
    for (const candidate of candidates) {
        try {
            fs.accessSync(candidate, fs.constants.X_OK);
            const real = fs.realpathSync(candidate);
            if (fs.statSync(real).isFile() && !isHarnessShim(real, env)) return real;
        } catch (_) {}
    }
    throw new HarnessLaunchError(`Official ${id} executable not found; install/authenticate the official client first.`);
}

function bootstrap(cwd, home) {
    const custom = path.join(home, 'harness-bootstrap.md');
    let text = `Delimit remains the durable authority, ledger and evidence owner. This is an explicitly selected interactive harness, not a build permit. Project: ${JSON.stringify(cwd)}.\n`
        + 'Before making changes, load the applicable project instructions, recover the project-scoped Delimit handoff and inspect current ledger/permissions and git state. Never infer a venture from the shell home directory. Preserve dirty edits and current pause/arming controls. Use supported Delimit tooling, never raw-edit ledger storage. Do not repeat completed external actions. A model/harness switch does not expand authority. Keep provider/model identity separate. Before exit, capture a scoped Delimit handoff with completed work, evidence, unresolved blockers and next actions. No Proof, No Done.\n';
    if (fs.existsSync(custom)) text += '\n' + fs.readFileSync(custom, 'utf8');
    if (Buffer.byteLength(text) > MAX_BOOTSTRAP_BYTES) throw new HarnessLaunchError('Harness bootstrap exceeds 16 KiB; refusing silent instruction truncation.');
    return text;
}

function checkMuseSettings(env) {
    const config = path.join(env.XDG_CONFIG_HOME || path.join(env.HOME || os.homedir(), '.config'), 'muse', 'settings.json');
    let data;
    try { data = JSON.parse(fs.readFileSync(config, 'utf8')); }
    catch (_) { throw new HarnessLaunchError('Muse settings are missing, unreadable or invalid; no settings content was disclosed.'); }
    if (!data || typeof data !== 'object' || Array.isArray(data)) {
        throw new HarnessLaunchError('Muse settings must be a configuration object; no settings content was disclosed.');
    }
    if (data.context?.foreign_personal_rules !== false || data.context?.foreign_personal_skills !== false) {
        throw new HarnessLaunchError('Muse foreign personal context is not excluded. Configure the native context settings and the reviewed Delimit bootstrap before launching; no shared Claude/Codex rules were modified.');
    }
    if (data.model !== 'muse-spark-1.3') throw new HarnessLaunchError('Muse must have the verified Standard muse-spark-1.3 selection, not Contributor, for this Delimit launch.');
    if (data.provider && data.provider !== 'meta') throw new HarnessLaunchError('Unexpected Muse provider; billing route requires explicit verification.');
    if (data.endpoint_transport?.base_url && data.endpoint_transport.base_url !== 'https://api.meta.ai/v1') throw new HarnessLaunchError('Unexpected Muse endpoint; refusing a different data route.');
}

function launchExplicitHarness(id, options = {}) {
    const env = {...(options.env || process.env)};
    const args = options.args || [];
    const spawn = options.spawnSync || cp.spawnSync;
    const error = options.error || (message => console.error(`[Delimit] ${message}`));
    try {
        if (!HARNESS_IDS.includes(id)) throw new HarnessLaunchError('Unknown execution harness.');
        const binary = resolveBinary(id, env);
        // Inspection must not hydrate private context, touch permissions, probe a
        // model, update packages or silently log the user in.
        if (args.length === 1 && ['--help', '-h', '--version', '-v', '-V'].includes(args[0])) {
            const flag = ['-v', '-V'].includes(args[0]) ? '--version' : args[0];
            const result = spawn(binary, [flag], {env, stdio: 'inherit'});
            return result.status ?? 1;
        }
        if (args.length) throw new HarnessLaunchError('This Delimit launch starts a fresh interactive session only. Use project-scoped Delimit handoff recovery; native resume/exec flags are not silently translated.');
        // Match the existing launcher boundary without mutating the parent's
        // environment. A shell hook's stale git routing cannot select a
        // different repository or index for this new interactive session.
        for (const name of GIT_ROUTING_ENV) delete env[name];
        if (id === 'muse' && (env.META_API_KEY || env.MODEL_API_KEY)) throw new HarnessLaunchError('An API-key environment override is present. Refusing an unverified Muse billing route; no credential was printed or changed.');
        const cwd = fs.realpathSync(options.cwd || process.cwd());
        const git = spawn('git', ['-C', cwd, 'rev-parse', '--show-toplevel'], {env, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe']});
        if (git.status !== 0 || !String(git.stdout || '').trim() || cwd === fs.realpathSync(env.HOME || os.homedir())) {
            throw new HarnessLaunchError('Launch from the actual venture repository, not the home directory: cd /path/to/repo && delimit chat --model ' + id);
        }
        const root = fs.realpathSync(String(git.stdout).trim());
        // Check the actual enclosing project chain without reading contents into
        // the outgoing prompt. Client still owns native project-rule loading.
        let dir = cwd;
        while (true) {
            for (const name of ['AGENTS.md', 'CLAUDE.md']) {
                const file = path.join(dir, name);
                if (fs.existsSync(file) && fs.statSync(file).size >= 65536) {
                    throw new HarnessLaunchError('Project rules exceed the native startup bound; refusing truncation.');
                }
            }
            if (dir === root || path.dirname(dir) === dir) break;
            dir = path.dirname(dir);
        }
        const home = env.DELIMIT_HOME || path.join(env.HOME || os.homedir(), '.delimit');
        const packet = bootstrap(cwd, home);
        let nativeArgs;
        if (id === 'muse') {
            checkMuseSettings(env);
            nativeArgs = ['--provider', 'meta', '--model', 'muse-spark-1.3', '--workspace', cwd, packet];
        } else {
            nativeArgs = ['--no-auto-update', '--no-remote', '--no-remote-export', '-i', packet];
        }
        env.DELIMIT_CHAT_RUN_ID = options.chatRunId || env.DELIMIT_CHAT_RUN_ID || '';
        console.log(`[Delimit] Explicit ${id} session; native approvals remain active. No automatic provider fallback. Save a Delimit handoff before exit.`);
        const result = spawn(binary, nativeArgs, {cwd, env, stdio: 'inherit'});
        if (result.error) error('Harness launch failed; no fallback or completion was inferred.');
        return result.status ?? (result.signal === 'SIGINT' ? 130 : 1);
    } catch (err) {
        error(err instanceof HarnessLaunchError ? err.message : 'Harness launch failed; no runtime/configuration details or credentials were disclosed.');
        return 1;
    }
}

function launchHarnessShim(id, options = {}) {
    const args = options.args || [];
    if (!args.length || (args.length === 1 && ['--help', '-h', '--version', '-v', '-V'].includes(args[0]))) {
        return launchExplicitHarness(id, options);
    }
    // Preserve existing native programmatic/auth/resume invocations shadowed
    // by PATH. This is plain transport compatibility, NOT evidence of Delimit
    // governance, subscription billing, data-use clearance, or task approval.
    // Caller-chosen arguments/environment pass unchanged; no added prompt,
    // probe, configuration write, update, login, or provider fallback.
    const error = options.error || (message => console.error(`[Delimit] ${message}`));
    try {
        if (!HARNESS_IDS.includes(id)) throw new Error('Unknown execution harness.');
        const env = {...(options.env || process.env)};
        const binary = resolveBinary(id, env);
        const result = (options.spawnSync || cp.spawnSync)(binary, args, {
            cwd: options.cwd || process.cwd(), env, stdio: 'inherit',
        });
        if (result.error) error('Native harness invocation failed; no fallback or governed completion was inferred.');
        return result.status ?? (result.signal === 'SIGINT' ? 130 : 1);
    } catch (_) {
        error('Native harness invocation unavailable; no credentials or configuration content were disclosed.');
        return 1;
    }
}

function installHarnessShims({delimitHome, packageRoot}) {
    const dir = path.join(delimitHome, 'shims');
    fs.mkdirSync(dir, {recursive: true});
    for (const id of HARNESS_IDS) {
        const file = path.join(dir, id);
        const source = `#!/usr/bin/env node\n// Delimit explicit harness shim\nconst {launchHarnessShim} = require(${JSON.stringify(path.join(packageRoot, 'lib', 'harness-launch.js'))});\nprocess.exitCode = launchHarnessShim(${JSON.stringify(id)}, {args: process.argv.slice(2)});\n`;
        if (fs.existsSync(file) && !fs.readFileSync(file, 'utf8').includes('// Delimit explicit harness shim')) throw new Error(`Refusing to replace an unrecognized ${id} shim.`);
        fs.writeFileSync(file, source, {mode: 0o755});
    }
}

module.exports = {launchExplicitHarness, launchHarnessShim, installHarnessShims, bootstrap, checkMuseSettings, resolveBinary};
