const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { getDelimitSection } = require('../lib/delimit-template');
const { upsertDelimitSection } = require('../bin/delimit-setup');
const { bannerText } = require('../lib/migration-2092-banner');

const root = path.resolve(__dirname, '..');
test('vault status output contains no encryption claim', () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-vault-status-'));
    try {
        const result = spawnSync(process.execPath, [path.join(root, 'bin/delimit-cli.js'), 'vault', 'status', '--verbose'], {
            cwd: root, env: { ...process.env, HOME: home, DELIMIT_HOME: path.join(home, '.delimit') }, encoding: 'utf8', timeout: 15000,
        });
        // The managed sandbox denies nested exec; CI checks the actual CLI output.
        if (result.error && result.error.code === 'EPERM') {
            const source = fs.readFileSync(path.join(root, 'bin/delimit-cli.js'), 'utf8');
            const status = source.slice(source.indexOf('if (action === "status")', source.indexOf('// Vault command')), source.indexOf('} else if (action === "list")', source.indexOf('// Vault command')));
            assert.match(status, /plaintext JSON/);
            assert.doesNotMatch(status, /AES|PBKDF2|secrets never leave|Enabled/i);
            return;
        }
        assert.equal(result.status, 0, result.stderr || String(result.error));
        assert.match(result.stdout, /plaintext JSON/);
        assert.match(result.stdout, /0600/);
        assert.match(result.stdout, /secrets\/.*<NAME>\.json/);
        assert.doesNotMatch(result.stdout, /AES|PBKDF2|secrets never leave|encryption:.*enabled/i);
    } finally { fs.rmSync(home, { recursive: true, force: true }); }
});

test('models status uses configured providers rather than unreachable free quota', () => {
    const source = fs.readFileSync(path.join(root, 'bin/delimit-cli.js'), 'utf8');
    const status = source.slice(source.indexOf('function printModelStatus('), source.indexOf('async function testModelKey('));
    assert.match(status, /Deliberations use your configured model CLIs or API keys/);
    assert.doesNotMatch(status, /free deliberation.*remaining/);
});

test('setup repairs managed text at current version and preserves user text', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-managed-'));
    const target = path.join(dir, 'AGENTS.md');
    try {
        const section = getDelimitSection();
        fs.writeFileSync(target, `user before\n${section.replace('## Auto-Trigger Rules', '## Auto-Trigger Rules\nNever ask permission — draft and send, the founder reviews via email.')}\nuser after\n`);
        assert.equal(upsertDelimitSection(target).action, 'updated');
        const updated = fs.readFileSync(target, 'utf8');
        assert.match(updated, /^user before\n/);
        assert.match(updated, /\nuser after\n$/);
        assert.doesNotMatch(updated, /founder reviews via email/);
        assert.equal(upsertDelimitSection(target).action, 'unchanged');
    } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});

test('generated guidance has no founder-only outbound instructions', () => {
    const setup = fs.readFileSync(path.join(root, 'bin/delimit-setup.js'), 'utf8');
    const active = setup.replace(/const LEGACY_CUSTOM_INSTRUCTIONS = .*?;\n/, '');
    for (const output of [active, getDelimitSection()]) {
        assert.doesNotMatch(output, /auto-trigger delimit_social_post|delimit_inbox_daemon\(action=|founder reviews via email|Social drafts.*delimit_social_post/);
    }
    assert.match(setup, /customInstructions === LEGACY_CUSTOM_INSTRUCTIONS/);
});

test('shim instructions name actual PATH and file removal', () => {
    const setup = fs.readFileSync(path.join(root, 'bin/delimit-setup.js'), 'utf8');
    assert.match(setup, /To remove shims: remove the Delimit governance wrapping PATH lines/);
    assert.doesNotMatch(setup, /delimit shims disable/);
});

test('migration banner omits unsupported retention and replay claims', () => {
    assert.doesNotMatch(bannerText(), /365-day|replay|every deliberation into a signed/i);
});
