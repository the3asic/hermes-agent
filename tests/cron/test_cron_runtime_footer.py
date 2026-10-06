"""Cron delivery reports the final route while preserving silent turns."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cron.scheduler import _cron_failure_marker_error, run_job


def _run(tmp_path, response, *, usage=True):
    (tmp_path / "config.yaml").write_text(
        "model:\n  default: primary-model\n  provider: custom\n"
        "display:\n  platforms:\n    cron:\n      runtime_footer:\n"
        "        enabled: true\n"
        "        fields: [model_last, reasoning_effort, tokens_turn, cache_hit, context_window, latency]\n",
        encoding="utf-8",
    )
    agent = MagicMock()
    agent.model = "gpt-6.1-sol"
    agent.reasoning_config = {"enabled": True, "effort": "max"}
    agent.context_compressor = SimpleNamespace(context_length=872000)
    agent.session_prompt_tokens = 110
    agent.session_input_tokens = 100
    agent.session_completion_tokens = 4
    agent.session_cache_read_tokens = 10
    agent.session_cache_write_tokens = 0
    agent.session_last_prompt_tokens = 110
    agent.session_usage_report_calls = int(usage)
    agent.session_cache_usage_report_calls = int(usage)
    agent.session_context_usage_report_calls = int(usage)
    agent.run_conversation.return_value = {"final_response": response, "api_calls": 1}
    runtime = {"provider": "custom", "api_key": "fixture-key", "base_url": "https://example.invalid/v1",
               "api_mode": "chat_completions"}
    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler._get_hermes_home", return_value=tmp_path), \
         patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_state_registry.acquire", return_value=MagicMock()), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=runtime), \
         patch("tools.mcp_tool_discovery.discover_mcp_tools", return_value=[]), \
         patch("run_agent.AIAgent", return_value=agent):
        return run_job({"id": "footer-fixture", "name": "footer fixture", "prompt": "hi"})


@pytest.mark.parametrize("response", [
    "Report", "[SILENT]", "", "Details\n[SILENT]", "[CRON_FAILURE]\nbroken",
])
def test_cron_footer_uses_final_route_and_preserves_silence(tmp_path, response):
    success, output, final, error = _run(tmp_path, response)
    assert (success, error) == (True, None)
    if response.startswith("[CRON_FAILURE]"):
        # The scheduler classifies agent-declared failure after run_job returns.
        assert final == response
        assert _cron_failure_marker_error(final) == "broken"
        assert "model(last)" not in output
        return
    if response == "Report":
        assert "model(last):gpt-6.1-sol" in final
        assert "effort(req,last):max" in final
        assert "tokens(turn,uncached):100 in/4 out" in final
        assert "cache(turn):9%" in final
        assert "ctx(last):110/872.0k" in final
        assert final in output
    else:
        assert final == response
        assert "model(last)" not in output


def test_missing_provider_usage_never_fabricates_footer_counts(tmp_path):
    success, _output, final, error = _run(tmp_path, "Report", usage=False)
    assert (success, error) == (True, None)
    assert "model(last):gpt-6.1-sol" in final
    assert "effort(req,last):max" in final
    for unavailable in ("tokens(turn", "cache(turn", "ctx(last)"):
        assert unavailable not in final
