'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const root = path.join(__dirname, '..');
const plugin = path.join(root, 'claude-plugin');
const bundledPlugin = path.join(root, 'claude-plugin-bundled');

test('Claude plugin manifests, slash commands, and records skills have the expected structure', () => {
    const manifest = JSON.parse(fs.readFileSync(path.join(plugin, '.claude-plugin', 'plugin.json')));
    assert.equal(manifest.name, 'delimit');
    assert.equal(manifest.version, '1.0.5');
    for (const field of ['displayName', 'version', 'description', 'homepage', 'repository', 'license']) assert.ok(manifest[field], field);
    assert.equal(manifest.author.name, 'Delimit');

    const marketplace = JSON.parse(fs.readFileSync(path.join(root, '.claude-plugin', 'marketplace.json')));
    assert.equal(marketplace.plugins[0].source, './claude-plugin');
    assert.equal(marketplace.plugins[0].name, 'delimit');

    const mcp = JSON.parse(fs.readFileSync(path.join(plugin, '.mcp.json'))).mcpServers.delimit;
    assert.equal(mcp.command, 'npx');
    assert.deepEqual(mcp.args, ['-y', 'delimit-cli@4.20.1', 'mcp', '--toolset', 'records']);

    for (const name of ['record', 'handoff', 'resume']) {
        const content = fs.readFileSync(path.join(plugin, 'skills', name, 'SKILL.md'), 'utf8');
        assert.match(content, /^---\n[\s\S]*?\ndescription: .+\n---\n/);
        assert.match(content, /never act without the user asking/);
    }

    for (const name of ['record', 'handoff', 'resume']) {
        const command = fs.readFileSync(path.join(plugin, 'commands', `${name}.md`), 'utf8');
        assert.match(command, /^---\n(?:[\s\S]*?\n)?description: .+\n---\n/);
        assert.match(command, /delimit_/);
    }

    const readme = fs.readFileSync(path.join(plugin, 'README.md'), 'utf8');
    assert.match(readme, /Getting started: type `\/delimit:resume` at the start of a session/);
    assert.doesNotMatch(readme, /automatically surfaces/);
});

test('claude-plugin v1.0.5 is structurally identical to delimit--v1.0.4 except Markdown files and version bump', () => {
    const git = (args) => {
        const r = spawnSync('git', args, { cwd: root, encoding: 'utf8' });
        assert.equal(r.status, 0, `git ${args.join(' ')} failed: ${r.stderr}`);
        return r.stdout;
    };

    // 1. .mcp.json is byte-for-byte identical to v1.0.4
    const mcp104 = git(['show', 'delimit--v1.0.4:claude-plugin/.mcp.json']);
    const mcpLocal = fs.readFileSync(path.join(plugin, '.mcp.json'), 'utf8');
    assert.equal(mcpLocal, mcp104, '.mcp.json must match delimit--v1.0.4 byte-for-byte');

    // 2. icon.svg is byte-for-byte identical to v1.0.4
    const icon104 = git(['show', 'delimit--v1.0.4:claude-plugin/.claude-plugin/icon.svg']);
    const iconLocal = fs.readFileSync(path.join(plugin, '.claude-plugin', 'icon.svg'), 'utf8');
    assert.equal(iconLocal, icon104, 'icon.svg must match delimit--v1.0.4 byte-for-byte');

    // 3. plugin.json matches v1.0.4 except version bump to 1.0.5
    const manifest104 = JSON.parse(git(['show', 'delimit--v1.0.4:claude-plugin/.claude-plugin/plugin.json']));
    const manifestLocal = JSON.parse(fs.readFileSync(path.join(plugin, '.claude-plugin', 'plugin.json'), 'utf8'));
    assert.deepEqual(manifestLocal, { ...manifest104, version: '1.0.5' });

    // 4. No hooks, scripts, lockfiles, or package manifests in claude-plugin/
    assert.ok(!fs.existsSync(path.join(plugin, 'hooks')), 'no hooks folder permitted');
    assert.ok(!fs.existsSync(path.join(plugin, 'server.cjs')), 'no scripts permitted');
    assert.ok(!fs.existsSync(path.join(plugin, 'package.json')), 'no package manifest permitted');
    assert.ok(!fs.existsSync(path.join(plugin, 'package-lock.json')), 'no package lock permitted');

    // 5. Every file in claude-plugin is either in v1.0.4 or a Markdown file
    function listFiles(dir) {
        const entries = fs.readdirSync(dir, { withFileTypes: true });
        return entries.flatMap((e) => {
            const p = path.join(dir, e.name);
            return e.isDirectory() ? listFiles(p) : [path.relative(plugin, p)];
        });
    }
    const files = listFiles(plugin);
    for (const f of files) {
        if (f.endsWith('.md')) continue;
        assert.ok(
            f === '.claude-plugin/icon.svg' || f === '.claude-plugin/plugin.json' || f === '.mcp.json',
            `non-Markdown file ${f} not permitted unless in v1.0.4 baseline`
        );
    }
});

test('Delimit bundled plugin in marketplace retains offline node server and hooks', () => {
    assert.ok(fs.existsSync(bundledPlugin), 'claude-plugin-bundled directory exists');
    const manifest = JSON.parse(fs.readFileSync(path.join(bundledPlugin, '.claude-plugin', 'plugin.json')));
    assert.equal(manifest.name, 'delimit-bundled');
    assert.equal(manifest.version, '1.2.0');

    const marketplace = JSON.parse(fs.readFileSync(path.join(root, '.claude-plugin', 'marketplace.json')));
    const entry = marketplace.plugins.find((p) => p.name === 'delimit-bundled');
    assert.ok(entry, 'delimit-bundled must be listed in marketplace.json');
    assert.equal(entry.source, './claude-plugin-bundled');

    const mcp = JSON.parse(fs.readFileSync(path.join(bundledPlugin, '.mcp.json'))).mcpServers.delimit;
    assert.equal(mcp.command, 'node');
    assert.deepEqual(mcp.args, ['${CLAUDE_PLUGIN_ROOT}/server.cjs']);
    assert.ok(fs.existsSync(path.join(bundledPlugin, 'server.cjs')));
    assert.ok(fs.existsSync(path.join(bundledPlugin, 'hooks', 'hooks.json')));
});
