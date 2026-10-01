"""Focused regressions for task-owned shared-CDP lifecycle fencing."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tools import browser_tool as bt

from tools import browser_tool_cdp as bt_cdp
from tools import browser_tool_cloud as bt_cloud
from tools import browser_tool_eval_policy as bt_eval_policy
from tools import browser_tool_install as bt_install
from tools import browser_tool_lifecycle as bt_lifecycle
from tools import browser_tool_session as bt_session


@pytest.fixture(autouse=True)
def _isolated_browser_lifecycle(monkeypatch):
    monkeypatch.setattr(bt, "_active_sessions", {})
    monkeypatch.setattr(bt, "_session_last_activity", {})
    monkeypatch.setattr(bt, "_last_active_session_key", {})
    monkeypatch.setattr(bt, "_retired_browser_tasks", set())
    monkeypatch.setattr(bt, "_browser_task_states", {})
    monkeypatch.setattr(bt, "_browser_task_generations", {})
    monkeypatch.setattr(bt, "_browser_task_cleanup_reasons", {})
    monkeypatch.setattr(bt, "_browser_task_cleanup_locks", {})
    monkeypatch.setattr(bt, "_pending_provider_cleanups", {})
    monkeypatch.setattr(bt, "_session_owner_homes", {})
    monkeypatch.setattr(bt, "_cleanup_failures", {})
    monkeypatch.setattr(bt_lifecycle, "_start_browser_cleanup_thread", lambda: None)
    monkeypatch.setattr(bt_cdp, "_stop_cdp_supervisor", lambda _task: None)
    monkeypatch.setattr(bt, "_maybe_stop_recording", lambda _task: None)
    monkeypatch.setattr(bt, "_maybe_start_recording", lambda _task: None)
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: False)


@pytest.mark.parametrize("target_record", [None, "broken", '{"pinned":true,"targetId":"A"}'])
def test_shared_cdp_timeout_retains_exact_cleanup_ownership(monkeypatch, tmp_path, target_record):
    task_id = "shared-timeout"
    session = {"session_name": "cdp_timeout", "cdp_url": "ws://shared"}
    bt._active_sessions[task_id] = session
    bt._last_active_session_key[task_id] = task_id
    socket_dir = tmp_path / "agent-browser-cdp_timeout"
    socket_dir.mkdir()
    target = socket_dir / "cdp_timeout.target"
    if target_record is not None:
        target.write_text(target_record)
    (socket_dir / "cdp_timeout.pid").write_text("12345")
    monkeypatch.setattr(bt_lifecycle, "_verify_reapable_browser_daemon", lambda *_args: True)
    with patch("agent.deadline.kill_process_tree") as kill:
        bt_session._discard_timed_out_browser_session(task_id, session, str(socket_dir))
    kill.assert_not_called()
    assert bt._active_sessions[task_id] is session
    assert bt._last_active_session_key[task_id] == task_id
    assert socket_dir.is_dir()
    assert target.read_text() == target_record if target_record is not None else not target.exists()


def test_provider_only_cleanup_retry_clears_profile_ownership():
    task_id = "provider-only-retry"
    provider = MagicMock()
    provider.close_session.return_value = True
    bt_lifecycle._remember_pending_provider_cleanup(task_id, provider, "provider-session")
    bt._cleanup_failures[task_id] = 1
    assert task_id in bt._session_owner_homes
    assert bt_lifecycle.cleanup_browser(task_id) is True
    provider.close_session.assert_called_once_with("provider-session")
    assert task_id not in bt._session_owner_homes
    assert task_id not in bt._cleanup_failures
    assert task_id not in bt._session_last_activity


@pytest.mark.parametrize("backend", ["local", "provider"])
def test_real_inactivity_cleanup_is_nonterminal_and_normal_command_recreates(
    monkeypatch,
    backend,
):
    task_id = f"idle-{backend}"
    now = 10_000.0

    provider = MagicMock()
    provider.close_session.return_value = True
    provider.create_session.return_value = {
        "session_name": "provider-new",
        "bb_session_id": "provider-new-id",
        "cdp_url": "wss://provider.example/new",
        "features": {},
    }
    if backend == "provider":
        old_session = {
            "session_name": "provider-old",
            "bb_session_id": "provider-old-id",
            "cdp_url": "wss://provider.example/old",
            "features": {},
            "_provider_cleanup_owner": provider,
        }
    else:
        old_session = {
            "session_name": "local-old",
            "bb_session_id": None,
            "cdp_url": None,
            "features": {"local": True},
        }

    bt._active_sessions[task_id] = old_session
    bt._session_last_activity[task_id] = now - 100
    monkeypatch.setattr(bt, "BROWSER_SESSION_INACTIVITY_TIMEOUT", 10)
    monkeypatch.setattr(bt.time, "time", lambda: now)
    monkeypatch.setattr(bt.os.path, "exists", lambda _path: False)
    monkeypatch.setattr(bt_cdp, "_get_cdp_override", lambda: "")
    monkeypatch.setattr(
        bt_cloud,
        "_get_cloud_provider",
        (lambda: provider) if backend == "provider" else (lambda: None),
    )
    monkeypatch.setattr(bt_cdp, "_resolve_cdp_override", lambda value: value)
    monkeypatch.setattr(bt_cdp, "_ensure_cdp_supervisor", lambda *_a, **_kw: None)

    with patch.object(bt_session, "_run_browser_command", return_value={"success": True}):
        bt_lifecycle._cleanup_inactive_browser_sessions()

    assert task_id not in bt._active_sessions
    assert task_id not in bt._retired_browser_tasks
    assert bt._browser_task_states[task_id] is bt_lifecycle.BrowserTaskState.ACTIVE

    # Drive the same implicit session lookup used by a normal snapshot command.
    monkeypatch.setattr(bt_cloud, "_is_local_backend", lambda: True)

    def _normal_snapshot(session_key, command, args, **_kwargs):
        assert command == "snapshot"
        session = bt_session._get_session_info(session_key)
        return {
            "success": True,
            "data": {"snapshot": session["session_name"], "refs": {}},
        }

    monkeypatch.setattr(bt_session, "_run_browser_command", _normal_snapshot)
    result = json.loads(bt.browser_snapshot(task_id=task_id))

    assert result["success"] is True
    assert bt._active_sessions[task_id] is not old_session
    if backend == "provider":
        provider.close_session.assert_called_once_with("provider-old-id")
        provider.create_session.assert_called_once_with(task_id)
    else:
        assert bt._active_sessions[task_id]["features"]["local"] is True


def test_provider_cdp_uses_generic_node22_path_and_provider_cleanup(
    monkeypatch, tmp_path
):
    task_id = "provider-node22"
    provider = MagicMock()
    provider.close_session.return_value = True
    session = {
        "session_name": "provider-session",
        "bb_session_id": "paid-provider-id",
        "cdp_url": "wss://provider.example/devtools/browser/paid",
        "features": {},
        "_provider_cleanup_owner": provider,
    }
    bt._active_sessions[task_id] = session
    bt._session_last_activity[task_id] = 1.0

    resolver_calls: list[bool] = []
    commands: list[list[str]] = []

    def _resolver(*, require_pin_tab: bool = False):
        resolver_calls.append(require_pin_tab)
        if require_pin_tab:
            raise bt_install.AgentBrowserCapabilityError("Node >=24 required")
        return "/tmp/agent-browser-node22"

    class _Proc:
        returncode = 0

        def __init__(self, argv, *, stdout, stderr, **_kwargs):
            commands.append(list(argv))
            os.write(stdout, b'{"success":true,"data":{"snapshot":"ok"}}')

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(bt_install, "_find_agent_browser", _resolver)
    monkeypatch.setattr(bt_install, "_requires_real_termux_browser_install", lambda _cmd: False)
    monkeypatch.setattr(bt_cloud, "_is_local_mode", lambda: False)
    monkeypatch.setattr(bt, "_socket_safe_tmpdir", lambda: str(tmp_path))
    monkeypatch.setattr(bt_lifecycle, "_write_owner_pid", lambda *_a: None)
    monkeypatch.setattr(bt, "_build_browser_env", lambda: {"PATH": "/usr/bin"})
    monkeypatch.setattr(bt_install, "_merge_browser_path", lambda value: value)
    monkeypatch.setattr(bt_session, "_needs_chromium_sandbox_bypass", lambda: False)
    monkeypatch.setattr(bt.subprocess, "Popen", _Proc)
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)

    command_result = bt_session._run_browser_command(task_id, "snapshot", ["-c"])
    cleanup_result = bt_lifecycle.cleanup_browser(task_id)

    assert command_result["success"] is True
    assert cleanup_result is True
    assert resolver_calls == [False, False]
    assert all("--pin-tab" not in argv for argv in commands)
    assert all("agent-browser@0.34.0" not in argv for argv in commands)
    assert commands[0][1:3] == ["--cdp", session["cdp_url"]]
    provider.close_session.assert_called_once_with("paid-provider-id")


def test_provider_api_close_is_not_blocked_by_local_tab_capability(monkeypatch):
    task_id = "provider-close-authoritative"
    provider = MagicMock()
    provider.close_session.return_value = True
    bt._active_sessions[task_id] = {
        "session_name": "provider-close",
        "bb_session_id": "provider-close-id",
        "cdp_url": "wss://provider.example/close",
        "features": {},
        "_provider_cleanup_owner": provider,
    }
    monkeypatch.setattr(bt.os.path, "exists", lambda _path: False)

    with patch.object(
        bt_session,
        "_run_browser_command",
        return_value={
            "success": False,
            "code": "pin_tab_unavailable",
            "error": "Node >=24 required",
        },
    ) as command:
        assert bt_lifecycle.cleanup_browser(task_id) is True

    command.assert_called_once_with(
        task_id,
        "close",
        [],
        timeout=10,
        _allow_cleanup=True,
    )
    provider.close_session.assert_called_once_with("provider-close-id")


def test_cleanup_command_ignores_turn_interrupt(monkeypatch, tmp_path):
    task_id = "cleanup-during-cancellation"
    bt._active_sessions[task_id] = {
        "session_name": "cleanup-during-cancellation",
        "bb_session_id": None,
        "cdp_url": None,
        "features": {"local": True},
    }
    commands: list[list[str]] = []

    class _Proc:
        returncode = 0

        def __init__(self, argv, *, stdout, stderr, **_kwargs):
            commands.append(list(argv))
            os.write(stdout, b'{"success":true,"data":{}}')

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(
        bt_install, "_find_agent_browser", lambda **_kwargs: "/tmp/agent-browser"
    )
    monkeypatch.setattr(bt_install, "_requires_real_termux_browser_install", lambda _cmd: False)
    monkeypatch.setattr(bt_cloud, "_is_local_mode", lambda: False)
    monkeypatch.setattr(bt, "_socket_safe_tmpdir", lambda: str(tmp_path))
    monkeypatch.setattr(bt_lifecycle, "_write_owner_pid", lambda *_a: None)
    monkeypatch.setattr(bt, "_build_browser_env", lambda: {"PATH": "/usr/bin"})
    monkeypatch.setattr(bt_install, "_merge_browser_path", lambda value: value)
    monkeypatch.setattr(bt_session, "_needs_chromium_sandbox_bypass", lambda: False)
    monkeypatch.setattr(bt.subprocess, "Popen", _Proc)
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: True)

    result = bt_session._run_browser_command(
        task_id,
        "close",
        [],
        _allow_cleanup=True,
    )

    assert result["success"] is True
    assert len(commands) == 1


@pytest.mark.parametrize("failure", ["false", "exception"])
def test_provider_close_failure_retains_retry_identity_until_success(
    monkeypatch,
    failure,
):
    task_id = f"provider-retry-{failure}"
    calls: list[str] = []

    class _Provider:
        def close_session(self, session_id):
            calls.append(session_id)
            if len(calls) == 1:
                if failure == "exception":
                    raise RuntimeError("provider unavailable")
                return False
            return True

    provider = _Provider()
    bt._active_sessions[task_id] = {
        "session_name": "provider-retry",
        "bb_session_id": "provider-retry-id",
        "cdp_url": "wss://provider.example/retry",
        "features": {},
        "_provider_cleanup_owner": provider,
    }
    monkeypatch.setattr(bt.os.path, "exists", lambda _path: False)

    with patch.object(bt_session, "_run_browser_command", return_value={"success": True}):
        assert bt_lifecycle.cleanup_browser(task_id) is False

    assert task_id not in bt._active_sessions
    pending = list(bt._pending_provider_cleanups.values())
    assert [(item.provider, item.session_id) for item in pending] == [
        (provider, "provider-retry-id")
    ]
    assert bt._browser_task_states[task_id] is bt_lifecycle.BrowserTaskState.RETIRING

    find_browser = MagicMock()
    monkeypatch.setattr(bt_install, "_find_agent_browser", find_browser)
    blocked = json.loads(bt.browser_snapshot(task_id=task_id))
    assert blocked["code"] == "browser_session_retired"
    assert blocked["data"]["cleanup_pending"] is True
    find_browser.assert_not_called()

    # Retry only the retained provider identity. No page lookup/adoption occurs.
    get_session = MagicMock()
    monkeypatch.setattr(bt_session, "_get_session_info", get_session)
    assert bt_lifecycle.cleanup_browser(task_id) is True
    assert calls == ["provider-retry-id", "provider-retry-id"]
    assert bt._pending_provider_cleanups == {}
    assert task_id in bt._retired_browser_tasks
    get_session.assert_not_called()


def test_cleanup_pending_blocks_all_business_commands_and_supervisor_eval(monkeypatch):
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    task_id = "cleanup-pending-all-commands"
    session = {
        "session_name": "cdp-pending",
        "bb_session_id": None,
        "cdp_url": "ws://shared",
        "features": {"cdp_override": True},
    }
    bt._active_sessions[task_id] = session
    bt._last_active_session_key[task_id] = task_id

    with patch.object(
        bt_session,
        "_run_browser_command",
        return_value={"success": False, "error": "exact close unavailable"},
    ):
        assert bt_lifecycle.cleanup_browser(task_id) is False

    monkeypatch.setattr(bt_eval_policy, "_eval_ssrf_guard_active", lambda _task: False)
    find_browser = MagicMock()
    supervisor_get = MagicMock()
    monkeypatch.setattr(bt_install, "_find_agent_browser", find_browser)
    monkeypatch.setattr(SUPERVISOR_REGISTRY, "get", supervisor_get)

    results = [
        json.loads(bt.browser_snapshot(task_id=task_id)),
        json.loads(bt.browser_click("@e1", task_id=task_id)),
        json.loads(bt.browser_type("@e1", "value", task_id=task_id)),
        json.loads(
            bt.browser_console(expression="window.sideEffect++", task_id=task_id)
        ),
    ]

    assert all(result["code"] == "browser_session_retired" for result in results)
    assert all(result["data"]["cleanup_pending"] is True for result in results)
    find_browser.assert_not_called()
    supervisor_get.assert_not_called()


def test_failed_restart_blanking_and_exact_close_stays_fail_closed(monkeypatch):
    task_id = "failed-restart-three-stage"
    bt._retired_browser_tasks.add(task_id)
    monkeypatch.setattr(bt, "_navigation_session_key", lambda task, _url: task)
    monkeypatch.setattr(bt_cdp, "_get_cdp_override", lambda: "ws://shared")
    monkeypatch.setattr(bt_cloud, "_is_local_backend", lambda: True)
    monkeypatch.setattr(
        bt,
        "_is_always_blocked_url",
        lambda url: url == "http://blocked.internal/",
    )
    monkeypatch.setattr(bt, "check_website_access", lambda _url: None)
    monkeypatch.setattr(bt_cdp, "_pinned_cdp_target_id", lambda _task: "TARGET")
    monkeypatch.setattr(bt_cdp, "_ensure_cdp_supervisor", lambda *_a, **_kw: None)

    command_calls: list[tuple[str, list[str]]] = []

    def _command(_task, command, args, **_kwargs):
        command_calls.append((command, list(args)))
        if command == "open" and args == ["https://example.com"]:
            return {
                "success": True,
                "data": {
                    "url": "http://blocked.internal/",
                    "title": "blocked",
                },
            }
        if command == "open" and args == ["about:blank"]:
            return {"success": False, "error": "blanking failed"}
        if command == "tab":
            return {"success": False, "error": "exact close failed"}
        raise AssertionError((command, args))

    monkeypatch.setattr(bt_session, "_run_browser_command", _command)

    result = json.loads(bt.browser_navigate("https://example.com", task_id=task_id))

    assert result["success"] is False
    assert bt._browser_task_states[task_id] is bt_lifecycle.BrowserTaskState.RETIRING
    assert bt._active_sessions[task_id]["_cleanup_retry_pending"] is True
    assert command_calls == [
        ("open", ["https://example.com"]),
        ("open", ["about:blank"]),
        ("tab", ["close"]),
    ]

    # A new explicit navigation is also rejected until exact rollback cleanup
    # completes; it cannot silently start another generation.
    blocked = json.loads(bt.browser_navigate("https://example.org", task_id=task_id))
    assert blocked["code"] == "browser_session_retired"
    assert blocked["data"]["cleanup_pending"] is True


def test_two_provider_creators_close_the_loser(monkeypatch):
    task_id = "provider-creator-race"
    barrier = threading.Barrier(2)
    created: list[str] = []
    closed: list[str] = []
    list_lock = threading.Lock()

    class _Provider:
        def create_session(self, _task_id):
            with list_lock:
                index = len(created) + 1
                session_id = f"provider-{index}"
                created.append(session_id)
            barrier.wait(timeout=3)
            return {
                "session_name": f"provider-session-{index}",
                "bb_session_id": session_id,
                "cdp_url": f"wss://provider.example/{index}",
                "features": {},
            }

        def close_session(self, session_id):
            closed.append(session_id)
            return True

    provider = _Provider()
    monkeypatch.setattr(bt_cdp, "_get_cdp_override", lambda: "")
    monkeypatch.setattr(bt_cloud, "_get_cloud_provider", lambda: provider)
    monkeypatch.setattr(bt_cdp, "_resolve_cdp_override", lambda value: value)
    monkeypatch.setattr(bt_cdp, "_ensure_cdp_supervisor", lambda *_a, **_kw: None)

    results: list[dict] = []
    threads = [
        threading.Thread(target=lambda: results.append(bt_session._get_session_info(task_id)))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=4)

    assert all(not thread.is_alive() for thread in threads)
    assert len(created) == 2
    assert len(closed) == 1
    assert closed[0] != bt._active_sessions[task_id]["bb_session_id"]
    assert {result["bb_session_id"] for result in results} == {
        bt._active_sessions[task_id]["bb_session_id"]
    }


def test_terminal_cleanup_fences_inflight_provider_creator(monkeypatch):
    task_id = "provider-create-vs-terminal"
    entered = threading.Event()
    release = threading.Event()
    closed: list[str] = []

    class _Provider:
        def create_session(self, _task_id):
            entered.set()
            assert release.wait(timeout=3)
            return {
                "session_name": "late-provider",
                "bb_session_id": "late-provider-id",
                "cdp_url": "wss://provider.example/late",
                "features": {},
            }

        def close_session(self, session_id):
            closed.append(session_id)
            return True

    provider = _Provider()
    monkeypatch.setattr(bt_cdp, "_get_cdp_override", lambda: "")
    monkeypatch.setattr(bt_cloud, "_get_cloud_provider", lambda: provider)
    monkeypatch.setattr(bt_cdp, "_resolve_cdp_override", lambda value: value)
    monkeypatch.setattr(bt_cdp, "_ensure_cdp_supervisor", lambda *_a, **_kw: None)

    errors: list[BaseException] = []

    def _create():
        try:
            bt_session._get_session_info(task_id)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=_create)
    thread.start()
    assert entered.wait(timeout=3)

    assert bt_lifecycle.cleanup_browser(task_id) is True
    assert bt._browser_task_states[task_id] is bt_lifecycle.BrowserTaskState.RETIRED
    release.set()
    thread.join(timeout=4)

    assert not thread.is_alive()
    assert task_id not in bt._active_sessions
    assert closed == ["late-provider-id"]
    assert len(errors) == 1
    assert isinstance(errors[0], bt_lifecycle._BrowserSessionRetiredError)


def test_late_stale_creator_close_failure_reopens_only_cleanup_state(monkeypatch):
    task_id = "provider-create-late-close-retry"
    entered = threading.Event()
    release = threading.Event()
    close_calls: list[str] = []

    class _Provider:
        def create_session(self, _task_id):
            entered.set()
            assert release.wait(timeout=3)
            return {
                "session_name": "late-provider-retry",
                "bb_session_id": "late-provider-retry-id",
                "cdp_url": "wss://provider.example/late-retry",
                "features": {},
            }

        def close_session(self, session_id):
            close_calls.append(session_id)
            return len(close_calls) > 1

    provider = _Provider()
    monkeypatch.setattr(bt_cdp, "_get_cdp_override", lambda: "")
    monkeypatch.setattr(bt_cloud, "_get_cloud_provider", lambda: provider)
    monkeypatch.setattr(bt_cdp, "_resolve_cdp_override", lambda value: value)
    monkeypatch.setattr(bt_cdp, "_ensure_cdp_supervisor", lambda *_a, **_kw: None)

    errors: list[BaseException] = []

    def _create():
        try:
            bt_session._get_session_info(task_id)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=_create)
    thread.start()
    assert entered.wait(timeout=3)

    assert bt_lifecycle.cleanup_browser(task_id) is True
    release.set()
    thread.join(timeout=4)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], bt_lifecycle._BrowserSessionRetiredError)
    assert task_id not in bt._active_sessions
    assert len(bt._pending_provider_cleanups) == 1
    assert bt._browser_task_states[task_id] is bt_lifecycle.BrowserTaskState.RETIRING

    blocked = json.loads(bt.browser_snapshot(task_id=task_id))
    assert blocked["code"] == "browser_session_retired"
    assert blocked["data"]["cleanup_pending"] is True

    assert bt_lifecycle.cleanup_browser(task_id) is True
    assert close_calls == ["late-provider-retry-id", "late-provider-retry-id"]
    assert bt._pending_provider_cleanups == {}
    assert task_id in bt._retired_browser_tasks


def test_headed_turn_retains_local_but_cleans_shared_external_cdp(monkeypatch):
    monkeypatch.setattr(bt_cloud, "_is_headed_mode", lambda: True)
    monkeypatch.setattr(bt.os.path, "exists", lambda _path: False)

    local_task = "headed-local"
    shared_task = "headed-shared"
    local = {
        "session_name": "local-headed",
        "bb_session_id": None,
        "cdp_url": None,
        "features": {"local": True},
    }
    shared = {
        "session_name": "shared-headed",
        "bb_session_id": None,
        "cdp_url": "ws://shared",
        "features": {"cdp_override": True},
    }
    bt._active_sessions.update({local_task: local, shared_task: shared})

    with patch.object(
        bt_session, "_run_browser_command", return_value={"success": True}
    ) as command:
        assert bt_lifecycle.cleanup_browser_for_turn(local_task) is True
        assert bt_lifecycle.cleanup_browser_for_turn(shared_task) is True

    assert bt._active_sessions[local_task] is local
    assert shared_task not in bt._active_sessions
    assert shared_task in bt._retired_browser_tasks
    assert command.call_count == 2


def test_hard_cleanup_still_closes_untracked_camofox_task(monkeypatch):
    task_id = "camofox-hard-boundary"
    soft_cleanup = MagicMock(return_value=False)
    close = MagicMock()
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: True)
    monkeypatch.setattr(
        "tools.browser_camofox.camofox_soft_cleanup",
        soft_cleanup,
    )
    monkeypatch.setattr("tools.browser_camofox.camofox_close", close)

    assert bt_lifecycle.cleanup_browser(task_id) is True

    soft_cleanup.assert_called_once_with(task_id)
    close.assert_called_once_with(task_id)
    assert task_id in bt._retired_browser_tasks


def test_headed_turn_preserves_untracked_camofox_task(monkeypatch):
    task_id = "camofox-headed-boundary"
    monkeypatch.setattr(bt_cloud, "_is_headed_mode", lambda: True)
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: True)
    soft_cleanup = MagicMock()
    monkeypatch.setattr(
        "tools.browser_camofox.camofox_soft_cleanup",
        soft_cleanup,
    )

    assert bt_lifecycle.cleanup_browser_for_turn(task_id) is True

    soft_cleanup.assert_not_called()
    assert task_id not in bt._browser_task_states
    assert task_id not in bt._retired_browser_tasks


@pytest.mark.parametrize("exit_kind", ["direct_return", "exception", "cancellation"])
def test_outer_turn_boundary_cleans_browser_on_every_exit(monkeypatch, exit_kind):
    import run_agent
    from agent import turn_facade

    cleanup = MagicMock()
    monkeypatch.setattr(bt_lifecycle, "cleanup_browser_for_turn", cleanup)

    def _run():
        with turn_facade._browser_turn_cleanup_boundary("turn-exit-task"):
            if exit_kind == "exception":
                raise RuntimeError("ordinary failure")
            if exit_kind == "cancellation":
                raise asyncio.CancelledError("cancelled")
            return {"completed": False, "partial": True}

    if exit_kind == "exception":
        with pytest.raises(RuntimeError):
            _run()
    elif exit_kind == "cancellation":
        with pytest.raises(asyncio.CancelledError):
            _run()
    else:
        assert _run()["partial"] is True

    cleanup.assert_called_once_with("turn-exit-task")


def test_outer_turn_boundary_does_not_repeat_normal_finalizer_cleanup(monkeypatch):
    import run_agent
    from agent import turn_facade

    cleanup = MagicMock()
    monkeypatch.setattr(bt_lifecycle, "cleanup_browser_for_turn", cleanup)

    with turn_facade._browser_turn_cleanup_boundary("already-finalized"):
        turn_facade._mark_browser_turn_cleanup_complete()

    cleanup.assert_not_called()


@pytest.mark.parametrize("failing_step", ["stop_refresher", "join_threads", "release"])
def test_facade_cleans_browser_when_lease_teardown_raises(monkeypatch, failing_step):
    from agent import background_review, relay_runtime, review_idle_queue, turn_facade_lease
    from agent.turn_facade import TurnFacadeMixin

    cleanup = MagicMock()
    lease = MagicMock()
    getattr(lease, failing_step).side_effect = RuntimeError("lease teardown failed")
    monkeypatch.setattr(bt_lifecycle, "cleanup_browser_for_turn", cleanup)
    monkeypatch.setattr(background_review, "cancel_background_review_for_live_turn", lambda agent: None)
    monkeypatch.setattr(turn_facade_lease, "admit_durable_turn_lease", lambda *a, **k: SimpleNamespace(
        early_result=None, lease=lease, conversation_history=[],
    ))
    coordinator = MagicMock()
    coordinator.acquire_conversation.side_effect = RuntimeError("turn admission failed")
    monkeypatch.setattr(relay_runtime, "SESSION_COORDINATOR", coordinator)
    queue = MagicMock()
    monkeypatch.setattr(review_idle_queue, "QUEUE", queue)

    with pytest.raises(RuntimeError, match="lease teardown failed") as caught:
        TurnFacadeMixin.run_conversation(SimpleNamespace(session_id="cleanup-test"), "hello", task_id="lease-exit")

    assert caught.value is not None
    cleanup.assert_called_once_with("lease-exit")
    queue.note_turn_finished.assert_called_once_with()


def test_outer_turn_boundary_retries_when_normal_finalizer_cleanup_raises(monkeypatch):
    import run_agent
    from agent import turn_facade
    from agent.chat_completion_helpers import cleanup_task_resources

    cleanup = MagicMock(side_effect=[RuntimeError("cleanup failed"), True])
    monkeypatch.setattr(bt_lifecycle, "cleanup_browser_for_turn", cleanup)
    monkeypatch.setattr(run_agent, "cleanup_vm", lambda _task: None)
    monkeypatch.setattr(
        "agent.chat_completion_helpers.is_persistent_env",
        lambda _task: False,
    )
    agent = SimpleNamespace(verbose_logging=False)

    with turn_facade._browser_turn_cleanup_boundary("retry-turn-boundary"):
        cleanup_task_resources(agent, "retry-turn-boundary")

    assert [item.args for item in cleanup.call_args_list] == [
        ("retry-turn-boundary",),
        ("retry-turn-boundary",),
    ]


def test_outer_turn_boundary_retries_when_normal_finalizer_cleanup_returns_false(monkeypatch):
    import run_agent
    from agent import turn_facade
    from agent.chat_completion_helpers import cleanup_task_resources

    cleanup = MagicMock(side_effect=[False, True])
    monkeypatch.setattr(bt_lifecycle, "cleanup_browser_for_turn", cleanup)
    monkeypatch.setattr(run_agent, "cleanup_vm", lambda _task: None)
    monkeypatch.setattr(
        "agent.chat_completion_helpers.is_persistent_env",
        lambda _task: False,
    )
    agent = SimpleNamespace(verbose_logging=False)

    with turn_facade._browser_turn_cleanup_boundary("retry-false-boundary"):
        cleanup_task_resources(agent, "retry-false-boundary")

    assert [item.args for item in cleanup.call_args_list] == [
        ("retry-false-boundary",),
        ("retry-false-boundary",),
    ]


def test_terminal_cleanup_waits_for_inflight_subprocess_command(monkeypatch):
    task_id = "command-vs-terminal-cleanup"
    bt._active_sessions[task_id] = {
        "session_name": "local-command-race",
        "bb_session_id": None,
        "cdp_url": None,
        "features": {"local": True},
    }
    command_entered = threading.Event()
    release_command = threading.Event()
    cleanup_done = threading.Event()
    events: list[str] = []

    class _Popen:
        def __init__(self, cmd, *, stdout, **_kwargs):
            if "snapshot" in cmd:
                events.append("snapshot-start")
                command_entered.set()
                assert release_command.wait(timeout=3)
                events.append("snapshot-end")
                payload = b'{"success":true,"data":{"snapshot":"ok"}}'
            elif "close" in cmd:
                events.append("close")
                payload = b'{"success":true}'
            else:
                raise AssertionError(cmd)
            os.write(stdout, payload)
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(bt_install, "_find_agent_browser", lambda **_kwargs: "/tmp/fake-agent-browser")
    monkeypatch.setattr(bt_install, "_chromium_installed", lambda: True)
    monkeypatch.setattr(bt.subprocess, "Popen", _Popen)
    command_result: list[dict] = []
    cleanup_result: list[bool] = []
    command_thread = threading.Thread(
        target=lambda: command_result.append(
            bt_session._run_browser_command(task_id, "snapshot", ["-c"])
        )
    )

    def _cleanup():
        cleanup_result.append(bt_lifecycle.cleanup_browser(task_id))
        cleanup_done.set()

    cleanup_thread = threading.Thread(target=_cleanup)
    command_thread.start()
    assert command_entered.wait(timeout=3)
    cleanup_thread.start()
    assert not cleanup_done.wait(timeout=0.1)

    release_command.set()
    command_thread.join(timeout=4)
    cleanup_thread.join(timeout=4)

    assert not command_thread.is_alive()
    assert not cleanup_thread.is_alive()
    assert command_result[0]["success"] is True
    assert cleanup_result == [True]
    assert events == ["snapshot-start", "snapshot-end", "close"]
    assert task_id in bt._retired_browser_tasks


def test_terminal_cleanup_waits_for_inflight_supervisor_eval(monkeypatch):
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    task_id = "supervisor-eval-vs-terminal-cleanup"
    bt._active_sessions[task_id] = {
        "session_name": "local-supervisor-race",
        "bb_session_id": None,
        "cdp_url": None,
        "features": {"local": True},
    }
    eval_entered = threading.Event()
    release_eval = threading.Event()
    cleanup_done = threading.Event()
    events: list[str] = []

    class _Supervisor:
        def evaluate_runtime(self, _expression):
            events.append("eval-start")
            eval_entered.set()
            assert release_eval.wait(timeout=3)
            events.append("eval-end")
            return {"ok": True, "result": 1}

    monkeypatch.setattr(SUPERVISOR_REGISTRY, "get", lambda _task: _Supervisor())
    monkeypatch.setattr(bt_eval_policy, "_eval_ssrf_guard_active", lambda _task: False)

    class _Popen:
        def __init__(self, cmd, *, stdout, **_kwargs):
            assert "close" in cmd
            events.append("close")
            os.write(stdout, b'{"success":true}')
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(bt_install, "_find_agent_browser", lambda **_kwargs: "/tmp/fake-agent-browser")
    monkeypatch.setattr(bt_install, "_chromium_installed", lambda: True)
    monkeypatch.setattr(bt.subprocess, "Popen", _Popen)
    eval_result: list[dict] = []
    cleanup_result: list[bool] = []
    eval_thread = threading.Thread(
        target=lambda: eval_result.append(
            json.loads(bt._browser_eval("window.value", task_id=task_id))
        )
    )

    def _cleanup():
        cleanup_result.append(bt_lifecycle.cleanup_browser(task_id))
        cleanup_done.set()

    cleanup_thread = threading.Thread(target=_cleanup)
    eval_thread.start()
    assert eval_entered.wait(timeout=3)
    cleanup_thread.start()
    assert not cleanup_done.wait(timeout=0.1)

    release_eval.set()
    eval_thread.join(timeout=4)
    cleanup_thread.join(timeout=4)

    assert not eval_thread.is_alive()
    assert not cleanup_thread.is_alive()
    assert eval_result[0]["success"] is True
    assert cleanup_result == [True]
    assert events == ["eval-start", "eval-end", "close"]
    assert task_id in bt._retired_browser_tasks
