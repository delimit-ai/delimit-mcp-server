'use strict';
const test=require('node:test'), assert=require('node:assert/strict');
const fs=require('fs'), path=require('path'), yaml=require('js-yaml');
const doc=yaml.load(fs.readFileSync(path.join(__dirname,'../.github/workflows/publish.yml'),'utf8'));
test('validation and publication use the same immutable reviewed gateway source',()=>{
    assert.match(doc.env.GATEWAY_SOURCE_SHA,/^[0-9a-f]{40}$/);
    for(const id of ['validate','publish']) {
        const steps=doc.jobs[id].steps;
        const checkouts=steps.filter(s=>s.with?.repository==='delimit-ai/delimit-gateway');
        assert.equal(checkouts.length,1);
        assert.equal(checkouts[0].with.ref,'${{ env.GATEWAY_SOURCE_SHA }}');
        const verify=steps.find(s=>s.name==='Verify frozen gateway source');
        assert(verify);assert(verify.run.includes('^[0-9a-f]{40}$'));
        assert(verify.run.includes('git -C delimit-gateway rev-parse HEAD'));
        assert(steps.indexOf(verify)>steps.indexOf(checkouts[0]));
        assert.equal(steps.find(s=>s.name==='Install dependencies').run,'npm ci');
    }
});

test('publication accepts then publishes the same packed file without another directory build',()=>{
    const steps=doc.jobs.publish.steps;
    const build=steps.findIndex(s=>s.name==='Build and pack exact release artifact');
    const accept=steps.findIndex(s=>s.name==='Accept exact packed artifact');
    assert(build>=0 && accept>build);
    assert.match(steps[build].run,/npm run prepublishOnly/);
    assert.match(steps[build].run,/npm pack --json --pack-destination/);
    assert(steps[build].run.indexOf('npm run prepublishOnly')<steps[build].run.indexOf('npm pack'));
    for(const name of ['Publish (dry run)','Publish to npm']) {
        const index=steps.findIndex(s=>s.name===name);
        assert(index>accept);
        assert.match(steps[index].run,/--verify "\$RELEASE_TARBALL" "\$RELEASE_EVIDENCE\/PASS.json"/);
        assert.match(steps[index].run,/npm publish "\$RELEASE_TARBALL"/);
        assert(!steps[index].run.includes('--ignore-scripts'));
    }
    assert.equal(doc.jobs.publish.needs,'validate');
    assert.match(steps.find(s=>s.name==='Verify published version').run,/d\.shasum!==a\.sha1/);
    assert.match(steps.find(s=>s.name==='Verify published version').run,/d\.integrity!==a\.integrity/);
});

const {spawnSync}=require('child_process');
function pythonCheck(code) {
    const result=spawnSync('python3',['-c',`import importlib.util, tempfile, pathlib, os, sys, subprocess\np=pathlib.Path('scripts/accept-packed-artifact.py').resolve()\ns=importlib.util.spec_from_file_location('acceptance',p); m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\n${code}`],{cwd:path.join(__dirname,'..'),encoding:'utf8',timeout:10000});
    assert.equal(result.status,0,result.stdout+result.stderr);
}
test('acceptance rejects empty/error/non-JSON MCP responses',()=>pythonCheck(`
for bad in ({'structuredContent':{}}, {'isError':True,'structuredContent':{'ok':True}}, {'content':[]}, {'content':[{'type':'text','text':'not json'}]}):
 try: m.payload(bad)
 except (RuntimeError,ValueError): pass
 else: raise AssertionError('accepted bad MCP response')
assert m.payload({'content':[{'type':'text','text':'{"status":"ok"}'}]}) == {'status':'ok'}
`));
test('acceptance rejects changed bytes and persists failing process diagnostics',()=>pythonCheck(`
with tempfile.TemporaryDirectory() as d:
 p=pathlib.Path(d)/'artifact';p.write_bytes(b'original');expected=m.digest(p)
 m.verify_digest(p,expected);p.write_bytes(b'changed')
 try: m.verify_digest(p,expected)
 except RuntimeError: pass
 else: raise AssertionError('accepted mutated tarball')
 log=pathlib.Path(d)/'failure'
 try: m.run([sys.executable,'-c','import sys; print("reason",file=sys.stderr);sys.exit(7)'],env={'PATH':os.defpath},cwd=d,log=log)
 except subprocess.CalledProcessError as e: assert e.returncode==7
 else: raise AssertionError('ignored failed process')
 assert 'reason' in pathlib.Path(str(log)+'.stderr').read_text()
`));
test('acceptance bounds a hung subprocess and saves timeout output',()=>pythonCheck(`
with tempfile.TemporaryDirectory() as d:
 log=pathlib.Path(d)/'timeout'
 try: m.run([sys.executable,'-u','-c','import time;print("started");time.sleep(30)'],env={'PATH':os.defpath},cwd=d,timeout=.1,log=log)
 except subprocess.TimeoutExpired: pass
 else: raise AssertionError('timeout failed')
 assert 'started' in pathlib.Path(str(log)+'.stdout').read_text()
`));
test('acceptance fails before setup for a tarball without native engines',()=>pythonCheck(`
import tarfile,io,json
with tempfile.TemporaryDirectory() as d:
 p=pathlib.Path(d)/'missing.tgz'
 with tarfile.open(p,'w:gz') as t:
  data=json.dumps({'version':'1.0.0'}).encode();info=tarfile.TarInfo('package/package.json');info.size=len(data);t.addfile(info,io.BytesIO(data))
 try: m.accept(p,'1.0.0',pathlib.Path(d)/'evidence')
 except RuntimeError as e: assert 'native module missing' in str(e)
 else: raise AssertionError('accepted missing native module')
`));

test('file publish dry-run preserves bytes and cannot rerun the directory build hook',()=>{
    const os=require('os'),crypto=require('crypto');
    const dir=fs.mkdtempSync(path.join(os.tmpdir(),'delimit-publish-file-'));
    try {
        fs.mkdirSync(path.join(dir,'home'));
        fs.writeFileSync(path.join(dir,'package.json'),JSON.stringify({name:'delimit-acceptance-never-publish',version:'0.0.0',scripts:{prepublishOnly:'node -e "process.exit(93)"'}}));
        const options={cwd:dir,env:{HOME:path.join(dir,'home'),PATH:process.env.PATH,npm_config_cache:path.join(dir,'cache')},encoding:'utf8',timeout:30000};
        const pack=spawnSync('npm',['pack','--json'],options);
        assert.equal(pack.status,0,pack.stderr);
        const tar=path.join(dir,JSON.parse(pack.stdout)[0].filename);
        const sha=()=>crypto.createHash('sha256').update(fs.readFileSync(tar)).digest('hex');
        const before=sha();
        const file=spawnSync('npm',['publish',tar,'--dry-run','--json'],options);
        assert.equal(file.status,0,file.stderr);
        assert.equal(sha(),before);
        const directory=spawnSync('npm',['publish','--dry-run'],options);
        assert.equal(directory.status,93,directory.stderr);
    } finally { fs.rmSync(dir,{recursive:true,force:true}); }
});
