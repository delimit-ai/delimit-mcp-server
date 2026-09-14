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
