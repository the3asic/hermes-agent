"""Connect task lifetimes to browser-harness's native named pages and IPC."""

from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import time
import weakref
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator, Optional

from hermes_constants import hermes_home_key
from tools.browser_harness_dirs import daemon_env, daemon_identity


class BrowserTaskEnded(RuntimeError):
    """An operation belongs to a task generation whose cleanup has begun."""


@dataclass(frozen=True)
class HarnessPage:
    daemon: tuple[str, ...]
    cdp_url: str
    target_id: str
    fingerprint: tuple[int, float]


@dataclass(eq=False)
class BrowserTask:
    home: str
    task_id: str
    ended: bool = False
    cleanup_complete: bool = False
    daemons: set[tuple[str, ...]] = field(default_factory=set)
    page: Optional[HarnessPage] = None
    supervisor: Optional[object] = None
    processes: set[subprocess.Popen] = field(default_factory=set)

    def current(self) -> bool:
        with _lock:
            return not self.ended and _tasks.get((self.home, self.task_id)) is self

    def require_current(self) -> None:
        if not self.current():
            raise BrowserTaskEnded("This browser task has ended; its late operation was refused")


_lock = threading.RLock()
_operations: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
_tasks: dict[tuple[str, str], BrowserTask] = {}
_native: dict[tuple[str, ...], HarnessPage] = {}
_current_task: ContextVar[Optional[BrowserTask]] = ContextVar("native_browser_task", default=None)
_call_deadline: ContextVar[Optional[float]] = ContextVar("native_browser_deadline", default=None)
_NATIVE_QUERY = """\
import json, os, psutil
from browser_harness import _ipc, admin
if os.environ.get('BH_REQUIRE_EXISTING_DAEMON') == '1':
    admin.require_existing_daemon()
else:
    admin.ensure_daemon()
connection, token = _ipc.connect(admin.NAME, timeout=10)
try:
    ping = _ipc.request(connection, token, {'meta': 'ping'})
finally:
    connection.close()
if not ping.get('pid'):
    raise RuntimeError('The native browser session did not identify its daemon')
fingerprint = [ping['pid'], psutil.Process(ping['pid']).create_time()]
connection, token = _ipc.connect(admin.NAME, timeout=10)
try:
    page = _ipc.request(connection, token, {'meta': 'current_tab'})
finally:
    connection.close()
connection, token = _ipc.connect(admin.NAME, timeout=10)
try:
    verified = _ipc.request(connection, token, {'meta': 'ping'})
finally:
    connection.close()
if not page.get('targetId') or not verified.get('pid'):
    raise RuntimeError('The existing native browser session has no live current page')
if [verified['pid'], psutil.Process(verified['pid']).create_time()] != fingerprint:
    raise RuntimeError('The native browser daemon changed during page discovery')
print(json.dumps({'page': page, 'fingerprint': fingerprint}))
"""


def remaining(deadline: float, phase: str) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError(f"browser_exec timed out during {phase}")
    return value


def operation_deadline() -> float:
    value = _call_deadline.get()
    return value if value is not None else time.monotonic() + 15


def task_session_name(task_id: Optional[str]) -> str:
    identity = json.dumps([hermes_home_key(), task_id or "default"], ensure_ascii=True)
    return "hermes-" + hashlib.sha256(identity.encode()).hexdigest()[:24]


def begin_task(task_id: Optional[str]) -> BrowserTask:
    """Turn admission can start a fresh generation after a previous turn ended."""
    key = (hermes_home_key(), task_id or "default")
    with _lock:
        current = _tasks.get(key)
        if current is not None and current.ended and not current.cleanup_complete:
            raise BrowserTaskEnded("The previous browser task is still being cleaned up")
        if current is None or current.ended:
            current = BrowserTask(*key)
            _tasks[key] = current
        return current


@contextmanager
def browser_task_scope(task_id: str) -> Iterator[BrowserTask]:
    task = begin_task(task_id)
    token = _current_task.set(task)
    try:
        yield task
    finally:
        _current_task.reset(token)


def capture_task(task_id: Optional[str]) -> BrowserTask:
    key = (hermes_home_key(), task_id or "default")
    admitted = _current_task.get()
    if admitted is not None and (admitted.home, admitted.task_id) == key:
        admitted.require_current()
        return admitted
    with _lock:
        current = _tasks.get(key)
        if current is None:
            current = BrowserTask(*key)
            _tasks[key] = current
        current.require_current()
        return current


def spawn_task_cli(task: BrowserTask, command: list[str], **kwargs) -> subprocess.Popen:
    with _lock:
        task.require_current()
        process = subprocess.Popen(command, **kwargs)
        task.processes.add(process)
        return process


def forget_task_cli(task: BrowserTask, process: subprocess.Popen) -> None:
    with _lock:
        task.processes.discard(process)


def cancel_task_cli(task: BrowserTask) -> None:
    from tools.browser_use_cli import _kill_cli_process_group
    with _lock:
        processes = list(task.processes)
    for process in processes:
        _kill_cli_process_group(process)


def register_task_daemon(task: BrowserTask, daemon: tuple[str, ...]) -> None:
    with _lock:
        task.require_current()
        task.daemons.add(daemon)


def retire_task(task_id: str, expected: Optional[BrowserTask] = None) -> Optional[BrowserTask]:
    key = (hermes_home_key(), task_id)
    admitted = _current_task.get()
    if expected is None and admitted is not None and (admitted.home, admitted.task_id) == key:
        expected = admitted
    with _lock:
        task = _tasks.get(key)
        if expected is not None and task is not expected:
            return None
        if task is None:
            task = BrowserTask(*key)
            _tasks[key] = task
        task.ended = True
        task.page = None
        return task


def complete_task_cleanup(task: BrowserTask) -> None:
    with _lock:
        task.cleanup_complete = True


@contextmanager
def daemon_operation(daemon: tuple[str, ...], deadline: Optional[float] = None) -> Iterator[None]:
    with _lock:
        operation = _operations.get(daemon)
        if operation is None:
            operation = threading.RLock()
            _operations[daemon] = operation
    acquired = operation.acquire(timeout=remaining(deadline, "session queue")) if deadline else operation.acquire()
    if not acquired:
        raise TimeoutError("browser_exec timed out waiting for its named session")
    try:
        yield
    finally:
        operation.release()


def current_harness_page(env: dict, task: BrowserTask, deadline: float) -> HarnessPage:
    """Query native IPC without importing the task's agent_helpers.py or replaying its code."""
    from tools.browser_use_cli import _find_cli, _run_cli_killing_process_group

    task.require_current()
    command = _find_cli()
    if command is None:
        raise RuntimeError("browser-harness is unavailable")
    identity = daemon_identity(env)
    with _lock:
        expected = _native.get(identity)
    probe_env = {**env, "BH_UPDATE_CHECK": "0"}
    if expected is not None:
        probe_env["BH_REQUIRE_EXISTING_DAEMON"] = "1"
    result = _run_cli_killing_process_group(
        [command[0], "-c", _NATIVE_QUERY], "", probe_env, remaining(deadline, "native page query"), task=task,
    )
    task.require_current()
    if result.returncode:
        from agent.redact import redact_sensitive_text
        detail = redact_sensitive_text(result.stderr.strip(), force=True)
        raise RuntimeError(f"Could not query this native browser session: {detail}")
    reply = json.loads(result.stdout)
    page = HarnessPage(identity, env.get("BU_CDP_WS") or env.get("BU_CDP_URL") or "",
                       reply["page"]["targetId"], tuple(reply["fingerprint"]))
    if expected is not None and page.fingerprint != expected.fingerprint:
        raise RuntimeError("The named browser daemon was replaced; the task cannot adopt its replacement")
    with _lock:
        task.require_current()
        _native[identity] = page
    return page


def bind_harness_supervisor(env: dict, task: BrowserTask, deadline: float) -> Optional[HarnessPage]:
    if not (env.get("BU_CDP_WS") or env.get("BU_CDP_URL")):
        return None
    from tools.browser_session_cdp import get_dialog_policy_config, resolve_cdp_override
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    page = current_harness_page(env, task, deadline)
    policy, timeout_s = get_dialog_policy_config()
    task.require_current()
    supervisor = SUPERVISOR_REGISTRY.get_or_start(
        task_id=task.task_id, cdp_url=resolve_cdp_override(page.cdp_url), target_id=page.target_id,
        dialog_policy=policy, dialog_timeout_s=timeout_s,
        start_timeout=remaining(deadline, "page supervision"), publish_guard=task.current,
    )
    with _lock:
        task.require_current()
        task.page = page
        task.supervisor = supervisor
    return page


def clear_task_binding(task: BrowserTask) -> None:
    from tools.browser_supervisor import SUPERVISOR_REGISTRY
    with _lock:
        task.page = None
        supervisor, task.supervisor = task.supervisor, None
    if supervisor is not None:
        SUPERVISOR_REGISTRY.stop(task.task_id, home_key=task.home, expected=supervisor)


def native_stop_command(python: str, daemon: tuple[str, ...]) -> list[str]:
    with _lock:
        page = _native.get(daemon)
    expected = page.fingerprint if page is not None else None
    code = (
        "import json, psutil\nfrom browser_harness import _ipc, admin\n"
        f"expected = {expected!r}\n"
        "pid = _ipc.identify(admin.NAME, timeout=2)\n"
        "if pid is None:\n"
        "    record = _ipc.pid_path(admin.NAME)\n"
        "    if record.exists():\n"
        "        published = json.loads(record.read_text())\n"
        "        pending = published.get('pid') if isinstance(published, dict) else published\n"
        "        started = published.get('started') if isinstance(published, dict) else None\n"
        "        if type(pending) is not int or pending <= 0:\n"
        "            raise RuntimeError('The native daemon has an invalid PID record; cleanup ownership retained')\n"
        "        if psutil.pid_exists(pending) and psutil.Process(pending).status() != psutil.STATUS_ZOMBIE:\n"
        "            if started is None or admin._process_start_time(pending) == started:\n"
        "                raise RuntimeError('The native browser daemon is still starting or unreachable; cleanup must be retried')\n"
        "    elif expected is None:\n"
        "        raise RuntimeError('The initial browser daemon has no verifiable exit record; cleanup ownership retained')\n"
        "    if expected is not None and psutil.pid_exists(expected[0]):\n"
        "        process = psutil.Process(expected[0])\n"
        "        if process.create_time() == expected[1] and process.status() != psutil.STATUS_ZOMBIE:\n"
        "            raise RuntimeError('The native daemon is alive without IPC; cleanup ownership retained')\n"
        "elif expected is not None and (pid, psutil.Process(pid).create_time()) != expected:\n"
        "    raise RuntimeError('The native daemon changed before cleanup')\n"
        "identity = (pid, psutil.Process(pid).create_time()) if pid is not None else None\n"
        "admin.restart_daemon(require_clean=pid is not None)\n"
        "if identity is not None and psutil.pid_exists(identity[0]):\n"
        "    process = psutil.Process(identity[0])\n"
        "    if process.create_time() == identity[1] and process.status() != psutil.STATUS_ZOMBIE:\n"
        "        raise RuntimeError('The native daemon has not exited; cleanup ownership retained')\n"
    )
    return [python, "-c", code]


def forget_native_daemon(daemon: tuple[str, ...]) -> None:
    with _lock:
        _native.pop(daemon, None)


def retire_profile_tasks(home_key: Optional[str] = None) -> None:
    with _lock:
        tasks = [task for task in _tasks.values() if home_key is None or task.home == home_key]
        for task in tasks:
            task.ended = True
            task.page = None
    for task in tasks:
        cancel_task_cli(task)
        clear_task_binding(task)


def complete_retired_tasks(home_key: Optional[str] = None) -> None:
    """A successful profile teardown admits a later turn, while old contexts stay ended."""
    with _lock:
        for task in _tasks.values():
            if task.ended and (home_key is None or task.home == home_key):
                if all(process.poll() is not None for process in task.processes):
                    task.cleanup_complete = True


def execute_harness_call(command, code: str, env: dict, task: BrowserTask,
                         deadline: float, activation: str) -> dict:
    from agent.periodic_scheduler import schedule
    from tools import browser_use_cli as cli
    from tools.browser_use_shot import harness_command

    identity = daemon_identity(env)
    register_task_daemon(task, identity)
    with daemon_operation(identity, deadline):
        task.require_current()
        with cli._driven_daemons_lock:
            cli._driven_daemons[identity] = task.home
        owning = schedule(lambda: cli._own_harness_daemon_until_owned(env), cli._OWN_DURING_CALL_S)
        task_token = _current_task.set(task)
        deadline_token = _call_deadline.set(deadline)
        try:
            page = cli._attach_vault_supervisor(env, task.task_id)
            execution_env = {**env, "BH_REQUIRE_EXISTING_DAEMON": "1"} if page else env
            prepared = harness_command(command, activate=activation, expected=page.fingerprint if page else None)
            task.require_current()
            proc = cli._run_cli_killing_process_group(
                prepared, code, execution_env, remaining(deadline, "user code"), task=task,
            )
            reply = {"proc": proc}
            try:
                selected = bind_harness_supervisor(execution_env, task, deadline) if page else None
                if selected:
                    reply["target_id"] = selected.target_id
            except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
                clear_task_binding(task)
                reply["binding_error"] = str(error)
            return reply
        finally:
            _call_deadline.reset(deadline_token)
            _current_task.reset(task_token)
            owning.cancel(wait=5)


def restore_harness_supervisor(task_id: str):
    from tools.browser_use_cli import _base_subprocess_env
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    task = capture_task(task_id)
    with _lock:
        page = task.page
    if page is None:
        return None
    deadline = time.monotonic() + 15
    env = daemon_env(_base_subprocess_env(), page.daemon)
    env.pop("BU_CDP_WS", None)
    env.pop("BU_CDP_URL", None)
    env["BU_CDP_WS" if page.cdp_url.startswith(("ws://", "wss://")) else "BU_CDP_URL"] = page.cdp_url
    env["BH_REQUIRE_EXISTING_DAEMON"] = "1"
    with daemon_operation(page.daemon, deadline):
        task.require_current()
        with _lock:
            if task.page is not page:
                raise BrowserTaskEnded("The browser binding changed while its supervisor was being restored")
        bind_harness_supervisor(env, task, deadline)
        return SUPERVISOR_REGISTRY.get(task_id)
