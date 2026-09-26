'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const plugin = path.join(root, 'claude-plugin');

test('Claude plugin manifests and records skills have the expected structure', () => {
    const manifest = JSON.parse(fs.readFileSync(path.join(plugin, '.claude-plugin', 'plugin.json')));
    assert.equal(manifest.name, 'delimit');
    for (const field of ['displayName', 'version', 'description', 'homepage', 'repository', 'license']) assert.ok(manifest[field], field);
    assert.equal(manifest.author.name, 'Delimit');
    const marketplace = JSON.parse(fs.readFileSync(path.join(root, '.claude-plugin', 'marketplace.json')));
    assert.equal(marketplace.plugins[0].source, './claude-plugin');
    const mcp = JSON.parse(fs.readFileSync(path.join(plugin, '.mcp.json'))).mcpServers.delimit;
    assert.equal(mcp.command, 'npx');
    assert.deepEqual(mcp.args.slice(2), ['mcp', '--toolset', 'records']);
    assert.match(mcp.args[1], /^delimit-cli@/);
    for (const name of ['record', 'handoff', 'resume']) {
        const content = fs.readFileSync(path.join(plugin, 'skills', name, 'SKILL.md'), 'utf8');
        assert.match(content, /^---\n[\s\S]*?\ndescription: .+\n---\n/);
    }
});
