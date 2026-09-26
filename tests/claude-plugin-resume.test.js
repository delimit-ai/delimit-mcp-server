'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawnSync } = require('node:child_process');
const script = path.resolve(__dirname, '../claude-plugin/hooks/resume.mjs');
function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-resume-'));
  const home = path.join(root, 'home'); const project = path.join(root, 'project');
  fs.mkdirSync(home); fs.mkdirSync(project);
  const run = () => spawnSync(process.execPath, [script], { cwd: project, env: { ...process.env, GIT_DIR: undefined, GIT_WORK_TREE: undefined, GIT_INDEX_FILE: undefined, HOME: home, DELIMIT_HOME: path.join(home, '.delimit'), GIT_CONFIG_NOSYSTEM: '1', GIT_CONFIG_GLOBAL: '/dev/null' }, encoding: 'utf8', timeout: 2000 });
  const store = path.join(home, '.delimit');
  function ledger(lines) { fs.mkdirSync(path.join(store, 'ledger'), { recursive: true }); fs.writeFileSync(path.join(store, 'ledger', 'operations.jsonl'), lines.join('\n')); }
  function receipt(id, attrs = {}) {
    const hash = crypto.createHash('sha256').update(fs.realpathSync(project)).digest('hex').slice(0, 12);
    const dir = path.join(store, 'handoff_receipts', hash); fs.mkdirSync(dir, { recursive: true });
    const item = { receipt_id: id, project_path: project, task_description: `Task ${id}`, not_completed: ['unfinished'], next_action: 'next', created_at: id, acknowledged: false, ...attrs };
    fs.writeFileSync(path.join(dir, `${id}.json`), JSON.stringify(item));
    const index = path.join(dir, 'index.json'); let data = { receipts: [] }; if (fs.existsSync(index)) data = JSON.parse(fs.readFileSync(index));
    data.receipts.push({ receipt_id: id }); fs.writeFileSync(index, JSON.stringify(data));
  }
  return { root, home, project, store, run, ledger, receipt };
}
function withFixture(fn) { const f = fixture(); try { fn(f); } finally { try { fs.chmodSync(f.home, 0o755); fs.chmodSync(f.store, 0o755); } catch {} fs.rmSync(f.root, { recursive: true, force: true }); } }
test('missing home prints nothing', () => withFixture(f => { const r=f.run(); assert.equal(r.status,0); assert.equal(r.stdout,''); }));
test('other project records do not appear', () => withFixture(f => { f.ledger([JSON.stringify({id:'LED-1',title:'secret',venture:'other',status:'open'})]); f.receipt('001', { project_path:'/another/project' }); assert.equal(f.run().stdout,''); }));
test('latest pending handoff wins and acknowledged handoffs are skipped', () => withFixture(f => { f.receipt('001'); f.receipt('002'); f.receipt('003',{acknowledged:true}); const out=f.run().stdout; assert.match(out,/Task 002/); assert.doesNotMatch(out,/Task 001|Task 003/); }));
test('corrupt lines are ignored and updates fold', () => withFixture(f => { f.ledger(['bad json', JSON.stringify({id:'LED-1',title:'Done',venture:'unsorted',status:'open',priority:'P0'}), JSON.stringify({id:'LED-1',type:'update',status:'done'}), JSON.stringify({id:'STR-2',title:'Open',venture:'unsorted',status:'open',priority:'P1'})]); const out=f.run().stdout; assert.match(out,/STR-2 Open/); assert.doesNotMatch(out,/LED-1/); }));
test('output is capped and oversized or symlinked files are ignored', () => withFixture(f => { f.ledger(Array.from({length:8},(_,i)=>JSON.stringify({id:`LED-${i}`,title:'x'.repeat(500),venture:'unsorted',status:'open',priority:'P1'}))); assert.ok(f.run().stdout.length<=1500); const file=path.join(f.store,'ledger','operations.jsonl'); fs.writeFileSync(file,Buffer.alloc(8*1024*1024+1)); assert.equal(f.run().stdout,''); fs.unlinkSync(file); fs.symlinkSync('/etc/passwd',file); assert.equal(f.run().stdout,''); }));
test('read-only home runs with no network and no writes', () => withFixture(f => { f.receipt('001'); const before=fs.readFileSync(path.join(f.store, 'handoff_receipts', crypto.createHash('sha256').update(fs.realpathSync(f.project)).digest('hex').slice(0,12), '001.json')); fs.chmodSync(f.store,0o555); fs.chmodSync(f.home,0o555); const r=f.run(); assert.equal(r.status,0); assert.match(r.stdout,/Task 001/); assert.deepEqual(fs.readFileSync(path.join(f.store, 'handoff_receipts', crypto.createHash('sha256').update(fs.realpathSync(f.project)).digest('hex').slice(0,12), '001.json')),before); }));
