const fs = require('fs');
const os = require('os');
const path = require('path');

const KNOWN_WORKSPACES_FILE = path.join(os.homedir(), '.delimit', 'known_workspaces.json');
const WORKSPACE_CACHE_TTL_MS = 6 * 60 * 60 * 1000;
const ACTIVE_VENTURE_FILE = path.join(os.homedir(), '.delimit', 'active_venture.json');

function resolveRepoRoot(startDir = process.cwd()) {
    let current = path.resolve(startDir);
    const seen = new Set();
    const homeDir = os.homedir();

    while (!seen.has(current)) {
        seen.add(current);
        if (
            fs.existsSync(path.join(current, '.git'))
            || (
                current !== homeDir
                && fs.existsSync(path.join(current, '.delimit', 'ledger', 'operations.jsonl'))
            )
        ) {
            return current;
        }
        const parent = path.dirname(current);
        if (parent === current) {
            return null;
        }
        current = parent;
    }
    return null;
}

function readGitHubIdentity(homeDir) {
    const candidates = [
        path.join(homeDir, '.config', 'gh', 'hosts.yml'),
        path.join(homeDir, '.delimit', 'github-user.json')
    ];

    for (const candidate of candidates) {
        if (!fs.existsSync(candidate)) {
            continue;
        }

        const content = fs.readFileSync(candidate, 'utf8');
        const loginMatch = content.match(/user:\s*([^\s]+)/) || content.match(/"login"\s*:\s*"([^"]+)"/);
        if (loginMatch) {
            return loginMatch[1];
        }
    }

    return process.env.GITHUB_USER || process.env.GITHUB_ACTOR || null;
}

function sanitizeSegment(value, fallback) {
    if (!value || typeof value !== 'string') {
        return fallback;
    }
    const cleaned = value.trim().replace(/[^a-zA-Z0-9._-]+/g, '-').replace(/^-+|-+$/g, '');
    return cleaned || fallback;
}

function resolveLedgerRoot(repoGovernanceRoot, delimitHome) {
    const repoLedgerRoot = repoGovernanceRoot ? path.join(repoGovernanceRoot, 'ledger') : null;
    if (repoLedgerRoot && fs.existsSync(path.join(repoLedgerRoot, 'operations.jsonl'))) {
        return {
            ledgerRoot: repoLedgerRoot,
            ledgerScope: 'repo',
        };
    }
    return {
        ledgerRoot: path.join(delimitHome, 'ledger'),
        ledgerScope: 'global',
    };
}

function readJson(filePath, fallback = null) {
    try {
        return JSON.parse(fs.readFileSync(filePath, 'utf-8'));
    } catch {
        return fallback;
    }
}

function writeJson(filePath, value) {
    fs.mkdirSync(path.dirname(filePath), { recursive: true });
    fs.writeFileSync(filePath, JSON.stringify(value, null, 2) + '\n');
}

function countImmediateRepoLedgers(basePath) {
    if (!fs.existsSync(basePath)) {
        return 0;
    }
    let count = 0;
    for (const child of ['ventures', 'apps', 'repos']) {
        const childRoot = path.join(basePath, child);
        if (!fs.existsSync(childRoot)) continue;
        for (const entry of fs.readdirSync(childRoot, { withFileTypes: true })) {
            if (!entry.isDirectory()) continue;
            const ledgerFile = path.join(childRoot, entry.name, '.delimit', 'ledger', 'operations.jsonl');
            if (fs.existsSync(ledgerFile)) {
                count += 1;
            }
        }
    }
    return count;
}

function loadKnownWorkspaces() {
    const data = readJson(KNOWN_WORKSPACES_FILE, { workspaces: [] });
    const workspaces = Array.isArray(data?.workspaces) ? data.workspaces : [];
    return workspaces.filter(item => item && typeof item.path === 'string');
}

function saveKnownWorkspace(candidate) {
    const existing = loadKnownWorkspaces();
    const now = new Date().toISOString();
    const next = existing.filter(item => item.path !== candidate.path);
    next.unshift({
        path: candidate.path,
        source: candidate.source,
        score: candidate.score,
        updatedAt: now,
    });
    writeJson(KNOWN_WORKSPACES_FILE, { workspaces: next.slice(0, 20) });
}

function loadActiveVenture() {
    return readJson(ACTIVE_VENTURE_FILE, null);
}

function saveActiveVenture(entry) {
    writeJson(ACTIVE_VENTURE_FILE, {
        venture: entry.venture,
        repoRoot: entry.repoRoot || null,
        updatedAt: new Date().toISOString(),
    });
}

function scoreWorkspaceCandidate(candidatePath) {
    if (!candidatePath || !fs.existsSync(candidatePath)) {
        return null;
    }
    const venturesPath = path.join(candidatePath, 'ventures');
    const appsPath = path.join(candidatePath, 'apps');
    const reposPath = path.join(candidatePath, 'repos');
    const rootLedger = path.join(candidatePath, '.delimit', 'ledger', 'operations.jsonl');
    const repoLedgerCount = countImmediateRepoLedgers(candidatePath);
    const score =
        (fs.existsSync(venturesPath) ? 40 : 0) +
        (fs.existsSync(appsPath) ? 20 : 0) +
        (fs.existsSync(reposPath) ? 20 : 0) +
        (fs.existsSync(rootLedger) ? 10 : 0) +
        Math.min(repoLedgerCount, 10) * 3;
    return {
        path: candidatePath,
        score,
        repoLedgerCount,
        hasVentures: fs.existsSync(venturesPath),
        hasApps: fs.existsSync(appsPath),
        hasRepos: fs.existsSync(reposPath),
        hasRootLedger: fs.existsSync(rootLedger),
    };
}

function listVentureLedgers(repoBase, delimitHome) {
    const results = [];
    const seen = new Set();

    const addLedger = (ledgerRoot, venture, repoRoot, scope) => {
        const key = `${scope}:${ledgerRoot}`;
        if (seen.has(key)) return;
        seen.add(key);
        results.push({ ledgerRoot, venture, repoRoot, scope });
    };

    const globalLedgerRoot = path.join(delimitHome, 'ledger');
    if (fs.existsSync(path.join(globalLedgerRoot, 'operations.jsonl'))) {
        addLedger(globalLedgerRoot, 'root', null, 'global');
    }

    const scanRoots = [repoBase];
    for (const child of ['ventures', 'apps', 'repos']) {
        const childRoot = path.join(repoBase, child);
        if (fs.existsSync(childRoot)) {
            scanRoots.push(childRoot);
        }
    }

    for (const scanRoot of scanRoots) {
        if (fs.existsSync(scanRoot)) {
            for (const entry of fs.readdirSync(scanRoot, { withFileTypes: true })) {
                if (!entry.isDirectory()) continue;
                const repoRoot = path.join(scanRoot, entry.name);
                const ledgerRoot = path.join(repoRoot, '.delimit', 'ledger');
                const ventureName = path.relative(repoBase, repoRoot) || entry.name;
                if (fs.existsSync(path.join(ledgerRoot, 'operations.jsonl'))) {
                    addLedger(ledgerRoot, ventureName, repoRoot, 'repo');
                }
            }
        }
    }

    return results;
}

function detectRepoBase(repoRoot) {
    const explicit = process.env.DELIMIT_REPO_BASE;
    if (explicit) {
        return explicit;
    }

    const candidates = [];
    const seen = new Set();
    const pushCandidate = (candidatePath, source) => {
        if (!candidatePath || seen.has(candidatePath)) return;
        seen.add(candidatePath);
        const scored = scoreWorkspaceCandidate(candidatePath);
        if (scored && scored.score > 0) {
            candidates.push({ ...scored, source });
        }
    };

    if (repoRoot) {
        let current = path.dirname(repoRoot);
        const stopAt = path.parse(current).root;
        while (current && current !== stopAt) {
            pushCandidate(current, 'ancestor');
            current = path.dirname(current);
        }
    }

    for (const known of loadKnownWorkspaces()) {
        const ageMs = Date.now() - new Date(known.updatedAt || 0).getTime();
        if (ageMs < WORKSPACE_CACHE_TTL_MS) {
            pushCandidate(known.path, 'cache');
        }
    }

    const homeDir = os.homedir();
    for (const candidate of [
        path.join(homeDir, 'projects'),
        path.join(homeDir, 'repos'),
        path.join(homeDir, 'ventures'),
        '/projects',
        '/workspace',
        '/workspaces',
        os.homedir(),
    ]) {
        pushCandidate(candidate, 'heuristic');
    }

    candidates.sort((a, b) => b.score - a.score);
    if (candidates.length > 0) {
        saveKnownWorkspace(candidates[0]);
        return candidates[0].path;
    }

    return repoRoot ? path.dirname(repoRoot) : homeDir;
}

function resolveContinuityContext(options = {}) {
    const homeDir = process.env.DELIMIT_HOME || path.join(os.homedir(), '.delimit');
    const repoRoot = resolveRepoRoot(options.cwd || process.cwd());
    const repoBase = detectRepoBase(repoRoot);
    const repoName = repoRoot ? path.basename(repoRoot) : 'no-repo';
    const osUser = os.userInfo().username;
    const githubUser = readGitHubIdentity(os.homedir());
    const actor = sanitizeSegment(githubUser || osUser, 'unknown-user');
    const requestedScope = sanitizeSegment(options.scope || process.env.DELIMIT_SCOPE || '', '');
    const scope = requestedScope === 'all' ? 'all' : '';
    const venture = scope === 'all'
        ? 'portfolio'
        : sanitizeSegment(options.venture || process.env.DELIMIT_VENTURE || repoName || 'global', 'global');
    const continuityRoot = path.join(homeDir, 'continuity', actor, venture);
    const repoGovernanceRoot = repoRoot ? path.join(repoRoot, '.delimit') : null;
    const ledgerInfo = scope === 'all'
        ? { ledgerRoot: path.join(homeDir, 'ledger'), ledgerScope: 'all' }
        : resolveLedgerRoot(repoGovernanceRoot, homeDir);
    const ventureLedgers = listVentureLedgers(repoBase, homeDir);

    return {
        actor,
        osUser,
        githubUser,
        venture,
        repoRoot,
        repoName,
        delimitHome: homeDir,
        continuityRoot,
        privateStateRoot: homeDir,
        repoGovernanceRoot,
        ledgerRoot: ledgerInfo.ledgerRoot,
        ledgerScope: ledgerInfo.ledgerScope,
        repoBase,
        ventureLedgers,
    };
}

function formatContinuityReport(context) {
    return [
        'Delimit continuity context:',
        `  actor: ${context.actor}`,
        `  osUser: ${context.osUser}`,
        `  githubUser: ${context.githubUser || 'unresolved'}`,
        `  venture: ${context.venture}`,
        `  repoRoot: ${context.repoRoot || 'none'}`,
        `  privateStateRoot: ${context.privateStateRoot}`,
        `  continuityRoot: ${context.continuityRoot}`,
        `  repoGovernanceRoot: ${context.repoGovernanceRoot || 'none'}`,
        `  repoBase: ${context.repoBase}`,
        `  ledgerRoot: ${context.ledgerRoot}`,
        `  ledgerScope: ${context.ledgerScope}`,
        `  ventures: ${context.ventureLedgers?.length || 0}`,
    ].join('\n');
}

module.exports = {
    resolveContinuityContext,
    formatContinuityReport,
    listVentureLedgers,
    resolveRepoRoot,
    loadActiveVenture,
    saveActiveVenture,
};
