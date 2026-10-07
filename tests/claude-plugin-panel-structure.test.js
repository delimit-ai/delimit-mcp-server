'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const plugin = path.join(root, 'claude-plugin-panel');

test('Delimit Panel plugin has pinned, opt-in panel structure and data disclosures', () => {
    const manifest = JSON.parse(fs.readFileSync(path.join(plugin, '.claude-plugin', 'plugin.json')));
    const reference = JSON.parse(fs.readFileSync(path.join(root, 'claude-plugin', '.claude-plugin', 'plugin.json')));
    assert.equal(manifest.name, 'delimit-panel');
    assert.equal(manifest.displayName, 'Delimit Panel');
    assert.equal(manifest.version, '1.0.0');
    assert.equal(manifest.author.name, reference.author.name);
    assert.equal(manifest.license, reference.license);
    for (const field of ['description', 'homepage', 'repository', 'documentationUrl', 'privacyPolicyUrl', 'termsOfServiceUrl', 'supportUrl']) {
        assert.ok(manifest[field], field);
    }
    const mcp = JSON.parse(fs.readFileSync(path.join(plugin, '.mcp.json')));
    assert.deepEqual(mcp.mcpServers['delimit-panel'], {
        command: 'npx', args: ['-y', 'delimit-cli@4.21.0', 'mcp', '--toolset', 'panel'],
    });
    assert.deepEqual(fs.readdirSync(path.join(plugin, 'skills')).sort(), ['panel', 'panel-setup']);
    for (const name of ['panel', 'panel-setup']) {
        const content = fs.readFileSync(path.join(plugin, 'skills', name, 'SKILL.md'), 'utf8');
        assert.match(content, /^---\nname: .+\ndescription: .+\n---\n/);
    }
    const panel = fs.readFileSync(path.join(plugin, 'skills', 'panel', 'SKILL.md'), 'utf8');
    assert.match(panel, /explicitly asks/);
    assert.match(panel, /approved/);
    assert.match(panel, /no_consensus/);
    const privacy = fs.readFileSync(path.join(plugin, 'PRIVACY.md'), 'utf8');
    for (const phrase of ['model providers', 'hosted', 'Supabase', 'Free', 'Pro', 'retention']) {
        assert.match(privacy, new RegExp(phrase, 'i'));
    }
});
