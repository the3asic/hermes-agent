"""Inspect real file fingerprints around the task's write and patch operations."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile

root = Path.cwd()
spec = importlib.util.spec_from_file_location('native_case', root / 'tests/tools/live_mutations/test_native_captured_targets.py')
case = importlib.util.module_from_spec(spec)
spec.loader.exec_module(case)
fixture = case.local_files.__wrapped__(Path(tempfile.mkdtemp(prefix='hermes-native-baseline-')))
tools, terminal, operations, task, home, workspace = next(fixture)
from tools import file_tools_read_tracking as tracking

content = (root / 'README.md').read_text(encoding='utf-8')
target = tools._capture_mutation_targets(['project-document.md'], task)['project-document.md'].backend_path
written = json.loads(tools.write_file_tool('project-document.md', content, task_id=task))
assert not written.get('error'), written
known = tracking._known_full_content_sha256([target], task)
print(json.dumps({'stage': 'written', 'has_baseline': tracking._has_full_write_baseline(target, task), 'known': list(known.values()), 'disk': hashlib.sha256(Path(target).read_bytes()).hexdigest()}))
original = next(line for line in content.splitlines() if line and line.upper() != line and content.count(line) == 1)
result = operations.patch_replace(target, original, original.upper())
assert not result.error, result.error
print(json.dumps({'stage': 'backend_patched', 'writes': [(path == target, read, write) for path, read, write in result._writes], 'disk': hashlib.sha256(Path(target).read_bytes()).hexdigest()}))
tracking._carry_full_write_baselines(task, known, result._writes, {'project-document.md': target})
print(json.dumps({'stage': 'carried', 'has_baseline': tracking._has_full_write_baseline(target, task)}))
try:
    next(fixture)
except StopIteration:
    pass
