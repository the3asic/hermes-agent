"""Generate the reviewed AnyIO lock update on a native work machine."""
import argparse
import base64
from collections import defaultdict
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tomllib
import zipfile

parser = argparse.ArgumentParser()
parser.add_argument('--manifest', type=Path, required=True)
parser.add_argument('--sha256', required=True)
parser.add_argument('--uv', required=True)
parser.add_argument('--write', action='store_true')
args = parser.parse_args()
base = 'fdb1d25cc440e7170451fff651dbee1019eef259'
root = Path.home() / 'Dev/hermes-health-repair-20261019'
repo = root / 'core-review-base'
work = root / 'core-security-anyio-preparation'
assert hashlib.sha256(args.manifest.read_bytes()).hexdigest() == args.sha256
assert not work.exists(), 'Preserve any existing lock preparation work'
if not args.write:
    print(json.dumps({'preview': True, 'base': base, 'workdir': str(work)}))
    raise SystemExit(0)
subprocess.run(['git', '-C', str(repo), 'fetch', '--no-tags', 'https://github.com/the3asic/hermes-agent.git', base], check=True)
subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '--detach', str(work), base], check=True)
out = work / 'artifacts/anyio-security-lock'
out.mkdir(mode=0o700, parents=True)
before = tomllib.loads((work / 'uv.lock').read_text(encoding='utf-8'))
old_manifest = tomllib.loads((work / 'pyproject.toml').read_text(encoding='utf-8'))
new_manifest = tomllib.loads(args.manifest.read_text(encoding='utf-8'))
old_manifest['tool']['uv']['constraint-dependencies'].insert(0, 'anyio>=4.14.2,<5')
assert old_manifest == new_manifest
(work / 'pyproject.toml').write_bytes(args.manifest.read_bytes())
for name, command in [('lock', [args.uv, 'lock', '--python', sys.executable, '--upgrade-package', 'anyio==4.14.2']),
                       ('check', [args.uv, 'lock', '--check', '--python', sys.executable])]:
    with (out / (name + '.stdout')).open('wb') as stdout, (out / (name + '.stderr')).open('wb') as stderr:
        subprocess.run(command, cwd=work, stdout=stdout, stderr=stderr, check=True, timeout=300)
after = tomllib.loads((work / 'uv.lock').read_text(encoding='utf-8'))
versions = []
for data in (before, after):
    current = defaultdict(set)
    for item in data['package']:
        current[item['name']].add(item['version'])
    versions.append(dict(current))
assert set(versions[0]) == set(versions[1])
changed = [name for name in versions[0] if versions[0][name] != versions[1][name]]
assert changed == ['anyio'] and versions[1]['anyio'] == {'4.14.2'}
subprocess.run(['git', 'diff', '--check'], cwd=work, check=True)
files = {name: (work / name).read_bytes() for name in ('pyproject.toml', 'uv.lock')}
record = {'accepted': True, 'base': base, 'only_changed_package': 'anyio', 'before': sorted(versions[0]['anyio']),
          'after': '4.14.2', 'lock_check_exit_code': 0, 'python': sys.version,
          'files': {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}}
memory = io.BytesIO()
with zipfile.ZipFile(memory, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
    bundle.writestr('acceptance.json', json.dumps(record))
    for name, data in files.items():
        bundle.writestr(name, data)
(out / 'acceptance.json').write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
print('PREPARED_ANYIO_SECURITY_LOCK_V1 ' + base64.b64encode(memory.getvalue()).decode())
