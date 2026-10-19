"""Validate an immutable maintained release on native Windows with real dependencies."""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from xml.etree import ElementTree

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--uv', required=True)
    parser.add_argument('--write', action='store_true')
    args = parser.parse_args()
    plan = json.loads(args.plan.read_bytes())
    root = Path.home() / 'Dev' / 'hermes-health-repair-20261019'
    tag = plan['commit'][:8]
    work = root / ('core-security-' + tag)
    out = root / 'artifacts' / ('dependency-security-' + tag)
    assert not (out / 'result.json').exists(), 'Read the completed result before repeating validation'
    if not args.write:
        print(json.dumps({'source': plan['commit'], 'workdir': str(work)}))
        return
    out.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(UV_CACHE_DIR=str(out / 'uv-cache'), UV_TOOL_DIR=str(out / 'uv-tools'),
               HERMES_HOME=str(out / 'home'), PYTHONUTF8='1')
    (out / 'home').mkdir(exist_ok=True)
    phases = []

    def execute(name, command, cwd=work, expected=0):
        with (out / (name + '.stdout')).open('wb') as stdout, (out / (name + '.stderr')).open('wb') as stderr:
            process = subprocess.run([str(item) for item in command], cwd=cwd, env=env,
                                     stdout=stdout, stderr=stderr, timeout=1200)
        phases.append({'name': name, 'exit_code': process.returncode, 'expected_exit_code': expected})
        (out / 'progress.json').write_text(json.dumps(phases, indent=2) + '\n', encoding='utf-8')
        assert process.returncode == expected, phases[-1]
        return (out / (name + '.stdout')).read_text(encoding='utf-8-sig').strip()

    repo = root / 'core-review-base'
    execute('fetch', ['git', 'fetch', '--no-tags', 'https://github.com/the3asic/hermes-agent.git', plan['commit']], cwd=repo)
    if not work.exists():
        execute('worktree', ['git', 'worktree', 'add', '--detach', work, plan['commit']], cwd=repo)
    assert execute('head', ['git', 'rev-parse', 'HEAD']) == plan['commit']
    assert execute('tree', ['git', 'rev-parse', 'HEAD^{tree}']) == plan['tree']
    assert execute('clean', ['git', 'status', '--porcelain', '--untracked-files=no']) == ''
    changed = execute('changes', ['git', 'diff', '--name-only', plan['base'], 'HEAD']).splitlines()
    assert sorted(changed) == sorted(plan['source_sha256'])
    for name, digest in plan['source_sha256'].items():
        assert hashlib.sha256((work / name).read_bytes()).hexdigest() == digest, name
    bash = Path('C:/Program Files/Git/bin/bash.exe')
    assert bash.is_file()
    baseline_env = root / ('core-security-baseline-env-' + tag)
    fixed_env = root / ('core-security-fixed-env-' + tag)
    baseline_python = baseline_env / 'Scripts/python.exe'
    fixed_python = fixed_env / 'Scripts/python.exe'
    assert not baseline_env.exists() and not fixed_env.exists(), 'Inspect existing environments before reusing them'
    execute('baseline-create', [args.uv, 'venv', '--python', sys.executable, baseline_env])
    execute('baseline-install', [args.uv, 'pip', 'install', '--python', baseline_python,
                               'PyJWT==2.13.0', 'pytest==9.1.1', 'pytest-asyncio==1.3.0'])
    env['HERMES_PYTHON'] = str(baseline_python)
    baseline_xml = out / 'baseline-jwt.xml'
    execute('baseline-jwt', [bash, 'scripts/run_tests.sh', 'tests/tools/live_dependencies/test_jwt_options.py',
                            '--file-timeout', '300', '--file-retries', '0', '--', '--noconftest',
                            '--basetemp=' + str(out / 'baseline-cases'), '--junitxml=' + str(baseline_xml), '-q'], expected=1)
    baseline_tree = ElementTree.parse(baseline_xml)
    baseline_counts = {key: sum(int(node.get(key, '0')) for node in baseline_tree.iter('testsuite'))
                       for key in ('tests', 'failures', 'errors', 'skipped')}
    assert baseline_counts == {'tests': 1, 'failures': 1, 'errors': 0, 'skipped': 0}, baseline_counts
    failures = '\n'.join(node.text or '' for node in baseline_tree.iter('failure'))
    assert 'assert options == before' in failures and 'verify_exp' in failures
    baseline_version = execute('baseline-version', [baseline_python, '-c', 'import jwt; print(jwt.__version__)'])
    assert baseline_version == '2.13.0'
    env['UV_PROJECT_ENVIRONMENT'] = str(fixed_env)
    execute('fixed-sync', [args.uv, 'sync', '--frozen', '--python', sys.executable, '--extra', 'dev',
                           '--extra', 'mcp', '--extra', 'messaging', '--extra', 'google'])
    execute('fixed-runtime-packages', [args.uv, 'pip', 'install', '--python', fixed_python, 'pip==26.2', 'tornado==6.5.9'])
    env['HERMES_PYTHON'] = str(fixed_python)
    acceptance_out = work / 'artifacts' / 'hermes-dependency-security-acceptance'
    execute('fixed-acceptance', [fixed_python, 'scripts/ci/run_dependency_security_acceptance.py',
                                '--bash', bash, '--uv', args.uv, '--output', acceptance_out,
                                '--expected-commit', plan['commit']])
    acceptance = json.loads((acceptance_out / 'acceptance.json').read_bytes())
    assert acceptance['accepted'] and acceptance['actual_tests_passed'] == 7 and len(acceptance['checks']) == 9
    assert acceptance['commit'] == plan['commit'] and acceptance['tree'] == plan['tree']
    packages = execute('installed-packages', [fixed_python, '-m', 'pip', 'list', '--exclude-editable', '--format=freeze', '--disable-pip-version-check'])
    requirements = out / 'installed-requirements.txt'
    requirements.write_text(packages + '\n', encoding='utf-8')
    audit_path = out / 'audit.json'
    execute('fixed-audit', [args.uv, 'tool', 'run', '--python', sys.executable, '--from', 'pip-audit==2.10.1',
                           'pip-audit', '--requirement', requirements, '--no-deps', '--disable-pip', '--strict',
                           '--format', 'json', '--desc', 'on', '--aliases', 'on', '--progress-spinner', 'off',
                           '--timeout', '30', '--output', audit_path], cwd=out)
    audit = json.loads(audit_path.read_bytes())
    assert audit['dependencies'] and all(not item['vulns'] and not item.get('skip_reason') for item in audit['dependencies'])
    xml = {name: (acceptance_out / (name + '.xml')).read_text(encoding='utf-8')
           for name in ('test_mcp_http_transport.py', 'test_jwt_options.py', 'test_network_clients.py', 'test_auth_crypto.py', 'test_cache_diagnostic.py')}
    record = {'accepted': True, 'hostname': os.environ['COMPUTERNAME'], 'completed_at': datetime.now(timezone.utc).isoformat(),
              'helper_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'plan_sha256': hashlib.sha256(args.plan.read_bytes()).hexdigest(), 'source': plan,
              'baseline': {'version': baseline_version, 'counts': baseline_counts, 'junit': baseline_xml.read_text(encoding='utf-8')},
              'acceptance': acceptance, 'junit': xml, 'audit': audit,
              'requirements_sha256': hashlib.sha256(requirements.read_bytes()).hexdigest(), 'phases': phases}
    data = json.dumps(record, ensure_ascii=False).encode()
    (out / 'result.json').write_bytes(data)
    print('NATIVE_DEPENDENCY_SECURITY_V1 ' + base64.b64encode(data).decode())

if __name__ == '__main__':
    main()
