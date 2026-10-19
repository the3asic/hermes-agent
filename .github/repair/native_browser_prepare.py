"""Prepare the candidate's declared native browser tools through Hermes PM."""
import argparse
import hashlib
import json
import os
from pathlib import Path

import pm

parser = argparse.ArgumentParser()
parser.add_argument('--write', action='store_true')
args = parser.parse_args()
os.umask(0o077)
lock = json.loads(Path('pm/lock.json').read_text())['packages']
if not args.write:
    print(json.dumps({'would_prepare': {name: lock[name]['version'] for name in ('agent-browser', 'chromium')}}))
    raise SystemExit(0)
rows = []
for name in ('agent-browser', 'chromium'):
    pm.ensure(name)
    installed = pm.installed_package(name)
    assert installed is not None and installed.binary is not None
    binary = Path(installed.binary)
    assert binary.is_file()
    rows.append({'package': name, 'declared_version': lock[name]['version'], 'binary': str(binary), 'binary_bytes': binary.stat().st_size})
receipt = Path('artifacts/hermes-repair-20261019/native-browser-environment.json')
receipt.parent.mkdir(parents=True, exist_ok=True)
assert not receipt.exists()
receipt.write_text(json.dumps({'prepared': rows, 'lock_sha256': hashlib.sha256(Path('pm/lock.json').read_bytes()).hexdigest()}, indent=2) + '\n')
print(json.dumps({'prepared': rows}))
