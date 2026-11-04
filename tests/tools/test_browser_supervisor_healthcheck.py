"""Supervisor health contracts exercised against the operator's real shared CDP browser."""

import asyncio

import pytest

from tools.browser_supervisor import SUPERVISOR_REGISTRY
from tests.waiting import wait_until
from tests.tools.test_browser_native_task_sessions import (
    _cdp,
    _exec,
    real_browser_harness,
    real_browser_launcher,
    real_desktop_configuration,
    real_harness_runtime_directories,
    shared_browser,
)

__all__ = ["real_browser_harness", "real_browser_launcher", "real_desktop_configuration",
           "real_harness_runtime_directories", "shared_browser"]

pytestmark = pytest.mark.integration


def test_healthy_supervisor_is_reused_for_the_same_native_page(shared_browser):
    result = _exec(shared_browser, "print(current_tab())")
    supervisor = SUPERVISOR_REGISTRY.get(shared_browser)
    reused = SUPERVISOR_REGISTRY.get_or_start(
        task_id=shared_browser, cdp_url=supervisor.cdp_url, target_id=result["target_id"],
    )
    assert reused is supervisor


def test_stopped_supervisor_is_recreated_on_the_exact_page(shared_browser):
    result = _exec(shared_browser, "print(current_tab())")
    previous = SUPERVISOR_REGISTRY.get(shared_browser)
    previous.stop()
    fresh = SUPERVISOR_REGISTRY.get_or_start(
        task_id=shared_browser, cdp_url=previous.cdp_url, target_id=result["target_id"],
    )
    assert fresh is not previous
    assert fresh.evaluate_runtime("location.href")["result"] == "about:blank"


def test_missing_page_fails_attachment_without_publishing_a_supervisor(shared_browser):
    result = _exec(shared_browser, "print(current_tab())")
    previous = SUPERVISOR_REGISTRY.get(shared_browser)
    endpoint = previous.cdp_url
    SUPERVISOR_REGISTRY.stop(shared_browser)
    _cdp("Target.closeTarget", {"targetId": result["target_id"]})
    with pytest.raises(RuntimeError, match="target is unavailable"):
        SUPERVISOR_REGISTRY.get_or_start(
            task_id=shared_browser, cdp_url=endpoint, target_id=result["target_id"],
        )
    assert SUPERVISOR_REGISTRY.get(shared_browser) is None


def test_focus_page_keeps_the_exact_target_among_same_origin_pages(shared_browser):
    address = "http://127.0.0.1:9222/json/version"
    first = _exec(shared_browser + "-a", f"goto_url({address!r})\njs(\"document.title = 'owned-page-a'\")")
    second = _exec(shared_browser + "-b", f"goto_url({address!r})\njs(\"document.title = 'owned-page-b'\")")
    supervisor = SUPERVISOR_REGISTRY.get(shared_browser + "-a")
    found = supervisor.focus_page("http://127.0.0.1:9222", accept="document.title === 'owned-page-b'")
    assert found["ok"] is False
    assert supervisor.evaluate_runtime("document.title")["result"] == "owned-page-a"
    _cdp("Target.closeTarget", {"targetId": first["target_id"]})
    found = supervisor.focus_page("http://127.0.0.1:9222")
    assert found["ok"] is False
    assert SUPERVISOR_REGISTRY.get(shared_browser + "-b").target_id == second["target_id"]


def test_background_reconnect_failure_removes_only_its_own_profile(shared_browser, tmp_path):
    from hermes_cli.config import save_config
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    _exec(shared_browser, "print(current_tab())")
    launch_supervisor = SUPERVISOR_REGISTRY.get(shared_browser)
    home = tmp_path / "secondary"
    home.mkdir(mode=0o700)
    token = set_hermes_home_override(str(home))
    try:
        save_config({"browser": {"cdp_url": "http://127.0.0.1:9222", "allow_private_urls": True}})
        result = _exec(shared_browser, "print(current_tab())")
        secondary = SUPERVISOR_REGISTRY.get(shared_browser)
        _cdp("Target.closeTarget", {"targetId": result["target_id"]})
        asyncio.run_coroutine_threadsafe(secondary._close_ws(), secondary._loop).result(timeout=5)
        assert wait_until(lambda: SUPERVISOR_REGISTRY.get(shared_browser) is None, timeout=20)
        assert wait_until(lambda: not secondary._thread.is_alive(), timeout=5)
    finally:
        reset_hermes_home_override(token)
    assert SUPERVISOR_REGISTRY.get(shared_browser) is launch_supervisor
    assert launch_supervisor.evaluate_runtime("location.href")["result"] == "about:blank"
