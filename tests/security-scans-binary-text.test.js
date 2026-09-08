const { describe, it } = require('node:test');
const assert = require('node:assert');
const { spawnSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { gitEnv } = require('./_git-hermetic');

const ROOT = path.resolve(__dirname, '..');

function run(command, args, cwd, env = {}) {
    return spawnSync(command, args, {
        cwd,
        encoding: 'utf8',
        // Hooks export GIT_DIR/GIT_WORK_TREE. Strip those inherited pointers
        // for every disposable fixture subprocess so a nested `git add` can
        // never mutate the package worktree's index or shared config.
        env: { ...gitEnv(cwd), npm_config_ignore_scripts: 'true', ...env },
        timeout: 30000,
    });
}

function writeNulBearingFixture(file) {
    const before = Buffer.from('// legitimate NUL-handling fixture\nconst marker = "ok";\n');
    const nul = Buffer.from([0]);
    // Tests are excluded from the production identity scan; this literal is
    // deliberately present only in a disposable fixture repository/package.
    const after = Buffer.from('\nconst forbiddenIdentity = "Bladabah";\n');
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, Buffer.concat([before, nul, after]));
}

describe('security scans treat NUL-bearing source as text', () => {
    it('identity gate reports the exact forbidden source line', () => {
        const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-identity-binary-'));
        try {
            fs.mkdirSync(path.join(tmp, 'scripts'), { recursive: true });
            fs.copyFileSync(
                path.join(ROOT, 'scripts', 'check_identity_strings.sh'),
                path.join(tmp, 'scripts', 'check_identity_strings.sh'),
            );
            writeNulBearingFixture(path.join(tmp, 'src', 'fixture.js'));
            assert.equal(run('git', ['init', '-q'], tmp).status, 0);
            assert.equal(run('git', ['add', 'scripts/check_identity_strings.sh', 'src/fixture.js'], tmp).status, 0);

            const result = run('bash', ['scripts/check_identity_strings.sh'], tmp);
            assert.notEqual(result.status, 0, 'fixture must fail the identity gate');
            const output = `${result.stdout}\n${result.stderr}`;
            assert.match(output, /src\/fixture\.js:[0-9]+: \[banned-identity\]/);
            assert.match(output, /Bladabah/);
            assert.doesNotMatch(output, /binary file .* matches/i);
        } finally {
            fs.rmSync(tmp, { recursive: true, force: true });
        }
    });

    it('prepublish scan reports the exact forbidden source line', () => {
        const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-security-binary-'));
        try {
            fs.mkdirSync(path.join(tmp, 'scripts'), { recursive: true });
            fs.copyFileSync(
                path.join(ROOT, 'scripts', 'security-check.sh'),
                path.join(tmp, 'scripts', 'security-check.sh'),
            );
            fs.writeFileSync(
                path.join(tmp, 'scripts', 'check-bundle-parity.sh'),
                '#!/bin/sh\nexit 0\n',
                'utf8',
            );
            fs.writeFileSync(
                path.join(tmp, 'package.json'),
                JSON.stringify({ name: 'delimit-security-fixture', version: '1.0.0', files: ['src/fixture.js'] }),
                'utf8',
            );
            writeNulBearingFixture(path.join(tmp, 'src', 'fixture.js'));

            const pack = run('npm', ['pack', '--dry-run', '--json'], tmp);
            assert.equal(pack.status, 0, `fixture npm pack failed: ${pack.stderr}`);
            const packed = JSON.parse(pack.stdout);
            assert.ok(
                packed[0].files.some((entry) => entry.path === 'src/fixture.js'),
                `fixture source missing from npm pack: ${pack.stdout}`,
            );

            const result = run('bash', ['scripts/security-check.sh'], tmp);
            const output = `${result.stdout}\n${result.stderr}`;
            assert.notEqual(result.status, 0, `fixture must fail the prepublish scan:\n${output}`);
            assert.match(output, /src\/fixture\.js/);
            assert.match(output, /Bladabah/);
            assert.doesNotMatch(output, /binary file .* matches/i);
        } finally {
            fs.rmSync(tmp, { recursive: true, force: true });
        }
    });

    it('prepublish scan fails closed after a later enumerated file cannot be copied', () => {
        const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'delimit-security-copy-fail-'));
        try {
            fs.mkdirSync(path.join(tmp, 'scripts'), { recursive: true });
            fs.copyFileSync(
                path.join(ROOT, 'scripts', 'security-check.sh'),
                path.join(tmp, 'scripts', 'security-check.sh'),
            );
            fs.writeFileSync(
                path.join(tmp, 'scripts', 'check-bundle-parity.sh'),
                '#!/bin/sh\nexit 0\n',
                'utf8',
            );
            fs.writeFileSync(
                path.join(tmp, 'package.json'),
                JSON.stringify({
                    name: 'delimit-copy-failure-fixture',
                    version: '1.0.0',
                    files: ['src/first.js', 'src/second.js'],
                }),
                'utf8',
            );
            fs.mkdirSync(path.join(tmp, 'src'));
            fs.writeFileSync(path.join(tmp, 'src', 'first.js'), 'module.exports = "first";\n');
            fs.writeFileSync(path.join(tmp, 'src', 'second.js'), 'module.exports = "second";\n');
            const fakeBin = path.join(tmp, 'fake-bin');
            fs.mkdirSync(fakeBin);
            const fakeCopy = path.join(fakeBin, 'cp');
            const realCopy = run('sh', ['-c', 'command -v cp'], tmp).stdout.trim();
            assert.ok(realCopy, 'test requires a real cp implementation');
            fs.writeFileSync(fakeCopy, `#!/bin/sh
case "$1" in
  src/second.js|*/src/second.js) exit 1 ;;
  *) exec ${JSON.stringify(realCopy)} "$@" ;;
esac
`, { mode: 0o755 });

            const result = run('bash', ['scripts/security-check.sh'], tmp, {
                PATH: `${fakeBin}${path.delimiter}${process.env.PATH || ''}`,
            });
            assert.notEqual(result.status, 0, 'copy failure must stop the scan');
            const output = `${result.stdout}\n${result.stderr}`;
            assert.match(output, /failed to copy npm-packed file: src\/second\.js/);
            assert.match(output, /enumeration\/copy pipeline failed/);
        } finally {
            fs.rmSync(tmp, { recursive: true, force: true });
        }
    });
});

describe('release test entry points stay complete', () => {
    it('runs the same test files with and without the git-config wrapper', () => {
        const wrapper = fs.readFileSync(
            path.join(ROOT, 'scripts', 'test-with-config-guard.js'),
            'utf8',
        );
        const listBody = wrapper.match(/const TEST_FILES = \[([\s\S]*?)\n\];/);
        assert.ok(listBody, 'TEST_FILES must remain statically auditable');
        const wrappedFiles = [...listBody[1].matchAll(/'([^']+\.test\.js)'/g)]
            .map((match) => match[1])
            .sort();

        const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, 'package.json'), 'utf8'));
        const rawPrefix = 'node --test ';
        assert.ok(pkg.scripts['test:raw'].startsWith(rawPrefix));
        const rawFiles = pkg.scripts['test:raw'].slice(rawPrefix.length).trim().split(/\s+/).sort();
        assert.deepStrictEqual(rawFiles, wrappedFiles);
    });
});
