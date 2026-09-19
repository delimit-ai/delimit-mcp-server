#!/usr/bin/env node
/**
 * LED-5478: make version drift IMPOSSIBLE on the bump path, not merely detected.
 *
 * WHY
 * ---
 * `npm version` moves package.json and package-lock.json and nothing else.
 * Three other fields carry the package version and it moved none of them:
 *
 *   gateway/VERSION              (the bundled MCP server's self-reported version)
 *   server.json .version         (the MCP Registry record)
 *   server.json .packages[0].version
 *
 * Each stale one failed a DIFFERENT publish-workflow step, so fixing the one
 * CI named just moved the failure one step later and burned the next tag:
 *
 *   v4.19.3  failed to publish
 *   v4.19.4  "Committed bundle is STALE"          - gateway/VERSION at 4.19.3
 *   v4.19.5  "server.json ... do not match tag"   - server.json at 4.19.2
 *
 * Detection was tried first and reviewed twice. Both panels landed on the same
 * point: a check that enumerates sources cannot cover one it does not know,
 * and a check that parses publish.yml only sees the gates written as inline
 * `require()` expressions — the gateway/VERSION gate is a bundle-sync
 * assertion and is invisible to it. So detection is a backstop, and THIS is
 * the fix: run on the `version` lifecycle, write every source from the
 * authoritative one, and stage them into the same commit as the bump.
 *
 * Authoritative source is package.json, which npm has already updated by the
 * time this runs.
 *
 * Idempotent, and safe to run by hand (`node scripts/sync-version-sources.js`)
 * to repair a tree that drifted before this existed.
 *
 * HAZARD when running this OUTSIDE a real `npm version` commit — read before
 * you test it by hand, because it already bit once. The `git add` below leaves
 * these paths STAGED. If you then restore the working tree some other way (a
 * `cp` from a backup, a checkout of one file), the INDEX still holds the value
 * this script wrote while the worktree holds the restored one. A later
 * `git commit` of unrelated paths will sweep the stale STAGED blobs in, and
 * `npm test` will NOT notice: the tests read the working tree, not the index.
 * That is exactly how one commit went out carrying 4.19.7 while the worktree
 * (and the local suite) read a perfectly consistent 4.19.6 — CI, which checks
 * out the commit, was the first thing to see it.
 *
 * So: after running this by hand, always `git status` and re-stage or restore
 * BOTH the worktree and the index together.
 */

'use strict';

const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const p = (rel) => path.join(ROOT, rel);

function readJson(rel) {
  return JSON.parse(fs.readFileSync(p(rel), 'utf8'));
}

/** Write JSON back preserving 2-space indent + trailing newline (npm's own style). */
function writeJson(rel, value) {
  fs.writeFileSync(p(rel), `${JSON.stringify(value, null, 2)}\n`);
}

const version = readJson('package.json').version;
if (!/^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$/.test(version)) {
  console.error(`sync-version-sources: package.json version is not plain semver: ${version}`);
  process.exit(1);
}

const changed = [];

// gateway/VERSION — plain text marker.
{
  const rel = 'gateway/VERSION';
  const current = fs.existsSync(p(rel)) ? fs.readFileSync(p(rel), 'utf8').trim() : null;
  if (current !== version) {
    fs.writeFileSync(p(rel), `${version}\n`);
    changed.push(`${rel}: ${current} -> ${version}`);
  }
}

// server.json — carries the version TWICE. Missing either one fails the
// publish gate, so both are written, not just the top-level field.
{
  const rel = 'server.json';
  const srv = readJson(rel);
  const before = [srv.version, srv.packages && srv.packages[0] && srv.packages[0].version];
  let dirty = false;
  if (srv.version !== version) {
    srv.version = version;
    dirty = true;
  }
  if (!Array.isArray(srv.packages) || srv.packages.length === 0) {
    console.error('sync-version-sources: server.json has no packages[] entry to update');
    process.exit(1);
  }
  if (srv.packages[0].version !== version) {
    srv.packages[0].version = version;
    dirty = true;
  }
  if (dirty) {
    writeJson(rel, srv);
    changed.push(`${rel}: ${before.join(' / ')} -> ${version} / ${version}`);
  }
}

if (changed.length === 0) {
  console.log(`sync-version-sources: all version sources already at ${version}`);
} else {
  console.log(`sync-version-sources: aligned to ${version}`);
  for (const line of changed) console.log(`  ${line}`);
}

// Stage them so they land in the SAME commit as the bump. npm runs this on the
// `version` lifecycle AFTER writing package.json and BEFORE committing, but it
// only commits what is staged — an unstaged gateway/VERSION is exactly how
// v4.19.4 shipped a bump without its marker.
//
// FAIL CLOSED when a git repo is present. An earlier cut swallowed the staging
// failure on the reasoning that "the files are already correct on disk" — but
// that is precisely the v4.19.4 condition: files correct in the worktree,
// absent from the commit, bump ships without them. Blocked in review of PR #230
// for exactly that. If we are in a repo and cannot stage, the bump must abort
// loudly rather than proceed toward a partial commit.
//
// The no-git case IS legitimate (an exported tarball, a vendored copy), and is
// the only case tolerated. `npm version --no-git-tag-version` still runs inside
// a repo and still stages fine — it simply makes no commit — so it is NOT an
// exemption here.
let inGitRepo = true;
try {
  execFileSync('git', ['rev-parse', '--git-dir'], { cwd: ROOT, stdio: 'ignore' });
} catch {
  inGitRepo = false;
}

if (!inGitRepo) {
  console.log('sync-version-sources: not a git checkout — files written, nothing to stage');
} else {
  try {
    execFileSync('git', ['add', '--', 'gateway/VERSION', 'server.json'], {
      cwd: ROOT,
      stdio: 'ignore',
    });
  } catch (err) {
    console.error(
      'sync-version-sources: FAILED to stage gateway/VERSION and server.json in a git ' +
        'checkout. Aborting rather than letting the bump commit without them — that is ' +
        'the exact failure that burned v4.19.4.\n' +
        `  ${err && err.message ? err.message : err}`,
    );
    process.exit(1);
  }
}
