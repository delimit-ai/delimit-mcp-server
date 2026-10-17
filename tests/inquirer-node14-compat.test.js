'use strict';
// Exact upstream source plus real PTY interactions, using synthetic values.
// Supply DELIMIT_NODE14_BINARIES_JSON and DELIMIT_PTY_PYTHON explicitly.
// This test never invokes Delimit auth/setup/provider actions or owner profiles.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const cp = require('node:child_process');
const root = path.resolve(__dirname, '..');
const vendor = path.join(root, 'lib/vendor/inquirer-8.2.7');
const provenance = JSON.parse(fs.readFileSync(path.join(vendor, 'PROVENANCE.json'), 'utf8'));
const sha = bytes => crypto.createHash('sha256').update(bytes).digest('hex');

test('vendor origin, license and exact one-line upstream delta remain visible', () => {
    assert.equal(provenance.component.purl, 'pkg:npm/inquirer@8.2.7');
    assert.equal(provenance.component.gitHead, '9c5259a889d37205829c2255171ef74c6336f7f3');
    assert.equal(provenance.component.license, 'MIT');
    assert.equal(provenance.security_visibility.npm_audit_covers_this_component, false);
    assert.equal(sha(fs.readFileSync(path.join(vendor, 'LICENSE'))), provenance.license_sha256);
    const rows = Object.entries(provenance.source_files);
    assert.equal(rows.length, 22);
    for (const [name, binding] of rows) {
        const bytes = fs.readFileSync(path.join(root, name));
        assert.equal(sha(bytes), binding.proposed_sha256, name);
        if (name.endsWith('/lib/inquirer.js')) {
            const original = bytes.toString().replace(
                "    this.registerPrompt('password', require('./prompts/password'));\n",
                "    this.registerPrompt('password', require('./prompts/password'));\n"
                + "    this.registerPrompt('editor', require('./prompts/editor'));\n");
            assert.equal(sha(original), binding.upstream_sha256);
        } else assert.equal(binding.proposed_sha256, binding.upstream_sha256);
    }
    assert.equal(fs.existsSync(path.join(vendor, 'lib/prompts/editor.js')), false);
});

test('production graph retains Node14 floor and excludes unused editor packages', () => {
    const pkg = JSON.parse(fs.readFileSync(path.join(root, 'package.json')));
    assert.equal(pkg.engines.node, '>=14.0.0');
    for (const filename of ['package-lock.json', 'npm-shrinkwrap.json']) {
        const lock = JSON.parse(fs.readFileSync(path.join(root, filename)));
        for (const name of Object.keys(lock.packages)) {
            assert.equal(/node_modules\/(?:inquirer|external-editor|tmp)$|node_modules\/@inquirer\/external-editor/.test(name), false, name);
        }
        assert.deepEqual(lock.packages[''].dependencies, pkg.dependencies);
    }
    for (const name of ['bin/delimit-cli.js', 'bin/delimit-setup.js', 'lib/control-browser.js']) {
        const source = fs.readFileSync(path.join(root, name), 'utf8');
        assert.equal(/require\(['"]inquirer['"]\)/.test(source), false, name);
        assert.ok(source.includes('vendor/inquirer-8.2.7/lib/inquirer'), name);
    }
});

const scenarioScript = String.raw`
const q = require(process.argv[1]);
const scenario = process.argv[2];
const choices = [{name:'First',value:'first'},new q.Separator('AUDIT_BOUNDARY'),
    {name:'Middle',value:'middle'},{name:'Last',value:'last'}];
const definitions = {
 confirm: [{type:'confirm',name:'no',message:'AUDIT_CONFIRM_FALSE',default:false},
           {type:'confirm',name:'yes',message:'AUDIT_CONFIRM_TRUE',default:true}],
 input: [{type:'input',name:'text',message:'AUDIT_INPUT_TEXT'},
         {type:'input',name:'fallback',message:'AUDIT_INPUT_DEFAULT',default:'fallback'}],
 list: [{type:'list',name:'middle',message:'AUDIT_LIST_SKIP_SEPARATOR',choices,pageSize:2,loop:false,default:'first'},
        {type:'list',name:'last',message:'AUDIT_LIST_NO_LOOP',choices,pageSize:2,loop:false,default:'last'}],
 checkbox: [{type:'checkbox',name:'values',message:'AUDIT_CHECKBOX',choices:[
     {name:'First',value:'first',checked:true},new q.Separator('AUDIT_BOUNDARY'),
     {name:'Middle',value:'middle'},{name:'Last',value:'last',checked:true}]}],
 password: [{type:'password',name:'secret',message:'AUDIT_PASSWORD',mask:'*',
     validate:v => v.length >= 3 || 'AUDIT_PASSWORD_RETRY'}],
 cancel: [{type:'input',name:'cancel',message:'AUDIT_CANCEL'}]
};
q.prompt(definitions[scenario]).then(answers => {
 if(scenario==='password') answers={length:answers.secret.length,matches:answers.secret==='synthetic-secret'};
 if(Object.keys(require.cache).some(name=>name.includes('external-editor'))) throw Error('editor loaded');
 process.stdout.write('\n__RESULT__'+JSON.stringify(answers)+'__END__\n');
}).catch(()=>{process.stderr.write('synthetic prompt failed\n');process.exitCode=1});
`;

const driver = String.raw`
import errno, fcntl, json, os, pty, re, select, signal, struct, subprocess, sys, tempfile, time
node, entry, scenario, script = sys.argv[1:]
actions = {
 'confirm': [('AUDIT_CONFIRM_FALSE',b'\r'),('AUDIT_CONFIRM_TRUE',b'\r')],
 'input': [('AUDIT_INPUT_TEXT',b'synthetic input\r'),('AUDIT_INPUT_DEFAULT',b'\r')],
 'list': [('AUDIT_LIST_SKIP_SEPARATOR',b'\x1b[B\r'),('AUDIT_LIST_NO_LOOP',b'\x1b[B\r')],
 'checkbox': [('AUDIT_CHECKBOX',b' \x1b[B \r')],
 'password': [('AUDIT_PASSWORD',b'x\r'),('AUDIT_PASSWORD_RETRY',b'synthetic-secret\r')],
 'cancel': [('AUDIT_CANCEL',b'\x03')],
}[scenario]
with tempfile.TemporaryDirectory(prefix='delimit-synthetic-prompt-') as home:
 master, slave = pty.openpty()
 fcntl.ioctl(slave, 0x5414, struct.pack('HHHH',24,100,0,0))
 env={'PATH':'','HOME':home,'USERPROFILE':home,'TMPDIR':home,'TERM':'xterm','FORCE_COLOR':'0','CI':'1'}
 child=subprocess.Popen([node,'-e',script,entry,scenario],stdin=slave,stdout=slave,stderr=slave,cwd=home,env=env,start_new_session=True)
 os.close(slave)
 raw=b'';step=0;deadline=time.monotonic()+12
 try:
  while True:
   if time.monotonic()>deadline: raise AssertionError('PTY deadline')
   ready,_,_=select.select([master],[],[],0.1)
   if ready:
    try: chunk=os.read(master,65536)
    except OSError as exc:
     if exc.errno==errno.EIO: chunk=b''
     else: raise
    if chunk:
     raw+=chunk
     if len(raw)>131072: raise AssertionError('PTY output budget')
     text=re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]','',raw.decode('utf-8','replace'))
     if step<len(actions) and actions[step][0] in text:
      os.write(master,actions[step][1]);step+=1
    elif child.poll() is not None: break
   if child.poll() is not None and not ready: break
  child.wait(timeout=2)
  assert step==len(actions),'missing interaction'
  text=raw.decode('utf-8','replace')
  if scenario=='cancel':
   assert child.returncode in (-signal.SIGINT,130),'cancel did not terminate with SIGINT'
   assert '__RESULT__' not in text
   result={'cancelled':True}
  else:
   assert child.returncode==0,'prompt process failed'
   found=re.search(r'__RESULT__(.*?)__END__',text,re.S);assert found,'result missing'
   result=json.loads(found.group(1))
  if scenario=='password': assert 'synthetic-secret' not in text,'password leaked'
  if scenario in ('list','checkbox'): assert 'AUDIT_BOUNDARY' in text,'separator not rendered'
  print(json.dumps({'scenario':scenario,'result':result,'exit':child.returncode,'real_tty':True,'output_bytes':len(raw)}))
 finally:
  if child.poll() is None: child.kill();child.wait(timeout=2)
  os.close(master)
`;

test('actual Node14.0 and14.21 upstream prompt behavior on real PTYs', async t => {
    const python = process.env.DELIMIT_PTY_PYTHON;
    const versions = JSON.parse(process.env.DELIMIT_NODE14_BINARIES_JSON || 'null');
    assert.ok(python && path.isAbsolute(python), 'Supply an explicit stdlib Python PTY driver.');
    assert.ok(Array.isArray(versions) && versions.length === 2, 'Supply both actual Node14 binaries.');
    assert.deepEqual(versions.map(v => v.version).sort(), ['14.0.0', '14.21.3']);
    const expected = {
        confirm: { no: false, yes: true }, input: { text: 'synthetic input', fallback: 'fallback' },
        list: { middle: 'middle', last: 'last' }, checkbox: { values: ['middle', 'last'] },
        password: { length: 16, matches: true }, cancel: { cancelled: true },
    };
    for (const runtime of versions) for (const scenario of Object.keys(expected)) {
        await t.test(`${runtime.version} ${scenario}`, () => {
            assert.equal(sha(fs.readFileSync(runtime.node)), runtime.node_sha256);
            const result = cp.spawnSync(python, ['-I','-B','-c',driver,runtime.node,
                path.join(vendor,'lib/inquirer.js'),scenario,scenarioScript], {
                env: { PATH:'', HOME:'/nonexistent/delimit-prompt-test', PYTHONDONTWRITEBYTECODE:'1' },
                encoding:'utf8',timeout:16000,maxBuffer:262144,
            });
            assert.equal(result.error, undefined, 'PTY driver unavailable or timed out');
            assert.equal(result.status, 0, 'PTY interaction failed: ' + result.stderr);
            const report = JSON.parse(result.stdout);
            assert.equal(report.real_tty, true);
            assert.deepEqual(report.result, expected[scenario]);
        });
    }
});
