// lib/migration-2092-banner.js
//
// LED-2095: one-time migration banner shown to existing Free-tier users
// who have BYOK deliberation configured. Surfaces the LED-2092 / LED-2093
// gateway behavior change: Free-tier BYOK deliberation is now operationally
// ephemeral (no persistent transcript, no signed attestation, no replay
// URL). Pro-tier behavior is unchanged.
//
// Trigger conditions (ALL must be true):
//   1. ~/.delimit/models.json exists with at least one BYOK entry
//      (entry.enabled === true AND entry.api_key truthy)
//   2. User is on the Free tier (no Pro/Premium/Enterprise license active
//      per the same check used by lib/wrap-engine.js)
//   3. The flag file ~/.delimit/migration_2092.shown does not exist yet
//
// On trigger: print the banner to stdout/stderr (caller's choice — we
// return the banner string and let the caller handle output) and write
// the flag file with `{ shown_at: ISO8601, version: <package version> }`
// so the banner never fires again on the same machine.
//
// Scope: trigger on the next deliberation-adjacent CLI call (deliberate,
// models). Do NOT trigger on every CLI call (would be noise). Do NOT
// trigger for Pro customers (no behavior change for them). Do NOT
// trigger for Free users without BYOK (they had no transcripts to lose).

const fs = require('fs');
const path = require('path');
const { delimitHome, homeSubpath } = require('./delimit-home');

const FLAG_FILE_BASENAME = 'migration_2092.shown';
const MODELS_CONFIG_BASENAME = 'models.json';
const LICENSE_FILE_BASENAME = 'license.json';

// Banner copy — locked. No em-dashes (per LED-2095 brief). No founder
// identity strings. Positive tone. The Pro upsell is a single line at
// the end with the pricing URL; do not expand without coordinated copy
// review.
const BANNER_BODY = [
    '',
    'Free deliberations now print to stdout only.',
    '',
    'Your existing transcripts under ~/.delimit/memory/deliberations/ are preserved.',
    'New deliberations on Free tier are ephemeral by design.',
    '',
    'Pro turns every deliberation into a signed, replayable attestation',
    'with 365-day retention: delimit.ai/pricing',
    '',
].join('\n');

function flagFilePath() {
    return homeSubpath(FLAG_FILE_BASENAME);
}

function modelsConfigPath() {
    return homeSubpath(MODELS_CONFIG_BASENAME);
}

function licenseFilePath() {
    return homeSubpath(LICENSE_FILE_BASENAME);
}

/**
 * Detect whether the user has at least one BYOK model entry configured.
 * Mirrors the truthiness rule used by bin/delimit-cli.js::getModelStatus:
 * `entry.enabled === true AND entry.api_key truthy`.
 */
function hasByokConfigured() {
    const p = modelsConfigPath();
    if (!fs.existsSync(p)) return false;
    let config;
    try {
        config = JSON.parse(fs.readFileSync(p, 'utf-8'));
    } catch {
        return false;
    }
    if (!config || typeof config !== 'object' || Array.isArray(config)) {
        return false;
    }
    for (const entry of Object.values(config)) {
        if (entry && typeof entry === 'object' && entry.enabled && entry.api_key) {
            return true;
        }
    }
    return false;
}

/**
 * Detect whether the user has an active Pro/Premium/Enterprise license.
 * Mirrors the rule used by lib/wrap-engine.js::checkQuota — keep these in
 * lockstep. A missing/malformed license.json means Free.
 */
function hasProLicense() {
    const p = licenseFilePath();
    if (!fs.existsSync(p)) return false;
    try {
        const lic = JSON.parse(fs.readFileSync(p, 'utf-8'));
        if (lic && lic.valid && ['pro', 'premium', 'enterprise'].includes(lic.tier)) {
            return true;
        }
    } catch {
        return false;
    }
    return false;
}

/**
 * Check whether the migration flag file has already been written. Once
 * written, the banner is suppressed forever on this machine (the file is
 * the source of truth — even a corrupt JSON body counts as "shown" so a
 * partially-written flag does not re-trigger the banner).
 */
function hasShownBanner() {
    return fs.existsSync(flagFilePath());
}

/**
 * Should the banner trigger right now? All three conditions must hold:
 * BYOK configured, no Pro license, flag file absent.
 */
function shouldShowBanner() {
    if (hasShownBanner()) return false;
    if (hasProLicense()) return false;
    if (!hasByokConfigured()) return false;
    return true;
}

/**
 * Persist the flag file so the banner never fires again. Best-effort:
 * directory is created at 0700 if missing, file is written at default
 * mode (no secrets in this file — just a timestamp + version).
 *
 * @param {object} [args]
 * @param {string} [args.now]      ISO8601 override for tests
 * @param {string} [args.version]  CLI version string
 */
function writeShownFlag(args) {
    const opts = args || {};
    const home = delimitHome();
    if (!fs.existsSync(home)) {
        fs.mkdirSync(home, { recursive: true, mode: 0o700 });
    }
    const payload = {
        shown_at: opts.now || new Date().toISOString(),
        version: opts.version || resolveCliVersion(),
        led: 'LED-2095',
    };
    const tmpPath = flagFilePath() + '.tmp';
    fs.writeFileSync(tmpPath, JSON.stringify(payload, null, 2));
    fs.renameSync(tmpPath, flagFilePath());
}

function resolveCliVersion() {
    try {
        // package.json sits two levels up from lib/migration-2092-banner.js
        const pkg = require(path.join(__dirname, '..', 'package.json'));
        return pkg.version || 'unknown';
    } catch {
        return 'unknown';
    }
}

/**
 * The exact banner text shown to triggered users. Exposed so tests can
 * snapshot it without re-running the trigger logic.
 */
function bannerText() {
    return BANNER_BODY;
}

/**
 * Composite entry point used by the CLI. If conditions are met, returns
 * the banner string AND writes the flag file. If conditions are not met,
 * returns "" and does not touch the filesystem.
 *
 * The CLI is responsible for printing the returned string. Returning the
 * string (rather than printing here) keeps this module side-effect-light
 * and lets tests assert on the output without capturing stdout.
 *
 * @param {object} [args]
 * @param {string} [args.now]      ISO8601 override for the flag-file
 *                                 timestamp (deterministic tests)
 * @param {string} [args.version]  Override the CLI version string
 * @returns {{ shown: boolean, text: string }}
 */
function maybeShowMigrationBanner(args) {
    if (!shouldShowBanner()) {
        return { shown: false, text: '' };
    }
    try {
        writeShownFlag(args);
    } catch {
        // If we cannot persist the flag, do NOT show the banner —
        // showing it without persisting would re-fire on every call.
        return { shown: false, text: '' };
    }
    return { shown: true, text: bannerText() };
}

module.exports = {
    FLAG_FILE_BASENAME,
    MODELS_CONFIG_BASENAME,
    LICENSE_FILE_BASENAME,
    flagFilePath,
    modelsConfigPath,
    licenseFilePath,
    hasByokConfigured,
    hasProLicense,
    hasShownBanner,
    shouldShowBanner,
    writeShownFlag,
    bannerText,
    maybeShowMigrationBanner,
};
