#!/usr/bin/env python3
"""Run offline LCM regressions against exact, clean Core and LCM revisions."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lcm-root', type=Path, required=True)
    parser.add_argument('--expected-core', required=True)
    parser.add_argument('--expected-lcm', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--preflight-only', action='store_true')
    args = parser.parse_args()
    core = Path(__file__).resolve().parents[2]
    plugin = args.lcm_root.resolve(strict=True)
    if not all(re.fullmatch(r'[0-9a-f]{40}', sha) for sha in (args.expected_core, args.expected_lcm)):
        raise ValueError('Compatibility validation requires exact 40-character commit identities')

    def git(root: Path, *arguments: str) -> str:
        return subprocess.check_output(['git', '-C', str(root), *arguments],
                                       text=True, encoding='utf-8', timeout=30).strip()

    sources = {'core': (core, args.expected_core), 'lcm': (plugin, args.expected_lcm)}
    for name, (root, expected) in sources.items():
        if git(root, 'rev-parse', 'HEAD') != expected or git(root, 'status', '--porcelain'):
            raise RuntimeError(f'Expected a clean {name} at {expected}')
    os.umask(0o077)
    output = args.output.resolve()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    os.environ.update(LCM_HERMES_CORE_ROOT=str(core), HERMES_LCM_HOST_ROOT=str(core),
                      PYTHONPATH=str(core), HERMES_HOME=str(output / 'home'),
                      HERMES_DISABLE_LAZY_INSTALLS='1')
    started = datetime.now(UTC).isoformat()
    subprocess.run([sys.executable, str(plugin / 'scripts/validate_release_host.py'),
                    '--core-root', str(core)], check=True, timeout=120, cwd=plugin)
    if args.preflight_only:
        result = 0
        attempts: list[str] = []
    else:
        if os.name != 'posix':
            raise RuntimeError('This bounded compatibility lane requires a POSIX host')
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        bounded = 1024 if soft == resource.RLIM_INFINITY else min(soft, 1024)
        resource.setrlimit(resource.RLIMIT_NOFILE, (bounded, hard))
        attempts = []

        def audit(event, arguments):
            if event == 'socket.connect':
                attempts.append(str(arguments[1]))
                raise RuntimeError('LCM isolated regressions must not open a network connection')

        sys.addaudithook(audit)
        sys.path.insert(0, str(core))
        import pytest

        os.chdir(plugin)
        result = pytest.main(['tests/', '-q', '--junitxml', str(output / 'pytest.xml')])
    for name, (root, expected) in sources.items():
        if git(root, 'rev-parse', 'HEAD') != expected or git(root, 'status', '--porcelain'):
            raise RuntimeError(f'{name} changed during compatibility validation')
    receipt = {'core_sha': args.expected_core, 'lcm_sha': args.expected_lcm,
               'sqlite_version': sqlite3.sqlite_version,
               'started_at': started, 'completed_at': datetime.now(UTC).isoformat(),
               'exit_code': int(result), 'preflight_only': args.preflight_only,
               'in_process_network_connect_attempts': attempts,
               'scope': 'isolated regressions against the real host; recorded-source and production acceptance are separate',
               'accepted': result == 0 and not attempts}
    (output / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(receipt), flush=True)
    return int(result) or int(bool(attempts))


if __name__ == '__main__':
    raise SystemExit(main())
