"""Inspect the real wrapper error using its already populated isolated cache."""
import asyncio
import json
import os
from pathlib import Path

root = Path.cwd().parent
home = root / 'core-upstream130122/artifacts/hermes-repair-20261019/pr130168-fixed-1a96ff7d-acceptance-cases/test_real_extract_wrapper_serv0'
assert (home / 'config.yaml').exists()
os.environ['HERMES_HOME'] = str(home)
from hermes_constants import set_hermes_home_override

set_hermes_home_override(home)
from tools.web_tools import web_extract_tool

result = json.loads(asyncio.run(web_extract_tool(['https://github.com/NousResearch/hermes-agent'], char_limit=30000)))
print(json.dumps({'keys': list(result), 'error': result.get('error'), 'provenance': result.get('provenance'), 'results': [{key: row.get(key) for key in ('url', 'error', 'cached')} for row in result.get('results', [])]}))
