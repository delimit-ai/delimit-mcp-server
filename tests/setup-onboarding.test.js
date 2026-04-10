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

Unify all AI coding assistants with persistent context, governance, and multi-model debate.

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
- GitHub: https://github.com/delimit-ai/delimit-mcp-server
- Action: https://github.com/marketplace/actions/delimit-api-governance
<!-- delimit:end -->`;
}

function getClaudeMdContent() {
    return getDelimitSection() + '\n';
}

// Mirror of bin/delimit-setup.js upsertDelimitSection. NEVER clobbers user
// content — always upserts between markers or appends below.
function upsertDelimitSection(filePath) {
    const newSection = getDelimitSection();
    const version = PKG_VERSION;

    if (!fs.existsSync(filePath)) {
        fs.writeFileSync(filePath, newSection + '\n');
        return { action: 'created' };
    }

    const rawExisting = fs.readFileSync(filePath, 'utf-8');
    // Strip UTF-8 BOM so the start-of-line anchor matches line 1.
    const existing = rawExisting.replace(/^\uFEFF/, '');

    // Anchored to start-of-line (multiline) with optional leading horizontal
    // whitespace only. Documentation prose that quotes the markers in
    // backticks/bullets/blockquotes must NOT be matched. v4.1.50 regression fix.
    const startMarkerRe = /^[ \t]*<!-- delimit:start[^>]*-->[ \t]*$/m;
    const endMarkerRe = /^[ \t]*<!-- delimit:end -->[ \t]*$/m;
    const startMatch = existing.match(startMarkerRe);
    const endMatch = existing.match(endMarkerRe);
    const hasStart = !!startMatch;
    const hasEnd = !!endMatch;

    if (hasStart && hasEnd) {
        const versionMatch = existing.match(/^[ \t]*<!-- delimit:start v([^ ]+) -->[ \t]*$/m);
        const currentVersion = versionMatch ? versionMatch[1] : '';
        if (currentVersion === version) {
            return { action: 'unchanged' };
        }
        const startIdx = startMatch.index;
        const endIdx = endMatch.index + endMatch[0].length;
        const before = existing.substring(0, startIdx);
        const after = existing.substring(endIdx);
        fs.writeFileSync(filePath, before + newSection + after);
        return { action: 'updated' };
    }

    // No markers — append below existing user content. Never clobber.
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

    it('preserves old Delimit content by appending markers below (v4.1.47 clobber fix)', () => {
        // Regression: v4.1.47 and earlier clobbered any CLAUDE.md containing
        // `# Delimit` + `delimit_ledger_context` or `# Delimit AI Guardrails`
        // by writing a fresh stock template over the top, destroying the
        // founder's customizations. The new upsert behavior preserves the
        // existing content and appends the managed section below.
        const f = tmpFile('old-delimit.md');
        const legacy = '# Delimit AI Guardrails\n\nSome old content with delimit_init and delimit_ledger_context\n';
        fs.writeFileSync(f, legacy);
        const result = upsertDelimitSection(f);
        assert.strictEqual(result.action, 'appended');
        const content = fs.readFileSync(f, 'utf-8');
        assert.ok(content.startsWith('# Delimit AI Guardrails'), 'Legacy content MUST be preserved at the top');
        assert.ok(content.includes('delimit_init'), 'Legacy content MUST still contain delimit_init');
        assert.ok(content.includes('<!-- delimit:start'), 'Managed section MUST be appended');
        assert.ok(content.includes('<!-- delimit:end -->'), 'Managed end marker MUST be present');
        cleanup(f);
    });

    it('never clobbers a customized CLAUDE.md containing # Delimit and delimit_ledger_context', () => {
        // Explicit regression for the exact pattern that broke the founder's
        // /root/CLAUDE.md on 2026-04-09 when shim auto-update ran setup.
        const f = tmpFile('custom-delimit.md');
        const founderCustomized = [
            '# Delimit',
            '',
            '## Auto-Trigger Rules (Consensus 123)',
            '- Session start: call delimit_ledger_context',
            '',
            '## Paying Customers (CRITICAL)',
            '- Never clobber user-customized files',
            '',
            '## Escalation Rules',
            '- Pre-approval of a plan does not extend to unforeseen escalations',
            '',
        ].join('\n');
        fs.writeFileSync(f, founderCustomized);
        upsertDelimitSection(f);
        const content = fs.readFileSync(f, 'utf-8');
        assert.ok(content.includes('## Paying Customers'), 'Founder custom sections MUST survive');
        assert.ok(content.includes('## Escalation Rules'), 'Founder custom sections MUST survive');
        assert.ok(content.includes('Pre-approval of a plan'), 'Full founder content MUST survive');
        cleanup(f);
    });

    it('does not match markers quoted in prose (v4.1.50 in-prose regression)', () => {
        // Regression: v4.1.49 used an unanchored regex /<!-- delimit:start[^>]*-->/
        // which matched the marker even when the user QUOTED it inside backticks
        // in a documentation bullet. On the next `delimit setup` run the upsert
        // mistook the prose mention for a real managed section, sliced everything
        // between the prose start and prose end markers, and clobbered the
        // founder's customizations on /root/CLAUDE.md (2026-04-09 incident).
        // Markers must be matched only when they are on their own line.
        const f = tmpFile('prose-markers.md');
        const founderContent = [
            '# Project Rules',
            '',
            '## Customer Protection',
            '- **Never clobber user-customized files**: Use managed-section markers',
            '  (`<!-- delimit:start -->` / `<!-- delimit:end -->`) or append-only writes.',
            '  Never replace the whole file.',
            '',
            '## Other Rules',
            '- Stuff that must survive upgrade',
            '',
        ].join('\n');
        fs.writeFileSync(f, founderContent);

        // First run should APPEND (no real markers exist), not UPDATE.
        const result = upsertDelimitSection(f);
        assert.strictEqual(
            result.action,
            'appended',
            'In-prose marker mentions must NOT be treated as a managed section',
        );

        const content = fs.readFileSync(f, 'utf-8');
        // Founder content must survive verbatim — no slicing between prose markers.
        assert.ok(content.includes('## Customer Protection'), 'Customer Protection section MUST survive');
        assert.ok(content.includes('## Other Rules'), 'Other Rules section MUST survive');
        assert.ok(content.includes('Never replace the whole file.'), 'Full bullet text MUST survive');
        assert.ok(content.includes('Stuff that must survive upgrade'), 'Trailing user content MUST survive');
        // The real managed section is appended below.
        assert.ok(content.indexOf('<!-- delimit:start v') > content.indexOf('Stuff that must survive upgrade'),
            'Real managed section must be appended BELOW user content');

        // Second run on the now-marked file must be a clean UPDATE, not a re-append.
        // First simulate a version bump by rewriting the marker version.
        const bumped = fs.readFileSync(f, 'utf-8').replace(`v${PKG_VERSION}`, 'v0.0.1');
        fs.writeFileSync(f, bumped);
        const result2 = upsertDelimitSection(f);
        assert.strictEqual(result2.action, 'updated', 'Second run must update the real managed section');

        const content2 = fs.readFileSync(f, 'utf-8');
        // User content STILL preserved across upgrade.
        assert.ok(content2.includes('## Customer Protection'), 'Customer Protection MUST survive upgrade');
        assert.ok(content2.includes('Never replace the whole file.'), 'In-prose markers MUST survive upgrade');
        assert.ok(content2.includes('Stuff that must survive upgrade'), 'Trailing content MUST survive upgrade');
        assert.ok(!content2.includes('v0.0.1'), 'Old version must be replaced');
        cleanup(f);
    });

    it('handles real markers with CRLF line endings (v4.1.50 edge case)', () => {
        // Markers separated by \r\n must still be recognized as a managed section.
        const f = tmpFile('crlf-markers.md');
        const userTop = '# User Header\r\n\r\nUser content above.\r\n\r\n';
        const oldSection = getDelimitSection().replace(`v${PKG_VERSION}`, 'v0.0.1').replace(/\n/g, '\r\n');
        const userBottom = '\r\n\r\n# User Footer\r\n';
        fs.writeFileSync(f, userTop + oldSection + userBottom);
        const result = upsertDelimitSection(f);
        assert.strictEqual(result.action, 'updated', 'CRLF marker file must be recognized and updated');
        const content = fs.readFileSync(f, 'utf-8');
        assert.ok(content.includes('# User Header'), 'CRLF: top user content preserved');
        assert.ok(content.includes('# User Footer'), 'CRLF: bottom user content preserved');
        assert.ok(content.includes(`v${PKG_VERSION}`), 'CRLF: version updated');
        assert.ok(!content.includes('v0.0.1'), 'CRLF: old version gone');
        cleanup(f);
    });

    it('handles UTF-8 BOM at file start (v4.1.50 edge case)', () => {
        // BOM-prefixed file with markers on the first line must still be recognized.
        const f = tmpFile('bom-markers.md');
        const oldSection = getDelimitSection().replace(`v${PKG_VERSION}`, 'v0.0.1');
        // BOM directly before the start marker — no other content above.
        fs.writeFileSync(f, '\uFEFF' + oldSection + '\n\n# After Section\n');
        const result = upsertDelimitSection(f);
        assert.strictEqual(result.action, 'updated', 'BOM-prefixed marker file must be recognized');
        const content = fs.readFileSync(f, 'utf-8');
        assert.ok(content.includes('# After Section'), 'BOM: trailing user content preserved');
        assert.ok(content.includes(`v${PKG_VERSION}`), 'BOM: version updated');
        assert.ok(!content.includes('v0.0.1'), 'BOM: old version gone');
        // BOM should be stripped after upsert.
        assert.ok(!content.startsWith('\uFEFF'), 'BOM should be stripped on rewrite');
        cleanup(f);
    });

    it('recognizes indented markers (v4.1.50 edge case)', () => {
        // Two-space-indented markers should still be matched as a real managed section.
        const f = tmpFile('indented-markers.md');
        const oldSection = getDelimitSection().replace(`v${PKG_VERSION}`, 'v0.0.1');
        const indented = oldSection
            .split('\n')
            .map((line, i, arr) => (i === 0 || i === arr.length - 1) ? '  ' + line : line)
            .join('\n');
        fs.writeFileSync(f, '# Doc\n\n' + indented + '\n\n# After\n');
        const result = upsertDelimitSection(f);
        assert.strictEqual(result.action, 'updated', 'Indented markers must be recognized');
        const content = fs.readFileSync(f, 'utf-8');
        assert.ok(content.includes('# Doc'), 'Indented: top content preserved');
        assert.ok(content.includes('# After'), 'Indented: bottom content preserved');
        assert.ok(!content.includes('v0.0.1'), 'Indented: old version gone');
        cleanup(f);
    });

    it('does NOT match bullet- or blockquote-prefixed marker mentions (v4.1.50 edge case)', () => {
        // Markers prefixed with `- `, `* `, or `> ` are documentation, not real markers.
        // Each variant alone must result in 'appended', not 'updated'.
        const variants = [
            '# Doc\n\n- <!-- delimit:start v9.9.9 -->\n- some bullet text\n- <!-- delimit:end -->\n',
            '# Doc\n\n* <!-- delimit:start v9.9.9 -->\n* some bullet text\n* <!-- delimit:end -->\n',
            '# Doc\n\n> <!-- delimit:start v9.9.9 -->\n> blockquote text\n> <!-- delimit:end -->\n',
        ];
        for (const variant of variants) {
            const f = tmpFile('prefixed-' + Math.random().toString(36).slice(2) + '.md');
            fs.writeFileSync(f, variant);
            const result = upsertDelimitSection(f);
            assert.strictEqual(
                result.action,
                'appended',
                `Bullet/blockquote-prefixed marker mention must NOT be treated as a real section. Variant: ${variant.slice(0, 40)}`,
            );
            const content = fs.readFileSync(f, 'utf-8');
            assert.ok(content.includes('v9.9.9'), 'Documentation prose mention must survive verbatim');
            cleanup(f);
        }
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
        assert.ok(setupContent.includes('What\'s next'), 'Should show what\'s next box');
        assert.ok(setupContent.includes('Keep Building'), 'Should end with Keep Building');
        assert.ok(setupContent.includes('npx delimit-cli lint'), 'Should suggest lint command');
        assert.ok(setupContent.includes('npx delimit-cli doctor'), 'Should suggest doctor command');
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

describe('spec auto-detection', () => {
    it('setup script contains findSpecFiles function', () => {
        const setupPath = path.join(__dirname, '..', 'bin', 'delimit-setup.js');
        const setupContent = fs.readFileSync(setupPath, 'utf-8');
        assert.ok(setupContent.includes('findSpecFiles'), 'Should have findSpecFiles function');
        assert.ok(setupContent.includes('openapi'), 'Should search for openapi files');
        assert.ok(setupContent.includes('swagger'), 'Should search for swagger files');
    });

    it('setup script reports found specs', () => {
        const setupPath = path.join(__dirname, '..', 'bin', 'delimit-setup.js');
        const setupContent = fs.readFileSync(setupPath, 'utf-8');
        assert.ok(setupContent.includes('API spec(s)'), 'Should report found specs');
        assert.ok(setupContent.includes('Scanning for API specs'), 'Should have scanning step');
    });
});

describe('postinstall telemetry', () => {
    it('postinstall script exists and sends anonymous ping', () => {
        const postinstallPath = path.join(__dirname, '..', 'scripts', 'postinstall.js');
        assert.ok(fs.existsSync(postinstallPath), 'postinstall.js should exist');
        const content = fs.readFileSync(postinstallPath, 'utf-8');
        assert.ok(content.includes("event: 'install'"), 'Should send install event');
        assert.ok(content.includes('delimit.ai'), 'Should ping delimit.ai');
        assert.ok(content.includes('/api/telemetry'), 'Should hit telemetry endpoint');
        assert.ok(content.includes('timeout: 3000'), 'Should have 3 second timeout');
        assert.ok(content.includes('silent fail'), 'Should fail silently');
    });

    it('postinstall does not collect PII', () => {
        const postinstallPath = path.join(__dirname, '..', 'scripts', 'postinstall.js');
        const content = fs.readFileSync(postinstallPath, 'utf-8');
        assert.ok(!content.includes('username'), 'Should not collect username');
        assert.ok(!content.includes('os.hostname'), 'Should not collect hostname');
        assert.ok(!content.includes('os.homedir'), 'Should not collect home directory');
        assert.ok(!content.includes('email'), 'Should not collect email');
        assert.ok(content.includes('process.version'), 'Should include node version');
        assert.ok(content.includes('process.platform'), 'Should include platform');
    });

    it('package.json postinstall points to script', () => {
        const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'package.json'), 'utf-8'));
        assert.ok(pkg.scripts.postinstall.includes('postinstall.js'), 'Should run postinstall.js');
    });

    it('package.json files includes scripts/', () => {
        const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'package.json'), 'utf-8'));
        assert.ok(pkg.files.includes('scripts/'), 'Should include scripts/ in published files');
    });
});
