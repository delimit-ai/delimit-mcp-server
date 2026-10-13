// LED-6258: the managed instruction section carries the git-safety rules.
const test = require('node:test');
const assert = require('node:assert/strict');
const { getDelimitSection, getDelimitSectionCondensed } = require('../lib/delimit-template');

test('managed section carries the git safety rules', () => {
    const section = getDelimitSection();
    assert.match(section, /### Git Safety/);
    assert.match(section, /Never push directly to the default branch/);
    assert.match(section, /Never move, force-push or delete a published tag/);
    assert.match(section, /Never use `--no-verify`/);
    assert.match(section, /beyond `0600`/);
    assert.match(section, /never post the same content twice/);
    // still a single managed block, rules inside the markers
    assert.equal(section.match(/<!-- delimit:start v/g).length, 1);
    assert.ok(section.indexOf('### Git Safety') < section.indexOf('<!-- delimit:end -->'));
    assert.match(getDelimitSectionCondensed(), /Git Safety/);
});

test('git safety rules name no owner-specific paths or accounts', () => {
    const section = getDelimitSection();
    assert.doesNotMatch(section, /governed_merge|pr_review\.py|delimit-gateway|infracore|\/root\//);
});
