"""Out-of-turn compression publishes the active session runtime to plugins."""

from __future__ import annotations

import agent.auxiliary_client as aux
from agent.conversation_compression import CompressionCommitFence
from run_agent import AIAgent


def test_compress_context_scopes_provider_model_and_reasoning(monkeypatch):
    agent = AIAgent.__new__(AIAgent)
    agent.model = "glm-5.3"
    agent.provider = "custom:cliproxyapi"
    agent.requested_provider = "custom:cliproxyapi"
    agent.base_url = "https://runtime.test/v1"
    agent.api_key = "runtime-key"
    agent.api_mode = "chat_completions"
    agent.auth_mode = "api_key"
    agent.session_id = "session-1"
    agent.reasoning_config = {"enabled": True, "effort": "max"}
    agent._session_db = None

    captured = {}

    def _compress(_agent, messages, system_message, **kwargs):
        captured.update(aux._normalize_main_runtime(None))
        return messages, system_message

    monkeypatch.setattr("agent.conversation_compression.compress_context", _compress)
    aux.clear_runtime_main()
    messages = [{"role": "user", "content": "hello"}]

    result = agent._compress_context(
        messages,
        "system",
        commit_fence=CompressionCommitFence(),
    )

    assert result == (messages, "system")
    assert captured["provider"] == "custom:cliproxyapi"
    assert captured["model"] == "glm-5.3"
    assert captured["session_id"] == "session-1"
    assert captured["cache_scope"] == "session-1"
    assert captured["reasoning_config"] == {
        "enabled": True,
        "effort": "max",
    }
    assert aux._normalize_main_runtime(None) == {}
