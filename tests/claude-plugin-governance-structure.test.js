'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const plugin = path.join(__dirname, '..', 'claude-plugin-governance');

test('Governance plugin manifest, MCP pin, and skills have the expected structure', () => {
    const manifest = JSON.parse(fs.readFileSync(path.join(plugin, '.claude-plugin', 'plugin.json')));
    assert.equal(manifest.name, 'delimit-governance');
    assert.equal(manifest.displayName, 'Delimit Governance');
    assert.equal(manifest.version, '1.0.0');
    assert.equal(manifest.author.name, 'Delimit');
    assert.equal(manifest.license, 'MIT');
    for (const field of ['description', 'homepage', 'repository', 'documentationUrl', 'privacyPolicyUrl', 'termsOfServiceUrl', 'supportUrl']) {
        assert.ok(manifest[field], field);
    }
    assert.match(manifest.homepage, /\/claude-plugin-governance$/);
    assert.match(manifest.documentationUrl, /\/claude-plugin-governance\/README\.md$/);
    assert.match(manifest.privacyPolicyUrl, /\/claude-plugin-governance\/PRIVACY\.md$/);

    const mcp = JSON.parse(fs.readFileSync(path.join(plugin, '.mcp.json'))).mcpServers.delimit;
    assert.equal(mcp.command, 'npx');
    assert.deepEqual(mcp.args, ['-y', 'delimit-cli@4.21.0', 'mcp', '--toolset', 'governance']);

    for (const name of ['api-check', 'spec-health', 'explain-change']) {
        const content = fs.readFileSync(path.join(plugin, 'skills', name, 'SKILL.md'), 'utf8');
        assert.match(content, new RegExp(`^---\\nname: ${name}\\ndescription: .+\\n---\\n`));
    }
    for (const name of ['README.md', 'PRIVACY.md']) {
        assert.ok(fs.statSync(path.join(plugin, name)).size > 0, name);
    }
});
