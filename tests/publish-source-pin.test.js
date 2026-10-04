'use strict';
const test=require('node:test'), assert=require('node:assert/strict');
const fs=require('fs'), path=require('path'), yaml=require('js-yaml');
const doc=yaml.load(fs.readFileSync(path.join(__dirname,'../.github/workflows/publish.yml'),'utf8'));
test('validation and publication use the same immutable reviewed gateway source',()=>{
    assert.match(doc.env.GATEWAY_SOURCE_SHA,/^[0-9a-f]{40}$/);
    for(const id of ['validate','build_release']) {
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

test('build and acceptance cannot access npm OIDC or persisted Git credentials',()=>{
    assert.deepEqual(doc.permissions,{contents:'read'});
    assert.equal(doc.concurrency.group,'npm-delimit-cli-release');
    assert.equal(doc.concurrency['cancel-in-progress'],false);
    for(const name of ['validate','build_release']) {
        const job=doc.jobs[name];
        assert.notEqual(job.permissions?.['id-token'],'write');
        for(const step of job.steps) {
            assert(!step.env?.NODE_AUTH_TOKEN);
            if(step.uses?.startsWith('actions/checkout@')) assert.equal(step.with['persist-credentials'],false);
        }
    }
    assert.deepEqual(doc.jobs.publish.permissions,{contents:'read',actions:'read',checks:'write','id-token':'write'});
    assert(doc.jobs.validate.if.includes("fin_order_id == ''"));
    assert(doc.jobs.select_artifact.steps[0].run.includes('complete Fin dispatch required'));
});

test('publication consumes the immutable accepted artifact without package execution',()=>{
    const buildSteps=doc.jobs.build_release.steps, steps=doc.jobs.publish.steps;
    const build=buildSteps.findIndex(s=>s.name==='Build and pack exact release artifact');
    const accept=buildSteps.findIndex(s=>s.name==='Accept exact packed artifact');
    assert(build>=0 && accept>build);
    assert.match(buildSteps[build].run,/npm run prepublishOnly/);
    assert.match(buildSteps[build].run,/npm pack --json --pack-destination/);
    assert(buildSteps[build].run.indexOf('npm run prepublishOnly')<buildSteps[build].run.indexOf('npm pack'));
    const download=steps.find(s=>s.name==='Download exact accepted artifact');
    assert.equal(download.with['artifact-ids'],'${{ needs.select_artifact.outputs.artifact_id }}');
    assert.equal(download.with['run-id'],'${{ needs.select_artifact.outputs.run_id }}');
    assert.equal(doc.jobs.build_release.outputs.artifact_id,'${{ steps.accepted.outputs.artifact-id }}');
    assert.equal(doc.jobs.publish.needs,'select_artifact');
    assert.deepEqual(doc.jobs.select_artifact.needs,['validate','build_release']);
    assert.equal(doc.jobs.build_release.needs,'validate');
    assert(!steps.some(s=>s.uses?.startsWith('actions/checkout@')));
    assert(!steps.some(s=>/npm ci|npm run|npm pack|accept-packed-artifact\.py/.test(s.run||'')));
    const verify=steps.findIndex(s=>s.name==='Verify immutable publication inputs');
    for(const name of ['Publish (dry run)','Publish to npm']) {
        const index=steps.findIndex(s=>s.name===name);
        assert(index>verify);
        assert.match(steps[index].run,/npm publish "\$RELEASE_TARBALL" --ignore-scripts/);
        assert.match(steps[index].run,/--registry=https:\/\/registry\.npmjs\.org --tag latest/);
        assert.equal(steps[index]['working-directory'],'${{ runner.temp }}/delimit-publish');
    }
    assert.deepEqual(steps.filter(s=>s.env?.NODE_AUTH_TOKEN).map(s=>s.name),['Publish to npm']);
    assert.match(steps.find(s=>s.name==='Verify published version').run,/d\.shasum!==a\.sha1/);
    assert.match(steps.find(s=>s.name==='Verify published version').run,/d\.integrity!==a\.integrity/);
});

const {spawnSync}=require('child_process');
const verificationStep=doc.jobs.publish.steps.find(s=>s.name==='Verify immutable publication inputs');
const verifier=verificationStep.run.match(/python3 - <<'PYVERIFY'\n([\s\S]+)\nPYVERIFY/)[1];
function publicationCheck(code) {
    const setup=`import base64,hashlib,io,json,pathlib,tarfile,tempfile\nns={'__name__':'fixture'}\nexec(${JSON.stringify(verifier)},ns)\n`;
    const fixture=`
def fixture(root,mutate=None):
 package={'name':'delimit-cli','version':'1.2.3','repository':{'url':'https://github.com/delimit-ai/delimit-mcp-server.git'},'scripts':{'postpublish':'exit 97'}}
 if mutate: mutate(package)
 tar=root/'delimit-cli-1.2.3.tgz'
 with tarfile.open(tar,'w:gz') as t:
  raw=json.dumps(package).encode();i=tarfile.TarInfo('package/package.json');i.size=len(raw);t.addfile(i,io.BytesIO(raw))
 raw=tar.read_bytes()
 accepted=dict(version='1.2.3',sha256=hashlib.sha256(raw).hexdigest(),sha1=hashlib.sha1(raw).hexdigest(),integrity='sha512-'+base64.b64encode(hashlib.sha512(raw).digest()).decode(),size=len(raw))
 (root/'acceptance').mkdir();(root/'acceptance/PASS.json').write_text(json.dumps(accepted))
 server={'name':'io.github.delimit-ai/delimit-mcp-server','version':'1.2.3','packages':[{'identifier':'delimit-cli','version':'1.2.3'}]}
 (root/'server.json').write_text(json.dumps(server))
 h=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
 meta=dict(format='delimit-release-artifact/1',package='delimit-cli',version='1.2.3',source_sha='a'*40,gateway_sha='b'*40,run_id='123',run_attempt='1',tarball=tar.name,sha256=h(tar),acceptance_sha256=h(root/'acceptance/PASS.json'),server_sha256=h(root/'server.json'))
 (root/'release.json').write_text(json.dumps(meta))
 return meta,accepted
def verify(root,**changes):
 args=dict(source='a'*40,repository='delimit-ai/delimit-mcp-server',run_id='123',attempt='1',gateway='b'*40);args.update(changes)
 return ns['verify'](root,**args)
`;
    const result=spawnSync('python3',['-c',setup+fixture+code],{encoding:'utf8',timeout:10000});
    assert.equal(result.status,0,result.stdout+result.stderr);
}

test('publisher verifies exact accepted bytes and reads metadata without running package scripts',()=>publicationCheck(`
with tempfile.TemporaryDirectory() as d:
 root=pathlib.Path(d);meta,accepted=fixture(root)
 got=verify(root);assert got[0]==meta and got[1]==accepted
`));

test('publisher rejects changed source, run, repository, bytes and unsafe publication metadata',()=>publicationCheck(`
for fault in ('source','run','repo','gateway','artifact','receipt','server','path','version','publishConfig','packageName','packageRepo','symlink'):
 with tempfile.TemporaryDirectory() as d:
  root=pathlib.Path(d)
  def mutate(p):
   if fault=='publishConfig':p['publishConfig']={'registry':'https://attacker.invalid'}
   if fault=='packageName':p['name']='another-package'
   if fault=='packageRepo':p['repository']['url']='https://attacker.invalid/repo'
  meta,accepted=fixture(root,mutate)
  args={}
  if fault=='source':args['source']='c'*40
  if fault=='run':args['run_id']='456'
  if fault=='repo':args['repository']='attacker/repo'
  if fault=='gateway':args['gateway']='c'*40
  if fault in ('artifact','receipt','server'):
   p={'artifact':root/meta['tarball'],'receipt':root/'acceptance/PASS.json','server':root/'server.json'}[fault];p.write_bytes(p.read_bytes()+b'changed')
  if fault=='path':meta['tarball']='../escape.tgz';(root/'release.json').write_text(json.dumps(meta))
  if fault=='version':meta['version']='1.2.3\\nNPM_CONFIG_REGISTRY=https://attacker.invalid';(root/'release.json').write_text(json.dumps(meta))
  if fault=='symlink':
   p=root/'acceptance/PASS.json';other=root/'elsewhere';p.rename(other);p.symlink_to(other)
  try:verify(root,**args)
  except (ValueError,RuntimeError,OSError):pass
  else:raise AssertionError('unsafe publication input accepted: '+fault)
`));

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
        const file=spawnSync('npm',['publish',tar,'--ignore-scripts','--dry-run','--json'],options);
        assert.equal(file.status,0,file.stderr);
        assert.equal(sha(),before);
        const directory=spawnSync('npm',['publish','--dry-run'],options);
        assert.equal(directory.status,93,directory.stderr);
    } finally { fs.rmSync(dir,{recursive:true,force:true}); }
});

test('credential-free no-provider deliberation status is useful, with strict quota and OAuth types',()=>pythonCheck(`
os.environ.clear()
# Actual fresh CI runtime shape: no API keys, CLIs, hosted keys or OAuth login.
status={'mode':'none','install_id':'fresh-ci-install','hosted_used':0,'hosted_remaining':3,'hosted_limit':3,'oauth_required':True,'oauth_signed_in':False}
m.validate_deliberation_status(status)
for mode in ('byok','hosted'):
 m.validate_deliberation_status(dict(status,mode=mode))
for field,value in [('mode','unknown'),('hosted_used',True),('hosted_remaining','3'),('hosted_limit',-1),('hosted_remaining',4),('oauth_required','true'),('oauth_signed_in',0),('install_id','')]:
 try: m.validate_deliberation_status(dict(status,**{field:value}))
 except RuntimeError: pass
 else: raise AssertionError('invalid status accepted: '+field)
for field in ('mode','hosted_used','hosted_remaining','hosted_limit','oauth_required','oauth_signed_in','install_id'):
 bad=dict(status);del bad[field]
 try: m.validate_deliberation_status(bad)
 except RuntimeError: pass
 else: raise AssertionError('missing field accepted: '+field)
`));
