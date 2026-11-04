"""CLI-9 (#134889): a live ``/browser connect`` belongs to ONE profile on a multiplexed gateway.

The override map is keyed by ``hermes_home_key()``; browser sessions and CDP supervisors remember
the profile that created them, so reaping one profile's browsers never closes another's.
"""
import pytest

from gateway.run import _profile_runtime_scope
from hermes_constants import hermes_home_key
from tools import browser_session as bs, browser_session_cdp as bs_cdp, browser_session_lifecycle as lifecycle
from tools.browser_supervisor import SUPERVISOR_REGISTRY


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (b / "config.yaml").write_text("browser:\n  cdp_url: ws://b.invalid/devtools/browser/PROFILE-B\n")
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    monkeypatch.setattr(bs_cdp, "_live_overrides", {})
    return a, b


def test_a_live_override_is_this_profiles_alone(profiles, monkeypatch):
    a, b = profiles
    with _profile_runtime_scope(a, hydrate_secrets=False):
        bs_cdp.set_live_cdp_override("http://127.0.0.1:9333")
        assert bs_cdp.get_cdp_override_raw() == "http://127.0.0.1:9333"
    with _profile_runtime_scope(b, hydrate_secrets=False):
        assert bs_cdp.live_cdp_override() == ""
        assert bs_cdp.get_cdp_override_raw().endswith("/PROFILE-B")
    with _profile_runtime_scope(a, hydrate_secrets=False):
        bs_cdp.set_live_cdp_override(None)
        assert bs_cdp.get_cdp_override_raw() == ""


def test_disconnect_falls_back_to_this_profiles_own_env_never_the_launch_env(profiles, monkeypatch):
    """After a disconnect the BROWSER_CDP_URL tier is the profile's own ``.env``; the launch process's
    ``os.environ`` value belongs to the launch profile and must not reach a served one."""
    from tui_gateway.server import _resolve_browser_cdp_url  # bound onto server.py globals

    a, b = profiles
    (b / ".env").write_text("BROWSER_CDP_URL=http://127.0.0.1:7777\n")
    monkeypatch.setenv("BROWSER_CDP_URL", "http://127.0.0.1:6666")  # the launch profile's
    with _profile_runtime_scope(b, hydrate_secrets=False):
        bs_cdp.set_live_cdp_override("http://127.0.0.1:4444")
        assert bs_cdp.get_cdp_override_raw() == "http://127.0.0.1:4444"
        bs_cdp.set_live_cdp_override(None)
        assert bs_cdp.get_cdp_override_raw() == _resolve_browser_cdp_url() == "http://127.0.0.1:7777"
    with _profile_runtime_scope(a, hydrate_secrets=False):  # no BROWSER_CDP_URL in A's .env
        assert bs_cdp.get_cdp_override_raw() == _resolve_browser_cdp_url() == ""
    assert bs_cdp.get_cdp_override_raw() == "http://127.0.0.1:6666"  # single-profile: env as before


def test_a_new_session_records_its_profile(profiles, monkeypatch):
    a, _b = profiles
    monkeypatch.setattr(bs, "_active_sessions", {})
    monkeypatch.setattr(lifecycle, "start_cleanup_thread", lambda: None)
    monkeypatch.setattr(bs, "_create_session_for_key", lambda key, force_local: {"session_name": "s"})
    monkeypatch.setattr(bs_cdp, "ensure_cdp_supervisor", lambda key: None)
    with _profile_runtime_scope(a, hydrate_secrets=False):
        info = bs.get_session_info("task-a")
    assert info["home_key"] == hermes_home_key(a)
