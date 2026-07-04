#!/usr/bin/env node
/**
 * Clean-tree publish gate (DRAFT-D2 / STR-2169, Wave 1).
 *
 * Makes it IMPOSSIBLE to `npm publish` from a dirty working tree, so that
 * uncommitted or untracked code can never ship to customers invisibly.
 *
 * Wired as the FIRST step of `prepublishOnly` in package.json — it runs
 * BEFORE sync-gateway / build-license-core, which intentionally mutate the
 * tree (rsync gateway files, generate the license_core .so). By checking
 * cleanliness first, the gate asserts that the committed state is exactly
 * what will be built and published.
 *
 * In CI the checkout is a fresh clone, so the tree is clean at this point and
 * the gate is a no-op. Locally, a dirty tree blocks the publish.
 *
 * Emergency override: set DELIMIT_ALLOW_DIRTY_PUBLISH=1 to bypass. This is
 * for hotfixes only and prints a loud warning so the bypass is never silent.
 *
 * The core decision logic is exported as a pure function (`evaluateCleanTree`)
 * so it can be unit-tested with a stubbed git-status string.
 */

'use strict';

const { execSync } = require('child_process');
const path = require('path');

const OVERRIDE_ENV = 'DELIMIT_ALLOW_DIRTY_PUBLISH';

/**
 * Pure decision function — no I/O, fully testable.
 *
 * @param {Object} opts
 * @param {string} opts.porcelain  Raw output of `git status --porcelain`.
 * @param {boolean} opts.allowDirty  Whether the emergency override is set.
 * @returns {{ blocked: boolean, exitCode: number, message: string, override: boolean }}
 */
function evaluateCleanTree({ porcelain, allowDirty }) {
  const dirty = typeof porcelain === 'string' && porcelain.trim().length > 0;

  if (!dirty) {
    return {
      blocked: false,
      exitCode: 0,
      override: false,
      message: 'Clean-tree gate: working tree is clean — OK to publish.',
    };
  }

  // Tree is dirty.
  const entries = porcelain
    .split('\n')
    .map((l) => l.trimEnd())
    .filter((l) => l.length > 0);
  const listing = entries.map((e) => `    ${e}`).join('\n');

  if (allowDirty) {
    return {
      blocked: false,
      exitCode: 0,
      override: true,
      message:
        `WARNING: ${OVERRIDE_ENV}=1 — publishing from a DIRTY tree by override.\n` +
        `The following uncommitted/untracked changes will be baked into the package:\n` +
        `${listing}\n` +
        `This bypass is for emergency hotfixes only. Prefer committing first.`,
    };
  }

  return {
    blocked: true,
    exitCode: 1,
    override: false,
    message:
      `PUBLISH BLOCKED — working tree is dirty (${entries.length} change(s)).\n` +
      `Uncommitted or untracked files would ship to customers invisibly:\n` +
      `${listing}\n\n` +
      `Fix: commit or stash your changes, then publish from a clean tree.\n` +
      `Emergency override (hotfix only): ${OVERRIDE_ENV}=1 npm publish`,
  };
}

function readPorcelain(cwd) {
  return execSync('git status --porcelain', {
    cwd,
    encoding: 'utf-8',
  });
}

function main() {
  // Package dir is the parent of scripts/.
  const pkgDir = path.resolve(__dirname, '..');

  let porcelain;
  try {
    porcelain = readPorcelain(pkgDir);
  } catch (err) {
    // Not a git repo (e.g. installed tarball) — nothing to gate. Do not block.
    console.log(
      'Clean-tree gate: not a git working tree — skipping (nothing to gate).'
    );
    process.exit(0);
  }

  const allowDirty = process.env[OVERRIDE_ENV] === '1';
  const result = evaluateCleanTree({ porcelain, allowDirty });

  if (result.blocked) {
    console.error('');
    console.error(result.message);
    console.error('');
    process.exit(result.exitCode);
  }

  console.log(result.message);
  process.exit(0);
}

if (require.main === module) {
  main();
}

module.exports = { evaluateCleanTree, OVERRIDE_ENV };
