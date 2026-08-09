"""Tests for per-channel model and system prompt overrides (Fixes #1955)."""

from unittest.mock import patch

import pytest

from gateway.config import (
    ChannelOverride,
    GatewayConfig,
    Platform,
    PlatformConfig,
    load_gateway_config,
)
from gateway.run import GatewayRunner, _get_channel_override, _get_channel_override_field
from gateway.session import SessionSource


class TestGetChannelOverride:
    def test_no_override_when_empty_config(self):
        config = GatewayConfig()
        assert _get_channel_override(config, Platform.DISCORD, "123") is None

    def test_no_override_when_platform_not_configured(self):
        config = GatewayConfig(platforms={})
        assert _get_channel_override(config, Platform.DISCORD, "123") is None

    def test_no_override_when_channel_not_in_overrides(self):
        config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "999": ChannelOverride(model="openrouter/healer-alpha"),
                    },
                ),
            },
        )
        assert _get_channel_override(config, Platform.DISCORD, "123") is None

    def test_returns_override_when_channel_matches(self):
        ov = ChannelOverride(
            model="openrouter/healer-alpha",
            provider="openrouter",
            system_prompt="You are a summarizer.",
        )
        config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={"1234567890": ov},
                ),
            },
        )
        result = _get_channel_override(config, Platform.DISCORD, "1234567890")
        assert result is not None
        assert result.model == "openrouter/healer-alpha"
        assert result.provider == "openrouter"
        assert result.system_prompt == "You are a summarizer."

    def test_returns_override_when_chat_id_is_int_like(self):
        """Caller may pass str(chat_id); override keys are normalized to str."""
        config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={"123": ChannelOverride(model="gpt-4")},
                ),
            },
        )
        assert _get_channel_override(config, Platform.DISCORD, "123").model == "gpt-4"

    def test_thread_id_lookup_when_chat_id_misses(self):
        config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "thread_99": ChannelOverride(model="topic-model"),
                    },
                ),
            },
        )
        result = _get_channel_override(
            config, Platform.DISCORD, "parent_chan", thread_id="thread_99"
        )
        assert result is not None
        assert result.model == "topic-model"

    def test_parent_id_fallback_when_thread_has_no_entry(self):
        config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "parent_chan": ChannelOverride(model="parent-model"),
                    },
                ),
            },
        )
        result = _get_channel_override(
            config,
            Platform.DISCORD,
            "thread_only",
            parent_id="parent_chan",
        )
        assert result is not None
        assert result.model == "parent-model"

    def test_exact_thread_overrides_parent(self):
        config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "thread_1": ChannelOverride(model="thread-model"),
                        "parent_chan": ChannelOverride(model="parent-model"),
                    },
                ),
            },
        )
        result = _get_channel_override(
            config, Platform.DISCORD, "thread_1", parent_id="parent_chan"
        )
        assert result.model == "thread-model"


class TestResolveModelForChannel:
    def test_uses_channel_override_when_present(self):
        config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "chan_1": ChannelOverride(model="anthropic/claude-opus-4.6"),
                    },
                ),
            },
        )
        runner = object.__new__(GatewayRunner)
        runner.config = config
        model = runner._resolve_model_for_channel(Platform.DISCORD, "chan_1")
        assert model == "anthropic/claude-opus-4.6"

    def test_falls_back_to_global_when_no_override(self, monkeypatch):
        monkeypatch.setattr(
            "gateway.run._resolve_gateway_model",
            lambda _cfg=None: "global-model/default",
        )
        config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(enabled=True, channel_overrides={}),
            },
        )
        runner = object.__new__(GatewayRunner)
        runner.config = config
        model = runner._resolve_model_for_channel(Platform.DISCORD, "unknown_channel")
        assert model == "global-model/default"


class TestGetSystemPromptForChannel:
    def test_uses_channel_override_when_present(self):
        config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "chan_1": ChannelOverride(system_prompt="You are a coding assistant."),
                    },
                ),
            },
        )
        runner = object.__new__(GatewayRunner)
        runner.config = config
        runner._ephemeral_system_prompt = "Global prompt"
        prompt = runner._get_system_prompt_for_channel(Platform.DISCORD, "chan_1")
        assert prompt == "You are a coding assistant."

    def test_falls_back_to_global_when_no_override(self):
        config = GatewayConfig(
            platforms={Platform.DISCORD: PlatformConfig(enabled=True)},
        )
        runner = object.__new__(GatewayRunner)
        runner.config = config
        runner._ephemeral_system_prompt = "Global prompt"
        prompt = runner._get_system_prompt_for_channel(Platform.DISCORD, "other")
        assert prompt == "Global prompt"


class TestResolveSessionAgentRuntimePriority:
    """Model/runtime priority: session /model → channel_overrides → global."""

    def test_channel_override_beats_global(self):
        runner = object.__new__(GatewayRunner)
        runner._session_model_overrides = {}
        runner.config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "chan_1": ChannelOverride(
                            model="channel/model",
                            provider="openrouter",
                        ),
                    },
                ),
            },
        )
        source = SessionSource(
            platform=Platform.DISCORD,
            chat_id="chan_1",
            user_id="u1",
        )
        with patch("gateway.run._resolve_gateway_model", return_value="global/model"), \
             patch("gateway.run._resolve_runtime_agent_kwargs", return_value={
                 "provider": "anthropic",
                 "api_key": "k",
                 "base_url": "https://api.anthropic.com",
                 "api_mode": "chat_completions",
             }), \
             patch(
                 "gateway.run._resolve_runtime_agent_kwargs_for_provider",
                 return_value={
                     "provider": "openrouter",
                     "api_key": "k2",
                     "base_url": "https://openrouter.ai/api/v1",
                     "api_mode": "chat_completions",
                 },
             ):
            model, runtime = runner._resolve_session_agent_runtime(
                source=source,
                user_config={"model": {"default": "global/model"}},
            )
        assert model == "channel/model"
        assert runtime["provider"] == "openrouter"

    def test_session_model_beats_channel_override(self):
        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "chan_1": ChannelOverride(model="channel/model"),
                    },
                ),
            },
        )
        session_key = "agent:main:discord:channel:chan_1"
        runner._session_model_overrides = {
            session_key: {
                "model": "session/model",
                "provider": "anthropic",
            },
        }
        source = SessionSource(
            platform=Platform.DISCORD,
            chat_id="chan_1",
            chat_type="channel",
            user_id="u1",
        )
        with patch("gateway.run._resolve_gateway_model", return_value="global/model"), \
             patch("gateway.run._resolve_runtime_agent_kwargs", return_value={
                 "provider": "openrouter",
                 "api_key": "k",
                 "base_url": "https://openrouter.ai/api/v1",
                 "api_mode": "chat_completions",
             }):
            model, runtime = runner._resolve_session_agent_runtime(
                source=source,
                session_key=session_key,
            )
        assert model == "session/model"
        assert runtime["provider"] == "anthropic"

    def test_parent_channel_model_inherited_in_thread(self):
        runner = object.__new__(GatewayRunner)
        runner._session_model_overrides = {}
        runner.config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "parent_chan": ChannelOverride(model="parent/model"),
                    },
                ),
            },
        )
        source = SessionSource(
            platform=Platform.DISCORD,
            chat_id="thread_1",
            chat_type="thread",
            parent_chat_id="parent_chan",
            user_id="u1",
        )
        with patch("gateway.run._resolve_gateway_model", return_value="global/model"), \
             patch("gateway.run._resolve_runtime_agent_kwargs", return_value={
                 "provider": "anthropic",
                 "api_key": "k",
                 "base_url": "https://api.anthropic.com",
                 "api_mode": "chat_completions",
             }):
            model, _runtime = runner._resolve_session_agent_runtime(source=source)
        assert model == "parent/model"


class TestChannelOverrideFieldLookup:
    def test_exact_entry_can_omit_field_and_inherit_parent(self):
        config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "parent": ChannelOverride(reasoning_effort="high"),
                        "thread": ChannelOverride(model="thread-model"),
                    },
                ),
            },
        )

        assert _get_channel_override_field(
            config,
            Platform.DISCORD,
            "thread",
            "reasoning_effort",
            parent_id="parent",
        ) == "high"

    def test_falsey_field_is_explicit_and_stops_parent_lookup(self):
        config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "parent": ChannelOverride(
                            reasoning_effort="high",
                            fallback_providers=[
                                {"provider": "parent", "model": "parent-model"},
                            ],
                        ),
                        "thread": ChannelOverride(
                            reasoning_effort=False,
                            fallback_providers=[],
                        ),
                    },
                ),
            },
        )

        assert _get_channel_override_field(
            config,
            Platform.DISCORD,
            "thread",
            "reasoning_effort",
            parent_id="parent",
        ) is False
        assert _get_channel_override_field(
            config,
            Platform.DISCORD,
            "thread",
            "fallback_providers",
            parent_id="parent",
        ) == []


class TestLoadedDiscordChannelContract:
    def test_yaml_load_resolves_all_precedence_and_real_agent_chain(
        self, tmp_path, monkeypatch
    ):
        from gateway import run as gateway_run

        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "agent:\n"
            "  reasoning_effort: low\n"
            "fallback_providers:\n"
            "  - provider: global-provider\n"
            "    model: global-model\n"
            "discord:\n"
            "  channel_overrides:\n"
            "    parent:\n"
            "      reasoning_effort: high\n"
            "      fallback_providers:\n"
            "        - provider: parent-provider\n"
            "          model: parent-model\n"
            "    inherited-thread:\n"
            "      model: inherited-primary\n"
            "    exact-thread:\n"
            "      reasoning_effort: xhigh\n"
            "      fallback_providers:\n"
            "        - provider: exact-provider\n"
            "          model: exact-model\n"
            "    disabled-thread:\n"
            "      reasoning_effort: false\n"
            "      fallback_providers: []\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)

        runner = object.__new__(GatewayRunner)
        runner.config = load_gateway_config()
        runner._session_reasoning_overrides = {}
        runner._fallback_model = None
        global_chain = runner._refresh_fallback_model()

        def source(chat_id, parent_id=None):
            return SessionSource(
                platform=Platform.DISCORD,
                chat_id=chat_id,
                chat_type="thread" if parent_id else "channel",
                parent_chat_id=parent_id,
                user_id="user",
            )

        exact_source = source("exact-thread", "parent")
        exact_chain = runner._resolve_session_fallback_chain(
            source=exact_source,
            global_chain=global_chain,
        )
        assert runner._resolve_session_reasoning_config(
            source=exact_source, model="primary"
        ) == {"enabled": True, "effort": "xhigh"}
        assert exact_chain == [
            {"provider": "exact-provider", "model": "exact-model"},
        ]

        inherited_source = source("inherited-thread", "parent")
        assert runner._resolve_session_reasoning_config(
            source=inherited_source, model="inherited-primary"
        ) == {"enabled": True, "effort": "high"}
        assert runner._resolve_session_fallback_chain(
            source=inherited_source,
            global_chain=global_chain,
        ) == [{"provider": "parent-provider", "model": "parent-model"}]

        disabled_source = source("disabled-thread", "parent")
        assert runner._resolve_session_reasoning_config(
            source=disabled_source, model="primary"
        ) == {"enabled": False}
        assert runner._resolve_session_fallback_chain(
            source=disabled_source,
            global_chain=global_chain,
        ) == []

        unconfigured_source = source("unconfigured")
        assert runner._resolve_session_reasoning_config(
            source=unconfigured_source, model="primary"
        ) == {"enabled": True, "effort": "low"}
        assert runner._resolve_session_fallback_chain(
            source=unconfigured_source,
            global_chain=global_chain,
        ) == [{"provider": "global-provider", "model": "global-model"}]

        # Feed the channel-resolved chain into the real AIAgent constructor,
        # while replacing only network/tool discovery dependencies.
        monkeypatch.setattr("hermes_logging.setup_logging", lambda **_kwargs: None)
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
        ):
            from run_agent import AIAgent

            agent = AIAgent(
                api_key="test-primary-key",
                base_url="https://primary.example/v1",
                provider="custom",
                model="primary",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                fallback_model=exact_chain,
            )

        assert agent._fallback_chain == exact_chain
        agent._fallback_chain[0]["model"] = "agent-mutated"
        stored = runner.config.platforms[Platform.DISCORD].channel_overrides[
            "exact-thread"
        ].fallback_providers
        assert stored == [{"provider": "exact-provider", "model": "exact-model"}]


class TestDiscordReasoningOverrides:
    @staticmethod
    def _runner(config, global_reasoning=None):
        runner = object.__new__(GatewayRunner)
        runner.config = config
        runner._session_reasoning_overrides = {}
        runner._load_reasoning_config = lambda model="": global_reasoning
        return runner

    @staticmethod
    def _discord_source(chat_id="channel", *, parent_id=None):
        return SessionSource(
            platform=Platform.DISCORD,
            chat_id=chat_id,
            chat_type="thread" if parent_id else "channel",
            parent_chat_id=parent_id,
            user_id="user",
        )

    def test_exact_beats_parent_and_global(self):
        config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "parent": ChannelOverride(reasoning_effort="low"),
                        "thread": ChannelOverride(reasoning_effort="xhigh"),
                    },
                ),
            },
        )
        runner = self._runner(config, {"enabled": True, "effort": "minimal"})

        assert runner._resolve_session_reasoning_config(
            source=self._discord_source("thread", parent_id="parent"),
            model="gpt-5",
        ) == {"enabled": True, "effort": "xhigh"}

    def test_omitted_inherits_parent(self):
        config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "parent": ChannelOverride(reasoning_effort="medium"),
                        "thread": ChannelOverride(model="thread-model"),
                    },
                ),
            },
        )
        runner = self._runner(config)

        assert runner._resolve_session_reasoning_config(
            source=self._discord_source("thread", parent_id="parent"),
            model="thread-model",
        ) == {"enabled": True, "effort": "medium"}

    def test_false_disables_and_stops_parent_inheritance(self):
        config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "parent": ChannelOverride(reasoning_effort="high"),
                        "thread": ChannelOverride(reasoning_effort=False),
                    },
                ),
            },
        )
        runner = self._runner(config, {"enabled": True, "effort": "low"})

        assert runner._resolve_session_reasoning_config(
            source=self._discord_source("thread", parent_id="parent"),
            model="gpt-5",
        ) == {"enabled": False}

    def test_session_override_beats_channel(self):
        config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "channel": ChannelOverride(reasoning_effort="low"),
                    },
                ),
            },
        )
        runner = self._runner(config)
        session_key = "agent:main:discord:channel:channel"
        runner._session_reasoning_overrides[session_key] = {
            "enabled": True,
            "effort": "ultra",
        }

        assert runner._resolve_session_reasoning_config(
            source=self._discord_source(),
            session_key=session_key,
            model="gpt-5",
        ) == {"enabled": True, "effort": "ultra"}
        assert runner._has_scoped_reasoning_override(
            source=self._discord_source(), session_key=session_key
        ) is True

    def test_omitted_channel_uses_effective_model_reasoning(self, monkeypatch):
        config = GatewayConfig(
            platforms={Platform.DISCORD: PlatformConfig(enabled=True)},
        )
        runner = object.__new__(GatewayRunner)
        runner.config = config
        runner._session_reasoning_overrides = {}
        monkeypatch.setattr(
            "gateway.run._load_gateway_runtime_config",
            lambda: {
                "model": {"default": "global-model"},
                "agent": {
                    "reasoning_effort": "low",
                    "reasoning_overrides": {"session-model": "max"},
                },
            },
        )

        assert runner._resolve_session_reasoning_config(
            source=self._discord_source("unconfigured"),
            model="session-model",
        ) == {"enabled": True, "effort": "max"}

    def test_non_discord_ignores_channel_reasoning(self):
        global_reasoning = {"enabled": True, "effort": "minimal"}
        config = GatewayConfig(
            platforms={
                Platform.TELEGRAM: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "group": ChannelOverride(reasoning_effort="ultra"),
                    },
                ),
            },
        )
        runner = self._runner(config, global_reasoning)
        source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="group",
            chat_type="group",
            user_id="user",
        )

        assert runner._resolve_session_reasoning_config(
            source=source, model="global-model"
        ) is global_reasoning
        assert runner._has_scoped_reasoning_override(source=source) is False


class TestDiscordFallbackOverrides:
    @staticmethod
    def _runner(config):
        runner = object.__new__(GatewayRunner)
        runner.config = config
        return runner

    @staticmethod
    def _source(chat_id="channel", *, parent_id=None, platform=Platform.DISCORD):
        return SessionSource(
            platform=platform,
            chat_id=chat_id,
            chat_type="thread" if parent_id else "channel",
            parent_chat_id=parent_id,
            user_id="user",
        )

    def test_exact_beats_parent_and_global(self):
        exact = [{"provider": "exact", "model": "exact-model"}]
        parent = [{"provider": "parent", "model": "parent-model"}]
        global_chain = [{"provider": "global", "model": "global-model"}]
        config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "parent": ChannelOverride(fallback_providers=parent),
                        "thread": ChannelOverride(fallback_providers=exact),
                    },
                ),
            },
        )

        result = self._runner(config)._resolve_session_fallback_chain(
            source=self._source("thread", parent_id="parent"),
            global_chain=global_chain,
        )
        assert result == exact
        assert result is not exact
        assert result[0] is not exact[0]

    def test_omitted_exact_inherits_parent_then_global(self):
        parent = [{"provider": "parent", "model": "parent-model"}]
        global_chain = [{"provider": "global", "model": "global-model"}]
        config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "parent": ChannelOverride(fallback_providers=parent),
                        "thread": ChannelOverride(model="session-model"),
                    },
                ),
            },
        )
        runner = self._runner(config)

        inherited = runner._resolve_session_fallback_chain(
            source=self._source("thread", parent_id="parent"),
            global_chain=global_chain,
        )
        assert inherited == parent

        global_result = runner._resolve_session_fallback_chain(
            source=self._source("other"),
            global_chain=global_chain,
        )
        assert global_result == global_chain
        assert global_result is not global_chain

    def test_explicit_empty_disables_parent_and_global(self):
        config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "parent": ChannelOverride(
                            fallback_providers=[
                                {"provider": "parent", "model": "parent-model"},
                            ],
                        ),
                        "thread": ChannelOverride(fallback_providers=[]),
                    },
                ),
            },
        )

        assert self._runner(config)._resolve_session_fallback_chain(
            source=self._source("thread", parent_id="parent"),
            global_chain=[{"provider": "global", "model": "global-model"}],
        ) == []

    def test_non_discord_keeps_global_chain(self):
        channel_chain = [{"provider": "channel", "model": "channel-model"}]
        global_chain = [{"provider": "global", "model": "global-model"}]
        config = GatewayConfig(
            platforms={
                Platform.TELEGRAM: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "group": ChannelOverride(fallback_providers=channel_chain),
                    },
                ),
            },
        )

        assert self._runner(config)._resolve_session_fallback_chain(
            source=self._source("group", platform=Platform.TELEGRAM),
            global_chain=global_chain,
        ) == global_chain
