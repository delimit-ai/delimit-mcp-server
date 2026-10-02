#!/usr/bin/env node
'use strict';
/**
 * Claude plugin release guard (release lane: docs/claude-plugin-release.md).
 *
 * A small deterministic check, run in CI on pull requests. It is not a
 * release system: it publishes, tags and submits nothing. It only refuses a
 * change to a marketplace plugin that would reach users without a version
 * bump and a provenance row.
 *
 * It is an ACCIDENT guard: it catches honest mistakes, it is not an
 * adversarial boundary. The adversarial controls are the fixed tracked tag,
 * governed review of every PR, the owner's Publish click, Anthropic's scan,
 * and `claude plugin tag` refusing an existing tag. Known out-of-scope cases:
 * alternate launchers or registries, inline mcpServers in plugin.json,
 * edits to an existing provenance row, and the stale-base race (caught by
 * `claude plugin tag`). CI runs this script as it is on the BASE commit, and a
 * PR that changes this script, its test or its CI job fails as
 * "guard changed: needs owner review".
 *
 * Guarded plugins are derived from EVERY entry's `source` in
 * .claude-plugin/marketplace.json, at the base AND at the head (union). Each
 * source must be exactly "./<folder>" (no whitespace, no trailing slash) and
 * metadata.pluginRoot must be unset; a file belongs to a plugin only if its
 * path starts with that exact folder plus "/" (so claude-plugin-panel/ is not
 * claude-plugin/). A changed path that matches a plugin folder only when case
 * is ignored fails.
 *
 * Always checked (offline):
 *   - marketplace.json parses, is named "delimit", lists the plugin "delimit"
 *     at ./claude-plugin, and every entry has a safe relative folder source;
 *   - every listed plugin's plugin.json parses, carries the entry's name and
 *     an X.Y.Z version; its .mcp.json (if any) pins every npx package to one
 *     exact X.Y.Z version; the "delimit" plugin pins delimit-cli;
 *   - the provenance table (docs/claude-plugin-releases.md) has, per plugin,
 *     strictly increasing versions and a row for the current version;
 *   - with a base: no provenance row recorded at the base was removed, and no
 *     plugin listed at the base was removed or renamed;
 *   - no symlink (mode 120000) or submodule gitlink (mode 160000) sits in, or
 *     is, a plugin folder.
 *
 * Checked per plugin that the change touches (a file under its folder, its
 * marketplace entry, or a marketplace-wide field):
 *   (a) its plugin.json version is strictly greater than its base version and
 *       than every version recorded for it at the base;
 *   (b) the provenance table has a row for (plugin, new version);
 *   (c) every pinned npx package exists on npm (`npm view`).
 *
 * The change set is `git diff --no-renames --ignore-submodules=none` from the
 * merge base, and every path of every status counts, so moving a file OUT of
 * a plugin folder is a change to that plugin. A .gitattributes or .gitmodules
 * at the root or in an ancestor of a plugin folder touches every plugin.
 *
 * Usage:
 *   node scripts/claude-plugin-release-guard.js                # invariants only
 *   node scripts/claude-plugin-release-guard.js --base <ref>   # PR mode
 *   ... --offline      skips the npm lookup (reported as a warning, local use only)
 *   ... --root <dir>   repository to check (default: this script's repository)
 *
 * Exit 0 = pass, 1 = guard failure, 2 = usage or git error.
 */

const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const MARKETPLACE = '.claude-plugin/marketplace.json';
const MARKETPLACE_DIR = '.claude-plugin/';
// Kept OUTSIDE every plugin folder on purpose: the directory scans the plugin
// folder, so recording a publication there would look like a new version.
const PROVENANCE = 'docs/claude-plugin-releases.md';
const MARKETPLACE_NAME = 'delimit';
// The plugin listed in the Claude plugin directory.
const LISTED_PLUGIN = 'delimit';
const LISTED_SOURCE = './claude-plugin';
const CLI_PACKAGE = 'delimit-cli';
// Paths of the listed plugin, kept for callers and tests.
const PLUGIN_MANIFEST = 'claude-plugin/.claude-plugin/plugin.json';
const MCP_CONFIG = 'claude-plugin/.mcp.json';

const SEMVER = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/;
const PLUGIN_NAME_RE = /^[a-z0-9][a-z0-9-]*$/;

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

const manifestPath = (folder) => `${folder}/.claude-plugin/plugin.json`;
const mcpPath = (folder) => `${folder}/.mcp.json`;

/** Canonical JSON (sorted keys) so key order alone is never a change. */
function canonical(value) {
    if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
    if (value && typeof value === 'object') {
        return `{${Object.keys(value).sort().map((k) => `${JSON.stringify(k)}:${canonical(value[k])}`).join(',')}}`;
    }
    return JSON.stringify(value);
}

const SOURCE_RE = /^\.\/([A-Za-z0-9._-]+(?:\/[A-Za-z0-9._-]+)*)$/;

/**
 * "./claude-plugin" -> "claude-plugin". Exact match only, no trimming: any
 * other form (whitespace, trailing slash, bare name, "..", absolute, URL or
 * object sources) returns null, which the caller reports as an error.
 */
function normalizeSource(source) {
    if (typeof source !== 'string') return null;
    const m = SOURCE_RE.exec(source);
    if (!m) return null;
    if (m[1].split('/').some((p) => p === '.' || p === '..')) return null;
    return m[1];
}

/** True only for files strictly inside `folder` (exact prefix + "/"). */
function inFolder(file, folder) {
    return file.startsWith(`${folder}/`);
}

/** inFolder, or the folder itself, ignoring case. */
function inFolderAnyCase(file, folder) {
    const f = file.toLowerCase();
    const d = folder.toLowerCase();
    return f === d || f.startsWith(`${d}/`);
}

/** Files that change how git checks out plugin files without being in them. */
const GIT_META_FILES = new Set(['.gitattributes', '.gitmodules']);

/**
 * True when `file` is a .gitattributes or .gitmodules at the repo root or in
 * a strict ancestor folder of `folder` (case ignored).
 */
function gitMetaAbove(file, folder) {
    const lower = file.toLowerCase();
    const name = lower.split('/').pop();
    if (!GIT_META_FILES.has(name)) return false;
    const dir = lower.slice(0, lower.length - name.length); // '' or 'a/b/'
    const target = `${folder.toLowerCase()}/`;
    return dir.length < target.length && target.startsWith(dir);
}

// The guard itself. CI runs the base copy of the script, and a PR that
// changes any of these is not judged by the guard but sent to the owner.
const GUARD_FILES = ['scripts/claude-plugin-release-guard.js', 'tests/claude-plugin-release-guard.test.js'];
const WORKFLOW = '.github/workflows/ci.yml';
const GUARD_JOB = 'claude-plugin-release-guard';

/** Text of the guard job in the workflow (its key line to the next job), or null. */
function guardJobBlock(text) {
    if (typeof text !== 'string') return null;
    const lines = text.split('\n');
    const start = lines.findIndex((l) => l.replace(/\s+$/, '') === `  ${GUARD_JOB}:`);
    if (start < 0) return null;
    let end = start + 1;
    while (end < lines.length && !/^ {0,2}\S/.test(lines[end])) end++;
    return lines.slice(start, end).join('\n').replace(/\s+$/, '');
}

/**
 * Rows of the provenance table: table lines whose first cell is a plugin name
 * and whose second cell is X.Y.Z.
 */
function parseReleases(markdown) {
    const rows = [];
    for (const line of String(markdown || '').split('\n')) {
        const t = line.trim();
        if (!t.startsWith('|')) continue;
        const cells = t.replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim().replace(/`/g, '').trim());
        if (cells.length < 2) continue;
        if (PLUGIN_NAME_RE.test(cells[0]) && parseSemver(cells[1])) rows.push({ plugin: cells[0], version: cells[1], cells });
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

/**
 * Marketplace entries as [{ name, folder, entry }]. `side` labels messages.
 * Pushes problems into errors. Returns null when the file cannot be used.
 */
function marketplacePlugins(text, side, errors) {
    const label = `${MARKETPLACE} (${side})`;
    const market = parseJson(label, text, errors);
    if (!market) return null;
    if (!Array.isArray(market.plugins)) {
        errors.push(`${label}: "plugins" must be an array`);
        return { market, plugins: [] };
    }
    // pluginRoot re-roots bare sources in Claude Code; the guard refuses it.
    if (market.metadata && typeof market.metadata === 'object' && Object.prototype.hasOwnProperty.call(market.metadata, 'pluginRoot')) {
        errors.push(`${label}: metadata.pluginRoot must not be set; the guard cannot check where it points`);
    }
    const plugins = [];
    const seenNames = new Set();
    const seenFolders = new Set();
    market.plugins.forEach((entry, i) => {
        const name = entry && entry.name;
        if (typeof name !== 'string' || !PLUGIN_NAME_RE.test(name)) {
            errors.push(`${label}: plugins[${i}] needs a lowercase name, got ${JSON.stringify(name)}`);
            return;
        }
        const folder = normalizeSource(entry.source);
        if (!folder) {
            errors.push(`${label}: plugin "${name}" source ${JSON.stringify(entry.source)} is not exactly "./<folder>"; the guard cannot check it`);
            return;
        }
        if (seenNames.has(name)) errors.push(`${label}: plugin name "${name}" is listed twice`);
        if (seenFolders.has(folder.toLowerCase())) errors.push(`${label}: folder ${folder}/ is listed twice`);
        seenNames.add(name);
        seenFolders.add(folder.toLowerCase());
        plugins.push({ name, folder, entry });
    });
    return { market, plugins };
}

/** Package specs of npx-launched servers in a parsed .mcp.json. */
function npxSpecs(mcp) {
    const specs = [];
    const servers = mcp && mcp.mcpServers && typeof mcp.mcpServers === 'object' ? mcp.mcpServers : {};
    for (const [server, cfg] of Object.entries(servers)) {
        if (!cfg || !/^npx(\.cmd)?$/.test(String(cfg.command || ''))) continue;
        const args = Array.isArray(cfg.args) ? cfg.args : [];
        const spec = args.find((a) => typeof a === 'string' && !a.startsWith('-'));
        specs.push({ server, spec: spec === undefined ? null : spec });
    }
    return specs;
}

/** "pkg@1.2.3" / "@scope/pkg@1.2.3" -> { pkg, version } (version may be null). */
function splitSpec(spec) {
    const at = spec.lastIndexOf('@');
    if (at <= 0) return { pkg: spec, version: null };
    return { pkg: spec.slice(0, at), version: spec.slice(at + 1) };
}

/** Checks one plugin's manifests at the head. Returns { version, pins }. */
function checkPlugin(p, files, errors) {
    const mPath = manifestPath(p.folder);
    const manifest = parseJson(`${mPath}`, files[mPath], errors);
    let version = null;
    if (manifest) {
        if (manifest.name !== p.name) errors.push(`${mPath}: name must be "${p.name}" (its marketplace entry), got ${JSON.stringify(manifest.name)}`);
        if (!parseSemver(manifest.version)) errors.push(`${mPath}: version must be X.Y.Z, got ${JSON.stringify(manifest.version)}`);
        else version = manifest.version;
    }
    const pins = [];
    const cPath = mcpPath(p.folder);
    const mcpText = files[cPath];
    if (mcpText === null || mcpText === undefined) {
        if (p.name === LISTED_PLUGIN) errors.push(`${cPath}: missing`);
        return { version, pins };
    }
    const mcp = parseJson(cPath, mcpText, errors);
    if (!mcp) return { version, pins };
    for (const { server, spec } of npxSpecs(mcp)) {
        if (!spec) { errors.push(`${cPath}: server "${server}" runs npx with no package`); continue; }
        const { pkg, version: v } = splitSpec(spec);
        if (!v || !parseSemver(v)) {
            errors.push(`${cPath}: server "${server}" must pin ${pkg} to an exact X.Y.Z version, got "${spec}"`);
            continue;
        }
        pins.push({ pkg, version: v });
    }
    if (p.name === LISTED_PLUGIN && pins.filter((x) => x.pkg === CLI_PACKAGE).length !== 1) {
        errors.push(`${cPath}: expected exactly one ${CLI_PACKAGE}@<X.Y.Z> pin`);
    }
    return { version, pins };
}

/**
 * Which plugin folders a change touches. `headPlugins`/`basePlugins` are
 * marketplace entry lists; returns a Map folder -> [reasons].
 */
function touchedPlugins({ changedFiles, headMarket, baseMarket, union }) {
    const touched = new Map();
    const add = (folder, why) => {
        if (!touched.has(folder)) touched.set(folder, []);
        touched.get(folder).push(why);
    };
    for (const file of changedFiles) {
        if (file === PROVENANCE) continue;
        for (const p of union.values()) if (inFolder(file, p.folder)) add(p.folder, file);
        if ([...union.values()].some((p) => gitMetaAbove(file, p.folder))) {
            for (const p of union.values()) add(p.folder, `${file} (git attributes or submodules above a plugin folder)`);
        }
        if (file === MARKETPLACE) {
            const h = headMarket ? headMarket.market : null;
            const b = baseMarket ? baseMarket.market : null;
            const topLevel = (m) => {
                if (!m || typeof m !== 'object') return canonical(m);
                const { plugins, ...rest } = m; // eslint-disable-line no-unused-vars
                return canonical(rest);
            };
            if (topLevel(h) !== topLevel(b)) {
                for (const p of union.values()) add(p.folder, `${MARKETPLACE} (marketplace-wide fields)`);
            }
            const byName = (mk) => new Map(((mk && mk.plugins) || []).map((p) => [p.name, canonical(p.entry)]));
            const hn = byName(headMarket);
            const bn = byName(baseMarket);
            for (const p of union.values()) {
                for (const name of p.names) {
                    if (hn.get(name) !== bn.get(name)) add(p.folder, `${MARKETPLACE} (entry "${name}")`);
                }
            }
        } else if (file.startsWith(MARKETPLACE_DIR)) {
            for (const p of union.values()) add(p.folder, file);
        }
    }
    return touched;
}

/**
 * Pure evaluation.
 *   head = { marketplace, releases, workflow, links, files: { path: text|null } }
 *   base = same shape (links unused), or null when no base was given.
 * `links` lists tree entries with mode 120000 or 160000 as { mode, path }.
 * `files` holds plugin.json and .mcp.json for every folder in the union.
 * `npmVersionExists(pkg, v)` returns true/false or throws (a throw fails
 * closed); null means offline (a warning).
 */
function evaluate({ changedFiles = [], head, base = null, npmVersionExists = null }) {
    const errors = [];
    const warnings = [];
    const headFiles = head.files || {};

    const headMarket = marketplacePlugins(head.marketplace, 'head', errors);
    if (headMarket && headMarket.market) {
        if (headMarket.market.name !== MARKETPLACE_NAME) errors.push(`${MARKETPLACE}: name must be "${MARKETPLACE_NAME}"`);
        const listed = headMarket.plugins.find((p) => p.name === LISTED_PLUGIN);
        if (!listed) errors.push(`${MARKETPLACE}: no plugin entry named "${LISTED_PLUGIN}"`);
        else if (listed.folder !== normalizeSource(LISTED_SOURCE)) errors.push(`${MARKETPLACE}: "${LISTED_PLUGIN}" source must be ${LISTED_SOURCE}`);
    }
    let baseMarket = null;
    if (base && base.marketplace !== null && base.marketplace !== undefined) {
        baseMarket = marketplacePlugins(base.marketplace, 'base', errors);
    }

    // Union of plugin folders, base and head.
    const union = new Map();
    const note = (list, side) => {
        for (const p of (list && list.plugins) || []) {
            if (!union.has(p.folder)) union.set(p.folder, { folder: p.folder, names: new Set(), head: null, base: null });
            const u = union.get(p.folder);
            u.names.add(p.name);
            u[side] = p;
        }
    };
    note(headMarket, 'head');
    note(baseMarket, 'base');

    for (const u of union.values()) {
        if (u.base && !u.head) errors.push(`plugin "${u.base.name}" (${u.folder}/) was removed from ${MARKETPLACE}; removing a listed plugin is an owner decision outside this guard`);
        if (u.base && u.head && u.base.name !== u.head.name) errors.push(`plugin in ${u.folder}/ is named "${u.base.name}" at the base but "${u.head.name}" at the head; renaming a listed plugin is an owner decision outside this guard`);
    }

    // Provenance.
    const rows = parseReleases(head.releases);
    if (head.releases === null || head.releases === undefined) errors.push(`${PROVENANCE}: missing`);
    else if (!rows.length) errors.push(`${PROVENANCE}: no release rows found`);
    const rowsFor = (list, name) => list.filter((r) => r.plugin === name);
    const pluginsInRows = [...new Set(rows.map((r) => r.plugin))];
    for (const name of pluginsInRows) {
        const own = rowsFor(rows, name);
        for (let i = 1; i < own.length; i++) {
            if (compareSemver(own[i - 1].version, own[i].version) >= 0) {
                errors.push(`${PROVENANCE}: ${name} versions must strictly increase (row ${own[i - 1].version} then ${own[i].version})`);
            }
        }
    }
    const baseRows = base ? parseReleases(base.releases) : [];
    for (const r of baseRows) {
        if (!rows.some((h) => h.plugin === r.plugin && h.version === r.version)) errors.push(`${PROVENANCE}: row ${r.plugin} ${r.version} was removed (the record is append-only)`);
    }

    // The guard itself: a PR that changes it goes to the owner.
    if (base) {
        for (const f of GUARD_FILES) if (changedFiles.includes(f)) errors.push(`guard changed: needs owner review (${f})`);
        if (changedFiles.includes(WORKFLOW) && guardJobBlock(head.workflow) !== guardJobBlock(base.workflow)) {
            errors.push(`guard changed: needs owner review (${WORKFLOW} job ${GUARD_JOB})`);
        }
    }

    // Paths that differ from a plugin folder only by case land in it on a
    // case-insensitive checkout.
    for (const file of changedFiles) {
        for (const u of union.values()) {
            if (!inFolder(file, u.folder) && inFolderAnyCase(file, u.folder)) {
                errors.push(`${file}: differs only in case from plugin folder ${u.folder}/`);
            }
        }
    }

    // No symlinks or gitlinks in, at, or above a plugin folder.
    for (const link of head.links || []) {
        const kind = link.mode === '120000' ? 'symlink' : 'submodule (gitlink)';
        for (const u of union.values()) {
            if (inFolderAnyCase(link.path, u.folder) || inFolderAnyCase(u.folder, link.path)) {
                errors.push(`${link.path}: ${kind} in plugin folder ${u.folder}/ is not allowed`);
            }
        }
    }

    // Per-plugin manifests at the head.
    const state = new Map();
    for (const p of (headMarket && headMarket.plugins) || []) {
        const s = checkPlugin(p, headFiles, errors);
        state.set(p.folder, { ...s, name: p.name });
        if (s.version && head.releases !== null && head.releases !== undefined && !rows.some((r) => r.plugin === p.name && r.version === s.version)) {
            errors.push(`${PROVENANCE}: no row for plugin ${p.name} version ${s.version}`);
        }
    }

    // Per-plugin release rules for every touched plugin.
    const touched = base ? touchedPlugins({ changedFiles, headMarket, baseMarket, union }) : new Map();
    const npmCache = new Map();
    for (const [folder, reasons] of touched) {
        const s = state.get(folder);
        if (!s) continue; // removed plugins are reported above
        const { name, version, pins } = s;
        if (!version) continue;
        const why = [...new Set(reasons)].join(', ');
        const baseText = base && base.files ? base.files[manifestPath(folder)] : null;
        if (baseText) {
            let baseVersion = null;
            try { baseVersion = JSON.parse(baseText).version; } catch { /* unreadable base: compare to rows only */ }
            if (parseSemver(baseVersion) && compareSemver(version, baseVersion) <= 0) {
                errors.push(`plugin ${name} changed (${why}) but ${manifestPath(folder)} version ${version} is not greater than the base version ${baseVersion}`);
            }
        }
        const recorded = rowsFor(baseRows, name).map((r) => r.version);
        if (recorded.length) {
            const latest = recorded.reduce((a, b) => (compareSemver(a, b) >= 0 ? a : b));
            if (compareSemver(version, latest) <= 0) {
                errors.push(`plugin ${name} changed but version ${version} is not greater than its latest recorded release ${latest}`);
            }
        }
        for (const pin of pins) {
            const key = `${pin.pkg}@${pin.version}`;
            if (!npmVersionExists) {
                warnings.push(`npm lookup skipped (--offline): ${key} (${name}) not verified`);
                continue;
            }
            if (!npmCache.has(key)) {
                try { npmCache.set(key, { ok: npmVersionExists(pin.pkg, pin.version) === true }); } catch (e) { npmCache.set(key, { err: e.message }); }
            }
            const r = npmCache.get(key);
            if (r.err) errors.push(`npm lookup for ${key} failed: ${r.err}`);
            else if (!r.ok) errors.push(`${mcpPath(folder)}: ${key} does not exist on npm`);
        }
    }

    const plugins = [...union.values()].map((u) => {
        const s = state.get(u.folder) || {};
        return { name: (u.head || u.base).name, folder: u.folder, version: s.version || null, pins: s.pins || [], touched: touched.get(u.folder) || [] };
    });
    return { errors, warnings, plugins, touched };
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
    const r = spawnSync('git', args, { cwd: root, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
    return { ok: r.status === 0, out: r.stdout || '', err: r.stderr || '' };
}

/**
 * Paths changed between two commits, with rename and copy detection OFF and
 * every path of every status included (a rename shows as delete + add).
 */
function changedPaths(root, from, to) {
    const d = git(root, ['-c', 'diff.renames=false', 'diff', '--no-renames', '--no-ext-diff', '--ignore-submodules=none', '--name-status', '-z', from, to]);
    if (!d.ok) return { ok: false, err: d.err };
    const fields = d.out.split('\0');
    const files = new Set();
    for (let i = 0; i < fields.length;) {
        const status = fields[i++];
        if (!status) continue;
        const n = /^[RC]/.test(status) ? 2 : 1;
        for (let k = 0; k < n && i < fields.length; k++) files.add(fields[i++]);
    }
    return { ok: true, files: [...files] };
}

/**
 * Symlink and gitlink entries: of commit `rev`, or of the index when rev is
 * null. Returns { ok, links: [{ mode, path }] }.
 */
function treeLinks(root, rev) {
    const r = rev ? git(root, ['ls-tree', '-r', '-z', '--full-tree', rev]) : git(root, ['ls-files', '-s', '-z']);
    if (!r.ok) return { ok: false, err: r.err };
    const links = [];
    for (const rec of r.out.split('\0')) {
        const tab = rec.indexOf('\t');
        if (tab < 0) continue;
        const mode = rec.slice(0, rec.indexOf(' '));
        if (mode === '120000' || mode === '160000') links.push({ mode, path: rec.slice(tab + 1) });
    }
    return { ok: true, links };
}

function readFileOrNull(root, rel) {
    try { return fs.readFileSync(path.join(root, rel), 'utf8'); } catch { return null; }
}

function folderList(...markets) {
    const folders = new Set();
    for (const text of markets) {
        try {
            const m = JSON.parse(text);
            for (const e of (m && Array.isArray(m.plugins) ? m.plugins : [])) {
                const f = normalizeSource(e && e.source);
                if (f) folders.add(f);
            }
        } catch { /* reported by evaluate() */ }
    }
    return [...folders];
}

function main(argv) {
    let root = path.resolve(__dirname, '..');
    let baseRef = null;
    let offline = false;
    for (let i = 0; i < argv.length; i++) {
        if (argv[i] === '--base') baseRef = argv[++i];
        else if (argv[i] === '--root') root = path.resolve(argv[++i] || '.');
        else if (argv[i] === '--offline') offline = true;
        else { console.error(`unknown argument: ${argv[i]}`); return 2; }
    }
    if (argv.includes('--base') && !baseRef) { console.error('--base needs a ref'); return 2; }

    let head;
    let base = null;
    let changedFiles = [];
    if (baseRef) {
        // PR mode judges the HEAD commit, the same tree the diff describes.
        const mb = git(root, ['merge-base', baseRef, 'HEAD']);
        if (!mb.ok) { console.error(`cannot find a merge base with ${baseRef}: ${mb.err.trim()}`); return 2; }
        const mergeBase = mb.out.trim();
        const diff = changedPaths(root, mergeBase, 'HEAD');
        if (!diff.ok) { console.error(`git diff failed: ${diff.err.trim()}`); return 2; }
        changedFiles = diff.files;
        const showAt = (rev) => (rel) => { const s = git(root, ['show', `${rev}:${rel}`]); return s.ok ? s.out : null; };
        const readHead = showAt('HEAD');
        const readBase = showAt(mergeBase);
        const lt = treeLinks(root, 'HEAD');
        if (!lt.ok) { console.error(`git ls-tree failed: ${lt.err.trim()}`); return 2; }
        head = { marketplace: readHead(MARKETPLACE), releases: readHead(PROVENANCE), workflow: readHead(WORKFLOW), links: lt.links, files: {} };
        base = { marketplace: readBase(MARKETPLACE), releases: readBase(PROVENANCE), workflow: readBase(WORKFLOW), files: {} };
        for (const folder of folderList(head.marketplace, base.marketplace)) {
            for (const rel of [manifestPath(folder), mcpPath(folder)]) {
                head.files[rel] = readHead(rel);
                base.files[rel] = readBase(rel);
            }
        }
    } else {
        const read = (rel) => readFileOrNull(root, rel);
        const lt = treeLinks(root, null);
        if (!lt.ok) { console.error(`git ls-files failed: ${lt.err.trim()}`); return 2; }
        head = { marketplace: read(MARKETPLACE), releases: read(PROVENANCE), links: lt.links, files: {} };
        for (const folder of folderList(head.marketplace)) {
            for (const rel of [manifestPath(folder), mcpPath(folder)]) head.files[rel] = read(rel);
        }
    }

    const res = evaluate({ changedFiles, head, base, npmVersionExists: offline ? null : npmVersionExistsViaCli });
    console.log(`claude plugin release guard: ${res.plugins.length} marketplace plugin(s)`);
    for (const p of res.plugins) {
        const pins = p.pins.map((x) => `${x.pkg}@${x.version}`).join(', ') || 'no npx pin';
        const t = p.touched.length ? `changed: ${[...new Set(p.touched)].join(', ')}` : 'unchanged';
        console.log(`  ${p.name} ${p.version || '?'} (${p.folder}/), ${pins}, ${t}`);
    }
    for (const w of res.warnings) console.log(`WARN  ${w}`);
    for (const e of res.errors) console.log(`FAIL  ${e}`);
    if (res.errors.length) {
        console.log('See docs/claude-plugin-release.md (bump that plugin\'s plugin.json and add its provenance row in the same PR).');
        return 1;
    }
    console.log('PASS');
    return 0;
}

module.exports = {
    PLUGIN_MANIFEST, MCP_CONFIG, MARKETPLACE, PROVENANCE,
    GUARD_FILES, WORKFLOW, GUARD_JOB,
    parseSemver, compareSemver, parseReleases, normalizeSource, inFolder, inFolderAnyCase, gitMetaAbove, guardJobBlock,
    marketplacePlugins, npxSpecs, splitSpec, touchedPlugins, evaluate, changedPaths, treeLinks, main,
};

if (require.main === module) process.exitCode = main(process.argv.slice(2));
