#!/usr/bin/env node
'use strict';
/**
 * Claude plugin release guard (release lane: docs/claude-plugin-release.md).
 *
 * A small deterministic check, run in CI on pull requests. It is not a
 * release system: it publishes, tags and submits nothing. It only refuses a
 * change to the plugin that would reach users without a version bump and a
 * provenance row.
 *
 * Always checked (offline):
 *   - plugin.json, marketplace.json and .mcp.json parse; plugin and
 *     marketplace are named "delimit"; the marketplace points at ./claude-plugin.
 *   - .mcp.json pins delimit-cli to one exact X.Y.Z version.
 *   - the provenance table (docs/claude-plugin-releases.md) has strictly
 *     increasing versions and a row for the current plugin.json version.
 *   - with a base: no provenance row recorded at the base was removed.
 *
 * Checked when the change touches claude-plugin/** or .claude-plugin/**
 * (other than the provenance file itself):
 *   (a) plugin.json version is strictly greater than the base plugin.json
 *       version and than every version already recorded at the base;
 *   (b) the provenance table has a row for the new version;
 *   (c) the pinned delimit-cli version exists on npm (`npm view`);
 *   (d) the manifest checks above.
 *
 * Usage:
 *   node scripts/claude-plugin-release-guard.js                # invariants only
 *   node scripts/claude-plugin-release-guard.js --base <ref>   # PR mode
 *   ... --offline   skips the npm lookup (reported as a warning, local use only)
 *
 * Exit 0 = pass, 1 = guard failure, 2 = usage or git error.
 */

const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const PLUGIN_MANIFEST = 'claude-plugin/.claude-plugin/plugin.json';
const MCP_CONFIG = 'claude-plugin/.mcp.json';
const MARKETPLACE = '.claude-plugin/marketplace.json';
// Kept OUTSIDE claude-plugin/ on purpose: the directory scans that folder, so
// recording a publication there would itself look like a new plugin version.
const PROVENANCE = 'docs/claude-plugin-releases.md';
const PLUGIN_NAME = 'delimit';
const MARKETPLACE_SOURCE = './claude-plugin';
const CLI_PACKAGE = 'delimit-cli';

const SEMVER = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/;

function parseSemver(value) {
    const m = SEMVER.exec(String(value === undefined ? '' : value).trim());
    return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
}

function compareSemver(a, b) {
    const pa = parseSemver(a);
    const pb = parseSemver(b);
    if (!pa || !pb) throw new Error(`not an X.Y.Z version: ${!pa ? a : b}`);
    for (let i = 0; i < 3; i++) if (pa[i] !== pb[i]) return pa[i] < pb[i] ? -1 : 1;
    return 0;
}

/** Rows of the provenance table: any table line whose first cell is X.Y.Z. */
function parseReleases(markdown) {
    const rows = [];
    for (const line of String(markdown || '').split('\n')) {
        const t = line.trim();
        if (!t.startsWith('|')) continue;
        const cells = t.replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());
        const version = cells[0].replace(/`/g, '').trim();
        if (parseSemver(version)) rows.push({ version, cells });
    }
    return rows;
}

function parseJson(label, text, errors) {
    if (text === null || text === undefined) {
        errors.push(`${label}: missing`);
        return null;
    }
    try {
        return JSON.parse(text);
    } catch (e) {
        errors.push(`${label}: invalid JSON (${e.message})`);
        return null;
    }
}

/** Returns { plugin, pin } and pushes manifest problems into errors. */
function checkManifests(files, errors) {
    const plugin = parseJson(PLUGIN_MANIFEST, files.plugin, errors);
    const market = parseJson(MARKETPLACE, files.marketplace, errors);
    const mcp = parseJson(MCP_CONFIG, files.mcp, errors);
    if (plugin) {
        if (plugin.name !== PLUGIN_NAME) errors.push(`${PLUGIN_MANIFEST}: name must be "${PLUGIN_NAME}"`);
        if (!parseSemver(plugin.version)) errors.push(`${PLUGIN_MANIFEST}: version must be X.Y.Z, got ${JSON.stringify(plugin.version)}`);
    }
    if (market) {
        if (market.name !== PLUGIN_NAME) errors.push(`${MARKETPLACE}: name must be "${PLUGIN_NAME}"`);
        const entry = Array.isArray(market.plugins) ? market.plugins.find((p) => p && p.name === PLUGIN_NAME) : null;
        if (!entry) errors.push(`${MARKETPLACE}: no plugin entry named "${PLUGIN_NAME}"`);
        else if (entry.source !== MARKETPLACE_SOURCE) errors.push(`${MARKETPLACE}: "${PLUGIN_NAME}" source must be ${MARKETPLACE_SOURCE}`);
    }
    let pin = null;
    if (mcp) {
        const args = mcp.mcpServers && mcp.mcpServers[PLUGIN_NAME] && mcp.mcpServers[PLUGIN_NAME].args;
        const specs = Array.isArray(args) ? args.filter((a) => typeof a === 'string' && a.startsWith(`${CLI_PACKAGE}@`)) : [];
        if (specs.length !== 1) {
            errors.push(`${MCP_CONFIG}: expected exactly one ${CLI_PACKAGE}@<version> argument`);
        } else {
            const v = specs[0].slice(CLI_PACKAGE.length + 1);
            if (!parseSemver(v)) errors.push(`${MCP_CONFIG}: ${CLI_PACKAGE} must be pinned to an exact X.Y.Z version, got "${v}"`);
            else pin = v;
        }
    }
    return { plugin, pin };
}

function inPluginScope(file) {
    return (file.startsWith('claude-plugin/') || file.startsWith('.claude-plugin/')) && file !== PROVENANCE;
}

/**
 * Pure evaluation. `head` and `base` hold file texts (null = absent);
 * `base` is null when no base was given. `npmVersionExists(pkg, v)` returns
 * true/false or throws (a throw fails closed).
 */
function evaluate({ changedFiles = [], head, base = null, npmVersionExists = null }) {
    const errors = [];
    const warnings = [];
    const { plugin, pin } = checkManifests(head, errors);

    const rows = parseReleases(head.releases);
    if (head.releases === null || head.releases === undefined) errors.push(`${PROVENANCE}: missing`);
    else if (!rows.length) errors.push(`${PROVENANCE}: no release rows found`);
    for (let i = 1; i < rows.length; i++) {
        if (compareSemver(rows[i - 1].version, rows[i].version) >= 0) {
            errors.push(`${PROVENANCE}: versions must strictly increase (row ${rows[i - 1].version} then ${rows[i].version})`);
        }
    }
    const headVersion = plugin && parseSemver(plugin.version) ? plugin.version : null;
    if (headVersion && rows.length && !rows.some((r) => r.version === headVersion)) {
        errors.push(`${PROVENANCE}: no row for plugin version ${headVersion}`);
    }

    const baseRows = base ? parseReleases(base.releases) : [];
    for (const r of baseRows) {
        if (!rows.some((h) => h.version === r.version)) errors.push(`${PROVENANCE}: row ${r.version} was removed (the record is append-only)`);
    }

    const scoped = changedFiles.filter(inPluginScope);
    if (scoped.length && headVersion) {
        if (base && base.plugin) {
            let baseVersion = null;
            try { baseVersion = JSON.parse(base.plugin).version; } catch { /* unreadable base: compare to rows only */ }
            if (parseSemver(baseVersion) && compareSemver(headVersion, baseVersion) <= 0) {
                errors.push(`plugin files changed (${scoped.join(', ')}) but ${PLUGIN_MANIFEST} version ${headVersion} is not greater than the base version ${baseVersion}`);
            }
        }
        const recorded = baseRows.map((r) => r.version);
        if (recorded.length) {
            const latest = recorded.reduce((a, b) => (compareSemver(a, b) >= 0 ? a : b));
            if (compareSemver(headVersion, latest) <= 0) {
                errors.push(`plugin files changed but version ${headVersion} is not greater than the latest recorded release ${latest}`);
            }
        }
        if (pin) {
            if (!npmVersionExists) {
                warnings.push(`npm lookup skipped (--offline): ${CLI_PACKAGE}@${pin} not verified`);
            } else {
                let ok = false;
                try { ok = npmVersionExists(CLI_PACKAGE, pin) === true; } catch (e) {
                    errors.push(`npm lookup for ${CLI_PACKAGE}@${pin} failed: ${e.message}`);
                    ok = null;
                }
                if (ok === false) errors.push(`${MCP_CONFIG}: ${CLI_PACKAGE}@${pin} does not exist on npm`);
            }
        }
    }
    return { errors, warnings, scoped, headVersion, pin };
}

function npmVersionExistsViaCli(pkg, version) {
    const r = spawnSync('npm', ['view', `${pkg}@${version}`, 'version', '--json'], { encoding: 'utf8', timeout: 60000 });
    if (r.error) throw r.error;
    const out = (r.stdout || '').trim();
    if (r.status !== 0) {
        if (/E404/.test(out + (r.stderr || ''))) return false;
        throw new Error(`npm view exited ${r.status}`);
    }
    if (!out) return false;
    const parsed = JSON.parse(out);
    return Array.isArray(parsed) ? parsed.includes(version) : parsed === version;
}

function git(root, args) {
    const r = spawnSync('git', args, { cwd: root, encoding: 'utf8' });
    return { ok: r.status === 0, out: r.stdout || '', err: r.stderr || '' };
}

function readFileOrNull(root, rel) {
    try { return fs.readFileSync(path.join(root, rel), 'utf8'); } catch { return null; }
}

function main(argv) {
    const root = path.resolve(__dirname, '..');
    let baseRef = null;
    let offline = false;
    for (let i = 0; i < argv.length; i++) {
        if (argv[i] === '--base') baseRef = argv[++i];
        else if (argv[i] === '--offline') offline = true;
        else { console.error(`unknown argument: ${argv[i]}`); return 2; }
    }
    if (argv.includes('--base') && !baseRef) { console.error('--base needs a ref'); return 2; }

    const head = {
        plugin: readFileOrNull(root, PLUGIN_MANIFEST),
        marketplace: readFileOrNull(root, MARKETPLACE),
        mcp: readFileOrNull(root, MCP_CONFIG),
        releases: readFileOrNull(root, PROVENANCE),
    };
    let base = null;
    let changedFiles = [];
    if (baseRef) {
        const mb = git(root, ['merge-base', baseRef, 'HEAD']);
        if (!mb.ok) { console.error(`cannot find a merge base with ${baseRef}: ${mb.err.trim()}`); return 2; }
        const mergeBase = mb.out.trim();
        const diff = git(root, ['diff', '--name-only', `${mergeBase}`, 'HEAD']);
        if (!diff.ok) { console.error(`git diff failed: ${diff.err.trim()}`); return 2; }
        changedFiles = diff.out.split('\n').filter(Boolean);
        const show = (rel) => { const s = git(root, ['show', `${mergeBase}:${rel}`]); return s.ok ? s.out : null; };
        base = { plugin: show(PLUGIN_MANIFEST), releases: show(PROVENANCE) };
    }

    const res = evaluate({ changedFiles, head, base, npmVersionExists: offline ? null : npmVersionExistsViaCli });
    const scope = res.scoped.length ? `${res.scoped.length} plugin file(s) changed` : 'no plugin files changed';
    console.log(`claude-plugin release guard: plugin ${res.headVersion || '?'}, ${CLI_PACKAGE}@${res.pin || '?'}, ${scope}`);
    for (const w of res.warnings) console.log(`WARN  ${w}`);
    for (const e of res.errors) console.log(`FAIL  ${e}`);
    if (res.errors.length) {
        console.log('See docs/claude-plugin-release.md (bump plugin.json and add a provenance row in the same PR).');
        return 1;
    }
    console.log('PASS');
    return 0;
}

module.exports = {
    PLUGIN_MANIFEST, MCP_CONFIG, MARKETPLACE, PROVENANCE,
    parseSemver, compareSemver, parseReleases, checkManifests, evaluate, inPluginScope,
};

if (require.main === module) process.exitCode = main(process.argv.slice(2));
