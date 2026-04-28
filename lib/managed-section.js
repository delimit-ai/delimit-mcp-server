// lib/managed-section.js
//
// Shared upsertDelimitSection helper. Used by:
//   bin/delimit-setup.js — for ~/CLAUDE.md, ~/.codex/instructions.md, ~/.cursorrules
//   adapters/cursor-rules.js — for ~/.cursor/rules/delimit.md
//
// NEVER clobbers user-authored content outside the markers. Behavior:
//   - File missing → create with just the managed section.
//   - File has markers → replace only the region between them (user content
//     above/below preserved).
//   - File has no markers → append the managed section at the bottom (user
//     content at top preserved).
//
// History (institutional memory; do NOT change marker semantics without
// understanding these incidents):
//   - v4.1.47: previous heuristic replaced the whole file whenever it
//     detected "old Delimit content" — destroyed founder-customized
//     CLAUDE.md files on every upgrade.
//   - v4.1.49: unanchored marker regex matched markers inside quoted
//     prose (backticks, bullets, blockquotes) — clobbered /root/CLAUDE.md.
//     The current regex is anchored with the multiline flag so markers
//     MUST be on their own line. Optional leading horizontal whitespace
//     [ \t]* permits genuinely indented markers but NOT prose-leading
//     characters like "- ", "> ", "`", "*".
//
// Returns: { action: 'created' | 'updated' | 'unchanged' | 'appended' }

const fs = require('fs');
const path = require('path');

function loadPackageVersion() {
    try {
        const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'package.json'), 'utf-8'));
        return pkg.version || '0.0.0';
    } catch {
        return '0.0.0';
    }
}

/**
 * Upsert the Delimit section in a file using <!-- delimit:start v<version> -->
 * and <!-- delimit:end --> markers.
 *
 * @param {string} filePath - Absolute path to the target file.
 * @param {string} newSection - The full managed-section text (including markers).
 * @param {string} [version] - Version string for staleness check. Defaults to package.json.
 * @returns {{action: 'created'|'updated'|'unchanged'|'appended'}}
 */
function upsertManagedSection(filePath, newSection, version) {
    if (!version) version = loadPackageVersion();

    if (!fs.existsSync(filePath)) {
        fs.writeFileSync(filePath, newSection + '\n');
        return { action: 'created' };
    }

    const rawExisting = fs.readFileSync(filePath, 'utf-8');
    // Strip a UTF-8 BOM if present so the start-of-line anchor still matches
    // the very first line of the file. We write back the stripped form to keep
    // serialization deterministic.
    const existing = rawExisting.replace(/^﻿/, '');

    const startMarkerRe = /^[ \t]*<!-- delimit:start[^>]*-->[ \t]*$/m;
    const endMarkerRe = /^[ \t]*<!-- delimit:end -->[ \t]*$/m;
    const startMatch = existing.match(startMarkerRe);
    const endMatch = existing.match(endMarkerRe);

    if (startMatch && endMatch) {
        // Extract current version from the marker (also anchored, allows indent)
        const versionMatch = existing.match(/^[ \t]*<!-- delimit:start v([^ ]+) -->[ \t]*$/m);
        const currentVersion = versionMatch ? versionMatch[1] : '';
        if (currentVersion === version) {
            return { action: 'unchanged' };
        }
        // Replace only the managed region — preserve content above/below
        const startIdx = startMatch.index;
        const endIdx = endMatch.index + endMatch[0].length;
        const before = existing.substring(0, startIdx);
        const after = existing.substring(endIdx);
        fs.writeFileSync(filePath, before + newSection + after);
        return { action: 'updated' };
    }

    // No markers present — append the managed section at the bottom.
    // User content above is preserved verbatim. Markers get added so future
    // upgrades can update just the managed region.
    const separator = existing.endsWith('\n') ? '\n' : '\n\n';
    fs.writeFileSync(filePath, existing + separator + newSection + '\n');
    return { action: 'appended' };
}

module.exports = { upsertManagedSection, loadPackageVersion };
