/**
 * LED-1716: hermetic git helpers for the node test suite.
 *
 * The package checkout's .git/config was repeatedly polluted with
 * `core.bare = true` and `user.email = test@delimit.test` (observed
 * 2026-06-08 and twice 2026-06-09). Root cause: tests ran `git` /
 * `git config` subprocesses whose cwd resolved INTO the package tree
 * (or whose cwd was deleted by teardown, so git walked UP the tree to
 * the nearest `.git` = this checkout). A local `git config` then wrote
 * to the real checkout config instead of an isolated tmp repo.
 *
 * Every git subprocess in a test MUST:
 *   1. pass an explicit `cwd` pointing at a tmp repo, AND
 *   2. run with a sanitized env so it can NEVER touch the package
 *      checkout or the developer's global config:
 *        - GIT_CONFIG_GLOBAL / GIT_CONFIG_SYSTEM -> os.devnull
 *        - GIT_DIR / GIT_WORK_TREE and other inherited GIT_* stripped
 *        - GIT_CEILING_DIRECTORIES set so git cannot walk above the tmp
 *
 * Use `makeTmpGitRepo()` to create an isolated repo, and `gitEnv()` /
 * `hermeticGit()` to run git against it.
 */

const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');
const { execSync } = require('child_process');

/**
 * Build a sanitized environment that prevents any git subprocess from
 * reading or writing the package checkout, global, or system config.
 *
 * @param {string} [ceiling] - directory git must not walk above (the tmp repo)
 * @returns {NodeJS.ProcessEnv}
 */
function gitEnv(ceiling) {
    // Start from the current env, then strip every inherited GIT_* var so a
    // stray GIT_DIR/GIT_WORK_TREE from the runner cannot redirect writes.
    const env = {};
    for (const [k, v] of Object.entries(process.env)) {
        if (k.startsWith('GIT_')) continue;
        env[k] = v;
    }
    // Redirect global + system config to /dev/null so `git config` (without
    // --local) and identity lookups never touch real files.
    env.GIT_CONFIG_GLOBAL = os.devnull;
    env.GIT_CONFIG_SYSTEM = os.devnull;
    // Provide a deterministic identity so commits don't fall back to a
    // global lookup (which would otherwise read the real ~/.gitconfig).
    env.GIT_AUTHOR_NAME = 'delimit-test';
    env.GIT_AUTHOR_EMAIL = 'test@delimit.test';
    env.GIT_COMMITTER_NAME = 'delimit-test';
    env.GIT_COMMITTER_EMAIL = 'test@delimit.test';
    if (ceiling) {
        // git will refuse to discover a repo above `ceiling`, so it can never
        // walk up to the package checkout's .git even if cwd is wrong.
        env.GIT_CEILING_DIRECTORIES = path.dirname(ceiling);
    }
    return env;
}

/**
 * Create an isolated tmp git repo with a sanitized env and an initial commit.
 *
 * @param {object} [opts]
 * @param {string} [opts.prefix] - mkdtemp prefix
 * @param {boolean} [opts.commit=true] - create an initial commit
 * @returns {{ dir: string, env: NodeJS.ProcessEnv, run: (cmd: string, o?: object) => string, cleanup: () => void }}
 */
function makeTmpGitRepo(opts = {}) {
    const { prefix = 'delimit-git-', commit = true } = opts;
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
    const env = gitEnv(dir);
    const run = (cmd, o = {}) =>
        execSync(cmd, {
            cwd: dir,
            env,
            encoding: 'utf-8',
            stdio: ['ignore', 'pipe', 'pipe'],
            ...o,
        }).toString().trim();

    run('git init -q');
    // Identity is also set locally (belt-and-braces with the env vars above)
    // but written into THIS tmp repo's config via the explicit cwd.
    run('git config user.email "test@delimit.test"');
    run('git config user.name "delimit-test"');
    if (commit) {
        fs.writeFileSync(path.join(dir, 'README.md'), '# tmp test repo\n');
        run('git add .');
        run('git commit -qm init');
    }
    const cleanup = () => {
        try { fs.rmSync(dir, { recursive: true, force: true }); } catch { /* ignore */ }
    };
    return { dir, env, run, cleanup };
}

/**
 * Run a single hermetic git command against an explicit tmp dir.
 *
 * @param {string} cmd - e.g. 'git status --porcelain'
 * @param {string} cwd - tmp repo directory (REQUIRED)
 * @param {object} [o] - extra execSync opts
 * @returns {string}
 */
function hermeticGit(cmd, cwd, o = {}) {
    if (!cwd) throw new Error('hermeticGit requires an explicit cwd (a tmp repo)');
    return execSync(cmd, {
        cwd,
        env: gitEnv(cwd),
        encoding: 'utf-8',
        stdio: ['ignore', 'pipe', 'pipe'],
        ...o,
    }).toString().trim();
}

module.exports = { gitEnv, makeTmpGitRepo, hermeticGit };

// Re-export for ad-hoc use
module.exports._crypto = crypto;
