#!/usr/bin/env python3
"""Accept one immutable customer tarball; never build, repack, or publish it.

Internal release helper (excluded by the customer scripts allowlist). All account
state is disposable. Synthetic Pro checks call admission only, never backends.
"""
import argparse
import asyncio
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import time
import subprocess
import sys
import tarfile
import tempfile

STAGED = ('audit build_loop_daemon vendor_news_scan vendor_news_draft '
          'content_publish social_target github_scan reddit_scan inbox_daemon '
          'social_daemon daemon_run notify_inbox').split()
GRANDFATHER = ('social_post social_generate social_approve social_history '
               'security_deliberate security_ingest gov_new_task').split()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(path):
    data = Path(path).read_bytes()
    return {'sha256': hashlib.sha256(data).hexdigest(),
            'sha1': hashlib.sha1(data).hexdigest(),
            'integrity': 'sha512-' + base64.b64encode(hashlib.sha512(data).digest()).decode(),
            'size': len(data)}


def verify_digest(path, expected):
    require(digest(path) == expected, 'accepted tarball bytes changed')


def run(command, *, env, cwd, timeout=600, log=None):
    process = subprocess.Popen(command, env=env, cwd=cwd, start_new_session=True,
                               text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        if log:
            Path(str(log) + '.stdout').write_text(stdout)
            Path(str(log) + '.stderr').write_text(stderr)
        raise
    if log:
        Path(str(log) + '.stdout').write_text(stdout)
        Path(str(log) + '.stderr').write_text(stderr)
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def payload(result):
    require(not result.get('isError'), 'MCP isError: ' + str(result))
    if isinstance(result.get('structuredContent'), dict):
        value = result['structuredContent']
        require(bool(value), 'empty structured MCP payload')
        return value
    text = '\n'.join(c.get('text', '') for c in result.get('content', []) if c.get('type') == 'text')
    value = json.loads(text)
    require(isinstance(value, dict) and bool(value), 'MCP payload is empty or not an object')
    return value


def validate_deliberation_status(value):
    # No provider keys/CLI configuration is a valid fresh installation. The
    # installed engine explicitly reports "none" rather than an error then.
    require(value.get('mode') in ('none', 'byok', 'hosted'), 'invalid deliberation mode')
    for name in ('hosted_used', 'hosted_remaining', 'hosted_limit'):
        require(type(value.get(name)) is int and value[name] >= 0, 'invalid deliberation quota: ' + name)
    require(value['hosted_remaining'] <= value['hosted_limit'], 'remaining quota exceeds limit')
    for name in ('oauth_required', 'oauth_signed_in'):
        require(type(value.get(name)) is bool, 'invalid deliberation OAuth field: ' + name)
    require(isinstance(value.get('install_id'), str) and bool(value['install_id']), 'missing installation id')


def installed_fixtures(root):
    from datetime import datetime, timedelta
    from importlib.machinery import ExtensionFileLoader
    sys.path.insert(0, str(root))
    native = {}
    for name in ('license_core', 'deliberation'):
        spec = importlib.util.find_spec('ai.' + name)
        require(spec is not None and isinstance(spec.loader, ExtensionFileLoader), name + ' is not native')
        origin = Path(spec.origin).resolve()
        require(origin.is_relative_to(root) and not (root / 'ai' / (name + '.py')).exists(), 'native origin/source mismatch')
        import re
        symbols = subprocess.run(['readelf', '--version-info', str(origin)], check=True,
                                 capture_output=True, text=True, timeout=30).stdout
        versions = [tuple(map(int, v.split('.'))) for v in re.findall(r'GLIBC_([0-9.]+)', symbols)]
        require(bool(versions) and max(versions) <= (2, 35), 'native GLIBC ceiling exceeded')
        native[name] = {'origin': str(origin), 'max_glibc': '.'.join(map(str, max(versions))), **digest(origin)}
    from ai import server as s, license as lic, license_core, deliberation
    require(not lic.is_premium() and not license_core.check_premium(), 'clean user is unexpectedly Pro')
    require(hasattr(deliberation, 'DEFAULT_MODELS'), 'deliberation native import incomplete')
    require(s._STAGED_PRO_NO_GRANDFATHER == {'delimit_' + n for n in STAGED}, 'staged membership drift')
    require(s._NEWLY_ENFORCED_PRO - s._STAGED_PRO_NO_GRANDFATHER == {'delimit_' + n for n in GRANDFATHER}, 'grandfather membership drift')
    deadline = datetime.fromisoformat(s._SOCIAL_PRO_ENFORCE_AFTER)
    state = Path(s._GRANDFATHER_FILE)
    require(not state.exists(), 'unexpected grandfather state in new cleanroom')
    for name in STAGED + GRANDFATHER:
        for when in (deadline, deadline + timedelta(days=1)):
            require(s._pro_gate_graced(name, now=when).get('status') == 'premium_required', name + ' fresh Free admitted')
    for name in STAGED + GRANDFATHER:
        require(s._pro_gate_graced(name, now=deadline - timedelta(seconds=1)) is None, 'historical grace failed: ' + name)
    for name in STAGED:
        for when in (deadline, deadline + timedelta(days=1)):
            require(s._pro_gate_graced(name, now=when).get('status') == 'premium_required', name + ' legacy Free admitted')
    for name in GRANDFATHER:
        require(s._pro_gate_graced(name, now=deadline) is None, 'grandfather contract lost: ' + name)
    license_path = Path(license_core.LICENSE_FILE)
    require(not license_path.exists(), 'unexpected fixture license')
    try:
        # Synthetic account state exercises real installed native validation;
        # this is admission only, not a real subscription or backend execution.
        license_path.write_text(json.dumps({'tier': 'pro', 'valid': True,
            'last_validated_at': time.time()}))
        require(lic.is_premium(), 'synthetic Pro fixture not admitted by native core')
        for name in STAGED + GRANDFATHER:
            require(s._pro_gate_graced(name, now=deadline) is None, 'synthetic Pro rejected: ' + name)
    finally:
        license_path.unlink(missing_ok=True)
        state.unlink(missing_ok=True)
    require(not lic.is_premium(), 'fixture leaked Pro state')
    try:
        __import__('ai.acceptance_deliberately_missing_backend')
    except ModuleNotFoundError:
        pass
    else:
        raise RuntimeError('unlisted missing module was absorbed')
    return {'native': native, 'staged_cutoff': STAGED, 'preserved_grandfather': GRANDFATHER,
            'synthetic_pro': 'admission only; no tool backend executed', 'is_premium': False}


def serve(root):
    # Installed server and real MCP transport. The safety wrapper preserves the
    # installed admission result; any unexpectedly open staged gate raises BEFORE
    # the tool can enter its backend. Assert the gate is the function's first
    # executable statement, with its immediate returning branch, before serving.
    import ast
    sys.path.insert(0, str(root))
    from ai import server as s, license as lic
    require(not lic.is_premium(), 'MCP server must be Free')
    tree = ast.parse((root / 'ai/server.py').read_text())
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    for name in STAGED:
        body = functions['delimit_' + name].body
        if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        require(isinstance(body[0], ast.Assign) and len(body[0].targets) == 1
                and isinstance(body[0].targets[0], ast.Name), 'staged gate assignment missing: ' + name)
        variable = body[0].targets[0].id
        expected = ast.parse(variable + ' = _pro_gate_graced(' + repr(name) + ')\nif ' + variable + ':\n return ' + variable).body
        require([ast.dump(n) for n in body[:2]] == [ast.dump(n) for n in expected], 'unsafe staged gate placement: ' + name)
    original = s._pro_gate_graced
    def fail_before_backend(name, *args, **kwargs):
        result = original(name, *args, **kwargs)
        if name.removeprefix('delimit_') in STAGED:
            require(isinstance(result, dict) and result.get('status') == 'premium_required', 'staged gate opened: ' + name)
        return result
    s._pro_gate_graced = fail_before_backend
    # No model/network action is permitted by this acceptance protocol.
    def no_network(event, args):
        if event in ('socket.connect', 'socket.getaddrinfo'):
            raise RuntimeError('network forbidden during MCP acceptance')
    sys.addaudithook(no_network)
    s.mcp.run()


async def mcp_checks(root, project, version):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=sys.executable,
        args=[str(Path(__file__).resolve()), '--serve', str(root)], env=dict(os.environ), cwd=str(project))
    results = {}
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for name, args in [
                ('delimit_scan', {'project_path': str(project)}),
                ('delimit_quickstart', {'project_path': str(project)}),
                ('delimit_deliberation_status', {}),
                ('delimit_license_status', {}), ('delimit_version', {}),
            ] + [('delimit_' + n, {}) for n in STAGED] + [
                ('delimit_deploy_plan', {}), ('delimit_vault_search', {'query': 'acceptance'}),
                ('delimit_evidence_collect', {'target': str(project)})]:
                response = await session.call_tool(name, args)
                value = payload(response.model_dump())
                print(json.dumps({"tool": name, "result": value}), flush=True)
                if name in {'delimit_' + n for n in STAGED} | {'delimit_deploy_plan', 'delimit_vault_search', 'delimit_evidence_collect'}:
                    require(value.get('status') == 'premium_required', name + ' did not block Free')
                elif name == 'delimit_version':
                    require(value.get('version') == version, 'MCP version mismatch')
                elif name == 'delimit_license_status':
                    require(value.get('tier') == 'free', 'MCP tier mismatch')
                elif name == 'delimit_scan':
                    require(isinstance(value.get('findings'), list) and bool(value['findings'])
                            and isinstance(value.get('suggestions'), list), 'scan missing useful findings')
                elif name == 'delimit_quickstart':
                    require(isinstance(value.get('steps'), list) and len(value['steps']) == 7,
                            'quickstart did not complete seven steps')
                elif name == 'delimit_deliberation_status':
                    validate_deliberation_status(value)
                else:
                    require(not value.get('error') and value.get('status') not in ('error', 'capability_unavailable', 'premium_required'), name + ' unavailable')
                results[name] = value
    return results


def accept(tarball, version, output):
    tarball, output = tarball.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    original = digest(tarball)
    (output / 'tarball.json').write_text(json.dumps(original, indent=2) + '\n')
    with tarfile.open(tarball) as archive:
        package = json.load(archive.extractfile('package/package.json'))
        require(package['version'] == version, 'packed version mismatch')
        names = archive.getnames()
        for module in ('license_core', 'deliberation'):
            require(any(n.startswith('package/gateway/ai/' + module + '.cpython-') and n.endswith('.so') for n in names),
                    'packed native module missing: ' + module)
            require('package/gateway/ai/' + module + '.py' not in names, 'plaintext proprietary source shipped')
        shipped = {m.name.removeprefix('package/'): hashlib.sha256(archive.extractfile(m).read()).hexdigest()
                   for m in archive.getmembers() if m.isfile() and m.name.startswith('package/gateway/')}
    with tempfile.TemporaryDirectory(prefix='delimit-artifact-') as temporary:
        clean = Path(temporary)
        home, prefix, project = clean / 'home', clean / 'prefix', clean / 'project'
        for directory in (home, prefix, project):
            directory.mkdir()
        (project / 'openapi.yaml').write_text('openapi: 3.0.3\ninfo:\n  title: Acceptance\n  version: 1.0.0\npaths: {}\n')
        binaries = [str(Path(shutil.which(name)).resolve().parent) for name in ('node', 'npm', 'python3')]
        path = os.pathsep.join(dict.fromkeys(binaries + ['/usr/local/bin', '/usr/bin', '/bin']))
        env = {'HOME': str(home), 'PATH': path, 'DELIMIT_SETUP_UPDATED': '1', 'DELIMIT_NO_TELEMETRY': '1', 'PYTHONNOUSERSITE': '1', 'FASTMCP_CHECK_FOR_UPDATES': 'off'}
        commands = [('install', ['npm', 'install', '-g', '--prefix', str(prefix), str(tarball)]),
                    ('setup', [str(prefix / 'bin/delimit'), 'setup', '--yes'])]
        for label, command in commands:
            result = run(command, env=env, cwd=project, log=output / label)
            (output / (label + '.stdout')).write_text(result.stdout)
            (output / (label + '.stderr')).write_text(result.stderr)
        root = home / '.delimit/server'
        for relative, expected in shipped.items():
            installed = root / relative.removeprefix('gateway/')
            require(installed.is_file() and hashlib.sha256(installed.read_bytes()).hexdigest() == expected,
                    'installed artifact differs: ' + relative)
        require(not (home / '.delimit/license.json').exists(), 'unexpected license')
        python = home / '.delimit/venv/bin/python'
        require(python.is_file(), 'setup-created venv missing')
        result = run([str(python), str(Path(__file__).resolve()), '--installed', str(root), str(project), version],
                     env=env, cwd=project, timeout=300, log=output / 'runtime')
        (output / 'runtime.stdout').write_text(result.stdout)
        (output / 'runtime.stderr').write_text(result.stderr)
    verify_digest(tarball, original)
    (output / 'PASS.json').write_text(json.dumps({'version': version, 'tarball': str(tarball), **original}, indent=2) + '\n')


def main():
    if len(sys.argv) > 1 and sys.argv[1] == '--verify':
        receipt = json.loads(Path(sys.argv[3]).read_text())
        verify_digest(sys.argv[2], {k: receipt[k] for k in ('sha256', 'sha1', 'integrity', 'size')})
    elif len(sys.argv) > 1 and sys.argv[1] == '--serve':
        serve(Path(sys.argv[2]).resolve())
    elif len(sys.argv) > 1 and sys.argv[1] == '--installed':
        root, project, version = Path(sys.argv[2]).resolve(), Path(sys.argv[3]).resolve(), sys.argv[4]
        fixtures = installed_fixtures(root)
        result = asyncio.run(asyncio.wait_for(mcp_checks(root, project, version), timeout=240))
        print(json.dumps({'fixtures': fixtures, 'mcp': result}, indent=2))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('tarball', type=Path)
        parser.add_argument('version')
        parser.add_argument('output', type=Path)
        args = parser.parse_args()
        accept(args.tarball, args.version, args.output)


if __name__ == '__main__':
    main()
