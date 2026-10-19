"""Inspect current PR merges in the native Git object database without touching its index."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import zipfile

parser = argparse.ArgumentParser()
parser.add_argument('--plan', type=Path, required=True)
parser.add_argument('--write', action='store_true')
args = parser.parse_args()
os.umask(0o077)
plan = json.loads(args.plan.read_bytes())
base = plan['base']
assert len(base) == 40 and all(c in '0123456789abcdef' for c in base)
root = Path.home() / 'Dev/hermes-health-repair-20261019'
repo = root / 'core-review-base'
out = root / 'artifacts' / ('core-current-base-' + base[:8] + '-r3')
if not args.write:
    print(json.dumps({'would_inspect_base': base, 'pull_requests': len(plan['prs'])}))
    raise SystemExit(0)
out.mkdir(parents=True, exist_ok=True)
result_path = out / 'merge-result.json'
assert not result_path.exists(), 'Read the existing native inspection first'


def git(*arguments, check=True):
    environment = dict(os.environ, GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never')
    result = subprocess.run(['git', '-C', str(repo), *arguments], env=environment, capture_output=True, timeout=900)
    if check and result.returncode:
        raise RuntimeError('Git failed: ' + result.stderr.decode('utf-8', errors='replace'))
    return result


index = Path(os.fsdecode(git('rev-parse', '--git-path', 'index').stdout.strip()))
if not index.is_absolute():
    index = repo / index


def index_identity():
    exists = index.exists()
    return {'exists': exists, 'sha256': hashlib.sha256(index.read_bytes()).hexdigest() if exists else None}


before = {'head': git('rev-parse', 'HEAD').stdout.decode().strip(),
          'status': git('status', '--porcelain', '--untracked-files=no').stdout.hex(),
          'index': index_identity()}
refspecs = ['refs/pull/' + str(row['number']) + '/head:refs/hermes-current-base/' + base[:8] + '/pr-' + str(row['number']) for row in plan['prs']]
git('fetch', '--no-tags', 'https://github.com/NousResearch/hermes-agent.git', base, *refspecs)
assert git('rev-parse', base + '^{commit}').stdout.decode().strip() == base
rows = []
for pr in plan['prs']:
    number = pr['number']
    ref = 'refs/hermes-current-base/' + base[:8] + '/pr-' + str(number)
    assert git('rev-parse', ref).stdout.decode().strip() == pr['head']
    source_head = pr.get('source_head', pr['head'])
    assert git('cat-file', '-t', source_head).stdout.strip() == b'commit'
    merged = git('merge-tree', '--write-tree', '--name-only', '--no-messages', '-z', source_head, base, check=False)
    assert merged.returncode in (0, 1), merged.stderr
    fields = merged.stdout.split(b'\0')
    tree = fields[0].decode('ascii')
    conflicts = [os.fsdecode(value) for value in fields[1:] if value]
    assert bool(conflicts) == bool(merged.returncode)
    row = {'number': number, 'head': pr['head'], 'source_head': source_head, 'base': base,
           'tree': tree, 'conflicts': conflicts, 'merge_exit': merged.returncode}
    if number in plan['export_numbers']:
        changed = [os.fsdecode(value) for value in git('diff-tree', '--no-commit-id', '--name-only', '-r', '-z', base, tree).stdout.split(b'\0') if value]
        ancestor = git('merge-base', source_head, base).stdout.decode().strip()
        paths = {'AGENTS.md', *changed, *conflicts}
        for name in tuple(paths):
            parent = PurePosixPath(name).parent
            while parent != PurePosixPath('.'):
                paths.add(str(parent / 'AGENTS.md'))
                parent = parent.parent
        files = []
        for name in sorted(paths):
            logical = PurePosixPath(name)
            assert not logical.is_absolute() and '..' not in logical.parts
            content = git('show', tree + ':' + name, check=False)
            if content.returncode:
                assert not git('ls-tree', tree, '--', name).stdout
                if name in changed:
                    files.append({'path': name, 'deleted': True})
                continue
            assert len(content.stdout) <= 4 * 1024**2
            target = out / str(number) / 'files' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            assert not target.exists()
            target.write_bytes(content.stdout)
            files.append({'path': name, 'sha256': hashlib.sha256(content.stdout).hexdigest(), 'bytes': len(content.stdout), 'changed': name in changed})
        for name in conflicts:
            for label, commit in (('base', ancestor), ('pr', source_head), ('upstream', base)):
                content = git('show', commit + ':' + name, check=False)
                if content.returncode:
                    assert not git('ls-tree', commit, '--', name).stdout
                    continue
                target = out / str(number) / 'stages' / label / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content.stdout)
        row.update(changed=changed, merge_base=ancestor, files=files)
    rows.append(row)
after = {'head': git('rev-parse', 'HEAD').stdout.decode().strip(),
         'status': git('status', '--porcelain', '--untracked-files=no').stdout.hex(),
         'index': index_identity()}
assert before == after
dependencies = {}
for name in ('pyproject.toml', 'uv.lock', 'pm/lock.json'):
    previous = git('show', plan['previous_base'] + ':' + name).stdout
    current = git('show', base + ':' + name).stdout
    dependencies[name] = {'unchanged': previous == current, 'previous_sha256': hashlib.sha256(previous).hexdigest(), 'current_sha256': hashlib.sha256(current).hexdigest()}
result = {'base': base, 'previous_base': plan['previous_base'], 'plan_sha256': hashlib.sha256(args.plan.read_bytes()).hexdigest(),
          'rows': rows, 'before': before, 'after': after, 'checkout_and_index_unchanged': True,
          'dependencies': dependencies, 'observed_at': datetime.now(timezone.utc).isoformat()}
result_path.write_text(json.dumps(result, indent=2) + '\n')
archive_path = out / 'inspection.zip'
with zipfile.ZipFile(archive_path, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(out.rglob('*')):
        if path.is_file() and path != archive_path:
            archive.write(path, path.relative_to(out).as_posix())
digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
(out / 'inspection-sha256.json').write_text(json.dumps({'sha256': digest, 'bytes': archive_path.stat().st_size}))
print(json.dumps({'base': base, 'pull_requests': len(rows), 'conflicting': [row['number'] for row in rows if row['conflicts']], 'dependencies': dependencies, 'archive_bytes': archive_path.stat().st_size, 'archive_sha256': digest}))
