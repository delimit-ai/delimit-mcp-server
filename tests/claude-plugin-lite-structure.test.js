'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const LITE_DIR = path.join(ROOT, 'claude-plugin-lite');
const MARKETPLACE_PATH = path.join(ROOT, '.claude-plugin', 'marketplace.json');

const ALLOWED_EXTENSIONS = new Set([
    '.md',
    '.markdown',
    '.json',
    '.yaml',
    '.yml',
    '.png',
    '.jpeg',
    '.jpg',
    '.gif',
    '.webp',
    '.ico',
]);

const ALLOWED_SPECIAL_BASENAMES = new Set([
    'license',
    'readme',
    '.gitignore',
]);

const ALLOWED_MANIFEST_FIELDS = new Set([
    'name',
    'displayName',
    'version',
    'description',
    'author',
    'homepage',
    'repository',
    'license',
    'keywords',
    'icon',
    'privacyPolicyUrl',
]);

const FORBIDDEN_LOCKFILES = new Set([
    'package.json',
    'package-lock.json',
    'npm-shrinkwrap.json',
    'pnpm-lock.yaml',
    'pnpm-workspace.yaml',
    'yarn.lock',
    'bun.lockb',
    'requirements.txt',
    'pipfile',
    'pipfile.lock',
    'pyproject.toml',
    'gemfile',
    'gemfile.lock',
    'cargo.toml',
    'cargo.lock',
]);

function getAllFiles(dir, fileList = []) {
    if (!fs.existsSync(dir)) return fileList;
    const entries = fs.readdirSync(dir, { withFileTypes: true });
    for (const entry of entries) {
        const fullPath = path.join(dir, entry.name);
        if (entry.isDirectory()) {
            getAllFiles(fullPath, fileList);
        } else if (entry.isFile()) {
            fileList.push(fullPath);
        }
    }
    return fileList;
}

function validateLiteDirectory(pluginDir) {
    const violations = [];
    const files = getAllFiles(pluginDir);

    for (const file of files) {
        const rel = path.relative(pluginDir, file);
        const baseName = path.basename(file).toLowerCase();
        const ext = path.extname(file).toLowerCase();

        // 1. Allowed file extensions or allowed special text basenames (e.g. LICENSE, .gitignore)
        const isAllowedExtension = ALLOWED_EXTENSIONS.has(ext);
        const isAllowedSpecialBasename = ext === '' && ALLOWED_SPECIAL_BASENAMES.has(baseName);
        if (!isAllowedExtension && !isAllowedSpecialBasename) {
            violations.push(`Disallowed file extension '${ext}': ${rel}`);
        }

        // 2. Lockfiles and package manifests
        if (FORBIDDEN_LOCKFILES.has(baseName)) {
            violations.push(`Forbidden lockfile or package manifest found: ${rel}`);
        }

        // 3. Hooks
        if (baseName === 'hooks.json' || rel.split(path.sep).includes('hooks')) {
            violations.push(`Hooks found in lite plugin: ${rel}`);
        }

        // 4. MCP configs
        if (baseName === '.mcp.json' || baseName.endsWith('mcp.json')) {
            violations.push(`MCP configuration file found in lite plugin: ${rel}`);
        }

        // 5. UTF-8 text and shebang checks
        const buf = fs.readFileSync(file);
        if (['.md', '.markdown', '.json', '.yaml', '.yml'].includes(ext)) {
            if (buf.length >= 2 && buf[0] === 0x23 && buf[1] === 0x21) {
                violations.push(`Text file starts with shebang (#!): ${rel}`);
            }
            for (let i = 0; i < buf.length; i++) {
                if (buf[i] === 0x00) {
                    violations.push(`Null byte found in text file: ${rel}`);
                    break;
                }
            }
        }
    }

    // 6. Manifest inspection
    const manifestPath = path.join(pluginDir, '.claude-plugin', 'plugin.json');
    if (!fs.existsSync(manifestPath)) {
        violations.push(`Missing plugin.json at ${manifestPath}`);
    } else {
        try {
            const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));

            // Check for hooks or mcpServers fields
            if (Object.prototype.hasOwnProperty.call(manifest, 'hooks')) {
                violations.push('plugin.json declares hooks');
            }
            if (Object.prototype.hasOwnProperty.call(manifest, 'mcpServers')) {
                violations.push('plugin.json declares mcpServers');
            }

            // Check allowlist of fields
            for (const key of Object.keys(manifest)) {
                if (!ALLOWED_MANIFEST_FIELDS.has(key)) {
                    violations.push(`plugin.json field '${key}' is outside allowed fields`);
                }
            }
        } catch (e) {
            violations.push(`Invalid JSON in plugin.json: ${e.message}`);
        }
    }

    return violations;
}

test('claude-plugin-lite satisfies all directory compliance rules', () => {
    assert.ok(fs.existsSync(LITE_DIR), 'claude-plugin-lite directory must exist');

    const violations = validateLiteDirectory(LITE_DIR);
    assert.deepEqual(violations, [], `Expected 0 violations, got:\n${violations.join('\n')}`);

    // Verify marketplace entry
    const market = JSON.parse(fs.readFileSync(MARKETPLACE_PATH, 'utf8'));
    const entry = market.plugins.find((p) => p.name === 'delimit-lite');
    assert.ok(entry, 'delimit-lite must be listed in marketplace.json');
    assert.equal(entry.source, './claude-plugin-lite');

    // Verify manifest contents
    const manifest = JSON.parse(fs.readFileSync(path.join(LITE_DIR, '.claude-plugin', 'plugin.json'), 'utf8'));
    assert.equal(manifest.name, 'delimit-lite');
    assert.equal(manifest.displayName, 'Delimit Lite');
    assert.equal(manifest.version, '1.0.0');
    assert.equal(manifest.author.name, 'Delimit');
    assert.equal(manifest.license, 'MIT');
    assert.ok(manifest.privacyPolicyUrl);

    // Verify skills
    for (const skill of ['handoff', 'record', 'resume']) {
        const skillPath = path.join(LITE_DIR, 'skills', skill, 'SKILL.md');
        assert.ok(fs.existsSync(skillPath), `Skill file missing: ${skillPath}`);
        const content = fs.readFileSync(skillPath, 'utf8');
        assert.match(content, new RegExp(`^---\\nname: ${skill}\\ndescription: .+\\n---\\n`));
        assert.match(content, /delimit-cli|delimit/i);
        assert.match(content, /npm install -g delimit-cli/);
    }

    // Verify README and icon
    assert.ok(fs.existsSync(path.join(LITE_DIR, 'README.md')));
    assert.ok(fs.existsSync(path.join(LITE_DIR, 'PRIVACY.md')));
    assert.ok(fs.existsSync(path.join(LITE_DIR, 'LICENSE.md')) || fs.existsSync(path.join(LITE_DIR, 'LICENSE')));
    assert.ok(fs.existsSync(path.join(LITE_DIR, '.claude-plugin', 'icon.png')));
});

test('validateLiteDirectory catches non-allowed files, hooks, mcpServers, lockfiles, and extra fields', () => {
    const tmp = fs.mkdtempSync(path.join(ROOT, 'tmp-lite-test-'));
    try {
        fs.mkdirSync(path.join(tmp, '.claude-plugin'), { recursive: true });

        // Base valid plugin.json
        fs.writeFileSync(path.join(tmp, '.claude-plugin', 'plugin.json'), JSON.stringify({
            name: 'delimit-lite',
            version: '1.0.0',
            description: 'test',
        }));

        // 1. Disallowed file extension (.sh)
        fs.writeFileSync(path.join(tmp, 'script.sh'), 'echo 1');
        assert.ok(validateLiteDirectory(tmp).some((v) => v.includes("Disallowed file extension '.sh'")));
        fs.unlinkSync(path.join(tmp, 'script.sh'));

        // 2. Disallowed file extension (.js)
        fs.writeFileSync(path.join(tmp, 'index.js'), 'console.log(1);');
        assert.ok(validateLiteDirectory(tmp).some((v) => v.includes("Disallowed file extension '.js'")));
        fs.unlinkSync(path.join(tmp, 'index.js'));

        // 3. Shebang in Markdown
        fs.writeFileSync(path.join(tmp, 'test.md'), '#!/bin/sh\nhello');
        assert.ok(validateLiteDirectory(tmp).some((v) => v.includes('starts with shebang')));
        fs.unlinkSync(path.join(tmp, 'test.md'));

        // 4. Null byte in text file
        fs.writeFileSync(path.join(tmp, 'null.md'), Buffer.from([0x61, 0x00, 0x62]));
        assert.ok(validateLiteDirectory(tmp).some((v) => v.includes('Null byte found')));
        fs.unlinkSync(path.join(tmp, 'null.md'));

        // 5. Hooks folder / file
        fs.mkdirSync(path.join(tmp, 'hooks'));
        fs.writeFileSync(path.join(tmp, 'hooks', 'hooks.json'), '{}');
        assert.ok(validateLiteDirectory(tmp).some((v) => v.includes('Hooks found in lite plugin')));
        fs.unlinkSync(path.join(tmp, 'hooks', 'hooks.json'));
        fs.rmdirSync(path.join(tmp, 'hooks'));

        // 6. .mcp.json
        fs.writeFileSync(path.join(tmp, '.mcp.json'), '{}');
        assert.ok(validateLiteDirectory(tmp).some((v) => v.includes('MCP configuration file found')));
        fs.unlinkSync(path.join(tmp, '.mcp.json'));

        // 7. Lockfiles
        fs.writeFileSync(path.join(tmp, 'package.json'), '{}');
        assert.ok(validateLiteDirectory(tmp).some((v) => v.includes('Forbidden lockfile or package manifest found')));
        fs.unlinkSync(path.join(tmp, 'package.json'));

        fs.writeFileSync(path.join(tmp, 'package-lock.json'), '{}');
        assert.ok(validateLiteDirectory(tmp).some((v) => v.includes('Forbidden lockfile or package manifest found')));
        fs.unlinkSync(path.join(tmp, 'package-lock.json'));

        // 8. Extra manifest fields
        fs.writeFileSync(path.join(tmp, '.claude-plugin', 'plugin.json'), JSON.stringify({
            name: 'delimit-lite',
            version: '1.0.0',
            description: 'test',
            unauthorizedKey: 'bad',
        }));
        assert.ok(validateLiteDirectory(tmp).some((v) => v.includes("field 'unauthorizedKey' is outside allowed fields")));

        // 9. Hooks inside manifest
        fs.writeFileSync(path.join(tmp, '.claude-plugin', 'plugin.json'), JSON.stringify({
            name: 'delimit-lite',
            version: '1.0.0',
            description: 'test',
            hooks: {},
        }));
        assert.ok(validateLiteDirectory(tmp).some((v) => v.includes('plugin.json declares hooks')));

        // 10. mcpServers inside manifest
        fs.writeFileSync(path.join(tmp, '.claude-plugin', 'plugin.json'), JSON.stringify({
            name: 'delimit-lite',
            version: '1.0.0',
            description: 'test',
            mcpServers: {},
        }));
        assert.ok(validateLiteDirectory(tmp).some((v) => v.includes('plugin.json declares mcpServers')));
    } finally {
        fs.rmSync(tmp, { recursive: true, force: true });
    }
});
