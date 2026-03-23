const { describe, it } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');

// Load the upsert and content functions by evaluating the relevant parts of setup
// We replicate the key functions here for isolated testing.

const PKG_VERSION = JSON.parse(
    fs.readFileSync(path.join(__dirname, '..', 'package.json'), 'utf-8')
).version;

function getDelimitSection() {
    return `<!-- delimit:start v${PKG_VERSION} -->
# Delimit

One workspace for every AI coding assistant.

## On every session start:
1. Call \`delimit_ledger_context\` to check for open tasks
2. Call \`delimit_gov_health\` to check governance status
3. If returning user, summarize what's pending
4. If new project, call \`delimit_scan\` to discover what Delimit can do

## After editing code:
- After editing UI/CSS: call \`delimit_design_validate_responsive\`
- After editing API specs: call \`delimit_lint\`
- After editing tests: call \`delimit_test_smoke\`

## Before deploying:
1. Call \`delimit_security_audit\`
2. Call \`delimit_test_smoke\`
3. Call \`delimit_deploy_plan\`

## Before committing:
- Call \`delimit_repo_diagnose\` to check for common issues

## When starting a new project:
- Call \`delimit_scan\` to discover what Delimit can do
- Call \`delimit_init\` to set up governance

## Try these:
- "scan this project" -- discover what Delimit can do here
- "lint my API spec" -- catch breaking changes in your OpenAPI spec
- "add to ledger: [anything]" -- track tasks across sessions
- "what's on the ledger?" -- pick up where you left off
- "deliberate [question]" -- get multi-model AI consensus

## What Delimit does:
- **API governance** -- lint, diff, semver classification, migration guides
- **Persistent ledger** -- tasks survive across sessions
- **Security audit** -- dependency scanning, secret detection
- **Test verification** -- confirm tests actually ran, measure coverage
- **Multi-model deliberation** -- say "deliberate [question]" to get consensus from multiple AI models

## GitHub Action
Add breaking change detection to any repo:
\`\`\`yaml
- uses: delimit-ai/delimit-action@v1
  with:
    spec: api/openapi.yaml
\`\`\`

## Links
- Docs: https://delimit.ai/docs
- GitHub: https://github.com/delimit-ai/delimit
- Action: https://github.com/marketplace/actions/delimit-api-governance
<!-- delimit:end -->`;
}

function getClaudeMdContent() {
    return getDelimitSection() + '\n';
}

function upsertDelimitSection(filePath) {
    const newSection = getDelimitSection();
    const version = PKG_VERSION;

    if (!fs.existsSync(filePath)) {
        fs.writeFileSync(filePath, newSection + '\n');
        return { action: 'created' };
    }

    const existing = fs.readFileSync(filePath, 'utf-8');

    const startMarkerRe = /<!-- delimit:start[^>]*-->/;
    const endMarker = '<!-- delimit:end -->';
    const hasStart = startMarkerRe.test(existing);
    const hasEnd = existing.includes(endMarker);

    if (hasStart && hasEnd) {
        const versionMatch = existing.match(/<!-- delimit:start v([^ ]+) -->/);
        const currentVersion = versionMatch ? versionMatch[1] : '';
        if (currentVersion === version) {
            return { action: 'unchanged' };
        }
        const before = existing.substring(0, existing.search(startMarkerRe));
        const after = existing.substring(existing.indexOf(endMarker) + endMarker.length);
        fs.writeFileSync(filePath, before + newSection + after);
        return { action: 'updated' };
    }

    const isOldDelimit = existing.includes('# Delimit AI Guardrails') ||
        existing.includes('delimit_init') ||
        existing.includes('persistent memory, verified execution') ||
        (existing.includes('# Delimit') && existing.includes('delimit_ledger_context'));

    if (isOldDelimit) {
        fs.writeFileSync(filePath, newSection + '\n');
        return { action: 'updated' };
    }

    const separator = existing.endsWith('\n') ? '\n' : '\n\n';
    fs.writeFileSync(filePath, existing + separator + newSection + '\n');
    return { action: 'appended' };
}

// Helper: create a temp file for testing
function tmpFile(name) {
    const dir = path.join(os.tmpdir(), 'delimit-test-' + process.pid);
    fs.mkdirSync(dir, { recursive: true });
    return path.join(dir, name);
}

function cleanup(filePath) {
    try { fs.unlinkSync(filePath); } catch {}
}

describe('CLAUDE.md content', () => {
    it('contains delimit:start and delimit:end markers', () => {
        const content = getClaudeMdContent();
        assert.ok(content.includes('<!-- delimit:start'), 'Should have start marker');
        assert.ok(content.includes('<!-- delimit:end -->'), 'Should have end marker');
    });

    it('includes version in start marker', () => {
        const content = getClaudeMdContent();
        assert.ok(content.includes(`<!-- delimit:start v${PKG_VERSION} -->`), 'Should include version');
    });

    it('contains governance triggers', () => {
        const content = getClaudeMdContent();
        assert.ok(content.includes('delimit_gov_health'), 'Should have gov_health trigger');
        assert.ok(content.includes('delimit_ledger_context'), 'Should have ledger_context trigger');
        assert.ok(content.includes('delimit_scan'), 'Should have scan trigger');
    });

    it('contains workflow-specific triggers', () => {
        const content = getClaudeMdContent();
        assert.ok(content.includes('After editing code'), 'Should have after-editing section');
        assert.ok(content.includes('delimit_lint'), 'Should have lint trigger for API specs');
        assert.ok(content.includes('delimit_test_smoke'), 'Should have test_smoke trigger');
        assert.ok(content.includes('Before deploying'), 'Should have before-deploying section');
        assert.ok(content.includes('delimit_security_audit'), 'Should have security_audit trigger');
        assert.ok(content.includes('Before committing'), 'Should have before-committing section');
        assert.ok(content.includes('delimit_repo_diagnose'), 'Should have repo_diagnose trigger');
        assert.ok(content.includes('delimit_init'), 'Should have init trigger for new projects');
    });

    it('contains natural language prompts', () => {
        const content = getClaudeMdContent();
        assert.ok(content.includes('scan this project'), 'Should have scan prompt');
        assert.ok(content.includes('add to ledger'), 'Should have ledger add prompt');
        assert.ok(content.includes('lint my API spec'), 'Should have lint prompt');
    });

    it('contains links', () => {
        const content = getClaudeMdContent();
        assert.ok(content.includes('https://delimit.ai/docs'), 'Should have docs link');
        assert.ok(content.includes('delimit-action@v1'), 'Should have action reference');
    });
});

describe('upsertDelimitSection', () => {
    it('creates file when it does not exist', () => {
        const f = tmpFile('new-claude.md');
        cleanup(f);
        const result = upsertDelimitSection(f);
        assert.strictEqual(result.action, 'created');
        const content = fs.readFileSync(f, 'utf-8');
        assert.ok(content.includes('<!-- delimit:start'));
        assert.ok(content.includes('<!-- delimit:end -->'));
        cleanup(f);
    });

    it('returns unchanged when same version markers exist', () => {
        const f = tmpFile('same-version.md');
        fs.writeFileSync(f, getClaudeMdContent());
        const result = upsertDelimitSection(f);
        assert.strictEqual(result.action, 'unchanged');
        cleanup(f);
    });

    it('updates only the delimit section when version changes', () => {
        const f = tmpFile('old-version.md');
        const userContent = '# My Project\n\nThis is my custom content.\n\n';
        const oldSection = getDelimitSection().replace(`v${PKG_VERSION}`, 'v0.0.1');
        const trailing = '\n\n# More user content\n';
        fs.writeFileSync(f, userContent + oldSection + trailing);
        const result = upsertDelimitSection(f);
        assert.strictEqual(result.action, 'updated');
        const content = fs.readFileSync(f, 'utf-8');
        // User content preserved
        assert.ok(content.includes('# My Project'), 'User content before should be preserved');
        assert.ok(content.includes('# More user content'), 'User content after should be preserved');
        // Version updated
        assert.ok(content.includes(`v${PKG_VERSION}`), 'Version should be updated');
        assert.ok(!content.includes('v0.0.1'), 'Old version should be gone');
        cleanup(f);
    });

    it('replaces old Delimit content without markers', () => {
        const f = tmpFile('old-delimit.md');
        fs.writeFileSync(f, '# Delimit AI Guardrails\n\nSome old content with delimit_init');
        const result = upsertDelimitSection(f);
        assert.strictEqual(result.action, 'updated');
        const content = fs.readFileSync(f, 'utf-8');
        assert.ok(content.includes('<!-- delimit:start'));
        assert.ok(!content.includes('# Delimit AI Guardrails'));
        cleanup(f);
    });

    it('appends to custom file without any Delimit content', () => {
        const f = tmpFile('custom.md');
        const custom = '# My Custom Project\n\nDo not overwrite this.\n';
        fs.writeFileSync(f, custom);
        const result = upsertDelimitSection(f);
        assert.strictEqual(result.action, 'appended');
        const content = fs.readFileSync(f, 'utf-8');
        assert.ok(content.startsWith('# My Custom Project'), 'Original content should be at top');
        assert.ok(content.includes('<!-- delimit:start'), 'Delimit section should be appended');
        assert.ok(content.includes('<!-- delimit:end -->'), 'Should have end marker');
        cleanup(f);
    });
});

describe('setup script structure', () => {
    it('setup script file contains install completion messaging', () => {
        const setupPath = path.join(__dirname, '..', 'bin', 'delimit-setup.js');
        const setupContent = fs.readFileSync(setupPath, 'utf-8');
        assert.ok(setupContent.includes('Delimit is installed'), 'Should confirm installation');
        assert.ok(setupContent.includes('$ claude'), 'Should suggest running claude');
        assert.ok(setupContent.includes('scan this project'), 'Should suggest scanning');
    });

    it('setup script contains governance wrapping step', () => {
        const setupPath = path.join(__dirname, '..', 'bin', 'delimit-setup.js');
        const setupContent = fs.readFileSync(setupPath, 'utf-8');
        assert.ok(setupContent.includes('Governance wrapping'), 'Should have governance wrapping step');
        assert.ok(setupContent.includes('shims'), 'Should mention shims');
    });

    it('setup script uses upsertDelimitSection', () => {
        const setupPath = path.join(__dirname, '..', 'bin', 'delimit-setup.js');
        const setupContent = fs.readFileSync(setupPath, 'utf-8');
        assert.ok(setupContent.includes('upsertDelimitSection'), 'Should use upsert function');
        assert.ok(setupContent.includes('delimit:start'), 'Should use start marker');
        assert.ok(setupContent.includes('delimit:end'), 'Should use end marker');
    });

    it('setup script handles codex and cursor instruction files', () => {
        const setupPath = path.join(__dirname, '..', 'bin', 'delimit-setup.js');
        const setupContent = fs.readFileSync(setupPath, 'utf-8');
        assert.ok(setupContent.includes('codexInstructions'), 'Should handle codex instructions');
        assert.ok(setupContent.includes('cursorRules'), 'Should handle cursor rules');
    });
});
