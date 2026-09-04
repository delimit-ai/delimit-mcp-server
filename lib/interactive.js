'use strict';

/**
 * Shared "may I prompt the user?" decision for every CLI command.
 *
 * Fresh-install test 2026-09-02: `delimit scan` exited 130 on a closed stdin
 * and `delimit init --preset default` still asked the compliance-template and
 * "Join the beta?" questions under CI=1. Every prompt site must consult this
 * helper and fall back to its documented default (the same behaviour as the
 * existing `--yes` flag) when the answer is false.
 *
 * Non-interactive when ANY of:
 *   - stdin or stdout is not a TTY (piped, closed, spawned by a script)
 *   - CI is set (GitHub Actions, GitLab, Jenkins, etc. all set CI=1/true)
 *   - DELIMIT_NON_INTERACTIVE is set (explicit opt-out for wrappers)
 *   - the caller passed { yes: true } (the command's own --yes flag)
 *
 * @param {{ yes?: boolean }} [opts]
 * @returns {boolean}
 */
function isInteractive(opts = {}) {
    if (opts && opts.yes) return false;
    const env = process.env;
    if (env.DELIMIT_NON_INTERACTIVE && !['0', 'false', ''].includes(String(env.DELIMIT_NON_INTERACTIVE).toLowerCase())) return false;
    if (env.CI && !['0', 'false', ''].includes(String(env.CI).toLowerCase())) return false;
    if (!process.stdin || !process.stdin.isTTY) return false;
    if (!process.stdout || !process.stdout.isTTY) return false;
    return true;
}

module.exports = { isInteractive };
