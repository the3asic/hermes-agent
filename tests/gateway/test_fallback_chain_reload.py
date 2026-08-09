"""Regression tests for #60955: gateway must not freeze fallback_providers.

Cron reloads ``fallback_providers`` from disk on every job. The gateway used to
freeze ``self._fallback_model`` at process start, so a chain configured (or
edited) after ``hermes gateway`` was already running never reached messaging
sessions — even though cron in the same process fell back correctly.

These tests pin the reload helpers and drive the Discord cached-agent turn path.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from gateway.config import ChannelOverride, GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionSource


def test_refresh_fallback_model_rereads_config(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner

    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "fallback_providers:\n"
        "  - provider: deepseek\n"
        "    model: deepseek-v4-flash\n"
    )

    runner = SimpleNamespace(
        _fallback_model=None,
    )
    runner._load_fallback_model = GatewayRunner._load_fallback_model
    bound = GatewayRunner._refresh_fallback_model.__get__(runner)
    chain = bound()

    assert chain == [{"provider": "deepseek", "model": "deepseek-v4-flash"}]
    assert runner._fallback_model == chain

    cfg.write_text(
        "fallback_providers:\n"
        "  - provider: openrouter\n"
        "    model: anthropic/claude-sonnet-4.6\n"
    )
    updated = bound()
    assert updated == [
        {"provider": "openrouter", "model": "anthropic/claude-sonnet-4.6"}
    ]
    assert runner._fallback_model == updated


def test_refresh_fallback_model_clears_when_config_removed(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner

    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "fallback_providers:\n"
        "  - provider: deepseek\n"
        "    model: deepseek-v4-flash\n"
    )

    runner = SimpleNamespace(
        _fallback_model=[{"provider": "stale", "model": "x"}],
    )
    runner._load_fallback_model = GatewayRunner._load_fallback_model
    bound = GatewayRunner._refresh_fallback_model.__get__(runner)
    assert bound() is not None

    cfg.write_text("model:\n  provider: nvidia\n")
    assert bound() is None
    assert runner._fallback_model is None


def test_refresh_fallback_model_keeps_last_known_good_on_read_failure(
    tmp_path, monkeypatch,
):
    """A transient config.yaml read/parse failure (user mid-edit, non-atomic
    write) must NOT wipe the last known-good chain — only a successful read
    that genuinely lacks the key clears it."""
    from gateway.run import GatewayRunner

    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "fallback_providers:\n"
        "  - provider: deepseek\n"
        "    model: deepseek-v4-flash\n"
    )

    runner = SimpleNamespace(_fallback_model=None)
    runner._load_fallback_model = GatewayRunner._load_fallback_model
    bound = GatewayRunner._refresh_fallback_model.__get__(runner)
    good = bound()
    assert good == [{"provider": "deepseek", "model": "deepseek-v4-flash"}]

    # Simulate a mid-edit torn write: invalid YAML.
    cfg.write_text("fallback_providers:\n  - provider: [unclosed\n")
    assert bound() == good
    assert runner._fallback_model == good


def test_apply_fallback_chain_updates_primary_agent():
    from gateway.run import GatewayRunner

    agent = SimpleNamespace(
        _fallback_chain=[],
        _fallback_model=None,
        _fallback_index=0,
        _fallback_activated=False,
        _rate_limited_until=0,
    )
    chain = [{"provider": "deepseek", "model": "deepseek-v4-flash"}]
    GatewayRunner._apply_fallback_chain_to_agent(agent, chain)

    assert agent._fallback_chain == chain
    assert agent._fallback_model == chain[0]
    assert agent._fallback_index == 0


def test_apply_fallback_chain_skips_while_cooldown_holds_fallback():
    """Do not clobber a live fallback activation during its cooldown window."""
    from gateway.run import GatewayRunner

    live = [{"provider": "deepseek", "model": "deepseek-v4-flash"}]
    agent = SimpleNamespace(
        _fallback_chain=live,
        _fallback_model=live[0],
        _fallback_index=1,
        _fallback_activated=True,
        _rate_limited_until=time.monotonic() + 30,
    )
    GatewayRunner._apply_fallback_chain_to_agent(
        agent,
        [{"provider": "openrouter", "model": "anthropic/claude-sonnet-4.6"}],
    )

    assert agent._fallback_chain == live
    assert agent._fallback_index == 1
    assert agent._fallback_activated is True


def test_apply_fallback_chain_updates_after_cooldown_expires():
    from gateway.run import GatewayRunner

    agent = SimpleNamespace(
        _fallback_chain=[{"provider": "deepseek", "model": "old"}],
        _fallback_model={"provider": "deepseek", "model": "old"},
        _fallback_index=1,
        _fallback_activated=True,
        _rate_limited_until=time.monotonic() - 1,
    )
    new_chain = [{"provider": "openrouter", "model": "anthropic/claude-sonnet-4.6"}]
    GatewayRunner._apply_fallback_chain_to_agent(agent, new_chain)

    assert agent._fallback_chain == new_chain
    assert agent._fallback_model == new_chain[0]
    # Activated agents keep their index; restore_primary_runtime owns reset.
    assert agent._fallback_index == 1


def test_apply_fallback_chain_clears_unavailable_memo_on_content_change():
    """A config edit must drop the session-scoped unavailability memo so a
    re-configured entry (credentials added mid-uptime) is retried instead of
    staying suppressed for the cached agent's lifetime."""
    from gateway.run import GatewayRunner

    agent = SimpleNamespace(
        _fallback_chain=[{"provider": "deepseek", "model": "old"}],
        _fallback_model={"provider": "deepseek", "model": "old"},
        _fallback_index=0,
        _fallback_activated=False,
        _rate_limited_until=0,
        _unavailable_fallback_keys={("deepseek", "old", "")},
    )
    new_chain = [{"provider": "deepseek", "model": "deepseek-v4-flash"}]
    GatewayRunner._apply_fallback_chain_to_agent(agent, new_chain)

    assert agent._fallback_chain == new_chain
    assert agent._unavailable_fallback_keys == set()


def test_apply_fallback_chain_keeps_unavailable_memo_when_unchanged():
    """The per-message no-op refresh must NOT clear the memo — it exists to
    rate-limit repeated activation attempts against dead entries."""
    from gateway.run import GatewayRunner

    chain = [{"provider": "deepseek", "model": "deepseek-v4-flash"}]
    memo = {("deepseek", "deepseek-v4-flash", "")}
    agent = SimpleNamespace(
        _fallback_chain=list(chain),
        _fallback_model=chain[0],
        _fallback_index=0,
        _fallback_activated=False,
        _rate_limited_until=0,
        _unavailable_fallback_keys=set(memo),
    )
    GatewayRunner._apply_fallback_chain_to_agent(agent, list(chain))

    assert agent._unavailable_fallback_keys == memo


def test_cached_agent_receives_effective_channel_chain_without_mutating_config():
    """The per-turn cached-agent apply uses a fresh copy of the Discord chain."""
    from gateway.run import GatewayRunner

    configured = [
        {
            "provider": "custom",
            "model": "channel-model",
            "base_url": "https://fallback.example/v1",
            "key_env": "CHANNEL_FALLBACK_KEY",
        },
    ]
    runner = SimpleNamespace(
        config=GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "channel": ChannelOverride(fallback_providers=configured),
                    },
                ),
            },
        ),
    )
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="channel",
        user_id="user",
    )
    effective = GatewayRunner._resolve_session_fallback_chain(
        runner,
        source=source,
        global_chain=[{"provider": "global", "model": "global-model"}],
    )
    agent = SimpleNamespace(
        _fallback_chain=[{"provider": "stale", "model": "stale-model"}],
        _fallback_model=None,
        _fallback_index=0,
        _fallback_activated=False,
        _rate_limited_until=0,
        _unavailable_fallback_keys=set(),
    )

    GatewayRunner._apply_fallback_chain_to_agent(agent, effective)

    assert agent._fallback_chain == configured
    assert agent._fallback_chain is not configured
    assert agent._fallback_chain[0] is not configured[0]
    agent._fallback_chain[0]["model"] = "mutated-by-agent"
    stored = runner.config.platforms[Platform.DISCORD].channel_overrides[
        "channel"
    ].fallback_providers
    assert stored[0]["model"] == "channel-model"
    assert "CHANNEL_FALLBACK_KEY" not in GatewayRunner._agent_config_signature(
        "primary",
        {"provider": "custom", "api_key": "primary-key"},
        ["hermes-discord"],
        "",
    )


def test_main_turn_reuses_cached_agent_and_refreshes_channel_chain(
    tmp_path, monkeypatch
):
    """The real gateway cache-hit branch reapplies the current Discord chain."""
    from gateway import run as gateway_run

    class CapturingCachedAgent:
        instances = []

        def __init__(self, *args, **kwargs):
            self.tools = []
            self._fallback_chain = [
                dict(entry) for entry in kwargs.get("fallback_model") or []
            ]
            self._fallback_model = (
                self._fallback_chain[0] if self._fallback_chain else None
            )
            self._fallback_index = 0
            self._fallback_activated = False
            self._rate_limited_until = 0
            self._unavailable_fallback_keys = set()
            self.turn_chains = []
            type(self).instances.append(self)

        def run_conversation(
            self, user_message: str, conversation_history=None, task_id=None
        ):
            self.turn_chains.append(
                [dict(entry) for entry in self._fallback_chain]
            )
            return {"final_response": "ok", "messages": [], "api_calls": 1}

    first_chain = [{"provider": "first", "model": "first-model"}]
    second_chain = [{"provider": "second", "model": "second-model"}]
    platform_config = PlatformConfig(
        enabled=True,
        channel_overrides={
            "channel": ChannelOverride(fallback_providers=first_chain),
        },
    )
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: platform_config},
    )
    runner.adapters = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._session_reasoning_overrides = {}
    runner._show_reasoning = False
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.hooks.loaded_hooks = []
    runner._session_db = None
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    runner._refresh_fallback_model = lambda: [
        {"provider": "global", "model": "global-model"},
    ]

    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "model:\n  default: primary-model\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    monkeypatch.setattr(gateway_run, "_env_path", hermes_home / ".env")
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "custom",
            "api_mode": "chat_completions",
            "base_url": "https://primary.example/v1",
            "api_key": "test-key",
        },
    )
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = CapturingCachedAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="channel",
        user_id="user",
    )
    run_kwargs = {
        "message": "ping",
        "context_prompt": "",
        "history": [],
        "source": source,
        "session_id": "session-1",
        "session_key": "agent:main:discord:channel:channel",
    }

    first_result = asyncio.run(runner._run_agent(**run_kwargs))
    platform_config.channel_overrides["channel"].fallback_providers = second_chain
    second_result = asyncio.run(runner._run_agent(**run_kwargs))

    assert first_result["final_response"] == second_result["final_response"] == "ok"
    assert len(CapturingCachedAgent.instances) == 1
    assert CapturingCachedAgent.instances[0].turn_chains == [first_chain, second_chain]


def test_load_fallback_model_static_unchanged_contract(tmp_path, monkeypatch):
    """_load_fallback_model remains a pure static reader used by refresh."""
    from gateway.run import GatewayRunner

    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    (tmp_path / "config.yaml").write_text(
        "fallback_providers:\n"
        "  - provider: deepseek\n"
        "    model: deepseek-v4-flash\n"
        "fallback_model:\n"
        "  provider: nous\n"
        "  model: Hermes-4\n"
    )

    chain = GatewayRunner._load_fallback_model()
    assert chain == [
        {"provider": "deepseek", "model": "deepseek-v4-flash"},
        {"provider": "nous", "model": "Hermes-4"},
    ]
