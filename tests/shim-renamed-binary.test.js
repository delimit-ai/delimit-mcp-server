const { describe, it, before, after } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');

/**
 * LED-4386: the governance shim must resolve a renamed real binary
 * (<tool>-real) on BOTH launch paths.
 *
 * The non-tty fast path always did. The interactive path (taken when stdout
 * is a terminal, which is also how the deliberation panel spawns CLIs under
 * a pty) only looked for a binary named <tool>, so a host whose only binary
 * was /usr/bin/claude-real printed the banner and exited 127. Shipped in
 * 4.18.0; silenced the panel's Claude seat until hotfixed.
 *
 * The template lives inline in bin/delimit-setup.js; we render it exactly
 * as setup does (same template literal, same substitution) rather than
 * running the whole setup against a throwaway HOME.
 */

const SETUP_SRC = fs.readFileSync(path.join(__dirname, '..', 'bin', 'delimit-setup.js'), 'utf-8');

function renderShim(toolName, displayName) {
    const start = SETUP_SRC.indexOf('const shimTemplate = (toolName, displayName) => `');
    assert.ok(start > 0, 'shim template not found in bin/delimit-setup.js');
    const bodyStart = SETUP_SRC.indexOf('`', start) ;
    const bodyEnd = SETUP_SRC.indexOf('\n`;', bodyStart);
    assert.ok(bodyEnd > bodyStart, 'shim template end not found');
    const literal = SETUP_SRC.slice(bodyStart, bodyEnd + 2); // includes closing backtick
    // eslint-disable-next-line no-new-func
    return new Function('toolName', 'displayName', `return ${literal};`)(toolName, displayName);
}

const HAS_SCRIPT = spawnSync('sh', ['-c', 'command -v script'], { encoding: 'utf-8' }).status === 0;

// The template searches fixed system locations (/usr/bin, /usr/local/bin)
// before $HOME. On a developer box that really has /usr/bin/claude(-real),
// those win over the sandbox stub, so the two launch-path assertions are
// only meaningful on a host with no system-level claude — which CI is.
const SYSTEM_CLAUDE = ['/usr/bin/claude', '/usr/bin/claude-real', '/usr/local/bin/claude', '/usr/local/bin/claude-real']
    .filter((p) => fs.existsSync(p));
const SKIP_LAUNCH = SYSTEM_CLAUDE.length ? `system claude present (${SYSTEM_CLAUDE.join(', ')}) shadows the sandbox stub` : false;

describe('shim: renamed <tool>-real binary is found on every launch path (LED-4386)', () => {
    let home, binDir, shimPath;

    before(() => {
        home = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-shim-real-'));
        // The ONLY binary on this host is ~/.local/bin/claude-real — no `claude` anywhere.
        binDir = path.join(home, '.local', 'bin');
        fs.mkdirSync(binDir, { recursive: true });
        fs.writeFileSync(path.join(binDir, 'claude-real'), '#!/bin/sh\necho "REAL-OK $*"\nexit 0\n');
        fs.chmodSync(path.join(binDir, 'claude-real'), '755');
        const shimsDir = path.join(home, '.delimit', 'shims');
        fs.mkdirSync(shimsDir, { recursive: true });
        shimPath = path.join(shimsDir, 'claude');
        fs.writeFileSync(shimPath, renderShim('claude', 'Claude'));
        fs.chmodSync(shimPath, '755');
    });

    after(() => { try { fs.rmSync(home, { recursive: true, force: true }); } catch {} });

    // A minimal PATH: the shim dir first, then coreutils — and no directory
    // that could contain a real `claude`.
    const env = () => ({
        HOME: home,
        PATH: `${path.join(home, '.delimit', 'shims')}:/usr/bin:/bin`,
        DELIMIT_QUIET: 'true',
        DELIMIT_HOME: path.join(home, '.delimit'),
    });

    it('non-tty (pipe) launch execs claude-real', { skip: SKIP_LAUNCH }, () => {
        const r = spawnSync(shimPath, ['-p', 'hi'], { env: env(), encoding: 'utf-8', timeout: 20000 });
        assert.strictEqual(r.status, 0, `exit ${r.status}; stderr: ${r.stderr}`);
        assert.match(r.stdout, /REAL-OK -p hi/);
    });

    it('interactive (pty) launch also reaches claude-real instead of exiting 127', { skip: SKIP_LAUNCH || (!HAS_SCRIPT && 'util-linux `script` not available') }, () => {
        // `script` allocates a pty so the shim takes the interactive branch,
        // exactly like the deliberation engine's CLI spawn.
        const r = spawnSync('script', ['-qec', `${shimPath} -p hi`, '/dev/null'], { env: env(), encoding: 'utf-8', timeout: 30000 });
        assert.strictEqual(r.status, 0, `exit ${r.status}; output: ${r.stdout} ${r.stderr}`);
        assert.match(r.stdout, /REAL-OK -p hi/);
        assert.doesNotMatch(r.stdout + r.stderr, /not found in PATH/);
    });

    it('still reports a clear 127 when neither claude nor claude-real exists', { skip: SKIP_LAUNCH }, () => {
        const bare = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-shim-none-'));
        try {
            const r = spawnSync(shimPath, ['-p', 'hi'], {
                env: { ...env(), HOME: bare, PATH: `${path.join(home, '.delimit', 'shims')}:/bin` },
                encoding: 'utf-8', timeout: 20000,
            });
            assert.strictEqual(r.status, 127);
            assert.match(r.stderr, /claude not found in PATH/);
        } finally { try { fs.rmSync(bare, { recursive: true, force: true }); } catch {} }
    });

    it('template emits the -real fallback on the interactive path for every shimmed tool', () => {
        for (const tool of ['claude', 'codex', 'gemini', 'antigravity', 'agy']) {
            const shim = renderShim(tool, tool);
            const interactive = shim.slice(shim.indexOf('delimit_run_and_exit() {'));
            assert.match(interactive, new RegExp(`/usr/bin/${tool}-real`), `${tool}: no -real fallback on the interactive path`);
        }
    });
});
