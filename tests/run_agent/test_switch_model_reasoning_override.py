"""Tests for per-model reasoning_effort override during /model switch.

Tests that switch_model:
1. Re-resolves reasoning_config when switching to a model with an override
2. Falls back to global when switching to a model without an override
3. Saves reasoning_config into _primary_runtime for fallback recovery
"""

import pytest
from unittest.mock import MagicMock, patch


class TestSwitchModelReasoningOverride:
    """Test switch_model re-resolves reasoning_config on model switch."""

    def _make_fake_agent(self, model="gpt-5", provider="openai"):
        """Create a minimal fake agent for switch_model testing."""
        agent = MagicMock()
        agent.model = model
        agent.provider = provider
        agent.base_url = "https://api.openai.com/v1"
        agent.api_mode = "openai"
        agent.api_key = "test-key"
        agent._client_kwargs = {"api_key": "test-key", "base_url": "https://api.openai.com/v1"}
        agent._use_prompt_caching = False
        agent._use_native_cache_layout = False
        agent.reasoning_config = {"enabled": True, "effort": "medium"}
        agent._fallback_activated = False
        agent._active_fallback_entry = None
        agent._runtime_reasoning_entry = None
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
        agent._anthropic_prompt_cache_policy = MagicMock(
            return_value=(False, False)
        )
        agent._ensure_lmstudio_runtime_loaded = MagicMock()
        agent._create_openai_client = MagicMock(return_value=MagicMock())
        return agent

    def test_primary_runtime_includes_reasoning_config(self):
        """After switch_model, _primary_runtime should contain reasoning_config key."""
        from agent.agent_runtime_helpers import switch_model

        agent = self._make_fake_agent()
        agent._fallback_activated = True
        agent._active_fallback_entry = {"reasoning_effort": "low"}
        agent._runtime_reasoning_entry = {"reasoning_effort": "low"}

        fake_cfg = {
            "model": {"default": "custom-reasoning-model"},
            "agent": {
                "reasoning_effort": "medium",
                "reasoning_overrides": {
                    "custom-reasoning-model": "xhigh",
                },
            },
        }

        with patch("hermes_cli.config.load_config", return_value=fake_cfg):
            switch_model(
                agent,
                new_model="custom-reasoning-model",
                new_provider="custom-openai",
                base_url="https://custom.example/v1",
                api_mode="chat_completions",
            )

        assert hasattr(agent, "_primary_runtime")
        assert "reasoning_config" in agent._primary_runtime
        assert agent._primary_runtime["reasoning_policy_entry"] is None
        assert agent._active_fallback_entry is None
        assert agent._runtime_reasoning_entry is None

    def test_switch_model_reasoning_resolution_failure_uses_provider_default(self):
        from agent.agent_runtime_helpers import switch_model

        agent = self._make_fake_agent()
        agent.reasoning_config = {"enabled": True, "effort": "low"}
        agent._fallback_activated = True
        agent._active_fallback_entry = {"reasoning_effort": "low"}
        agent._runtime_reasoning_entry = {"reasoning_effort": "low"}

        with patch(
            "hermes_cli.config.load_config",
            side_effect=RuntimeError("config unavailable"),
        ):
            switch_model(
                agent,
                new_model="custom-reasoning-model",
                new_provider="custom-openai",
                base_url="https://custom.example/v1",
                api_mode="chat_completions",
            )

        assert agent.reasoning_config is None
        assert agent._primary_runtime["reasoning_config"] is None
        assert agent._primary_runtime["reasoning_policy_entry"] is None
        assert agent._active_fallback_entry is None
        assert agent._runtime_reasoning_entry is None



    def test_restore_primary_runtime_restores_reasoning(self):
        """restore_primary_runtime should restore reasoning_config from snapshot."""
        from agent.agent_runtime_helpers import restore_primary_runtime

        agent = MagicMock()
        agent._primary_runtime = {
            "model": "claude-opus-4.5",
            "provider": "anthropic",
            "base_url": "https://api.anthropic.com",
            "api_mode": "anthropic_messages",
            "api_key": "key",
            "client_kwargs": {},
            "use_prompt_caching": True,
            "use_native_cache_layout": False,
            "reasoning_config": {"enabled": True, "effort": "xhigh"},
            "compressor_model": "claude-opus-4.5",
            "compressor_base_url": "",
            "compressor_api_key": "",
            "compressor_provider": "",
            "compressor_context_length": 0,
            "compressor_api_mode": "",
            "compressor_threshold_tokens": 0,
            "anthropic_api_key": "key",
            "anthropic_base_url": "https://api.anthropic.com",
            "is_anthropic_oauth": False,
        }
        agent._fallback_activated = True
        agent._fallback_index = 0
        agent._fallback_chain = []
        agent._fallback_model = None
        agent._transport_cache = {}
        agent._config_context_length = None
        agent._rate_limited_until = 0
        agent.model = "fallback-model"
        agent.provider = "openai"
        agent.reasoning_config = {"enabled": True, "effort": "medium"}
        agent.context_compressor = MagicMock()
        agent.base_url = ""
        # Mock the methods restore_primary_runtime calls
        agent._anthropic_prompt_cache_policy = MagicMock(return_value=(True, False))
        agent._create_openai_client = MagicMock(return_value=MagicMock())
        agent._ensure_lmstudio_runtime_loaded = MagicMock()

        with patch(
            "agent.anthropic_adapter.build_anthropic_client",
            return_value=MagicMock(),
        ):
            result = restore_primary_runtime(agent)
        assert result is True
        assert agent.reasoning_config == {"enabled": True, "effort": "xhigh"}
