"""Regression tests for session-scoped model/provider overrides in gateway agents.

These cover the bug where `/model ...` stored a session override, but fresh
agent constructions still resolved model/provider from global config/runtime.
That let helper agents (and cache-miss main agents) route GPT-5.4 to the wrong
provider, e.g. Nous instead of OpenAI Codex.
"""

import asyncio
import sys
import threading
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.session import SessionSource


class _CapturingAgent:
    """Fake agent that records init kwargs for assertions."""

    last_init = None

    def __init__(self, *args, **kwargs):
        type(self).last_init = dict(kwargs)
        self.tools = []

    def run_conversation(self, user_message: str, conversation_history=None, task_id=None):
        return {
            "final_response": "ok",
            "messages": [],
            "api_calls": 1,
        }


def _make_runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner.session_store = None
    runner.config = None
    runner._voice_mode = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._show_reasoning = False
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._service_tier = None
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._background_tasks = set()
    runner._session_db = None
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    runner._pending_model_notes = {}
    runner._pending_approvals = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.hooks.loaded_hooks = []
    return runner


def _codex_override():
    return {
        "model": "gpt-5.4",
        "provider": "openai-codex",
        "api_key": "***",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "api_mode": "codex_responses",
    }


def _explode_runtime_resolution():
    raise AssertionError(
        "global runtime resolution should not run when a complete session override exists"
    )


def test_gateway_auth_fallback_uses_fallback_model_from_config(tmp_path, monkeypatch):
    """Fallback resolution must use the fallback model's runtime shape.

    A model-sensitive provider can select its URL and wire mode from the target
    model. If primary auth fails, both those fields and the eventual AIAgent
    model must come from the fallback entry rather than the persisted primary.
    """
    config = tmp_path / "config.yaml"
    config.write_text(
        """
model:
  default: gpt-5.5
  provider: openai-codex
providers:
  opencode-go-bridge:
    base_url: https://opencode.ai/zen/go/v1
    key_env: OPENCODE_GO_BRIDGE_API_KEY
fallback_providers:
  - provider: opencode-go-bridge
    model: minimax-m2.7
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("OPENCODE_GO_BRIDGE_API_KEY", "fallback-key")

    resolver_calls = []
    import hermes_cli.runtime_provider as runtime_provider

    real_resolve_runtime_provider = runtime_provider.resolve_runtime_provider

    def tracking_resolve_runtime_provider(**kwargs):
        resolver_calls.append(
            (kwargs.get("requested"), kwargs.get("target_model"))
        )
        if kwargs.get("requested") in {None, "", "openai-codex"}:
            from hermes_cli.auth import AuthError
            raise AuthError("No Codex credentials stored. Run `hermes auth` to authenticate.")
        return real_resolve_runtime_provider(**kwargs)

    monkeypatch.setattr(
        runtime_provider,
        "resolve_runtime_provider",
        tracking_resolve_runtime_provider,
    )

    runner = _make_runner()
    model, runtime_kwargs = runner._resolve_session_agent_runtime(
        session_key="agent:main:telegram:group:-1003715515980:63",
        user_config={
            "model": {"default": "gpt-5.5", "provider": "openai-codex"},
            "fallback_providers": [
                {
                    "provider": "opencode-go-bridge",
                    "model": "minimax-m2.7",
                }
            ],
        },
    )

    assert resolver_calls[-1] == (
        "opencode-go-bridge",
        "minimax-m2.7",
    )
    assert model == "minimax-m2.7"
    assert runtime_kwargs["provider"] == "custom"
    assert runtime_kwargs["api_key"] == "fallback-key"
    assert runtime_kwargs["api_mode"] == "anthropic_messages"
    assert runtime_kwargs["base_url"] == "https://opencode.ai/zen/go"
