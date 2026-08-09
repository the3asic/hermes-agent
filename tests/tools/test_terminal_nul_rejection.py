"""Regression coverage for malformed commands containing real NUL bytes."""

import importlib
import json

from cron.lifecycle_guard import (
    contains_gateway_lifecycle_command_or_referenced_script,
)

terminal_module = importlib.import_module("tools.terminal_tool")


def test_terminal_rejects_nul_before_environment_setup(monkeypatch):
    def _unexpected_config_load():
        raise AssertionError("environment setup must not run for malformed commands")

    monkeypatch.setattr(terminal_module, "_get_env_config", _unexpected_config_load)

    result = json.loads(
        terminal_module.terminal_tool(command="python malformed\x00path.py")
    )

    assert result["status"] == "error"
    assert result["exit_code"] == -1
    assert "embedded NUL" in result["error"]


def test_terminal_allows_textual_backslash_zero(monkeypatch):
    def _passed_preflight():
        raise RuntimeError("passed NUL preflight")

    monkeypatch.setattr(terminal_module, "_get_env_config", _passed_preflight)

    result = json.loads(terminal_module.terminal_tool(command=r"printf '\\0'"))
    assert result["error"] == "Failed to execute command: passed NUL preflight"


def test_lifecycle_guard_tolerates_nul_in_candidate_path():
    assert not contains_gateway_lifecycle_command_or_referenced_script(
        "python malformed\x00path.py",
        cwd="/tmp",
    )


def test_lifecycle_guard_skips_nul_in_referenced_script_path():
    assert not contains_gateway_lifecycle_command_or_referenced_script(
        'bash "malformed\x00path.sh"',
        cwd="/tmp",
    )


def test_lifecycle_guard_skips_nul_in_nested_remote_script_path():
    def _read_remote_script(path: str):
        assert path == "/remote/outer.sh"
        return 'bash "malformed\x00path.sh"'

    assert not contains_gateway_lifecycle_command_or_referenced_script(
        "bash /remote/outer.sh",
        cwd="/tmp",
        read_remote_script=_read_remote_script,
    )
