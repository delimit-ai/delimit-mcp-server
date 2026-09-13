const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const h = require('../lib/harness-launch');

test('actual setup block propagates explicit shim failures to its CLI error handler', () => {
    const vm = require('vm');
    const source = fs.readFileSync(path.join(__dirname, '../bin/delimit-setup.js'), 'utf8');
    const start = source.indexOf('            try {', source.indexOf('// The new explicit harness shims'));
    const end = source.indexOf('            // Governance is enforced via PATH ordering', start);
    assert(start > 0 && end > start);
    const block = source.slice(start, end);
    for (const explicit of [true, false]) {
        const warnings = [];
        const context = {options: {harnessShims: explicit}, process: {argv: []},
            DELIMIT_HOME: '/synthetic', __dirname, path,
            require: () => ({installHarnessShims: () => {throw new Error('fixture collision');}}),
            log: message => warnings.push(message), yellow: message => message};
        if (explicit) assert.throws(() => vm.runInNewContext(block, context), /Requested harness shims were not installed/);
        else {vm.runInNewContext(block, context); assert.equal(warnings.length, 1);}
    }
    const cli = fs.readFileSync(path.join(__dirname, '../bin/delimit-cli.js'), 'utf8');
    assert.match(cli, /return runSetup\([\s\S]*?\.catch\(\(err\) => \{[\s\S]*?process\.exitCode = 1/);
});

function fixture(t) {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-harness-'));
    t.after(() => fs.rmSync(root, {recursive: true, force: true}));
    const home = path.join(root, 'home'), repo = path.join(root, 'repo');
    fs.mkdirSync(path.join(home, '.local/bin'), {recursive: true});
    fs.mkdirSync(path.join(home, '.config/muse'), {recursive: true});
    fs.mkdirSync(repo);
    for (const n of ['muse-bin-1.2.1-R2847.1', 'copilot']) fs.writeFileSync(path.join(home, '.local/bin', n), '#!/bin/sh\nexit 0\n', {mode:0o755});
    const config = path.join(home, '.config/muse/settings.json');
    fs.writeFileSync(config, JSON.stringify({model:'muse-spark-1.3',context:{foreign_personal_rules:false,foreign_personal_skills:false}}));
    const calls = [], errors = [];
    const opts = {cwd:repo,env:{HOME:home},error:m=>errors.push(m),spawnSync:(cmd,args,opts)=>{
        calls.push({cmd,args,opts}); return cmd==='git'? {status:0,stdout:repo+'\n'}:{status:0};
    }};
    return {root,home,repo,config,calls,errors,opts};
}
test('Muse uses Standard, correct project and bounded bootstrap without permission bypass', t=>{
    const f=fixture(t); assert.equal(h.launchExplicitHarness('muse',f.opts),0);
    const c=f.calls.at(-1); assert.equal(c.args[3],'muse-spark-1.3'); assert.equal(c.opts.cwd,f.repo);
    assert(!c.args.includes('--no-foreign-personal-context')); // exec-only, not TUI
    assert(!c.args.some(a=>/--(yolo|disable-approval|disable-sandbox|trust-workspace)/.test(a)));
    assert(c.args.at(-1).includes('supported Delimit tooling')); assert(Buffer.byteLength(c.args.at(-1))<16384);
});
test('both instruction sources are referenced without modifying or exporting their contents', t=>{
    const f=fixture(t);
    const contents={'AGENTS.md':'Unique agents guidance\n','CLAUDE.md':'Unique private Claude guidance\n'};
    for(const [name,body] of Object.entries(contents))fs.writeFileSync(path.join(f.repo,name),body);
    assert.equal(h.launchExplicitHarness('muse',f.opts),0);
    const prompt=f.calls.at(-1).args.at(-1);
    for(const [name,body] of Object.entries(contents)) {
        const file=path.join(f.repo,name);
        assert(prompt.includes(file));
        assert(prompt.includes(require('crypto').createHash('sha256').update(body).digest('hex')));
        assert(!prompt.includes(body.trim()));
        assert.equal(fs.readFileSync(file,'utf8'),body);
    }
    assert(prompt.includes('Read every listed source completely'));
    assert(prompt.includes('Preserve native scope/precedence'));
});
test('Copilot interactive arguments do not use noninteractive prompt or allow-all', t=>{
    const f=fixture(t); assert.equal(h.launchExplicitHarness('copilot',f.opts),0);
    assert(f.calls.at(-1).args.includes('-i')); assert(!f.calls.at(-1).args.includes('-p'));
    assert(f.calls.at(-1).args.includes('--no-remote-export'));
});
test('help does not require project/config or run git/inference', t=>{
    const f=fixture(t); fs.unlinkSync(f.config);
    assert.equal(h.launchExplicitHarness('muse',{...f.opts,args:['--help'],cwd:f.home}),0);
    assert.equal(f.calls.length,1); assert.deepEqual(f.calls[0].args,['--help']);
});
for(const change of [{model:'muse-spark-1.3-contributor'},{context:{}},{provider:'other'},{endpoint_transport:{base_url:'https://example.com'}}]) {
    test('Muse rejects mismatched route/config '+JSON.stringify(change), t=>{
        const f=fixture(t); const d=JSON.parse(fs.readFileSync(f.config)); fs.writeFileSync(f.config,JSON.stringify({...d,...change}));
        assert.equal(h.launchExplicitHarness('muse',f.opts),1); assert(f.calls.every(c=>c.cmd==='git'));
    });
}
test('rejects API billing override without exposing it',t=>{
    const f=fixture(t); f.opts.env.META_API_KEY='synthetic-never-print';
    assert.equal(h.launchExplicitHarness('muse',f.opts),1); assert.equal(f.calls.length,0);
    assert(!JSON.stringify(f.errors).includes('synthetic-never-print'));
});
test('rejects root and non-project instead of assigning wrong venture',t=>{
    const f=fixture(t); assert.equal(h.launchExplicitHarness('muse',{...f.opts,cwd:f.home}),1);
    assert.equal(f.calls.length,1);
});
test('oversize personal bootstrap and project rules fail without truncation',t=>{
    const f=fixture(t); fs.mkdirSync(path.join(f.home,'.delimit'));
    fs.writeFileSync(path.join(f.home,'.delimit/harness-bootstrap.md'),'a'.repeat(16385));
    assert.equal(h.launchExplicitHarness('muse',f.opts),1);
    fs.unlinkSync(path.join(f.home,'.delimit/harness-bootstrap.md'));
    fs.writeFileSync(path.join(f.repo,'AGENTS.md'),'a'.repeat(65536));
    assert.equal(h.launchExplicitHarness('muse',f.opts),1);
    assert(f.calls.every(c=>c.cmd==='git'));
});
for (const oversized of ['AGENTS.md', 'CLAUDE.md']) {
    test('checks both co-located rules including enclosing directory: '+oversized,t=>{
        const f=fixture(t);
        const child=path.join(f.repo,'nested'); fs.mkdirSync(child);
        for (const name of ['AGENTS.md','CLAUDE.md']) {
            fs.writeFileSync(path.join(f.repo,name), name===oversized?'a'.repeat(65536):'small');
            fs.writeFileSync(path.join(child,name),'small');
        }
        assert.equal(h.launchExplicitHarness('muse',{...f.opts,cwd:child}),1);
        assert(f.calls.every(c=>c.cmd==='git'));
        assert(f.errors.some(e=>e.includes('startup bound')));
    });
}
test('no native resume or arbitrary flags translated',t=>{
    const f=fixture(t); assert.equal(h.launchExplicitHarness('muse',{...f.opts,args:['resume','old-id']}),1);
    assert.equal(f.calls.length,0);
});
test('exit signal is failure, never launch another model',t=>{
    const f=fixture(t), spawn=f.opts.spawnSync; f.opts.spawnSync=(cmd,args,opts)=>cmd==='git'?spawn(cmd,args,opts):{status:null,signal:'SIGINT'};
    assert.equal(h.launchExplicitHarness('muse',f.opts),130);
});
test('only managed shims overwritten; other client/config files untouched',t=>{
    const f=fixture(t), dh=path.join(f.home,'.delimit');
    h.installHarnessShims({delimitHome:dh,packageRoot:path.dirname(__dirname)});
    const file=path.join(dh,'shims/muse'); assert(fs.readFileSync(file,'utf8').includes('launchHarnessShim'));
    h.installHarnessShims({delimitHome:dh,packageRoot:path.dirname(__dirname)});
    fs.writeFileSync(file,'owner script');
    assert.throws(()=>h.installHarnessShims({delimitHome:dh,packageRoot:path.dirname(__dirname)}),/unrecognized/);
    assert.equal(fs.readFileSync(file,'utf8'),'owner script');
});

test('interactive sessions scrub inherited git routing without changing parent env', t=>{
    const f=fixture(t);
    const keys=['GIT_DIR','GIT_WORK_TREE','GIT_INDEX_FILE','GIT_OBJECT_DIRECTORY','GIT_COMMON_DIR','GIT_QUARANTINE_PATH'];
    for(const key of keys) f.opts.env[key]='synthetic-wrong-repository';
    const before={...f.opts.env};
    assert.equal(h.launchExplicitHarness('muse',f.opts),0);
    assert.deepEqual(f.opts.env,before);
    for(const call of f.calls) for(const key of keys) assert.equal(call.opts.env[key],undefined);
    assert.equal(f.calls.at(-1).opts.cwd,f.repo);
});

test('default setup does not create newly shadowed commands',t=>{
    const f=fixture(t), dh=path.join(f.home,'.delimit');
    assert.deepEqual(h.installHarnessShims({delimitHome:dh,packageRoot:__dirname,includeNew:false}),{installed:[],skipped:[]});
    assert(!fs.existsSync(path.join(dh,'shims')));
});
test('default setup refreshes managed shims and preserves custom ones',t=>{
    const f=fixture(t), dh=path.join(f.home,'.delimit');
    h.installHarnessShims({delimitHome:dh,packageRoot:'old-package'});
    fs.writeFileSync(path.join(dh,'shims/muse'),'owner custom script');
    assert.deepEqual(h.installHarnessShims({delimitHome:dh,packageRoot:'new-package',includeNew:false}),{installed:['copilot'],skipped:['muse']});
    assert.equal(fs.readFileSync(path.join(dh,'shims/muse'),'utf8'),'owner custom script');
    assert(fs.readFileSync(path.join(dh,'shims/copilot'),'utf8').includes('new-package'));
});
test('explicit install preflights every collision before replacing any shim',t=>{
    const f=fixture(t), dh=path.join(f.home,'.delimit');
    h.installHarnessShims({delimitHome:dh,packageRoot:'old-package'});
    const copilot=fs.readFileSync(path.join(dh,'shims/copilot'),'utf8');
    fs.writeFileSync(path.join(dh,'shims/muse'),'owner custom script');
    assert.throws(()=>h.installHarnessShims({delimitHome:dh,packageRoot:'new-package'}),/no harness shims were changed/);
    assert.equal(fs.readFileSync(path.join(dh,'shims/copilot'),'utf8'),copilot);
});
test('explicit install does not follow a symlink to a managed-looking file',t=>{
    const f=fixture(t), dh=path.join(f.home,'.delimit');
    fs.mkdirSync(path.join(dh,'shims'),{recursive:true});
    const target=path.join(f.root,'owner-file');
    const content='#!/usr/bin/env node\n// Delimit explicit harness shim\nowner-data';
    fs.writeFileSync(target,content);fs.symlinkSync(target,path.join(dh,'shims/muse'));
    assert.throws(()=>h.installHarnessShims({delimitHome:dh,packageRoot:__dirname}),/unrecognized muse/);
    assert(!fs.existsSync(path.join(dh,'shims/copilot')));
    assert.equal(fs.readFileSync(target,'utf8'),content);
});

for(const [id,args] of [['copilot',['-p','synthetic task','--model','some-model']],
    ['muse',['exec','--model','muse-spark-1.3','synthetic task']],
    ['muse',['resume','synthetic-session']],['copilot',['login']]]) {
    test('native argument passthrough preserves '+id+' '+args[0]+' without added work', t=>{
        const f=fixture(t); fs.unlinkSync(f.config);
        f.opts.env.MODEL_API_KEY='caller-selected-synthetic';
        f.opts.env.GIT_WORK_TREE='caller-selected-worktree';
        const before={...f.opts.env};
        assert.equal(h.launchHarnessShim(id,{...f.opts,cwd:f.home,args}),0);
        assert.equal(f.calls.length,1);
        assert.deepEqual(f.calls[0].args,args);
        assert.deepEqual(f.calls[0].opts.env,before);
        assert.deepEqual(f.opts.env,before);
        assert.equal(f.calls[0].opts.cwd,f.home);
        assert.equal(f.errors.length,0);
    });
}

test('Copilot resolver accepts direct system install without running it', t=>{
    const f=fixture(t); fs.unlinkSync(path.join(f.home,'.local/bin/copilot'));
    const local=path.join(f.home,'.local/bin/fixture-system-copilot');
    fs.writeFileSync(local,'#!/bin/sh\nexit 0\n',{mode:0o755});
    const access=fs.accessSync,realpath=fs.realpathSync;
    const seen=[];
    t.mock.method(fs,'accessSync',(file,mode)=>{
        seen.push(file);
        if(file==='/usr/bin/copilot') return access(local,mode);
        if(file.startsWith('/usr/')) throw new Error('synthetically absent');
        return access(file,mode);
    });
    t.mock.method(fs,'realpathSync',file=>file==='/usr/bin/copilot'?realpath(local):realpath(file));
    assert.equal(h.resolveBinary('copilot',f.opts.env),local);
    assert(seen.includes('/usr/local/bin/copilot'));
    assert(seen.includes('/usr/bin/copilot'));
});

test('binary resolution refuses copied and symlinked Delimit shims', t=>{
    const f=fixture(t), local=path.join(f.home,'.local/bin/copilot');
    const nativeAccess=fs.accessSync;
    t.mock.method(fs,'accessSync',(file,mode)=>{
        if(file.startsWith('/usr/')) throw new Error('synthetically absent');
        return nativeAccess(file,mode);
    });
    fs.writeFileSync(local,'#!/usr/bin/env node\n// Delimit explicit harness shim\n');
    assert.throws(()=>h.resolveBinary('copilot',f.opts.env),/executable not found/);
    fs.unlinkSync(local);
    const dir=path.join(f.home,'.delimit/shims');fs.mkdirSync(dir,{recursive:true});
    const shim=path.join(dir,'copilot');fs.writeFileSync(shim,'#!/bin/sh\nexit 0\n',{mode:0o755});
    fs.symlinkSync(shim,local);
    assert.throws(()=>h.resolveBinary('copilot',f.opts.env),/executable not found/);
});

test('malformed native settings and raw runtime errors never disclose content', t=>{
    const f=fixture(t), secret='synthetic-private-setting';
    for(const contents of ['{"key":"'+secret+'",broken','null','[]','"'+secret+'"']) {
        fs.writeFileSync(f.config,contents);
        assert.equal(h.launchExplicitHarness('muse',f.opts),1);
        assert(f.calls.every(call=>call.cmd==='git'));
        assert(!f.errors.join(' ').includes(secret));
    }
    assert.equal(h.launchHarnessShim('copilot',{...f.opts,args:['-p','synthetic'],spawnSync:()=>{throw new Error(secret);}}),1);
    assert(!f.errors.join(' ').includes(secret));
});
