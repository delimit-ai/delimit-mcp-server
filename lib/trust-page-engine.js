// lib/trust-page-engine.js
//
// LED-1018 Venture #6 MVP: `delimit trust-page` render.
// Scans a directory of delimit.attestation.v1 JSON files, verifies signatures,
// renders a static index.html + JSON Feed 1.1-shaped feed.json.
//
// Local-only render. Cloud sync is a Pro/Premium feature, deferred.

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const os = require('os');

function loadHmacKey() {
    const keyPath = path.join(os.homedir(), '.delimit', 'wrap-hmac.key');
    if (!fs.existsSync(keyPath)) return null;
    return fs.readFileSync(keyPath);
}

function verifySignature(attestation, key) {
    if (!key) return 'unverifiable';
    try {
        const canonical = JSON.stringify(attestation.bundle, Object.keys(attestation.bundle).sort());
        const expected = crypto.createHmac('sha256', key).update(canonical).digest('hex');
        return expected === attestation.signature ? 'verified' : 'signature_mismatch';
    } catch {
        return 'verify_error';
    }
}

function loadAttestations(dir) {
    if (!fs.existsSync(dir)) return [];
    const results = [];
    for (const f of fs.readdirSync(dir)) {
        if (!f.startsWith('att_') || !f.endsWith('.json')) continue;
        try {
            const att = JSON.parse(fs.readFileSync(path.join(dir, f), 'utf-8'));
            if (att.id && att.bundle) results.push(att);
        } catch { /* skip corrupted */ }
    }
    // Reverse-chronological
    results.sort((a, b) => {
        const ta = a.bundle?.started_at || '';
        const tb = b.bundle?.started_at || '';
        return tb.localeCompare(ta);
    });
    return results;
}

function escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

function redactCommand(cmd, redactLevel = 'basic') {
    // MVP: basic redaction only. Strip quoted strings longer than 24 chars (likely prompt text).
    if (!cmd) return '';
    if (redactLevel === 'none') return cmd;
    return cmd.replace(/"[^"]{24,}"/g, '"<prompt redacted>"')
              .replace(/'[^']{24,}'/g, "'<prompt redacted>'");
}

function countGateResults(governance) {
    const gates = governance?.gates || [];
    let pass = 0, fail = 0, info = 0;
    for (const g of gates) {
        if (g.exit === 0) pass++;
        else if (g.exit !== undefined) fail++;
        else info++;
    }
    return { pass, fail, info, total: gates.length };
}

function renderHTML(attestations, title = 'Trust Page') {
    const hmacKey = loadHmacKey();
    const rows = attestations.map(att => {
        const verify = verifySignature(att, hmacKey);
        const b = att.bundle || {};
        const counts = countGateResults(b.governance);
        const violations = (b.governance?.violations || []).length;
        const status = violations > 0 ? 'violations' : (counts.fail > 0 ? 'failures' : 'clean');
        return `    <tr>
      <td><code>${escapeHtml(att.id)}</code></td>
      <td class="cmd">${escapeHtml(redactCommand(b.wrapped_command))}</td>
      <td>${escapeHtml(b.started_at || '')}</td>
      <td class="gates">${counts.pass}/${counts.total}</td>
      <td class="v-${verify}">${verify.replace('_', ' ')}</td>
      <td class="s-${status}">${status}</td>
    </tr>`;
    }).join('\n');

    const empty = attestations.length === 0
        ? `  <p class="empty">No attestations yet. Run <code>delimit wrap &lt;cmd&gt;</code> to create one.</p>`
        : '';

    return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>${escapeHtml(title)}</title>
<style>
  :root { color-scheme: light dark; --fg:#111; --bg:#fff; --muted:#666; --ok:#087443; --warn:#b45309; --err:#b91c1c; }
  @media (prefers-color-scheme: dark) { :root { --fg:#eee; --bg:#0a0a0a; --muted:#888; } }
  body { font: 14px/1.5 -apple-system, BlinkMacSystemFont, system-ui, sans-serif; color: var(--fg); background: var(--bg); max-width: 980px; margin: 2rem auto; padding: 0 1rem; }
  h1 { margin-bottom: .25rem; font-weight: 600; }
  .sub { color: var(--muted); margin-bottom: 2rem; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: .5rem .75rem; border-bottom: 1px solid rgba(0,0,0,.08); }
  th { font-weight: 600; color: var(--muted); font-size: .82em; text-transform: uppercase; letter-spacing: .03em; }
  code { font: 12px/1.3 ui-monospace, SFMono-Regular, Menlo, monospace; }
  td.cmd { max-width: 380px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .gates { font-variant-numeric: tabular-nums; color: var(--muted); }
  .v-verified { color: var(--ok); }
  .v-unverifiable, .v-signature_mismatch { color: var(--warn); }
  .s-clean { color: var(--ok); }
  .s-violations, .s-failures { color: var(--err); }
  .empty { color: var(--muted); padding: 3rem 0; text-align: center; }
  footer { margin-top: 3rem; color: var(--muted); font-size: .85em; }
</style>
</head>
<body>
  <h1>${escapeHtml(title)}</h1>
  <p class="sub">${attestations.length} signed attestation${attestations.length === 1 ? '' : 's'} · generated ${new Date().toISOString()}</p>
${empty || `  <table>
    <thead><tr><th>ID</th><th>Command</th><th>Started</th><th>Gates</th><th>Signature</th><th>Status</th></tr></thead>
    <tbody>
${rows}
    </tbody>
  </table>`}
  <footer>
    Generated by <code>delimit trust-page</code>. <a href="feed.json">JSON Feed</a>.<br>
    Each row is an <code>att_*</code> record signed with HMAC-SHA256. Schema: <code>delimit.attestation.v1</code>.
  </footer>
</body>
</html>
`;
}

function renderFeed(attestations, title = 'Trust Page') {
    const items = attestations.map(att => {
        const b = att.bundle || {};
        const verify = verifySignature(att, loadHmacKey());
        return {
            id: att.id,
            title: redactCommand(b.wrapped_command || att.id),
            content_text: `wrapped=${b.wrapped_command || ''} | exit=${b.wrapped_exit} | gates=${(b.governance?.gates || []).length} | violations=${(b.governance?.violations || []).length} | signature=${verify}`,
            date_published: b.started_at,
            _delimit: {
                attestation_id: att.id,
                signature: att.signature,
                signature_alg: att.signature_alg,
                wrapped_exit: b.wrapped_exit,
                changed_files: b.changed_files,
                governance: b.governance,
                ai_surface: b.ai_surface || null,
            }
        };
    });
    return {
        version: 'https://jsonfeed.org/version/1.1',
        title,
        description: 'Signed replayable attestations for AI-assisted merges. Schema: delimit.attestation.v1.',
        items,
    };
}

function renderTrustPage(attestationDir, outDir, title) {
    const attestations = loadAttestations(attestationDir);
    fs.mkdirSync(outDir, { recursive: true });
    const html = renderHTML(attestations, title);
    const feed = renderFeed(attestations, title);
    fs.writeFileSync(path.join(outDir, 'index.html'), html);
    fs.writeFileSync(path.join(outDir, 'feed.json'), JSON.stringify(feed, null, 2));
    return { count: attestations.length, outDir, html_bytes: html.length, feed_items: feed.items.length };
}

module.exports = { renderTrustPage, loadAttestations, verifySignature, renderHTML, renderFeed };
