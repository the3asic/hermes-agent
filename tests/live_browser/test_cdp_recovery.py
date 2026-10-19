"""Live Chrome invariants; run with HERMES_E2E_BROWSER=1 and agent-browser >=0.34."""

import contextlib
import functools
import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
import threading
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psutil
import pytest
import requests
import yaml


pytestmark = pytest.mark.skipif(
    os.environ.get("HERMES_E2E_BROWSER") != "1",
    reason="requires explicit live headless Chrome opt-in",
)


class Chrome:
    def __init__(self, directory, port=None):
        from hermes_cli.browser_connect import chromium_executable

        executable = chromium_executable("chrome")
        assert executable, "Install real Chrome before opting into live browser checks"
        if port is None:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
        self.port = port
        self.root = f"http://127.0.0.1:{port}"
        self.http = requests.Session()
        self.http.trust_env = False
        directory.mkdir(parents=True)
        self.log = (directory / "chrome.log").open("wb")
        self.process = subprocess.Popen(
            [executable, "--headless=new", "--no-first-run", "--no-default-browser-check",
             "--disable-background-networking", "--remote-debugging-address=127.0.0.1",
             f"--remote-debugging-port={port}", f"--user-data-dir={directory / 'user-data'}",
             "about:blank"],
            stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log,
        )
        deadline = time.monotonic() + 20
        while True:
            assert self.process.poll() is None, f"Chrome exited; inspect {directory / 'chrome.log'}"
            try:
                response = self.http.get(self.root + "/json/version", timeout=1)
            except requests.ConnectionError:
                if time.monotonic() >= deadline:
                    self.stop()
                    raise
                time.sleep(0.1)
                continue
            response.raise_for_status()
            self.websocket = response.json()["webSocketDebuggerUrl"]
            break

    def targets(self):
        response = self.http.get(self.root + "/json/list", timeout=5)
        response.raise_for_status()
        return {row["id"] for row in response.json() if row["type"] == "page"}

    def stop(self):
        if self.process.poll() is None:
            parent = psutil.Process(self.process.pid)
            children = parent.children(recursive=True)
            parent.terminate()
            parent.wait(timeout=10)
            for child in children:
                if child.is_running():
                    child.terminate()
            _, alive = psutil.wait_procs(children, timeout=10)
            for child in alive:
                child.kill()
            _, alive = psutil.wait_procs(alive, timeout=5)
            assert not alive, "A test-owned Chrome process did not terminate"
        self.http.close()
        self.log.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.stop()


@contextlib.contextmanager
def profile_scope(directory, browser):
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    directory.mkdir(parents=True, exist_ok=True)
    config = directory / "config.yaml"
    if not config.exists():
        config.write_text(yaml.safe_dump({"browser": {"cdp_url": browser.root, "allow_private_urls": True}}), encoding="utf-8")
    home_token = set_hermes_home_override(directory)
    secret_token = set_secret_scope(build_profile_secret_scope(directory), profile_home=str(directory))
    try:
        yield
    finally:
        reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)


@pytest.fixture
def owned_sessions(tmp_path, record_testsuite_property):
    previous_home = os.environ.get("HERMES_HOME")
    launch_home = tmp_path / "launch-profile"
    launch_home.mkdir()
    os.environ["HERMES_HOME"] = str(launch_home)
    from tools import browser_tool as browser
    from tools import browser_tool_cdp as cdp
    from tools import browser_tool_session as session
    from tools.browser_tool_lifecycle import _release_session_resources, _session_owner_scope

    before = set(browser._active_sessions)
    repository = Path(session.__file__).resolve().parents[1]
    record_testsuite_property("browser_source_sha256", hashlib.sha256(Path(session.__file__).read_bytes()).hexdigest())
    public = tmp_path / "public"
    public.mkdir()
    shutil.copyfile(repository / "README.md", public / "README.txt")
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(SimpleHTTPRequestHandler, directory=str(public)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/README.txt"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
        # Chrome context managers have already stopped only this test's browsers.
        # Release test-owned helpers even when a behavioral assertion failed.
        for task in set(browser._active_sessions) - before:
            assert task.startswith("live-")
            with _session_owner_scope(task):
                cdp._stop_cdp_supervisor(task)
                assert _release_session_resources(task, browser._active_sessions[task])
        if previous_home is None:
            del os.environ["HERMES_HOME"]
        else:
            os.environ["HERMES_HOME"] = previous_home


@pytest.fixture
def multiplex(owned_sessions):
    from agent.secret_scope import is_multiplex_active, set_multiplex_active

    previous = is_multiplex_active()
    set_multiplex_active(True)
    try:
        yield owned_sessions
    finally:
        set_multiplex_active(previous)


def create_owned_page(task, page_url):
    from model_tools import handle_function_call
    from tools import browser_tool as browser

    result = json.loads(handle_function_call("browser_navigate", {"url": page_url}, task_id=task))
    assert result.get("success"), result
    info = browser._active_sessions[task]
    socket_dir = Path(browser._socket_safe_tmpdir()) / ("agent-browser-" + info["session_name"])
    binding = json.loads((socket_dir / (info["session_name"] + ".target")).read_text())
    assert binding["pinned"] is True
    return info, binding["targetId"], socket_dir


def test_endpoint_rotation_retires_missing_target_without_closing_other_profiles(tmp_path, multiplex):
    from tools.browser_tool_lifecycle import cleanup_browser
    from tools import browser_tool as browser

    task_a, task_b = "live-rotate-a-" + uuid.uuid4().hex, "live-rotate-b-" + uuid.uuid4().hex
    with contextlib.ExitStack() as resources:
        first = resources.enter_context(Chrome(tmp_path / "chrome-a-first"))
        other = resources.enter_context(Chrome(tmp_path / "chrome-b"))
        with profile_scope(tmp_path / "profile-a", first):
            _, target_a, _ = create_owned_page(task_a, multiplex)
        with profile_scope(tmp_path / "profile-b", other):
            _, target_b, _ = create_owned_page(task_b, multiplex)
        first.stop()
        replacement = resources.enter_context(Chrome(tmp_path / "chrome-a-replacement", first.port))
        assert replacement.websocket != first.websocket
        assert target_a not in replacement.targets()
        replacement_pages, other_pages = replacement.targets(), other.targets()
        assert target_b in other_pages
        with profile_scope(tmp_path / "profile-a", replacement):
            assert cleanup_browser(task_a), "A vanished pinned target must finish cleanup after endpoint rotation"
        assert task_a not in browser._active_sessions
        assert replacement.targets() == replacement_pages
        assert other.targets() == other_pages
        with profile_scope(tmp_path / "profile-b", other):
            assert cleanup_browser(task_b)


def test_daemon_loss_closes_only_owned_target_under_another_profile_scope(tmp_path, multiplex):
    from tools.browser_tool_lifecycle import _kill_verified_daemon, cleanup_browser

    task_a, task_b = "live-daemon-a-" + uuid.uuid4().hex, "live-daemon-b-" + uuid.uuid4().hex
    with Chrome(tmp_path / "chrome-a") as first, Chrome(tmp_path / "chrome-b") as other:
        original_pages = first.targets()
        with profile_scope(tmp_path / "profile-a", first):
            info, target_a, socket_dir = create_owned_page(task_a, multiplex)
        with profile_scope(tmp_path / "profile-b", other):
            _, target_b, _ = create_owned_page(task_b, multiplex)
        daemon_pid = int((socket_dir / (info["session_name"] + ".pid")).read_text())
        daemon = psutil.Process(daemon_pid)
        assert _kill_verified_daemon(str(socket_dir), info["session_name"])
        daemon.wait(timeout=5)
        assert not psutil.pid_exists(daemon_pid)
        other_pages = other.targets()
        assert target_b in other_pages
        with profile_scope(tmp_path / "profile-b", other):
            assert cleanup_browser(task_a)
            assert first.targets() == original_pages
            assert target_a not in first.targets()
            assert other.targets() == other_pages
            assert cleanup_browser(task_b)
