/**
 * LED-2095: tests for lib/migration-2092-banner.js — one-time migration
 * banner shown to existing Free-tier users with BYOK deliberation
 * configured. Surfaces the LED-2092 / LED-2093 gateway change (Free-tier
 * BYOK deliberation is now operationally ephemeral).
 *
 * Locks the contract that:
 *   - banner triggers for Free + BYOK users on first call
 *   - banner does NOT trigger for Pro / Premium / Enterprise users
 *   - banner does NOT trigger for Free users WITHOUT BYOK configured
 *   - banner does NOT trigger when the flag file already exists
 *   - flag file is written on trigger and persists across calls
 *   - banner copy matches the locked snapshot (no em-dashes, no founder
 *     identity strings, includes the pricing URL)
 *   - read errors on models.json / license.json fail closed (no banner)
 */

const { describe, it, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const {
    FLAG_FILE_BASENAME,
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
} = require('../lib/migration-2092-banner');

const ORIG_DELIMIT_HOME = process.env.DELIMIT_HOME;
const ORIG_NAMESPACE_ROOT = process.env.DELIMIT_NAMESPACE_ROOT;

let tmpHome;

function setupTmpHome() {
    tmpHome = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-migration-2092-test-'));
    process.env.DELIMIT_HOME = tmpHome;
    delete process.env.DELIMIT_NAMESPACE_ROOT;
}

function teardownTmpHome() {
    try {
        fs.rmSync(tmpHome, { recursive: true, force: true });
    } catch {}
    if (ORIG_DELIMIT_HOME === undefined) delete process.env.DELIMIT_HOME;
    else process.env.DELIMIT_HOME = ORIG_DELIMIT_HOME;
    if (ORIG_NAMESPACE_ROOT === undefined) delete process.env.DELIMIT_NAMESPACE_ROOT;
    else process.env.DELIMIT_NAMESPACE_ROOT = ORIG_NAMESPACE_ROOT;
}

// Helpers -------------------------------------------------------------

function writeModelsConfig(config) {
    fs.writeFileSync(modelsConfigPath(), JSON.stringify(config, null, 2));
}

function writeLicense(license) {
    fs.writeFileSync(licenseFilePath(), JSON.stringify(license, null, 2));
}

function writeByokFreeUser() {
    writeModelsConfig({
        grok: { enabled: true, api_key: 'xai-fake-key-1234', model: 'grok-4-0709' },
    });
    // No license.json — Free tier.
}

// Tests ---------------------------------------------------------------

describe('lib/migration-2092-banner: banner copy', () => {
    it('does NOT contain em-dashes (per LED-2095 brief)', () => {
        const text = bannerText();
        assert.ok(!text.includes('—'), 'banner must not contain em-dashes');
        assert.ok(!text.includes('–'), 'banner must not contain en-dashes either');
    });

    it('does NOT contain founder identity strings', () => {
        // Per feedback_no_jamsons_in_public_artifacts.md — no holdco /
        // founder name strings in any user-facing artifact.
        const text = bannerText().toLowerCase();
        assert.ok(!text.includes('jamsons'));
        assert.ok(!text.includes('holdings'));
    });

    it('mentions the pricing URL for the Pro upsell', () => {
        assert.ok(bannerText().includes('delimit.ai/pricing'));
    });

    it('mentions the Free-tier ephemeral behavior', () => {
        const text = bannerText();
        assert.ok(/Free deliberations/i.test(text));
        assert.ok(/stdout/i.test(text));
        assert.ok(/ephemeral/i.test(text));
    });

    it('mentions that existing transcripts are preserved', () => {
        assert.ok(bannerText().includes('~/.delimit/memory/deliberations/'));
    });

    it('matches the exact locked snapshot', () => {
        const expected = [
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
        assert.equal(bannerText(), expected);
    });
});

describe('lib/migration-2092-banner: hasByokConfigured', () => {
    beforeEach(setupTmpHome);
    afterEach(teardownTmpHome);

    it('returns false when models.json does not exist', () => {
        assert.equal(hasByokConfigured(), false);
    });

    it('returns false when models.json is malformed', () => {
        fs.writeFileSync(modelsConfigPath(), '{ not json');
        assert.equal(hasByokConfigured(), false);
    });

    it('returns false when models.json is an empty object', () => {
        writeModelsConfig({});
        assert.equal(hasByokConfigured(), false);
    });

    it('returns false when no entries are enabled', () => {
        writeModelsConfig({
            grok: { enabled: false, api_key: 'xai-key', model: 'grok-4-0709' },
        });
        assert.equal(hasByokConfigured(), false);
    });

    it('returns false when an entry is enabled but has no api_key', () => {
        writeModelsConfig({
            grok: { enabled: true, api_key: '', model: 'grok-4-0709' },
        });
        assert.equal(hasByokConfigured(), false);
    });

    it('returns true when at least one entry is enabled with an api_key', () => {
        writeModelsConfig({
            grok: { enabled: true, api_key: 'xai-fake-key-1234', model: 'grok-4-0709' },
        });
        assert.equal(hasByokConfigured(), true);
    });

    it('returns true when ANY entry meets the bar (mixed config)', () => {
        writeModelsConfig({
            grok: { enabled: false, api_key: 'xai-key', model: 'grok-4-0709' },
            gemini: { enabled: true, api_key: 'AIza-key', model: 'gemini-2.5-pro' },
            openai: { enabled: true, api_key: '', model: 'gpt-4o' },
        });
        assert.equal(hasByokConfigured(), true);
    });

    it('returns false when models.json is a JSON array', () => {
        fs.writeFileSync(modelsConfigPath(), JSON.stringify(['grok']));
        assert.equal(hasByokConfigured(), false);
    });
});

describe('lib/migration-2092-banner: hasProLicense', () => {
    beforeEach(setupTmpHome);
    afterEach(teardownTmpHome);

    it('returns false when license.json does not exist', () => {
        assert.equal(hasProLicense(), false);
    });

    it('returns false when license.json is malformed', () => {
        fs.writeFileSync(licenseFilePath(), '{ broken');
        assert.equal(hasProLicense(), false);
    });

    it('returns false when valid=false', () => {
        writeLicense({ valid: false, tier: 'pro' });
        assert.equal(hasProLicense(), false);
    });

    it('returns false when valid=true but tier is "free"', () => {
        writeLicense({ valid: true, tier: 'free' });
        assert.equal(hasProLicense(), false);
    });

    it('returns true for valid pro license', () => {
        writeLicense({ valid: true, tier: 'pro' });
        assert.equal(hasProLicense(), true);
    });

    it('returns true for valid premium license', () => {
        writeLicense({ valid: true, tier: 'premium' });
        assert.equal(hasProLicense(), true);
    });

    it('returns true for valid enterprise license', () => {
        writeLicense({ valid: true, tier: 'enterprise' });
        assert.equal(hasProLicense(), true);
    });
});

describe('lib/migration-2092-banner: shouldShowBanner / trigger logic', () => {
    beforeEach(setupTmpHome);
    afterEach(teardownTmpHome);

    it('triggers for Free + BYOK first time (all conditions met)', () => {
        writeByokFreeUser();
        assert.equal(shouldShowBanner(), true);
    });

    it('does NOT trigger for Pro users (tier=pro)', () => {
        writeByokFreeUser();
        writeLicense({ valid: true, tier: 'pro' });
        assert.equal(shouldShowBanner(), false);
    });

    it('does NOT trigger for Premium users', () => {
        writeByokFreeUser();
        writeLicense({ valid: true, tier: 'premium' });
        assert.equal(shouldShowBanner(), false);
    });

    it('does NOT trigger for Enterprise users', () => {
        writeByokFreeUser();
        writeLicense({ valid: true, tier: 'enterprise' });
        assert.equal(shouldShowBanner(), false);
    });

    it('does NOT trigger for Free users WITHOUT BYOK', () => {
        // No models.json at all.
        assert.equal(shouldShowBanner(), false);
    });

    it('does NOT trigger for Free users with disabled BYOK entries', () => {
        writeModelsConfig({
            grok: { enabled: false, api_key: 'xai-key', model: 'grok-4-0709' },
        });
        assert.equal(shouldShowBanner(), false);
    });

    it('does NOT trigger when the flag file already exists', () => {
        writeByokFreeUser();
        // Pre-write the flag file.
        writeShownFlag({ now: '2026-05-08T12:00:00.000Z', version: '4.5.13' });
        assert.equal(shouldShowBanner(), false);
    });
});

describe('lib/migration-2092-banner: writeShownFlag', () => {
    beforeEach(setupTmpHome);
    afterEach(teardownTmpHome);

    it('writes the flag file with shown_at, version, and led', () => {
        writeShownFlag({ now: '2026-05-08T12:00:00.000Z', version: '4.5.14' });
        assert.ok(fs.existsSync(flagFilePath()));
        const data = JSON.parse(fs.readFileSync(flagFilePath(), 'utf-8'));
        assert.equal(data.shown_at, '2026-05-08T12:00:00.000Z');
        assert.equal(data.version, '4.5.14');
        assert.equal(data.led, 'LED-2095');
    });

    it('uses ISO8601 for shown_at when not overridden', () => {
        writeShownFlag();
        const data = JSON.parse(fs.readFileSync(flagFilePath(), 'utf-8'));
        assert.match(data.shown_at, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/);
        assert.ok(!Number.isNaN(Date.parse(data.shown_at)));
    });

    it('creates DELIMIT_HOME if missing', () => {
        fs.rmSync(tmpHome, { recursive: true, force: true });
        writeShownFlag({ now: '2026-05-08T12:00:00.000Z', version: '4.5.14' });
        assert.ok(fs.existsSync(tmpHome));
        assert.ok(fs.existsSync(flagFilePath()));
    });

    it('uses tmp + rename (no .tmp left behind)', () => {
        writeShownFlag();
        const tmp = flagFilePath() + '.tmp';
        assert.ok(!fs.existsSync(tmp));
    });
});

describe('lib/migration-2092-banner: maybeShowMigrationBanner', () => {
    beforeEach(setupTmpHome);
    afterEach(teardownTmpHome);

    it('returns shown=true with banner text on first qualifying call', () => {
        writeByokFreeUser();
        const result = maybeShowMigrationBanner({
            now: '2026-05-08T12:00:00.000Z',
            version: '4.5.14',
        });
        assert.equal(result.shown, true);
        assert.equal(result.text, bannerText());
        assert.ok(fs.existsSync(flagFilePath()));
    });

    it('returns shown=false on the second call (flag-file gates re-fire)', () => {
        writeByokFreeUser();
        const first = maybeShowMigrationBanner({
            now: '2026-05-08T12:00:00.000Z',
            version: '4.5.14',
        });
        assert.equal(first.shown, true);

        const second = maybeShowMigrationBanner({
            now: '2026-05-08T13:00:00.000Z',
            version: '4.5.14',
        });
        assert.equal(second.shown, false);
        assert.equal(second.text, '');
    });

    it('returns shown=false for Pro users without writing the flag', () => {
        writeByokFreeUser();
        writeLicense({ valid: true, tier: 'pro' });
        const result = maybeShowMigrationBanner();
        assert.equal(result.shown, false);
        assert.equal(result.text, '');
        // Flag file MUST NOT be written for Pro users — they have not
        // seen the banner and should not have it suppressed if they
        // ever downgrade to Free in the future.
        assert.ok(!fs.existsSync(flagFilePath()));
    });

    it('returns shown=false for Free users without BYOK and does not write the flag', () => {
        // No models.json, no license.json.
        const result = maybeShowMigrationBanner();
        assert.equal(result.shown, false);
        assert.ok(!fs.existsSync(flagFilePath()));
    });

    it('preserves the flag file across re-runs (file is the source of truth)', () => {
        writeByokFreeUser();
        maybeShowMigrationBanner({
            now: '2026-05-08T12:00:00.000Z',
            version: '4.5.14',
        });
        const firstStat = fs.statSync(flagFilePath());

        // Second call must NOT touch the flag file.
        maybeShowMigrationBanner();
        const secondStat = fs.statSync(flagFilePath());
        assert.equal(firstStat.mtimeMs, secondStat.mtimeMs);
    });
});

describe('lib/migration-2092-banner: hasShownBanner', () => {
    beforeEach(setupTmpHome);
    afterEach(teardownTmpHome);

    it('returns false when the flag file is missing', () => {
        assert.equal(hasShownBanner(), false);
    });

    it('returns true after writeShownFlag', () => {
        writeShownFlag();
        assert.equal(hasShownBanner(), true);
    });

    it('flag file basename is migration_2092.shown', () => {
        assert.equal(FLAG_FILE_BASENAME, 'migration_2092.shown');
    });
});
