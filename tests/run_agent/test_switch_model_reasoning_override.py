"""Reasoning resolution across model switches."""

from unittest.mock import MagicMock, patch


def _make_fake_agent(model="gpt-5", provider="openai"):
    agent = MagicMock()
    agent.model = model
    agent.provider = provider
    agent.base_url = "https://api.openai.com/v1"
    agent.api_mode = "chat_completions"
    agent.api_key = "test-key"
    agent._client_kwargs = {
        "api_key": "test-key",
        "base_url": "https://api.openai.com/v1",
    }
    agent._use_prompt_caching = False
    agent._use_native_cache_layout = False
    agent.reasoning_config = {"enabled": True, "effort": "medium"}
    agent._reasoning_config_fixed = False
    agent._fallback_activated = False
    agent._fallback_index = 0
    agent._fallback_chain = []
    agent._fallback_model = None
    agent._config_context_length = None
    agent._transport_cache = {}
    agent.context_compressor = None
    agent._cached_system_prompt = None
    agent._anthropic_api_key = ""
    agent._anthropic_base_url = None
    agent._is_anthropic_oauth = False
    agent._anthropic_prompt_cache_policy = MagicMock(return_value=(False, False))
    agent._ensure_lmstudio_runtime_loaded = MagicMock()
    agent._create_openai_client = MagicMock(return_value=MagicMock())
    return agent


def test_model_switch_resolves_effective_model_reasoning():
    from agent.agent_runtime_helpers import switch_model

    agent = _make_fake_agent()
    config = {
        "agent": {
            "reasoning_effort": "low",
            "reasoning_overrides": {"claude-opus-4.6": "xhigh"},
        },
    }

    with patch("hermes_cli.config.load_config", return_value=config):
        switch_model(
            agent,
            new_model="claude-opus-4.6",
            new_provider="anthropic",
            base_url="https://api.anthropic.com",
            api_mode="anthropic_messages",
        )

    assert agent.reasoning_config == {"enabled": True, "effort": "xhigh"}
    assert agent._primary_runtime["reasoning_config"] == agent.reasoning_config


def test_scoped_reasoning_survives_model_switch():
    from agent.agent_runtime_helpers import switch_model

    agent = _make_fake_agent()
    agent.reasoning_config = {"enabled": False}
    agent._reasoning_config_fixed = True
    config = {
        "agent": {
            "reasoning_effort": "high",
            "reasoning_overrides": {"claude-opus-4.6": "xhigh"},
        },
    }

    with patch("hermes_cli.config.load_config", return_value=config):
        switch_model(
            agent,
            new_model="claude-opus-4.6",
            new_provider="anthropic",
            base_url="https://api.anthropic.com",
            api_mode="anthropic_messages",
        )

    assert agent.reasoning_config == {"enabled": False}
    assert agent._primary_runtime["reasoning_config"] == {"enabled": False}
