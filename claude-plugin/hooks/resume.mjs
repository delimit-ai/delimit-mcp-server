// Delimit SessionStart context. Deliberately read-only and dependency-free.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';

const deadline = Date.now() + 1300;
const MAX_FILE = 8 * 1024 * 1024;
const MAX_OUTPUT = 1500;
const safe = (value, n) => typeof value === 'string'
  ? value.replace(/[\x00-\x1f\x7f]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, n)
  : '';

function read(home, ...parts) {
  if (Date.now() > deadline) return null;
  let p = home;
  for (const part of parts) {
    p = path.join(p, part);
    const st = fs.lstatSync(p, { throwIfNoEntry: false });
    if (!st || st.isSymbolicLink()) return null;
  }
  const st = fs.lstatSync(p);
  if (!st.isFile() || st.size > MAX_FILE) return null;
  return fs.readFileSync(p, 'utf8');
}
function projectName() {
  // Match ledger_manager._detect_venture: local package metadata wins over git.
  try {
    const pkg = path.join(process.cwd(), 'package.json');
    const st = fs.lstatSync(pkg, { throwIfNoEntry: false });
    if (st?.isFile() && !st.isSymbolicLink() && st.size <= 65536) {
      const name = JSON.parse(fs.readFileSync(pkg, 'utf8')).name;
      if (typeof name === 'string' && name) return name.slice(0, 100);
    }
  } catch {}
  try {
    const py = path.join(process.cwd(), 'pyproject.toml');
    const st = fs.lstatSync(py, { throwIfNoEntry: false });
    if (st?.isFile() && !st.isSymbolicLink() && st.size <= 65536) {
      const match = fs.readFileSync(py, 'utf8').match(/^\s*name\s*=\s*["']([^"']+)/m);
      if (match) return match[1].slice(0, 100);
    }
  } catch {}
  try {
    const url = execFileSync('git', ['config', '--get', 'remote.origin.url'], {
      cwd: process.cwd(), encoding: 'utf8', timeout: 250, stdio: ['ignore', 'pipe', 'ignore']
    }).trim();
    const name = url.replace(/\/$/, '').split(/[/:]/).pop().replace(/\.git$/, '');
    if (name) return name.slice(0, 100);
  } catch {}
  // Mirror ledger_manager._detect_venture + registry_guards.is_ephemeral_path:
  // a path with no project signal that lives under a temp root or a
  // pytest/tempfile-style segment is "unsorted"; other bare dirs keep their
  // basename (the gateway remaps only server/.delimit-style junk names).
  const cwd = process.cwd();
  const tmpRoot = os.tmpdir().replace(/\/$/, '');
  const ephemeral = cwd === '/tmp' || cwd.startsWith('/tmp/')
    || cwd === tmpRoot || cwd.startsWith(tmpRoot + '/')
    || /(^|\/)(pytest-of-[^/]+|tmp[0-9a-z_]{4,}|test_[^/]+)(\/|$)/.test(cwd);
  const base = path.basename(cwd);
  if (ephemeral || base === 'server' || base === '.delimit' || base.startsWith('tmp')) return 'unsorted';
  return base;
}
function main() {
  const currentHome = os.homedir();
  const defaultHome = path.join(currentHome, '.delimit');
  const ambientHome = path.join(os.userInfo().homedir, '.delimit');
  const selectedHome = process.env.DELIMIT_HOME || process.env.DELIMIT_NAMESPACE_ROOT;
  const home = path.resolve(selectedHome && !(currentHome !== os.userInfo().homedir && selectedHome === ambientHome)
    ? selectedHome : defaultHome);
  const hs = fs.lstatSync(home, { throwIfNoEntry: false });
  const receiptHome = defaultHome; // handoff_receipts.py uses Path.home()/.delimit.
  const rs = fs.lstatSync(receiptHome, { throwIfNoEntry: false });
  if ((!hs || !hs.isDirectory() || hs.isSymbolicLink()) && (!rs || !rs.isDirectory() || rs.isSymbolicLink())) return;
  const repo = projectName();
  if (!repo || repo.length > 100) return;
  const aliases = { 'delimit-cli': 'delimit', 'delimit-mcp': 'delimit', 'delimit-gateway': 'delimit',
    'delimit-action': 'delimit', 'delimit-ui': 'delimit', wirereport: 'wire-report',
    'wire.report': 'wire-report', 'wire-api': 'wire-report', 'action-wire-report': 'wire-report',
    stakeone: 'stake-one', 'stake.one': 'stake-one', 'domain-monetization': 'domainvested',
    'domainvested-console': 'domainvested', 'electricgrill-com': 'domainvested' };
  const slug = aliases[repo.toLowerCase()] || repo.toLowerCase();
  const partitioned = new Set(['delimit', 'wire-report', 'domainvested', 'livetube',
    'stake-one', 'root', 'unsorted', 'solicitsignal']).has(slug);
  const cwd = fs.realpathSync(process.cwd());
  const hash = crypto.createHash('sha256').update(cwd).digest('hex').slice(0, 12);
  let handoff;
  const indexText = rs && rs.isDirectory() && !rs.isSymbolicLink()
    ? read(receiptHome, 'handoff_receipts', hash, 'index.json') : null;
  if (indexText) {
    const index = JSON.parse(indexText);
    const entries = Array.isArray(index.receipts) ? index.receipts : [];
    for (const entry of entries.slice(-200).reverse()) {
      if (Date.now() > deadline) return;
      const id = entry?.receipt_id;
      if (typeof id !== 'string' || !/^[a-f0-9-]{1,40}$/i.test(id)) continue;
      const body = read(receiptHome, 'handoff_receipts', hash, `${id}.json`);
      if (!body) continue;
      let item;
      try { item = JSON.parse(body); } catch { continue; }
      if (item.project_path !== cwd || item.acknowledged) continue;
      if (!handoff || String(item.created_at || '') > String(handoff.created_at || '')) handoff = item;
    }
  }
  let dir = ['ledger'];
  if (partitioned && hs && hs.isDirectory() && !hs.isSymbolicLink() && read(home, 'ledger-v2', slug, 'operations.jsonl') !== null) dir = ['ledger-v2', slug];
  else if (partitioned && hs && hs.isDirectory() && !hs.isSymbolicLink() && read(home, 'ledger', slug, 'operations.jsonl') !== null) dir = ['ledger', slug];
  const state = new Map();
  for (const file of ['operations.jsonl', 'strategy.jsonl']) {
    const data = hs && hs.isDirectory() && !hs.isSymbolicLink() ? read(home, ...dir, file) : null;
    if (data === null) continue;
    for (const line of data.split('\n')) {
      if (Date.now() > deadline) return;
      let item;
      try { item = JSON.parse(line); } catch { continue; }
      if (!item || typeof item !== 'object' || !item.id) continue;
      if (item.type === 'update') {
        const prior = state.get(item.id);
        if (prior) for (const field of ['status', 'priority']) if (field in item) prior[field] = item[field];
      } else state.set(item.id, item);
    }
  }
  const order = { P0: 0, P1: 1, P2: 2, P3: 3 };
  const items = [...state.values()].filter(x => x.venture === repo && x.status === 'open' && x.id && x.title)
    .sort((a, b) => (order[a.priority] ?? 9) - (order[b.priority] ?? 9)).slice(0, 5);
  if (!handoff && !items.length) return;
  const lines = [`Delimit — saved state for ${repo}`];
  if (handoff) {
    lines.push(`Handoff: ${safe(handoff.task_description, 220)}`);
    const undone = Array.isArray(handoff.not_completed) ? handoff.not_completed.map(x => safe(x, 140)).filter(Boolean).join('; ') : '';
    if (undone) lines.push(`Not completed: ${undone.slice(0, 320)}`);
    if (handoff.next_action) lines.push(`Next action: ${safe(handoff.next_action, 260)}`);
  }
  if (items.length) {
    lines.push('Open decisions/tasks:');
    for (const item of items) lines.push(`- ${safe(item.id, 30)} ${safe(item.title, 110)} (${safe(item.priority, 4)})`);
  }
  const footer = 'Records live in ~/.delimit; list them with the resume skill.';
  let body = lines.join('\n');
  body = body.slice(0, MAX_OUTPUT - footer.length - 2).trimEnd();
  process.stdout.write(`${body}\n${footer}\n`);
}
try { main(); } catch { /* A startup hook must never break a session. */ }
