"""Real shared-CDP contracts for the native browser-harness task integration.

Run with HERMES_E2E_BROWSER=1 against the existing operator CDP browser on port 9222.
Only task-created blank pages are used; no browser or profile is launched here.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
import psutil

from hermes_cli.config import save_config
from tools import browser_use_cli  # registers the real browser_exec handler
from tools.browser_cdp_tool import browser_cdp
from tools.browser_session_lifecycle import cleanup_all_browsers, cleanup_browser
from tools.browser_supervisor import SUPERVISOR_REGISTRY
from tools.browser_vault_tool import _ensure_supervisor
from tools.registry import registry
from tests.waiting import wait_until

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True, name="_no_host_browser_use_cli")
def real_browser_harness():
    """Use the installed driver instead of the parent directory's unit-test replacement."""
    yield


@pytest.fixture(autouse=True, name="_no_shared_temp_socket_dirs")
def real_harness_runtime_directories():
    yield


@pytest.fixture(autouse=True, name="_no_chromium_download")
def real_browser_launcher():
    yield


@pytest.fixture(autouse=True, name="_no_host_bot_desktop_autostart")
def real_desktop_configuration():
    yield


def _cdp(method, params=None, target_id=None):
    result = json.loads(browser_cdp(method, params, target_id=target_id, timeout=10))
    assert result.get("success") is True, result
    return result["result"]


def _exec(task_id, code, **options):
    assert browser_use_cli.browser_exec_available()
    result = registry.dispatch("browser_exec", {"code": code, **options}, task_id=task_id)
    assert isinstance(result, str), result
    value = json.loads(result)
    assert value.get("success") is True, value
    return value


@pytest.fixture
def shared_browser(tmp_path):
    if os.environ.get("HERMES_E2E_BROWSER") != "1":
        pytest.skip("Requires explicit access to the operator's existing CDP browser")
    previous = {key: os.environ.get(key) for key in ("BH_HOME", "BH_UPDATE_CHECK", "BH_TAB_MARKER")}
    os.environ["BH_HOME"] = str(tmp_path / "harness")
    os.environ["BH_UPDATE_CHECK"] = "0"
    os.environ["BH_TAB_MARKER"] = "0"
    save_config({"browser": {
        "cdp_url": "http://127.0.0.1:9222", "cloud_provider": "local",
        "allow_private_urls": True, "inactivity_timeout": 120,
    }})
    try:
        assert _cdp("Browser.getVersion")["product"].startswith("Chrome/")
        yield "native-" + uuid.uuid4().hex[:16]
    finally:
        cleanup_all_browsers()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_implicit_tasks_keep_pages_supervisors_and_cleanup_separate(shared_browser):
    tasks = [shared_browser + "-a", shared_browser + "-b"]

    def start(task):
        return _exec(task, f"js({json.dumps('document.title = ' + json.dumps(task))})\nprint(current_tab())")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(start, tasks))
    assert first["session"] != second["session"]
    assert first["target_id"] != second["target_id"]
    for task, result in zip(tasks, (first, second)):
        supervisor = SUPERVISOR_REGISTRY.get(task)
        assert supervisor.target_id == result["target_id"]
        assert supervisor.evaluate_runtime("document.title")["result"] == task
    again = _exec(tasks[0], "print(js('document.title'))")
    assert again["session"] == first["session"]
    assert again["target_id"] == first["target_id"]
    assert tasks[0] in again["output"]
    cleanup_browser(tasks[0])
    removed = json.loads(browser_cdp("Target.getTargetInfo", {"targetId": first["target_id"]}))
    assert removed.get("error"), removed
    remaining = _exec(tasks[1], "print(js('document.title'))")
    assert remaining["target_id"] == second["target_id"]
    assert tasks[1] in remaining["output"]
    assert SUPERVISOR_REGISTRY.get(tasks[0]) is None


def test_selected_page_and_vault_reconnect_follow_the_native_current_target(shared_browser):
    task = shared_browser
    original = _exec(task, "print(current_tab())")
    extra = _cdp("Target.createTarget", {"url": "about:blank", "background": True})["targetId"]
    try:
        expression = json.dumps("document.title = 'selected native page'")
        result = _exec(task, f"switch_tab({json.dumps(extra)})\njs({expression})")
        assert result["target_id"] == extra
        assert SUPERVISOR_REGISTRY.get(task).target_id == extra
        SUPERVISOR_REGISTRY.stop(task)
        supervisor = _ensure_supervisor(task)
        assert supervisor.target_id == extra
        assert supervisor.evaluate_runtime("document.title")["result"] == "selected native page"
        cleanup_browser(task)
        assert _cdp("Target.getTargetInfo", {"targetId": extra})["targetInfo"]["targetId"] == extra
        removed = json.loads(browser_cdp("Target.getTargetInfo", {"targetId": original["target_id"]}))
        assert removed.get("error"), removed
    finally:
        _cdp("Target.closeTarget", {"targetId": extra})


def test_explicit_named_workflow_survives_individual_task_cleanup(shared_browser):
    name = shared_browser
    first = _exec(shared_browser + "-a", "print(current_tab())", session=name)
    cleanup_browser(shared_browser + "-a")
    second = _exec(shared_browser + "-b", "print(current_tab())", session=name)
    assert first["target_id"] == second["target_id"]
    assert first["session"] == second["session"] == name


def test_native_output_redacts_values_registered_by_the_real_vault_boundary(shared_browser):
    from agent import redact

    value = "native-vault-check-" + uuid.uuid4().hex
    redact.register_vault_redaction_value(value)
    try:
        expression = json.dumps("document.body.textContent = " + json.dumps(value))
        result = _exec(
            shared_browser,
            f"import sys\njs({expression})\nprint(js('document.body.textContent'))\n"
            "print(js('document.body.textContent'), file=sys.stderr)",
        )
        assert value not in json.dumps(result)
        assert result["output"].strip()
        assert result["stderr"].strip()
    finally:
        redact.clear_vault_redaction_values()


def test_native_screenshot_returns_a_real_image(shared_browser):
    from PIL import Image

    result = registry.dispatch(
        "browser_exec", {"code": "print(capture_screenshot())"}, task_id=shared_browser,
    )
    payload = json.loads(result) if isinstance(result, str) else result["meta"]
    path = payload["screenshot_path"]
    with Image.open(path) as screenshot:
        assert screenshot.width > 0 and screenshot.height > 0
        screenshot.verify()


def test_native_timeout_stops_user_code_and_keeps_the_task_page_reusable(shared_browser, tmp_path):
    first = _exec(shared_browser, "print(current_tab())")
    pid_path = tmp_path / "exec.pid"
    code = (
        "import os, pathlib, threading\n"
        f"pathlib.Path({json.dumps(str(pid_path))}).write_text(str(os.getpid()))\n"
        "threading.Event().wait(30)\n"
    )
    result = json.loads(registry.dispatch(
        "browser_exec", {"code": code, "timeout_s": 5}, task_id=shared_browser,
    ))
    assert "timed out" in result["error"]
    pid = int(pid_path.read_text())
    assert wait_until(lambda: not psutil.pid_exists(pid), timeout=5)
    again = _exec(shared_browser, "print(current_tab())")
    assert again["target_id"] == first["target_id"]


def test_reserved_default_name_is_rejected_before_browser_execution(shared_browser):
    result = json.loads(registry.dispatch(
        "browser_exec", {"code": "raise RuntimeError('must not execute')", "session": "default"},
        task_id=shared_browser,
    ))
    assert "reserved" in result["error"]


def test_helpers_are_loaded_once_for_user_code_and_never_for_internal_queries(shared_browser):
    first = _exec(shared_browser, "print('workspace ready')")
    workspace = Path(first["workspace"])
    counter = workspace / "helper-loads.txt"
    source = (
        "from pathlib import Path\n"
        f"_counter = Path({json.dumps(str(counter))})\n"
        "_count = int(_counter.read_text()) if _counter.exists() else 0\n"
        "_counter.write_text(str(_count + 1))\nprint('helpers loaded')\n"
    )
    (workspace / "agent_helpers.py").write_text(source)
    result = _exec(shared_browser, "print('user code completed')")
    assert result["output"].count("helpers loaded") == 1
    assert "user code completed" in result["output"]
    assert counter.read_text() == "1"
    cleanup_browser(shared_browser)
    assert counter.read_text() == "1"


def test_page_close_preserves_completed_execution_output(shared_browser):
    _exec(shared_browser, "print(current_tab())")
    result = json.loads(registry.dispatch(
        "browser_exec", {"code": "print('completed before page close')\nclose_tab()"}, task_id=shared_browser,
    ))
    assert result["exit_code"] == 0
    assert result["execution_completed"] is True
    assert "completed before page close" in result["output"]
    assert result["binding_error"]
    assert SUPERVISOR_REGISTRY.get(shared_browser) is None


def test_user_exception_preserves_exit_code_and_stderr(shared_browser):
    result = json.loads(registry.dispatch(
        "browser_exec", {"code": "print('before failure')\nraise ValueError('native user failure')"},
        task_id=shared_browser,
    ))
    assert result["success"] is False
    assert result["exit_code"] != 0
    assert "before failure" in result["output"]
    assert "native user failure" in result["stderr"]


def test_cleanup_cancels_running_code_and_rejects_a_queued_late_call(shared_browser, tmp_path):
    from tools.browser_use_sessions import capture_task

    first = _exec(shared_browser, "print(current_tab())")
    ready = tmp_path / "running"
    late = tmp_path / "late"
    running_code = f"import pathlib, threading\npathlib.Path({json.dumps(str(ready))}).touch()\nthreading.Event().wait(30)"
    late_code = f"import pathlib\npathlib.Path({json.dumps(str(late))}).touch()"
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(registry.dispatch, "browser_exec", {"code": running_code}, task_id=shared_browser)
        assert wait_until(ready.exists, timeout=10)
        queued = pool.submit(registry.dispatch, "browser_exec", {"code": late_code}, task_id=shared_browser)
        state = capture_task(shared_browser)
        cleanup_browser(shared_browser)
        assert state.ended
        running.result(timeout=10)
        rejected = json.loads(queued.result(timeout=10))
    assert rejected.get("success") is not True
    assert not late.exists()
    assert SUPERVISOR_REGISTRY.get(shared_browser) is None
    removed = json.loads(browser_cdp("Target.getTargetInfo", {"targetId": first["target_id"]}))
    assert removed.get("error"), removed


def test_named_session_queue_wait_obeys_the_call_deadline(shared_browser, tmp_path):
    ready = tmp_path / "running"
    forbidden = tmp_path / "queued-code-ran"
    name = shared_browser
    code = f"import pathlib, threading\npathlib.Path({json.dumps(str(ready))}).touch()\nthreading.Event().wait(30)"
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(registry.dispatch, "browser_exec", {"code": code, "session": name}, task_id=name + "-a")
        assert wait_until(ready.exists, timeout=10)
        started = time.monotonic()
        result = json.loads(registry.dispatch(
            "browser_exec", {"code": f"open({json.dumps(str(forbidden))}, 'w').close()",
                             "session": name, "timeout_s": 5}, task_id=name + "-b",
        ))
        elapsed = time.monotonic() - started
        cleanup_browser(name + "-a")
        running.result(timeout=10)
    assert "timed out" in result["error"]
    assert elapsed < 10
    assert not forbidden.exists()


def test_replaced_native_daemon_is_refused_before_user_code(shared_browser, tmp_path):
    from tools.browser_harness_dirs import daemon_env
    from tools.browser_use_sessions import capture_task

    _exec(shared_browser, "print(current_tab())")
    binding = capture_task(shared_browser).page
    env = daemon_env(browser_use_cli._base_subprocess_env(), binding.daemon)
    env["BU_CDP_WS" if binding.cdp_url.startswith("ws") else "BU_CDP_URL"] = binding.cdp_url
    stop = "from browser_harness import admin; admin.restart_daemon(require_clean=True)"
    subprocess.run([sys.executable, "-c", stop], env=env, check=True, timeout=25)
    subprocess.run([sys.executable, "-c", "from browser_harness import admin; admin.ensure_daemon()"],
                   env=env, check=True, timeout=25)
    marker = tmp_path / "replacement-used"
    try:
        result = json.loads(registry.dispatch(
            "browser_exec", {"code": f"open({json.dumps(str(marker))}, 'w').close()"}, task_id=shared_browser,
        ))
        assert result.get("success") is not True
        assert "replaced" in result["error"]
        assert not marker.exists()
    finally:
        subprocess.run([sys.executable, "-c", stop], env=env, check=True, timeout=25)


def test_profiles_with_the_same_task_id_keep_independent_supervisors(shared_browser, tmp_path):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    profiles = [tmp_path / "profile-a", tmp_path / "profile-b"]
    results, captured = [], []
    for home in profiles:
        home.mkdir(mode=0o700)
        token = set_hermes_home_override(str(home))
        try:
            save_config({"browser": {"cdp_url": "http://127.0.0.1:9222", "allow_private_urls": True}})
            result = _exec(shared_browser, "print(current_tab())")
            assert SUPERVISOR_REGISTRY.get(shared_browser).target_id == result["target_id"]
            results.append(result)
            captured.append(SUPERVISOR_REGISTRY.capture(shared_browser))
        finally:
            reset_hermes_home_override(token)
    assert results[0]["target_id"] != results[1]["target_id"]
    assert all(connection.is_valid() for connection in captured)
    token = set_hermes_home_override(str(profiles[0]))
    try:
        cleanup_browser(shared_browser)
    finally:
        reset_hermes_home_override(token)
    assert not captured[0].is_valid()
    assert captured[1].is_valid()


def test_automatic_names_cannot_be_borrowed_as_explicit_workflows(shared_browser):
    automatic = _exec(shared_browser, "print(current_tab())")
    rejected = json.loads(registry.dispatch(
        "browser_exec", {"code": "raise RuntimeError('must not execute')", "session": automatic["session"]},
        task_id=shared_browser + "-other",
    ))
    assert "reserved" in rejected["error"]


def test_concurrent_native_screenshots_keep_distinct_paths_and_pixels(shared_browser):
    from PIL import Image

    def capture(label):
        color = "red" if label == "a" else "blue"
        expression = json.dumps(f"document.documentElement.style.backgroundColor = '{color}'")
        raw = registry.dispatch("browser_exec", {
            "code": f"js({expression})\nprint(capture_screenshot())",
        }, task_id=shared_browser + label)
        result = json.loads(raw) if isinstance(raw, str) else raw["meta"]
        with Image.open(result["screenshot_path"]) as image:
            pixel = image.convert("RGB").getpixel((image.width // 2, image.height // 2))
        return result["screenshot_path"], pixel

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        red, blue = list(pool.map(capture, ("a", "b")))
    assert red[0] != blue[0]
    assert red[1][0] > 200 and red[1][2] < 50
    assert blue[1][2] > 200 and blue[1][0] < 50


def test_first_running_call_registers_the_actual_daemon_owner(shared_browser, tmp_path):
    from tools.browser_use_sessions import capture_task

    ready = tmp_path / "running"
    code = f"import pathlib, threading\npathlib.Path({json.dumps(str(ready))}).touch()\nthreading.Event().wait(30)"
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(registry.dispatch, "browser_exec", {"code": code}, task_id=shared_browser)
        assert wait_until(ready.exists, timeout=10)
        binding = capture_task(shared_browser).page
        assert wait_until(lambda: browser_use_cli._owned_daemon_groups.get(binding.daemon) == binding.fingerprint[0],
                          timeout=5)
        assert not running.done()
        cleanup_browser(shared_browser)
        running.result(timeout=10)


def test_cleanup_before_first_daemon_start_blocks_the_queued_operation(shared_browser, tmp_path):
    from tools.browser_harness_dirs import daemon_identity, fit_default_socket_dir
    from tools.browser_use_sessions import capture_task, daemon_operation, task_session_name

    state = capture_task(shared_browser)
    env = browser_use_cli._base_subprocess_env()
    env["BU_NAME"] = task_session_name(shared_browser)
    fit_default_socket_dir(env)
    identity = daemon_identity(env)
    marker = tmp_path / "late-start"
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        with daemon_operation(identity):
            queued = pool.submit(registry.dispatch, "browser_exec", {
                "code": f"open({json.dumps(str(marker))}, 'w').close()",
            }, task_id=shared_browser)
            assert wait_until(lambda: identity in state.daemons, timeout=5)
            cleaning = pool.submit(cleanup_browser, shared_browser)
            assert wait_until(lambda: state.ended, timeout=5)
        cleaning.result(timeout=20)
        result = json.loads(queued.result(timeout=10))
    assert result.get("success") is not True
    assert not marker.exists()
    assert SUPERVISOR_REGISTRY.get(shared_browser) is None


def test_new_turn_admission_can_reuse_an_ended_task_identity(shared_browser):
    from tools.browser_use_sessions import browser_task_scope

    with browser_task_scope(shared_browser):
        first = _exec(shared_browser, "print(current_tab())")
        cleanup_browser(shared_browser)
        late = json.loads(registry.dispatch("browser_exec", {"code": "print(current_tab())"}, task_id=shared_browser))
        assert late.get("success") is not True
    with browser_task_scope(shared_browser):
        next_turn = _exec(shared_browser, "print(current_tab())")
    assert first["target_id"] != next_turn["target_id"]


@pytest.mark.parametrize(("code", "session", "message"), [
    ("", "", "No code"),
    ("print('must not execute')", "bad name!", "Invalid session"),
])
def test_invalid_exec_input_is_rejected_without_starting_a_browser_call(shared_browser, code, session, message):
    result = json.loads(registry.dispatch("browser_exec", {"code": code, "session": session}, task_id=shared_browser))
    assert message in result["error"]
    assert SUPERVISOR_REGISTRY.get(shared_browser) is None


def test_profile_endpoint_change_closes_old_drivers_and_uses_a_new_task_page(shared_browser):
    from hermes_constants import hermes_home_key
    from tools.browser_session_cdp import resolve_cdp_override, set_live_cdp_override
    from tools.browser_session_lifecycle import cleanup_profile_browsers

    first = _exec(shared_browser + "-a", "print(current_tab())")
    try:
        cleanup_profile_browsers(hermes_home_key())
        set_live_cdp_override(resolve_cdp_override("http://127.0.0.1:9222"))
        next_task = _exec(shared_browser + "-b", "print(current_tab())")
        assert first["target_id"] != next_task["target_id"]
        removed = json.loads(browser_cdp("Target.getTargetInfo", {"targetId": first["target_id"]}))
        assert removed.get("error"), removed
    finally:
        set_live_cdp_override(None)


def test_timeout_kills_real_descendants_that_hold_capture_pipes(shared_browser, tmp_path):
    child_pid = tmp_path / "descendant.pid"
    descendant = "import threading; threading.Event().wait(30)"
    code = (
        "import pathlib, subprocess, sys, threading\n"
        f"child = subprocess.Popen([sys.executable, '-c', {json.dumps(descendant)}])\n"
        f"pathlib.Path({json.dumps(str(child_pid))}).write_text(str(child.pid))\n"
        "threading.Event().wait(30)"
    )
    result = json.loads(registry.dispatch("browser_exec", {"code": code, "timeout_s": 5}, task_id=shared_browser))
    assert "timed out" in result["error"]
    pid = int(child_pid.read_text())
    assert wait_until(lambda: not any(process.pid == pid and process.info["status"] != psutil.STATUS_ZOMBIE
                                     for process in psutil.process_iter(["status"])), timeout=5)


def test_running_native_call_survives_the_browser_inactivity_threshold(shared_browser):
    expression = json.dumps("document.title = 'long native call'")
    first = _exec(shared_browser, f"js({expression})")
    second = _exec(
        shared_browser,
        "import threading\nthreading.Event().wait(125)\nprint(js('document.title'))",
        timeout_s=150,
    )
    assert second["target_id"] == first["target_id"]
    assert "long native call" in second["output"]
    assert SUPERVISOR_REGISTRY.get(shared_browser).target_id == first["target_id"]
