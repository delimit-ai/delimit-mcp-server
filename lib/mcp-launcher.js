'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn, spawnSync } = require('child_process');
const { copyDir, writeInstalledVersionMarker } = require('../bin/delimit-setup');
const pkg = require('../package.json');

// Same pinned set `delimit setup` installs when the bundle has no
// requirements.txt (the published package ships none). Kept in lockstep by
// tests/mcp-subcommand.test.js.
const FALLBACK_PY_DEPS = ['fastmcp==3.2.4', 'pyyaml==6.0.3', 'pydantic==2.12.5', 'packaging==26.0'];

function run(command, args) {
    const result = spawnSync(command, args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
    return result.status === 0 ? result.stdout.trim() : null;
}

function ensureInsideHome(home, target) {
    let current = home;
    const relative = path.relative(home, target);
    for (const component of ['', ...relative.split(path.sep)]) {
        if (component) current = path.join(current, component);
        if (fs.existsSync(current) && fs.lstatSync(current).isSymbolicLink()) {
            throw new Error(`Refusing symlink in Delimit installation path: ${current}`);
        }
    }
}

function ensureMcpInstall() {
    const home = path.join(os.homedir(), '.delimit');
    // The plugin runs its OWN versioned copy of the bundled server. It never
    // reuses or overwrites ~/.delimit/server, which `delimit setup` may have
    // composed with Pro modules. An older server there would not know the
    // requested toolset and would fall back to the full surface. Records
    // stay shared: DELIMIT_HOME is still ~/.delimit.
    const serverDir = path.join(home, 'plugin-server', pkg.version);
    const server = path.join(serverDir, 'ai', 'server.py');
    const requirements = path.join(serverDir, 'requirements.txt');
    // The venv is versioned with the server and belongs to the plugin alone.
    // A shared ~/.delimit/venv would be skipped on upgrade (stale pins under a
    // new server) and is the user's `delimit setup` venv, which the plugin
    // must never modify or delete (2026-09-25 plugin review).
    const venv = path.join(serverDir, 'venv');
    const python = path.join(venv, process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
    const depsMarker = path.join(venv, '.delimit-deps-ok');
    // Symlink guard covers the directories we create or copy into. It must NOT
    // include the venv interpreter: venv's bin/python is always a symlink to
    // the base interpreter, so checking it made every launch after the first
    // fail (found by the 2026-09-25 clean-install test).
    for (const target of [home, path.join(home, 'plugin-server'), serverDir, server, requirements, venv, depsMarker, path.join(home, 'ledger')]) ensureInsideHome(home, target);

    // VERSION is written last, so a copy interrupted part-way is redone.
    if (!fs.existsSync(path.join(serverDir, 'VERSION'))) {
        const bundled = path.join(__dirname, '..', 'gateway');
        if (!fs.existsSync(path.join(bundled, 'ai', 'server.py'))) {
            throw new Error('Bundled Delimit MCP server is missing. Reinstall delimit-cli.');
        }
        fs.rmSync(serverDir, { recursive: true, force: true });
        fs.mkdirSync(home, { recursive: true });
        copyDir(bundled, serverDir);
        writeInstalledVersionMarker(serverDir, pkg.version);
    }
    fs.mkdirSync(path.join(home, 'ledger'), { recursive: true });

    // The marker is written only after pip succeeds, so an interrupted or
    // failed install is rebuilt from scratch on the next launch.
    if (!fs.existsSync(depsMarker)) {
        let systemPython;
        for (const candidate of ['python3', 'python']) {
            const version = run(candidate, ['-c', 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")']);
            if (version && Number(version.split('.')[0]) === 3 && Number(version.split('.')[1]) >= 9) {
                systemPython = candidate;
                break;
            }
        }
        if (!systemPython) throw new Error('Python 3.9+ is required to run Delimit MCP. Install Python and retry.');
        fs.rmSync(venv, { recursive: true, force: true });
        const created = spawnSync(systemPython, ['-m', 'venv', venv], { stdio: ['ignore', 'pipe', 'pipe'] });
        if (created.status !== 0 || !fs.existsSync(python)) {
            throw new Error('Could not create the Delimit Python venv. Check that Python venv support is installed.');
        }
        const pipArgs = fs.existsSync(requirements) ? ['-r', requirements] : FALLBACK_PY_DEPS;
        const installed = spawnSync(python, ['-m', 'pip', 'install', '--quiet', '--no-cache-dir', ...pipArgs], { stdio: ['ignore', 'pipe', 'pipe'] });
        if (installed.status !== 0) {
            fs.rmSync(venv, { recursive: true, force: true });
            throw new Error('Could not install Delimit Python requirements in the venv. Check network access and Python pip.');
        }
        fs.writeFileSync(depsMarker, `${pkg.version}\n`);
    }
    return { home, python, server, serverDir };
}

function toolsetSupported(paths, toolset) {
    if (!toolset || toolset === 'full') return true;
    const probe = spawnSync(paths.python, ['-c',
        'import sys; from ai.tool_metadata import VALID_TOOLSETS; sys.exit(0 if sys.argv[1] in VALID_TOOLSETS else 3)', toolset],
        { cwd: paths.serverDir, env: { ...process.env, PYTHONPATH: paths.serverDir }, stdio: ['ignore', 'ignore', 'ignore'] });
    return probe.status === 0;
}

function launchMcp(toolset) {
    let paths;
    try {
        paths = ensureMcpInstall();
    } catch (error) {
        console.error(`delimit mcp: ${error.message}`);
        process.exitCode = 1;
        return;
    }
    if (!toolsetSupported(paths, toolset)) {
        // Fail closed: an unknown toolset would otherwise expose every tool.
        console.error(`delimit mcp: toolset "${toolset}" is not supported by the bundled server. Update delimit-cli.`);
        process.exitCode = 1;
        return;
    }
    const child = spawn(paths.python, [paths.server], {
        // Keep the caller's working directory: Claude Code starts the server in
        // the user's project, and Delimit keys ledger/handoff records by project.
        // Imports resolve through PYTHONPATH, not cwd.
        cwd: process.cwd(),
        env: { ...process.env, DELIMIT_HOME: paths.home, PYTHONPATH: `${paths.serverDir}${path.delimiter}${path.join(paths.serverDir, 'ai')}`, DELIMIT_TOOLSET: toolset,
            // fastmcp checks PyPI for updates on start by default: an undisclosed
            // network call (found by the clean-install e2e). Keep it off.
            FASTMCP_CHECK_FOR_UPDATES: 'off', FASTMCP_SHOW_SERVER_BANNER: 'false' },
        stdio: 'inherit',
    });
    child.on('error', error => {
        console.error(`delimit mcp: Could not start server: ${error.message}`);
        process.exitCode = 1;
    });
    child.on('exit', (code, signal) => {
        if (signal) process.kill(process.pid, signal);
        else process.exitCode = code === null ? 1 : code;
    });
}

module.exports = { ensureMcpInstall, launchMcp, toolsetSupported, FALLBACK_PY_DEPS };
