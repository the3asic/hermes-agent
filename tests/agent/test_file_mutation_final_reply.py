"""Real file-tool failures are reconciled at the final reply boundary."""

import json
import logging
from pathlib import Path

import pytest

from agent.turn_explainers import TurnExplainersMixin
from agent.turn_finalizer import _append_file_mutation_footer
from tools.file_tools import patch_tool


@pytest.mark.parametrize("operation", ["unchanged", "rewrite", "create", "delete"])
def test_final_reply_rechecks_external_file_changes(tmp_path, operation):
    target = tmp_path / "report.md"
    if operation != "create":
        target.write_text("The file mutation verifier checks actual filesystem changes.\n")
    args = dict(mode="replace", path=str(target), old_string="missing replacement target", new_string="updated")
    receipt = patch_tool(**args)
    assert json.loads(receipt).get("error")
    agent = TurnExplainersMixin()
    agent._turn_failed_file_mutations = {}
    agent._turn_file_mutation_paths = set()
    agent._record_file_mutation_result("patch", args, receipt, is_error=True)
    assert agent._turn_failed_file_mutations
    if operation in {"rewrite", "create"}:
        target.write_text("An external file writer completed this update.\n")
    elif operation == "delete":
        target.unlink()
    reply = _append_file_mutation_footer(agent, "File operation completed.", logging.getLogger(__name__))
    if operation == "unchanged":
        assert target.name in reply
    else:
        assert reply == "File operation completed."


@pytest.mark.parametrize("backend", ["docker", "ssh"])
def test_unverifiable_backend_retains_the_failure(tmp_path, backend):
    target = tmp_path / "remote-report.md"
    target.write_text("A host file cannot prove a sandbox target changed.\n")
    args = dict(mode="replace", path=str(target), old_string="missing replacement target", new_string="updated")
    receipt = patch_tool(**args)
    assert json.loads(receipt).get("error")
    agent = TurnExplainersMixin()
    agent._turn_failed_file_mutations = {}
    agent._turn_file_mutation_paths = set()
    agent._record_file_mutation_result("patch", args, receipt, is_error=True)
    target.write_text("The host copy changed after the failed call.\n")
    from tools.terminal_tool_lifecycle import cleanup_vm
    cleanup_vm("default")
    from hermes_constants import get_hermes_home
    config = Path(get_hermes_home()) / "config.yaml"
    config.write_text(f"terminal:\n  backend: {backend}\n")
    from tools.terminal_scope import build_profile_terminal_scope, reset_terminal_scope, set_terminal_scope
    token = set_terminal_scope(build_profile_terminal_scope(get_hermes_home()))
    agent._current_task_id = "remote-target-validation"
    try:
        from tools.file_tools_paths import _terminal_env_type_for_task
        assert _terminal_env_type_for_task(agent._current_task_id) == backend
        reply = _append_file_mutation_footer(agent, "File operation completed.", logging.getLogger(__name__))
        assert target.name in reply
    finally:
        reset_terminal_scope(token)
