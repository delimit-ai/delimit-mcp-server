'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('fs'),path=require('path'),yaml=require('js-yaml');
const {spawnSync}=require('child_process');
const doc=yaml.load(fs.readFileSync(path.join(__dirname,'../.github/workflows/publish.yml'),'utf8'));
const select=doc.jobs.select_artifact.steps[0].run.match(/python3 - <<'PYSELECT'\n([\s\S]+)\nPYSELECT/)[1];
const gate=doc.jobs.publish.steps.find(s=>s.name==='Enforce publication window and one-use Fin claim').run.match(/python3 - <<'PYGATE'\n([\s\S]+)\nPYGATE/)[1];
function check(code){
 const setup=`import copy,hashlib,json\nfrom datetime import datetime,timezone,timedelta\nns={'__name__':'fixture'}\nexec(${JSON.stringify(select)},ns)\nexec(${JSON.stringify(gate)},ns)\n`;
 const fixture=`
now=datetime(2026,10,3,20,tzinfo=timezone.utc)
accepted={'version':'1.2.3','sha256':'d'*64,'sha1':'e'*40,'integrity':'sha512-fixture-new','size':32}
value={'format':'shipping-npm-artifact/1','package':'delimit-cli','repo':'delimit-ai/delimit-mcp-server','repository_id':20,'workflow_id':40,
 'main_sha':'a'*40,'gateway_sha':'b'*40,'build_run_id':10,'build_run_attempt':1,'artifact_id':30,'artifact_zip_sha256':'c'*64,
 'artifact_expires_at':'2026-10-10T20:00:00Z','acceptance_sha256':'f'*64,'server_sha256':'a'*64,'channel':'latest',
 'previous':{'version':'1.2.2','shasum':'f'*40,'integrity':'sha512-fixture-old','published_at':'2026-10-02T18:00:00Z'},
 'next':accepted,'next_routine_at':'2026-10-03T18:00:00Z'}
order='wo_'+'a'*32
execution={'format':'shipping-npm-execution/1','order_id':order,'binding':{'object_sha256':hashlib.sha256(ns['canonical'](value)).hexdigest()},
 'envelope_sha256':'9'*64,'requested_at':int(now.timestamp()),'expires_at':int(now.timestamp())+300,'trigger':'owner_request',
 'policy':{'timezone':'UTC','restricted_window':{'start':'08:00','end':'17:00'},'manual_ops_restricted_days':['tuesday','wednesday']}}
run={'id':10,'run_attempt':1,'workflow_id':40,'path':'.github/workflows/publish.yml','head_sha':'a'*40,'head_branch':'main','event':'workflow_dispatch',
 'status':'completed','conclusion':'success','repository':{'id':20}}
artifact={'id':30,'expired':False,'name':'npm-release-'+'a'*40+'-1','digest':'sha256:'+'c'*64,'expires_at':value['artifact_expires_at'],
 'workflow_run':{'id':10,'head_sha':'a'*40,'head_branch':'main','repository_id':20,'head_repository_id':20}}
registry={'name':'delimit-cli','dist-tags':{'latest':'1.2.2'},'versions':{'1.2.2':{'dist':{'shasum':'f'*40,'integrity':'sha512-fixture-old'}}},
 'time':{'1.2.2':'2026-10-02T18:00:00Z'}}
def read(path):
 if '/runs/' in path:return run
 if '/artifacts/' in path:return artifact
 raise AssertionError(path)
def selected(v=None,e=None):return ns['select'](value if v is None else v,execution if e is None else e,order,source='a'*40,repository_id=20,read=read,now=now)
def gated(v=value,e=execution,r=None,t=None):return ns['gate'](registry if r is None else r,accepted,'1.2.3',bound=v,execution=e,order=order,now=now if t is None else t)
def refuses(fn):
 try:fn()
 except (RuntimeError,ValueError,KeyError,TypeError):return
 raise AssertionError('unsafe input accepted')
`;
 const r=spawnSync('python3',['-c',setup+fixture+code],{encoding:'utf8',timeout:10000});
 assert.equal(r.status,0,r.stdout+r.stderr);
}

test('Fin selects existing accepted bytes and never reruns build or acceptance with publication token',()=>{
 assert(doc.jobs.validate.if.includes("fin_order_id == ''"));
 assert(doc.jobs.select_artifact.if.includes('always()'));
 assert.deepEqual(doc.jobs.select_artifact.permissions,{contents:'read',actions:'read'});
 assert(!doc.jobs.select_artifact.steps.some(s=>s.uses?.startsWith('actions/checkout')));
 const steps=doc.jobs.publish.steps;const gateIndex=steps.findIndex(s=>s.name==='Enforce publication window and one-use Fin claim');
 assert(gateIndex<steps.findIndex(s=>s.name==='Publish to npm'));
 assert(steps.findIndex(s=>s.name==='Complete verified Fin publication receipt')>steps.findIndex(s=>s.name==='Verify published version'));
 assert.equal(doc.concurrency.group,'npm-delimit-cli-release');assert.equal(doc.concurrency['cancel-in-progress'],false);
});

test('exact prior run and immutable artifact provenance are selected',()=>check(`
assert selected()==(30,10,1)
assert gated()=='1.2.2'
for doc,key,bad in [(run,'head_sha','f'*40),(run,'workflow_id',41),(run,'path','other.yml'),(run,'conclusion','failure'),
 (run,'event','pull_request'),(run,'run_attempt',2),(artifact,'expired',True),(artifact,'digest','sha256:'+'f'*64)]:
 old=doc[key];doc[key]=bad;refuses(selected);doc[key]=old
artifact['workflow_run']['head_repository_id']=21;refuses(selected)
`));

test('source, capability binding, envelope and expiry cannot be changed or renewed',()=>check(`
for key,bad in [('main_sha','f'*40),('artifact_id',True),('extra','arbitrary')]:
 v=copy.deepcopy(value);v[key]=bad;refuses(lambda:selected(v=v))
for key,bad in [('order_id','wo_other'),('trigger','autonomous'),('binding',{'object_sha256':'f'*64}),('envelope_sha256','not-a-hash'),
 ('expires_at',int(now.timestamp())),('expires_at',int(now.timestamp())+301),('requested_at',True)]:
 e=copy.deepcopy(execution);e[key]=bad;refuses(lambda:selected(e=e))
`));

test('authenticated event excludes ordinary collaborators and changed dispatch input',()=>check(`
raw=json.dumps(value);cap=json.dumps(execution)
event={'sender':{'id':266558014,'login':'infracore'},'repository':{'id':20},
 'inputs':{'dry_run':'false','expected_source':'','fin_order_id':order,'fin_release':raw,'fin_execution':cap}}
f=lambda:ns['trusted_event'](event,order,raw,cap,actor_id=266558014,repository_id=20)
f()
event['sender']['id']=99;refuses(f);event['sender']['id']=266558014
event['inputs']['fin_release']='{}';refuses(f);event['inputs']['fin_release']=raw
event['repository']['id']=21;refuses(f)
`));

test('all standing and Fin publication shares actual rolling24h including prereleases',()=>check(`
assert gated(v=None,e=None)=='1.2.2'
registry['time']['1.3.0-beta.1']='2026-10-03T19:00:00Z'
refuses(gated);refuses(lambda:gated(v=None,e=None))
del registry['time']['1.3.0-beta.1']
registry['versions']['1.2.3']={};refuses(gated)
`));

test('Fin must retain exact previous latest and finish in current visible-action window',()=>check(`
registry['versions']['1.2.2']['dist']['integrity']='sha512-other';refuses(gated)
registry['versions']['1.2.2']['dist']['integrity']='sha512-fixture-old'
late=now+timedelta(seconds=300);refuses(lambda:gated(t=late))
monday=datetime(2026,10,5,12,tzinfo=timezone.utc)
e=copy.deepcopy(execution);e.update(requested_at=int(monday.timestamp()),expires_at=int(monday.timestamp())+300)
refuses(lambda:gated(e=e,t=monday))
`));

test('one durable claim rejects duplicates, in-progress, completed, reruns and uncertain create',()=>check(`
records=[];calls=[]
def remote(url,**kw):
 calls.append((url,kw))
 if 'data' in kw:
  records.append(kw['data']);return {'id':80}
 return {'total_count':len(records),'check_runs':records}
def claim(**changes):
 args=dict(run_id=70,attempt=1,actor_id=266558014,read=remote,now=now);args.update(changes)
 return ns['claim_order'](value,execution,order,**args)
result=claim();assert result['claim']['binding']==execution['binding'] and result['claim']['envelope_sha256']==execution['envelope_sha256']
assert records[0]['status']=='in_progress';refuses(claim)
records[0]['status']='completed';refuses(claim)
records.clear();refuses(lambda:claim(attempt=2));refuses(lambda:claim(actor_id=99))
def uncertain(url,**kw):
 result=remote(url,**kw)
 if 'data' in kw:raise TimeoutError('created but response lost')
 return result
try:claim(read=uncertain)
except TimeoutError:pass
else:raise AssertionError('uncertainty should propagate')
assert len(records)==1;refuses(claim)
`));

test('legacy publish wrapper routes through sole fixed workflow and fails actual tests',()=>{
 const text=fs.readFileSync(path.join(__dirname,'../scripts/publish-guard.sh'),'utf8');
 assert(!/\nnpm publish /.test(text));
 assert.match(text,/FAIL — test suite failed[\s\S]+FAIL=1/);
 assert(text.includes('gh workflow run publish.yml --repo delimit-ai/delimit-mcp-server --ref main'));
 assert(text.includes('-f expected_source="$EXPECTED_SOURCE"'));
});
