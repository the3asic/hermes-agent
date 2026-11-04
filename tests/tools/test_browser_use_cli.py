"""Tests for ``browser_exec`` (tools/browser_use_cli.py), Hermes' only core browser tool.

Covers the seams the integration relies on:

* Routing — every browser comes from ``tools.browser_session`` in a fixed precedence
  (BU_CDP_* env > CDP override > hybrid ``::local`` sidecar > cloud provider > local engine);
  Browser Use direct-API configs keep the CLI's native cloud autospawn.
* Tool surface — ``browser_exec`` needs the terminal surface.
* ``browser_exec`` execution — code is piped on stdin, ``session`` becomes
  ``BU_NAME``, bad session names and a missing CLI produce actionable errors.
"""
import json
import os
import socket
import stat
import subprocess
import sys
import time

import pytest
import psutil

import tools.browser_use_cli as bu_cli
from tools import browser_session as bs
from tools import browser_session_cloud as bs_cloud


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("BU_NAME", raising=False)
    monkeypatch.delenv("BU_AUTOSPAWN", raising=False)
    monkeypatch.delenv("BROWSER_USE_API_KEY", raising=False)
    bs_cloud.reset_caches()
    yield
    bs_cloud.reset_caches()


@pytest.fixture(autouse=True)
def _fake_managed_chromium(monkeypatch):
    """The local route asks the session layer for this key's local browser (packaged Chromium via the
    launcher); never launch a real browser from a unit test. Records every session key requested."""
    calls = []

    def fake_session_info(key):
        calls.append(key)
        return {"cdp_url": f"ws://127.0.0.1:47000/devtools/browser/{key}", "features": {"local": True}}

    monkeypatch.setattr(bs, "get_session_info", fake_session_info)
    return calls


@pytest.fixture(autouse=True)
def _fake_supervisor_registry(monkeypatch):
    """browser_exec attaches the vault supervisor to the resolved CDP endpoint; the fake endpoint above is
    not a real browser, so record the attach instead of opening a WebSocket (15 s start timeout)."""
    from tools import browser_supervisor

    attached = []

    class _Registry:
        def get_or_start(self, task_id, cdp_url, **kw):
            attached.append((task_id, cdp_url))

        def get(self, task_id):  # browser_exec reads pending dialogs from the task's supervisor
            return None

    monkeypatch.setattr(browser_supervisor, "SUPERVISOR_REGISTRY", _Registry())
    return attached


def _fake_cli(tmp_path, body):
    """Write an executable fake browser-use CLI and return its path."""
    script = tmp_path / "browser-use"
    script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)



class TestSubprocessEnvironment:
    def test_browser_use_telemetry_defaults_off(self, monkeypatch):
        monkeypatch.setattr(bs, "build_browser_env", dict)
        env = bu_cli._base_subprocess_env()
        assert env["ANONYMIZED_TELEMETRY"] == "false"

    def test_subprocess_env_replaces_parent_python_import_paths(self, monkeypatch):
        """#83427/#84841/#86006/#86104: an inherited PYTHONPATH/PYTHONHOME must never reach the
        harness child; PYTHONPATH is replaced by the harness's own site dir, unrelated vars survive."""
        monkeypatch.setattr(bs, "build_browser_env", lambda: {
            "PYTHONPATH": "/hermes:/hermes/venv/lib/site-packages",
            "PYTHONHOME": "/hermes/venv",
            "KEEP_ME": "yes",
        })
        monkeypatch.setattr(bu_cli, "_harness_site_dir", lambda: "/harness-site")

        env = bu_cli._base_subprocess_env()

        assert env["PYTHONPATH"] == "/harness-site"
        assert "PYTHONHOME" not in env
        assert env["KEEP_ME"] == "yes"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX PATH-floor semantics")
    def test_subprocess_env_floors_version_manager_only_path(self, monkeypatch):
        """Profile workers (kanban bots, cron) can inherit a PATH of only
        version-manager dirs (observed in the wild: one nvm dir repeated
        7x). The uv browser-use trampoline resolves dirname/realpath
        through PATH, so /usr/bin must be guaranteed or the CLI dies
        'realpath: not found' (exit 127) before its Python starts."""
        monkeypatch.setattr(bs, "build_browser_env", lambda: {
            "PATH": os.pathsep.join(
                ["/home/u/.nvm/versions/node/v24.18.0/bin"] * 7
            ),
        })

        env = bu_cli._base_subprocess_env()

        parts = env["PATH"].split(os.pathsep)
        assert "/usr/bin" in parts
        assert "/bin" in parts

    @pytest.mark.skipif(os.name == "nt", reason="POSIX PATH-floor semantics")
    def test_floor_preserves_existing_entries_and_order(self):
        """The floor only adds dirs — never drops or reorders what the
        caller's environment already had."""
        original = "/opt/toolchain/bin:/usr/bin:/snap/bin"
        merged = bu_cli._floor_subprocess_path(original).split(os.pathsep)

        assert set(original.split(os.pathsep)) <= set(merged)
        positions = [merged.index(p) for p in original.split(os.pathsep)]
        assert positions == sorted(positions)



class TestToolSurfaceSwap:
    def test_browser_exec_stripped_without_terminal(self, monkeypatch):
        """Sessions without the terminal surface must not regain host code
        execution through browser_exec (arbitrary Python via the CLI)."""
        from tools.registry import registry

        entry = registry.get_entry("browser_exec")
        monkeypatch.setattr(entry, "check_fn", lambda: True)
        import model_tools

        defs = model_tools.get_tool_definitions(
            enabled_toolsets=["browser"], quiet_mode=False
        )
        names = {t["function"]["name"] for t in defs}
        assert "browser_exec" not in names

    def test_browser_exec_present_with_terminal(self, monkeypatch):
        from tools.registry import registry

        entry = registry.get_entry("browser_exec")
        monkeypatch.setattr(entry, "check_fn", lambda: True)
        import model_tools

        defs = model_tools.get_tool_definitions(
            enabled_toolsets=["browser", "terminal"], quiet_mode=False
        )
        names = {t["function"]["name"] for t in defs}
        assert "browser_exec" in names









class TestLegacyCloudMigration:
    """Pre-CLI direct-API Browser Use cloud configs (cloud_provider:
    "browser-use" + BROWSER_USE_API_KEY) run on the CLI's native Browser Use
    cloud (BU_AUTOSPAWN); Nous-gateway users stay on the provider path."""

    _LEGACY = {"browser": {"cloud_provider": "browser-use"}}

    def test_direct_api_config_migrates(self, monkeypatch):
        monkeypatch.setenv("BROWSER_USE_API_KEY", "bu-key")
        assert bu_cli.is_legacy_browser_use_cloud_config(self._LEGACY["browser"]) is True

    def test_gateway_config_stays_on_legacy_path(self, monkeypatch):
        monkeypatch.setenv("BROWSER_USE_API_KEY", "bu-key")
        assert bu_cli.is_legacy_browser_use_cloud_config(
            {"cloud_provider": "browser-use", "use_gateway": True}) is False

    def test_no_api_key_stays_on_legacy_path(self, monkeypatch):
        assert bu_cli.is_legacy_browser_use_cloud_config(self._LEGACY["browser"]) is False

    def test_other_cloud_provider_does_not_migrate(self, monkeypatch):
        monkeypatch.setenv("BROWSER_USE_API_KEY", "bu-key")
        assert bu_cli.is_legacy_browser_use_cloud_config({"cloud_provider": "browserbase"}) is False


    def test_auto_detect_with_key_migrates(self, monkeypatch):
        """No cloud_provider configured + BROWSER_USE_API_KEY set: credential
        auto-detection prefers Browser Use (even when Browserbase creds are
        also present), which means the CLI's native Browser Use cloud."""
        monkeypatch.setenv("BROWSER_USE_API_KEY", "bu-key")
        monkeypatch.setenv("BROWSERBASE_API_KEY", "bb-key")
        monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "bb-project")
        assert bu_cli.is_legacy_browser_use_cloud_config({}) is True





class TestBackendCdpResolution:
    """browser_exec routes through the configured browser backend via the session layer
    (browser_session.get_session_info)."""

    def _env(self):
        return {}

    def test_existing_bu_env_wins(self, monkeypatch):
        env = {"BU_CDP_WS": "ws://operator-override:9222"}
        assert bu_cli._resolve_backend_cdp(env, "t1") is None
        assert env["BU_CDP_WS"] == "ws://operator-override:9222"

    def test_cdp_override_exported(self, monkeypatch):

        monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override", lambda: "http://127.0.0.1:9222")
        env = self._env()
        assert bu_cli._resolve_backend_cdp(env, "t1") is None
        assert env["BU_CDP_URL"] == "http://127.0.0.1:9222"

    def test_ws_override_uses_bu_cdp_ws(self, monkeypatch):

        monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override", lambda: "wss://connect.example/x")
        env = self._env()
        assert bu_cli._resolve_backend_cdp(env, "t1") is None
        assert env["BU_CDP_WS"] == "wss://connect.example/x"

    def test_cloud_provider_session_exported(self, monkeypatch):

        monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override", lambda: "")
        monkeypatch.setattr(bs_cloud, "get_cloud_provider", lambda: object())
        monkeypatch.setattr(
            bs, "get_session_info",
            lambda task_id: {"cdp_url": "wss://browser.example/cdp/abc"},
        )
        env = self._env()
        assert bu_cli._resolve_backend_cdp(env, "t1") is None
        assert env["BU_CDP_WS"] == "wss://browser.example/cdp/abc"

    def test_no_provider_drives_packaged_chromium_not_user_chrome(self, monkeypatch, _fake_managed_chromium):
        """Local mode must hand the harness the launcher's packaged Chromium (through the session layer)
        — never leave BU_CDP_* unset, which makes the harness hunt for the user's installed
        Chrome (Allow popup, >=136 default-profile block, chrome-not-running on headless hosts)."""
        monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override", lambda: "")
        monkeypatch.setattr(bs_cloud, "get_cloud_provider", lambda: None)
        env = self._env()
        assert bu_cli._resolve_backend_cdp(env, "t1") is None
        assert env["BU_CDP_WS"] == "ws://127.0.0.1:47000/devtools/browser/t1"
        assert env[bu_cli._PRIVATE_BROWSER_SENTINEL] == "1"
        assert _fake_managed_chromium == ["t1"]
        env = self._env()
        assert bu_cli._resolve_backend_cdp(env, "t1", session_name="r7k2") is None
        assert _fake_managed_chromium[-1] == "bu-named-r7k2"  # named session → its own Chromium

    def test_packaged_chromium_launch_failure_is_an_error(self, monkeypatch):
        monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override", lambda: "")
        monkeypatch.setattr(bs_cloud, "get_cloud_provider", lambda: None)
        def no_chromium(key):
            raise RuntimeError("Chromium browser not installed")
        monkeypatch.setattr(bs, "get_session_info", no_chromium)
        env = self._env()
        err = bu_cli._resolve_backend_cdp(env, "t1")
        assert err and "Chromium browser not installed" in err
        assert "BU_CDP_WS" not in env and "BU_CDP_URL" not in env

    def test_provider_failure_returns_error(self, monkeypatch):

        def boom(task_id):
            raise RuntimeError("api down")

        monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override", lambda: "")
        monkeypatch.setattr(bs_cloud, "get_cloud_provider", lambda: object())
        monkeypatch.setattr(bs, "get_session_info", boom)
        err = bu_cli._resolve_backend_cdp(self._env(), "t1")
        assert err and "api down" in err

    def test_provider_fallback_to_local_drives_packaged_chromium(self, monkeypatch, _fake_managed_chromium):
        """A cloud failure the session layer already fell back from (429, quota) must drive the local
        Chromium it chose, not fail the call as 'no CDP endpoint'."""
        monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override", lambda: "")
        monkeypatch.setattr(bs_cloud, "get_cloud_provider", lambda: object())
        def fell_back(key):
            _fake_managed_chromium.append(key)
            return {"cdp_url": f"ws://127.0.0.1:47000/devtools/browser/{key}", "features": {"local": True},
                    "fallback_from_cloud": True, "fallback_reason": "429"}
        monkeypatch.setattr(bs, "get_session_info", fell_back)
        env = self._env()
        assert bu_cli._resolve_backend_cdp(env, "t1") is None
        assert env["BU_CDP_WS"] == "ws://127.0.0.1:47000/devtools/browser/t1"
        assert _fake_managed_chromium == ["t1"]

    def test_provider_without_cdp_returns_error(self, monkeypatch):

        monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override", lambda: "")
        monkeypatch.setattr(bs_cloud, "get_cloud_provider", lambda: object())
        monkeypatch.setattr(bs, "get_session_info", lambda task_id: {"cdp_url": None})
        err = bu_cli._resolve_backend_cdp(self._env(), "t1")
        assert err and "no" in err.lower() and "CDP" in err


    def test_named_session_key_stable_across_tasks(self, monkeypatch):
        """The same session name maps to the same provider cache key no
        matter which task calls it — that is what lets a follow-up call
        reattach to the same cloud browser."""

        seen = []
        monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override", lambda: "")
        monkeypatch.setattr(bs_cloud, "get_cloud_provider", lambda: object())
        monkeypatch.setattr(
            bs, "get_session_info",
            lambda key: seen.append(key) or {"cdp_url": "wss://x/cdp/a"},
        )
        env1, env2 = {}, {}
        assert bu_cli._resolve_backend_cdp(env1, "task-A", session_name="research") is None
        assert bu_cli._resolve_backend_cdp(env2, "task-B", session_name="research") is None
        assert seen == ["bu-named-research", "bu-named-research"]

    def test_named_session_direct_api_bu_cloud_still_skips_provider(
        self, tmp_path, monkeypatch
    ):
        """Direct-API Browser Use cloud configs keep the native named-daemon
        path: resolving through the provider would double-session and
        double-bill."""

        class _BUProvider:
            name = "browser-use"

        monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override", lambda: "")
        monkeypatch.setattr(bs_cloud, "get_cloud_provider", lambda: _BUProvider())
        monkeypatch.setattr(
            bs, "get_session_info",
            lambda key: (_ for _ in ()).throw(AssertionError("must skip provider")),
        )
        monkeypatch.setattr(bu_cli, "_read_browser_cfg", lambda: {"cloud_provider": "browser-use"})
        monkeypatch.setenv("BROWSER_USE_API_KEY", "bu-key")
        env = {}
        assert bu_cli._resolve_backend_cdp(env, "t1", session_name="r7k2") is None
        assert "BU_CDP_WS" not in env and "BU_CDP_URL" not in env

    def test_direct_api_without_key_uses_session_path_not_chrome_hunt(self, monkeypatch, _fake_managed_chromium):
        """cloud_provider browser-use with no API key must not take the autospawn shortcut: the CLI
        only autospawns with the key, so None left the harness hunting an installed Chrome
        (DevToolsActivePort). The session path's local-Chromium fallback drives the packaged one."""

        class _BUProvider:
            name = "browser-use"

        monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override", lambda: "")
        monkeypatch.setattr(bs_cloud, "get_cloud_provider", lambda: _BUProvider())
        def fell_back(key):
            _fake_managed_chromium.append(key)
            return {"cdp_url": f"ws://127.0.0.1:47000/devtools/browser/{key}", "features": {"local": True},
                    "fallback_from_cloud": True, "fallback_reason": "BROWSER_USE_API_KEY is not set"}
        monkeypatch.setattr(bs, "get_session_info", fell_back)
        monkeypatch.setattr(bu_cli, "_read_browser_cfg", lambda: {"cloud_provider": "browser-use"})
        env = self._env()
        assert bu_cli._resolve_backend_cdp(env, "t1") is None
        assert env["BU_CDP_WS"] == "ws://127.0.0.1:47000/devtools/browser/t1"
        assert _fake_managed_chromium == ["t1"]

    def test_picker_managed_selection_resolves_gateway_provider(self, monkeypatch):
        """``cloud_provider: nous`` (the `hermes tools` managed row) must resolve through the
        provider: the picker never writes the legacy ``use_gateway`` flag, and the direct-API
        branch leaves browser_exec with no CDP endpoint at all (#108310)."""
        class _BUProvider:
            name = "browser-use"

        monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override", lambda: "")
        monkeypatch.setattr(bs_cloud, "get_cloud_provider", lambda: _BUProvider())
        monkeypatch.setattr(
            bs, "get_session_info",
            lambda task_id: {"cdp_url": "wss://gateway.example/cdp/managed"},
        )
        monkeypatch.setattr(bu_cli, "_read_browser_cfg", lambda: {"cloud_provider": "nous"})
        env = {}
        assert bu_cli._resolve_backend_cdp(env, "t1") is None
        assert env["BU_CDP_WS"] == "wss://gateway.example/cdp/managed"

    def test_never_configured_autodetect_managed_provider_resolves_through_provider(self, monkeypatch):
        """A never-configured install (no ``browser.cloud_provider``, no BROWSER_USE_API_KEY) can
        still get a ``browser-use`` provider back from autodetect — via the managed Nous gateway
        token, which needs no API key. That provider DOES have a session/CDP path, so it must
        resolve through the provider instead of the direct-API shortcut: the shortcut leaves
        BU_CDP_* unset and the harness falls back to hunting the user's profile dirs
        (chrome-not-running on a headless host with no ~/.config/chromium)."""
        class _BUProvider:
            name = "browser-use"

        monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override", lambda: "")
        monkeypatch.setattr(bs_cloud, "get_cloud_provider", lambda: _BUProvider())
        monkeypatch.setattr(
            bs, "get_session_info",
            lambda key: {"cdp_url": "wss://gateway.example/cdp/autodetect"},
        )
        # Never configured: no cloud_provider selection at all.
        monkeypatch.setattr(bu_cli, "_read_browser_cfg", dict)
        env = {}
        assert bu_cli._resolve_backend_cdp(env, "t1") is None
        assert env["BU_CDP_WS"] == "wss://gateway.example/cdp/autodetect"

    def test_legacy_use_gateway_flag_still_resolves_gateway_provider(self, monkeypatch):
        """Regression guard for the pre-picker shape of the same selection."""
        class _BUProvider:
            name = "browser-use"

        monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override", lambda: "")
        monkeypatch.setattr(bs_cloud, "get_cloud_provider", lambda: _BUProvider())
        monkeypatch.setattr(
            bs, "get_session_info",
            lambda task_id: {"cdp_url": "wss://gateway.example/cdp/legacy"},
        )
        monkeypatch.setattr(bu_cli, "_read_browser_cfg", lambda: {"use_gateway": True})
        env = {}
        assert bu_cli._resolve_backend_cdp(env, "t1") is None
        assert env["BU_CDP_WS"] == "wss://gateway.example/cdp/legacy"




class TestProviderPickerIntegration:
    """The `hermes tools` Browser Automation picker rows."""

    def test_picker_row_names_stay_unique(self):
        """The CLI row is named "Browser Use"; the legacy plugin API row must
        keep a distinct name — apply_provider_selection matches by name."""
        from hermes_cli.tools_config import TOOL_CATEGORIES, _plugin_browser_providers

        names = [r["name"] for r in TOOL_CATEGORIES["browser"]["providers"]]
        names += [r["name"] for r in _plugin_browser_providers()]
        assert len(names) == len(set(names))


class TestBrowserSlashDispatch:
    """/browser routes through the _BROWSER_SUBCOMMANDS table: the subcommand word is
    case-insensitive, the argument keeps its case (CDP URL paths are case-sensitive),
    and an unknown word prints the usage block without touching any handler."""

    def _run(self, cmd, monkeypatch):
        import contextlib
        import io

        import hermes_cli.cli_commands_mixin as mod

        calls = []
        monkeypatch.setattr(mod, "_browser_connect", lambda cli, url: calls.append(("connect", url)))
        monkeypatch.setattr(mod, "_browser_disconnect", lambda cli: calls.append(("disconnect",)))
        monkeypatch.setattr(mod, "_browser_status", lambda: calls.append(("status",)))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mod.CLICommandsMixin._handle_browser_command(object(), cmd)
        return calls, buf.getvalue()

    def test_connect_keeps_url_case_and_defaults_to_status(self, monkeypatch):
        from hermes_cli.browser_connect import DEFAULT_BROWSER_CDP_URL

        assert self._run("/browser CONNECT ws://127.0.0.1:9222/devtools/browser/AbC", monkeypatch)[0] == [
            ("connect", "ws://127.0.0.1:9222/devtools/browser/AbC")]
        assert self._run("/browser connect", monkeypatch)[0] == [("connect", DEFAULT_BROWSER_CDP_URL)]
        assert self._run("/browser", monkeypatch)[0] == [("status",)]

    def test_unknown_subcommand_prints_usage_only(self, monkeypatch):
        calls, out = self._run("/browser frobnicate", monkeypatch)
        assert calls == []
        assert out.strip()


class TestNativeScreenshots:
    """Screenshots printed by capture_screenshot() attach directly to the
    model's context when it has native vision — no aux vision-LLM detour."""

    def _shot(self, tmp_path):
        shot = tmp_path / "shot.png"
        shot.write_bytes(b"\x89PNG fake")
        return str(shot)

    def test_find_screenshot_returns_last_fresh_path(self, tmp_path):
        a, b = self._shot(tmp_path), str(tmp_path / "b.png")
        (tmp_path / "b.png").write_bytes(b"\x89PNG fake2")
        out = f"step one saved {a}\nthen saved {b}\n"
        assert bu_cli._find_screenshot(out, since=time.time() - 5) == b

    def test_find_screenshot_rejects_stale_and_missing(self, tmp_path):
        stale = self._shot(tmp_path)
        os.utime(stale, (time.time() - 900, time.time() - 900))
        out = f"{stale}\n/nonexistent/dir/x.png\n"
        assert bu_cli._find_screenshot(out, since=time.time()) is None





class TestStepLabels:
    """browser_exec code leads with a `# …` comment (per the tool
    description); the TUI surfaces it as the step label and keeps the code
    collapsed behind display.tool_preview_length."""

    _CODE = "# Searching Amazon for paper towels\nnew_tab('https://amazon.com')\nwait_for_load()"

    def test_leading_comment_becomes_step_label(self):
        from agent.display import _browser_exec_step_label

        assert _browser_exec_step_label({"code": self._CODE}) == "Searching Amazon for paper towels"

    def test_no_comment_returns_none(self):
        from agent.display import _browser_exec_step_label

        assert _browser_exec_step_label({"code": "new_tab('x')"}) is None
        assert _browser_exec_step_label({"code": ""}) is None
        assert _browser_exec_step_label({"code": "#   "}) is None

    def test_label_hard_capped_regardless_of_global_setting(self):
        from agent.display import _browser_exec_step_label

        long = "# " + "x" * 200
        label = _browser_exec_step_label({"code": long})
        assert len(label) <= 80 and label.endswith("…")

    def test_preview_prefers_comment_over_code(self):
        from agent.display import build_tool_preview

        assert build_tool_preview("browser_exec", {"code": self._CODE}) == (
            "Searching Amazon for paper towels"
        )
        assert "new_tab" in build_tool_preview("browser_exec", {"code": "new_tab('x')"})

    def test_progress_line_shows_label(self):
        from agent.display import get_cute_tool_message

        line = get_cute_tool_message("browser_exec", {"code": self._CODE}, 1.2)
        assert "Searching Amazon for paper towels" in line
        assert "new_tab" not in line



class TestHeaderVariants:

    def test_text_only_header_teaches_text_workflow(self, monkeypatch):
        monkeypatch.setattr(
            "tools.vision_tools._should_use_native_vision_fast_path", lambda: False
        )
        monkeypatch.setattr(
            "tools.browser_lightpanda.lightpanda_engine_status", lambda: (False, "")
        )
        assert bu_cli._description_header() == bu_cli._HEADER_BASE + bu_cli._HEADER_TEXT_ONLY

    def test_non_native_header_never_calls_the_model_blind(self, monkeypatch):
        """The gate measures whether screenshots ride back in THIS tool's results, not whether the
        model sees (#131568: "your model cannot view images" made a model skip a user image). The
        header names no other toolset's tool; model_tools adds the vision_analyze pointer."""
        monkeypatch.setattr("tools.vision_tools._should_use_native_vision_fast_path", lambda: False)
        monkeypatch.setattr("tools.browser_lightpanda.lightpanda_engine_status", lambda: (False, ""))
        for aux_route in (True, False):
            monkeypatch.setattr("tools.vision_tools.check_vision_requirements", lambda: aux_route)
            header = bu_cli._description_header().lower()
            assert "cannot view images" not in header and "your model cannot" not in header
            assert "described below" not in header  # no screenshot section follows the header
            assert "not attached to this tool's results" in header and "text-first" in header
            assert "vision_analyze" not in header


class TestSkillTextDescription:
    """The schema description is fully pinned: header + _HELPERS_DIGEST.

    The live ``browser-use skill`` fetch was removed after A/B benchmarking
    showed the pinned digest matches the full skill dump on success rate
    (36/36 vs 36/36, opus-4.8 + kimi-k3) — see tools/browser_use_cli.py.
    """

    def test_description_is_pinned_header_plus_digest(self, monkeypatch):
        # Even with a CLI present, the description must NOT shell out.
        monkeypatch.setattr(
            bu_cli, "_find_cli",
            lambda: (_ for _ in ()).throw(AssertionError("schema must not invoke the CLI")),
        )
        overrides = bu_cli._dynamic_schema_overrides()
        assert overrides["description"].startswith(bu_cli._HEADER_BASE)
        assert overrides["description"].endswith(bu_cli._HELPERS_DIGEST)









class TestLightpandaBackendResolution:
    """browser.engine: lightpanda — the session layer spawns ``lightpanda serve`` through the same
    get_session_info machinery and browser_exec exports its endpoint, but only when nothing with
    higher precedence (BU_CDP_* env, a CDP override, a cloud provider) claimed the session."""

    def _setup(self, monkeypatch, *, engine=True, info=None, boom=None):

        seen = []

        def fake_session_info(key):
            seen.append(key)
            if boom:
                raise boom
            return info if info is not None else {"cdp_url": "http://127.0.0.1:43111"}

        monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override", lambda: "")
        monkeypatch.setattr(bs_cloud, "get_cloud_provider", lambda: None)
        monkeypatch.setattr("tools.browser_lightpanda.using_lightpanda_engine", lambda: engine)
        monkeypatch.setattr(bs, "get_session_info", fake_session_info)
        return seen

    def test_exports_bu_cdp_url_and_private_sentinel(self, monkeypatch):
        seen = self._setup(monkeypatch)
        env = {}
        assert bu_cli._resolve_backend_cdp(env, "t1") is None
        assert env["BU_CDP_URL"] == "http://127.0.0.1:43111"
        assert env[bu_cli._PRIVATE_BROWSER_SENTINEL] == "1"
        assert seen == ["t1"]

    def test_named_session_keys_its_own_process(self, monkeypatch):
        seen = self._setup(monkeypatch)
        assert bu_cli._resolve_backend_cdp({}, "t1", session_name="r7k2") is None
        assert seen == ["bu-named-r7k2"]

    def test_default_key_without_task(self, monkeypatch):
        seen = self._setup(monkeypatch)
        assert bu_cli._resolve_backend_cdp({}, None) is None
        assert seen == ["browser-exec-default"]

    def test_launch_failure_returns_actionable_error(self, monkeypatch):
        self._setup(monkeypatch, boom=RuntimeError("no lightpanda binary was found"))
        err = bu_cli._resolve_backend_cdp({}, "t1")
        assert err and "no lightpanda binary was found" in err

    def test_missing_cdp_returns_error(self, monkeypatch):
        self._setup(monkeypatch, info={"cdp_url": None})
        err = bu_cli._resolve_backend_cdp({}, "t1")
        assert err

    def test_engine_auto_falls_through_to_packaged_chromium(self, monkeypatch):
        seen = self._setup(monkeypatch, engine=False, info={
            "cdp_url": "ws://127.0.0.1:47000/devtools/browser/t1", "features": {"local": True}})
        env = {}
        assert bu_cli._resolve_backend_cdp(env, "t1") is None
        assert seen == ["t1"]  # one session-layer call; the engine is chosen inside it
        assert env["BU_CDP_WS"].startswith("ws://")

    def test_engine_names_the_failing_local_browser(self, monkeypatch):
        self._setup(monkeypatch, engine=False, boom=RuntimeError("Chromium browser is missing."))
        err = bu_cli._resolve_backend_cdp({}, "t1")
        assert "local browser could not be started" in err and "Lightpanda" not in err

    def test_bu_env_wins(self, monkeypatch):
        seen = self._setup(monkeypatch)
        env = {"BU_CDP_WS": "ws://operator:9222"}
        assert bu_cli._resolve_backend_cdp(env, "t1") is None
        assert env["BU_CDP_WS"] == "ws://operator:9222"
        assert seen == []

    def test_cdp_override_wins(self, monkeypatch):

        seen = self._setup(monkeypatch)
        monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override", lambda: "http://127.0.0.1:9222")
        env = {}
        assert bu_cli._resolve_backend_cdp(env, "t1") is None
        assert env["BU_CDP_URL"] == "http://127.0.0.1:9222"
        assert seen == []

    def test_cloud_provider_wins(self, monkeypatch):

        seen = self._setup(monkeypatch, info={"cdp_url": "wss://cloud.example/x"})
        monkeypatch.setattr(bs_cloud, "get_cloud_provider", lambda: object())
        env = {}
        assert bu_cli._resolve_backend_cdp(env, "t1") is None
        assert env["BU_CDP_WS"] == "wss://cloud.example/x"
        assert seen == ["t1"]  # provider path, same cache key




class TestLightpandaHeader:
    def test_lightpanda_header_is_text_first_even_for_vision_models(self, monkeypatch):
        monkeypatch.setattr(
            "tools.vision_tools._should_use_native_vision_fast_path", lambda: True
        )
        monkeypatch.setattr(
            "tools.browser_lightpanda.lightpanda_engine_status", lambda: (True, "used")
        )
        header = bu_cli._description_header()
        assert header.startswith(bu_cli._HEADER_BASE)
        assert header.endswith(bu_cli._HEADER_LIGHTPANDA)
        assert bu_cli._HEADER_VISION not in header
        overrides = bu_cli._dynamic_schema_overrides()
        assert overrides["description"].startswith(bu_cli._HEADER_BASE)
        assert overrides["description"].endswith(bu_cli._HELPERS_DIGEST)

    def test_shadowed_engine_keeps_default_header(self, monkeypatch):
        monkeypatch.setattr(
            "tools.vision_tools._should_use_native_vision_fast_path", lambda: True
        )
        monkeypatch.setattr(
            "tools.browser_lightpanda.lightpanda_engine_status", lambda: (False, "cloud")
        )
        assert bu_cli._description_header() == bu_cli._HEADER_BASE + bu_cli._HEADER_VISION


class TestLightpandaPickerRow:
    def _rows(self):
        from hermes_cli.tools_config import TOOL_CATEGORIES

        return TOOL_CATEGORIES["browser"]["providers"]

    def _row(self, name):
        return next(r for r in self._rows() if r["name"] == name)


    def test_selecting_lightpanda_writes_engine_and_local(self):
        from hermes_cli.tools_config import _write_provider_config

        config = {"browser": {"cloud_provider": "browserbase"}}
        _write_provider_config(self._row("Lightpanda"), config, managed_feature=None)
        assert config["browser"]["cloud_provider"] == "local"
        assert config["browser"]["engine"] == "lightpanda"

    def test_selecting_local_browser_resets_engine(self):
        from hermes_cli.tools_config import _write_provider_config

        config = {"browser": {"cloud_provider": "local", "engine": "lightpanda"}}
        _write_provider_config(self._row("Local Browser"), config, managed_feature=None)
        assert config["browser"]["engine"] == "auto"

    def test_active_row_follows_engine(self):
        from hermes_cli.tools_config import _is_provider_active

        lp_row, local_row = self._row("Lightpanda"), self._row("Local Browser")
        lp_cfg = {"browser": {"cloud_provider": "local", "engine": "lightpanda"}}
        assert _is_provider_active(lp_row, lp_cfg) is True
        assert _is_provider_active(local_row, lp_cfg) is False
        default_cfg = {"browser": {"cloud_provider": "local"}}
        assert _is_provider_active(lp_row, default_cfg) is False
        assert _is_provider_active(local_row, default_cfg) is True
        cloud_cfg = {"browser": {"cloud_provider": "browserbase", "engine": "lightpanda"}}
        assert _is_provider_active(lp_row, cloud_cfg) is False




class TestTimeoutProcessGroupKill:
    """#106244: on timeout the whole CLI process group must die. A pipe-holding
    grandchild (browser_harness daemon / Chrome helper) otherwise keeps communicate()
    blocked forever, and the wedged call's activity heartbeat pins the session at
    "now" in the sidebar indefinitely."""


    def test_post_kill_drain_is_bounded(self, monkeypatch):
        """If even the post-kill drain misses its deadline, give up instead of wedging."""

        class _StuckProc:
            pid = 424243
            returncode = None

            def communicate(self, input=None, timeout=None):
                raise subprocess.TimeoutExpired("browser-use", timeout)

        monkeypatch.setattr(bu_cli.subprocess, "Popen", lambda *a, **k: _StuckProc())
        monkeypatch.setattr(bu_cli, "_kill_cli_process_group", lambda proc: None)
        with pytest.raises(subprocess.TimeoutExpired):
            bu_cli._run_cli_killing_process_group(["x"], "code", {}, 5)
