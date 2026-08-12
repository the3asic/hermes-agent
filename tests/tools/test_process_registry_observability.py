"""Privacy and lifecycle contract for ProcessRegistry observability."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import time

import pytest

from tools import process_registry as process_registry_module
from tools.process_registry import (
    AGENT_RUNS_OBSERVABILITY_SCHEMA,
    MAX_OBSERVABILITY_RUNS,
    ProcessRegistry,
    ProcessSession,
)


@pytest.fixture()
def snapshot_path(tmp_path, monkeypatch):
    path = tmp_path / "runtime" / "agent-runs-observability.json"
    monkeypatch.setattr(
        process_registry_module,
        "AGENT_RUNS_OBSERVABILITY_PATH",
        path,
    )
    return path


def _read_snapshot(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _run_by_id(path, proc_id):
    snapshot = _read_snapshot(path)
    return next(run for run in snapshot["runs"] if run["proc_id"] == proc_id)


def _wait_until(predicate, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_snapshot_updates_atomically_after_spawn_output_and_exit(snapshot_path):
    registry = ProcessRegistry()
    command = (
        f"{sys.executable} -c \"import time; "
        "print('private-output', flush=True); time.sleep(0.2)\""
    )
    session = registry.spawn_local(command, cwd=str(snapshot_path.parent.parent))

    spawned = _run_by_id(snapshot_path, session.id)
    assert spawned["proc_id"] == session.id
    assert spawned["orchestration_state"] == "running"
    assert spawned["started_at"] == session.started_at
    assert spawned["ended_at"] is None
    assert spawned["exit_code"] is None
    assert spawned["completion_reason"] is None
    assert spawned["output_history_available"] is True
    # The reader thread may publish the first output before spawn_local returns.
    # Both states are valid; the snapshot must keep availability and timestamp
    # internally consistent rather than promise an impossible ordering.
    if spawned["last_output_at"] is None:
        assert spawned["last_output_available"] is False
    else:
        assert spawned["last_output_available"] is True

    assert _wait_until(
        lambda: _run_by_id(snapshot_path, session.id)["last_output_at"] is not None
    )
    output_seen = _run_by_id(snapshot_path, session.id)
    assert output_seen["last_output_available"] is True
    assert output_seen["output_history_available"] is True

    assert _wait_until(
        lambda: _run_by_id(snapshot_path, session.id)["orchestration_state"]
        == "finished"
    )
    finished = _run_by_id(snapshot_path, session.id)
    assert finished["ended_at"] is not None
    assert finished["exit_code"] == 0
    assert finished["completion_reason"] == "exited"

    snapshot = _read_snapshot(snapshot_path)
    assert snapshot["schema_version"] == AGENT_RUNS_OBSERVABILITY_SCHEMA
    assert snapshot["observation_scope"] == "orchestration"
    assert snapshot["freshness"] == {
        "observed_at": snapshot["generated_at"],
        "max_age_seconds": None,
    }
    assert stat.S_IMODE(snapshot_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(snapshot_path.parent.stat().st_mode) == 0o700
    assert list(snapshot_path.parent.glob(".*.tmp")) == []


def test_snapshot_never_contains_private_process_fields_or_output(snapshot_path):
    registry = ProcessRegistry()
    private_values = {
        "raw-command-secret": "command",
        "/private/workdir-secret": "cwd",
        "task-secret": "task_id",
        "discord:dm:session-secret": "session_key",
        "discord-secret": "watcher_platform",
        "chat-secret": "watcher_chat_id",
        "user-secret": "watcher_user_id",
        "thread-secret": "watcher_thread_id",
        "message-secret": "watcher_message_id",
        "output-body-secret": "output_buffer",
        "https://private.example/secret": "watcher_user_name",
    }
    session = ProcessSession(
        id="proc_privacy123",
        command="raw-command-secret",
        cwd="/private/workdir-secret",
        task_id="task-secret",
        session_key="discord:dm:session-secret",
        watcher_platform="discord-secret",
        watcher_chat_id="chat-secret",
        watcher_user_id="user-secret",
        watcher_user_name="https://private.example/secret",
        watcher_thread_id="thread-secret",
        watcher_message_id="message-secret",
        output_buffer="output-body-secret",
        started_at=time.time(),
    )
    registry._running[session.id] = session
    registry._write_observability_snapshot()

    snapshot = _read_snapshot(snapshot_path)
    encoded = snapshot_path.read_text(encoding="utf-8")
    assert set(snapshot) == {
        "schema_version",
        "observation_scope",
        "orchestration_host_alias",
        "generated_at",
        "freshness",
        "runs",
    }
    assert set(snapshot["runs"][0]) == {
        "proc_id",
        "orchestration_state",
        "started_at",
        "ended_at",
        "exit_code",
        "completion_reason",
        "last_output_at",
        "last_output_available",
        "output_history_available",
    }
    for private_value, source_field in private_values.items():
        assert private_value not in encoded, source_field
    for forbidden_key in (
        "command",
        "cwd",
        "pid",
        "task_id",
        "session_key",
        "discord",
        "output_buffer",
        "output",
        "url",
        "credential",
    ):
        assert f'"{forbidden_key}"' not in encoded


def test_snapshot_bounds_and_validates_states_reasons_and_numbers(snapshot_path):
    registry = ProcessRegistry()
    registry._orchestration_host_alias = "bad alias with spaces"
    now = time.time()
    for index in range(MAX_OBSERVABILITY_RUNS + 5):
        session = ProcessSession(
            id=f"proc_{index:012d}",
            command="secret",
            started_at=now + index,
            exited=True,
            ended_at=now + index + 1,
            exit_code=0,
            completion_reason="exited",
        )
        registry._finished[session.id] = session

    invalid = registry._finished[f"proc_{MAX_OBSERVABILITY_RUNS + 4:012d}"]
    invalid.id = "../../not-an-opaque-id"
    invalid.started_at = float("nan")
    invalid.ended_at = "not-a-number"
    invalid.exit_code = True
    invalid.completion_reason = "raw secret reason"

    registry._write_observability_snapshot()
    snapshot = _read_snapshot(snapshot_path)

    assert len(snapshot["runs"]) == MAX_OBSERVABILITY_RUNS
    assert snapshot["orchestration_host_alias"] == "unknown"
    invalid_run = registry._observability_run_entry(invalid)
    assert invalid_run["proc_id"] == "unknown"
    assert invalid_run["orchestration_state"] == "finished"
    assert invalid_run["started_at"] is None
    assert invalid_run["ended_at"] is None
    assert invalid_run["exit_code"] is None
    assert invalid_run["completion_reason"] == "unknown"


def test_recovered_process_marks_output_observations_unknown(snapshot_path, tmp_path, monkeypatch):
    checkpoint = tmp_path / "processes.json"
    checkpoint.write_text(
        json.dumps([
            {
                "session_id": "proc_recovered1",
                "command": "private recovered command",
                "pid": os.getpid(),
                "pid_scope": "host",
                "started_at": time.time() - 10,
                "task_id": "private-task",
                "session_key": "discord:private-session",
            }
        ]),
        encoding="utf-8",
    )
    monkeypatch.setattr(process_registry_module, "CHECKPOINT_PATH", checkpoint)

    registry = ProcessRegistry()
    assert registry.recover_from_checkpoint() == 1

    recovered = _run_by_id(snapshot_path, "proc_recovered1")
    assert recovered["orchestration_state"] == "running"
    assert recovered["last_output_at"] is None
    assert recovered["last_output_available"] is None
    assert recovered["output_history_available"] is None


def test_snapshot_does_not_report_local_wrapper_pid_or_resources(snapshot_path):
    registry = ProcessRegistry()
    session = ProcessSession(
        id="proc_remotejoin1",
        command="ssh remote-host codex exec secret",
        pid=424242,
        host_start_time=123456,
        started_at=time.time(),
    )
    registry._running[session.id] = session
    registry._write_observability_snapshot()

    encoded = snapshot_path.read_text(encoding="utf-8")
    assert "424242" not in encoded
    assert "123456" not in encoded
    assert "remote-host" not in encoded
    assert "resource" not in encoded
    assert _run_by_id(snapshot_path, session.id)["proc_id"] == session.id


def test_spawn_injects_opaque_process_id_for_cooperating_remote_runner(snapshot_path):
    registry = ProcessRegistry()
    session = registry.spawn_local(
        f"{sys.executable} -c \"import os; print(os.environ['HERMES_PROCESS_ID'])\"",
        cwd=str(snapshot_path.parent.parent),
    )
    assert _wait_until(lambda: session.exited)
    # A non-interactive PTY shell may prepend its own job-control warning. The
    # child-owned identity must still be the final non-empty output line.
    output_lines = [line.strip() for line in session.output_buffer.splitlines() if line.strip()]
    assert output_lines[-1] == session.id


def test_noisy_output_snapshot_writes_are_rate_limited(snapshot_path, monkeypatch):
    registry = ProcessRegistry()
    session = ProcessSession(
        id="proc_ratelimit1",
        command="private",
        started_at=time.time(),
    )
    registry._running[session.id] = session
    writes = []
    real_write = registry._write_observability_snapshot

    def counted_write(*args, **kwargs):
        writes.append(time.monotonic())
        return real_write(*args, **kwargs)

    monkeypatch.setattr(registry, "_write_observability_snapshot", counted_write)
    registry._record_output_observation(session, "first")
    registry._record_output_observation(session, "second")
    registry._record_output_observation(session, "third")
    assert len(writes) == 1


def test_concurrent_noisy_output_snapshot_writes_are_rate_limited(snapshot_path, monkeypatch):
    registry = ProcessRegistry()
    session = ProcessSession(
        id="proc_concurrent1",
        command="private",
        started_at=time.time(),
        last_output_at=time.time(),
    )
    registry._running[session.id] = session
    writes = []
    real_write = registry._write_observability_snapshot

    def counted_write(*args, **kwargs):
        writes.append(time.monotonic())
        return real_write(*args, **kwargs)

    monkeypatch.setattr(registry, "_write_observability_snapshot", counted_write)
    registry._snapshot_last_write_monotonic = 0.0
    threads = [
        threading.Thread(target=registry._record_output_observation, args=(session, "chunk"))
        for _ in range(16)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(writes) == 1


@pytest.mark.parametrize("windows,stdout", [(True, object()), (False, None)])
def test_reconcile_local_exit_sets_terminal_state_without_posix_pipe_drain(
    snapshot_path, monkeypatch, windows, stdout
):
    class ExitedProcess:
        def __init__(self):
            self.stdout = stdout

        def poll(self):
            return 7

    registry = ProcessRegistry()
    session = ProcessSession(
        id="proc_reconcile1",
        command="private",
        started_at=time.time(),
        process=ExitedProcess(),
    )
    registry._running[session.id] = session
    monkeypatch.setattr(process_registry_module, "_IS_WINDOWS", windows)

    registry._reconcile_local_exit(session)

    assert session.exited is True
    assert session.exit_code == 7
    assert session.completion_reason == "exited"
    assert session.id not in registry._running
    assert registry._finished[session.id] is session
    assert _run_by_id(snapshot_path, session.id)["orchestration_state"] == "finished"


def test_failed_remote_spawn_remains_in_finished_snapshot(snapshot_path):
    class FailedEnv:
        def get_temp_dir(self):
            return "/tmp"

        def execute(self, _command, **_kwargs):
            return {"output": "launch failed", "returncode": 9}

    registry = ProcessRegistry()
    failed = registry.spawn_via_env(FailedEnv(), "private command")
    assert failed.exited is True
    assert failed.completion_reason == "failed_start"
    assert registry.get(failed.id) is failed

    registry._write_observability_snapshot()
    retained = _run_by_id(snapshot_path, failed.id)
    assert retained["orchestration_state"] == "finished"
    assert retained["completion_reason"] == "failed_start"


def test_remote_spawn_carries_opaque_process_id_without_changing_public_command(snapshot_path):
    class FakeEnv:
        def __init__(self):
            self.commands = []

        def get_temp_dir(self):
            return "/tmp"

        def execute(self, command, **_kwargs):
            self.commands.append(command)
            if "echo $!" in command:
                return {"output": "4242\n", "returncode": 0}
            return {"output": "", "returncode": 0}

    registry = ProcessRegistry()
    env = FakeEnv()
    session = registry.spawn_via_env(env, "sleep 30")
    assert session.command == "sleep 30"
    assert session.id in env.commands[0]
    assert "HERMES_PROCESS_ID" in env.commands[0]
    session.exited = True
