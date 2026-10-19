"""Prepare a reviewed dependency update on the native maintained-release checkout."""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import tomllib
import zipfile

parser = argparse.ArgumentParser()
parser.add_argument('--workdir', type=Path, required=True)
parser.add_argument('--test-source', type=Path, required=True)
parser.add_argument('--uv', type=Path, required=True)
parser.add_argument('--python', type=Path, required=True)
parser.add_argument('--resume-lock-sha')
parser.add_argument('--write', action='store_true')
args = parser.parse_args()
base = 'ed063b312be2f1f50c470c70c8b96900ea4f3fc3'
work = args.workdir.resolve()


def git(*values):
    return subprocess.run(['git', '-C', str(work), *values], check=True, capture_output=True, text=True).stdout.strip()


assert git('rev-parse', 'HEAD') == base
pyproject = work / 'pyproject.toml'
lock = work / 'uv.lock'
original_manifest = subprocess.run(['git', '-C', str(work), 'show', base + ':pyproject.toml'], check=True, capture_output=True).stdout
original_lock = subprocess.run(['git', '-C', str(work), 'show', base + ':uv.lock'], check=True, capture_output=True).stdout
assert hashlib.sha256(original_manifest).hexdigest() == '6f969b9fdff95e7269ec808361e3ae8be5357036e6257e53a3306878cf8c41dd'
assert hashlib.sha256(original_lock).hexdigest() == '5b3798f326209475abca8ef7cbf7c9406f12e687c28c0b540dfe597466f48590'
before_lock = tomllib.loads(original_lock.decode())
text = original_manifest.decode()
changes = [('"PyJWT[crypto]==2.13.0"', '"PyJWT[crypto]==2.15.0"', 1),
           ('"urllib3>=2.7.0,<3"', '"urllib3>=2.8.0,<3"', 1),
           ('"mcp==2.0.0"', '"mcp==2.2.0"', 3),
           ('"httpx2==2.7.0"', '"httpx2==2.12.0"', 3),
           ('"aiohttp==3.14.3"', '"aiohttp==3.14.4"', 6),
           ('"pynacl>=1.6,<1.7"', '"pynacl>=1.6.2,<1.7"', 1)]
for old, new, count in changes:
    assert text.count(old) == count, old
    text = text.replace(old, new)
text = text.replace('# urllib3 2.7.0 fixes GHSA-mf9v-mfxr-j63j (decompression-bomb bypass)\n  # and GHSA-qccp-gfcp-xxvc (header leak across origins).',
                    '# Keep HTTP response and redirect handling above the reviewed security floor.')
text = text.replace('aiohttp 3.14.3:', 'aiohttp 3.14.4:')
old_comment = ('  # discord.py\'s latest published version, 2.7.1, pins pynacl at <1.6. however, pynacl 1.5.0 has known vulnerabilities.\n'
               '  # discord.py has updated pynacl to 1.6 on `main`, but has not yet published a patch release.\n'
               '  # so, we force-override this to pynacl 1.6. here.\n'
               '  # remove this when we update discord.py\n')
assert text.count(old_comment) == 1
text = text.replace(old_comment, '  # discord.py 2.7.1 excludes patched PyNaCl; retain the reviewed security floor.\n')
constraints = ['multidict>=6.9.1,<7', 'oauthlib>=4.0.0,<5', 'pip>=26.2,<27',
               'setuptools>=83.0.0,<84', 'tornado>=6.5.9,<7']
assert 'constraint-dependencies' not in text
text = text.replace('[tool.uv]\n', '[tool.uv]\nconstraint-dependencies = ' + json.dumps(constraints) + '\n', 1)
parsed = tomllib.loads(text)
assert parsed['project']['version'] == '0.21.5' and parsed['project']['requires-python'] == '>=3.11,<3.14'
assert parsed['tool']['uv']['constraint-dependencies'] == constraints
targets = {'aiohttp': '3.14.4', 'httpcore2': '2.12.0', 'httpx2': '2.12.0', 'mcp': '2.2.0',
           'multidict': '6.9.1', 'oauthlib': '4.0.0', 'pip': '26.2', 'pyjwt': '2.15.0',
           'pynacl': '1.6.2', 'setuptools': '83.0.0', 'tornado': '6.5.9', 'urllib3': '2.8.0'}
if args.resume_lock_sha is None:
    assert not git('status', '--porcelain', '--untracked-files=no')
    assert pyproject.read_bytes() == original_manifest and lock.read_bytes() == original_lock
else:
    assert pyproject.read_text(encoding='utf-8') == text, 'Preserve changed native manifest edits'
    assert hashlib.sha256(lock.read_bytes()).hexdigest() == args.resume_lock_sha, 'Preserve changed native lock edits'
    assert set(git('diff', '--name-only', base).splitlines()) <= {'pyproject.toml', 'uv.lock'}
if not args.write:
    print(json.dumps({'would_prepare': str(work), 'base': base, 'targets': targets}))
    raise SystemExit(0)
out = work / 'artifacts/hermes-dependency-security-20261020'
out.mkdir(parents=True, exist_ok=True)
pyproject.write_text(text, encoding='utf-8', newline='\n')
test_path = work / 'tests/tools/live_dependencies/test_mcp_http_transport.py'
test_path.parent.mkdir(parents=True, exist_ok=True)
if test_path.exists():
    assert test_path.read_bytes() == args.test_source.read_bytes(), 'Preserve changed native test source'
else:
    test_path.write_bytes(args.test_source.read_bytes())
uv_version = subprocess.run([str(args.uv), '--version'], check=True, capture_output=True, text=True).stdout.strip()
assert uv_version.startswith('uv 0.9.28 '), uv_version
present = {row['name'] for row in before_lock['package']}
command = [str(args.uv), 'lock', '--python', str(args.python)]
for name in sorted(targets.keys() & present):
    command += ['--upgrade-package', name + '==' + targets[name]]
with (out / 'lock.stdout').open('wb') as stdout, (out / 'lock.stderr').open('wb') as stderr:
    subprocess.run(command, cwd=work, stdout=stdout, stderr=stderr, check=True, timeout=900)
with (out / 'lock-check.stdout').open('wb') as stdout, (out / 'lock-check.stderr').open('wb') as stderr:
    subprocess.run([str(args.uv), 'lock', '--check', '--python', str(args.python)], cwd=work,
                   stdout=stdout, stderr=stderr, check=True, timeout=300)
after_lock = tomllib.loads(lock.read_text(encoding='utf-8'))
assert after_lock['requires-python'] == before_lock['requires-python']
versions = {name: sorted({row['version'] for row in after_lock['package'] if row['name'] == name}) for name in targets}
for name in targets.keys() & present:
    assert versions[name] == [targets[name]], (name, versions[name])
before_versions = {row['name']: sorted({item['version'] for item in before_lock['package'] if item['name'] == row['name']})
                   for row in before_lock['package']}
after_versions = {row['name']: sorted({item['version'] for item in after_lock['package'] if item['name'] == row['name']})
                  for row in after_lock['package']}
changed = {name: {'before': before_versions.get(name), 'after': after_versions.get(name)}
           for name in before_versions.keys() | after_versions.keys() if before_versions.get(name) != after_versions.get(name)}
paths = ['pyproject.toml', 'uv.lock', 'tests/tools/live_dependencies/test_mcp_http_transport.py']
git('add', '--', *paths)
assert set(git('diff', '--cached', '--name-only').splitlines()) == set(paths)
record = {'base': base, 'prepared_at': datetime.now(timezone.utc).isoformat(), 'tree': git('write-tree'),
          'uv_version': uv_version, 'targets': targets, 'resolved_target_versions': versions, 'package_changes': changed,
          'source_sha256': {name: hashlib.sha256((work / name).read_bytes()).hexdigest() for name in paths}}
(out / 'candidate-preparation.json').write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
package = out / 'candidate-source.zip'
with zipfile.ZipFile(package, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
    for name in paths:
        archive.write(work / name, name)
    archive.write(out / 'candidate-preparation.json', 'candidate-preparation.json')
print('DEPENDENCY_SECURITY_CANDIDATE_V1 ' + base64.b64encode(package.read_bytes()).decode(), flush=True)
