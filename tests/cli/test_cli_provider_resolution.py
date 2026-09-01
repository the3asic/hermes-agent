import importlib
import sys
import types
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from hermes_cli.auth import AuthError
from hermes_cli import main as hermes_main


# ---------------------------------------------------------------------------
# Module isolation: _import_cli() wipes tools.* / cli / run_agent from
# sys.modules so it can re-import cli fresh.  Without cleanup the wiped
# modules leak into subsequent tests, breaking
# mock patches that target "tools.file_tools._get_file_ops" etc.
# ---------------------------------------------------------------------------

def _reset_modules(prefixes: tuple[str, ...]):
    for name in list(sys.modules):
        if any(name == p or name.startswith(p + ".") for p in prefixes):
            sys.modules.pop(name, None)


@pytest.fixture(autouse=True)
def _restore_cli_and_tool_modules():
    """Save and restore tools/cli/run_agent modules around every test."""
    prefixes = ("tools", "cli", "run_agent")
    original_modules = {
        name: module
        for name, module in sys.modules.items()
        if any(name == p or name.startswith(p + ".") for p in prefixes)
    }
    try:
        yield
    finally:
        _reset_modules(prefixes)
        sys.modules.update(original_modules)


def _install_prompt_toolkit_stubs():
    class _Dummy:
        def __init__(self, *args, **kwargs):
            pass

    class _Condition:
        def __init__(self, func):
            self.func = func

        def __bool__(self):
            return bool(self.func())

    class _ANSI(str):
        pass

    root = types.ModuleType("prompt_toolkit")
    history = types.ModuleType("prompt_toolkit.history")
    styles = types.ModuleType("prompt_toolkit.styles")
    patch_stdout = types.ModuleType("prompt_toolkit.patch_stdout")
    application = types.ModuleType("prompt_toolkit.application")
    layout = types.ModuleType("prompt_toolkit.layout")
    processors = types.ModuleType("prompt_toolkit.layout.processors")
    filters = types.ModuleType("prompt_toolkit.filters")
    dimension = types.ModuleType("prompt_toolkit.layout.dimension")
    menus = types.ModuleType("prompt_toolkit.layout.menus")
    widgets = types.ModuleType("prompt_toolkit.widgets")
    key_binding = types.ModuleType("prompt_toolkit.key_binding")
    completion = types.ModuleType("prompt_toolkit.completion")
    formatted_text = types.ModuleType("prompt_toolkit.formatted_text")

    history.FileHistory = _Dummy
    styles.Style = _Dummy
    patch_stdout.patch_stdout = lambda *args, **kwargs: nullcontext()
    application.Application = _Dummy
    layout.Layout = _Dummy
    layout.HSplit = _Dummy
    layout.Window = _Dummy
    layout.FormattedTextControl = _Dummy
    layout.ConditionalContainer = _Dummy
    processors.Processor = _Dummy
    processors.Transformation = _Dummy
    processors.PasswordProcessor = _Dummy
    processors.ConditionalProcessor = _Dummy
    filters.Condition = _Condition
    dimension.Dimension = _Dummy
    menus.CompletionsMenu = _Dummy
    widgets.TextArea = _Dummy
    key_binding.KeyBindings = _Dummy
    completion.Completer = _Dummy
    completion.Completion = _Dummy
    formatted_text.ANSI = _ANSI
    root.print_formatted_text = lambda *args, **kwargs: None

    sys.modules.setdefault("prompt_toolkit", root)
    sys.modules.setdefault("prompt_toolkit.history", history)
    sys.modules.setdefault("prompt_toolkit.styles", styles)
    sys.modules.setdefault("prompt_toolkit.patch_stdout", patch_stdout)
    sys.modules.setdefault("prompt_toolkit.application", application)
    sys.modules.setdefault("prompt_toolkit.layout", layout)
    sys.modules.setdefault("prompt_toolkit.layout.processors", processors)
    sys.modules.setdefault("prompt_toolkit.filters", filters)
    sys.modules.setdefault("prompt_toolkit.layout.dimension", dimension)
    sys.modules.setdefault("prompt_toolkit.layout.menus", menus)
    sys.modules.setdefault("prompt_toolkit.widgets", widgets)
    sys.modules.setdefault("prompt_toolkit.key_binding", key_binding)
    sys.modules.setdefault("prompt_toolkit.completion", completion)
    sys.modules.setdefault("prompt_toolkit.formatted_text", formatted_text)


def _import_cli():
    for name in list(sys.modules):
        if name == "cli" or name == "run_agent" or name == "tools" or name.startswith("tools."):
            sys.modules.pop(name, None)

    if "firecrawl" not in sys.modules:
        sys.modules["firecrawl"] = types.SimpleNamespace(Firecrawl=object)

    try:
        importlib.import_module("prompt_toolkit")
    except ModuleNotFoundError:
        _install_prompt_toolkit_stubs()
    return importlib.import_module("cli")


def test_provider_flag_uses_named_custom_default_model(monkeypatch):
    """`--provider <custom>` without `-m` uses that entry's default_model (#86978)."""
    cli = _import_cli()
    monkeypatch.setitem(
        cli.CLI_CONFIG,
        "model",
        {"default": "tencent/hy3:free", "provider": "nous"},
    )
    config = {
        "model": {"default": "tencent/hy3:free", "provider": "nous"},
        "providers": {
            "gmk-lan": {
                "name": "GMK Local",
                "base_url": "http://gmk.lan:9931/v1",
                "api_key": "not-needed",
                "default_model": "/models/gemma.gguf",
            }
        },
    }
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: config)
    monkeypatch.setattr("hermes_cli.runtime_provider.load_config", lambda: config)

    shell = cli.HermesCLI(provider="gmk-lan", compact=True, max_turns=1)

    assert shell.model == "/models/gemma.gguf"
    assert shell.requested_provider == "gmk-lan"


def test_explicit_model_wins_over_provider_default_model(monkeypatch):
    """`-m` still wins when `--provider` also names a custom default_model."""
    cli = _import_cli()
    monkeypatch.setitem(
        cli.CLI_CONFIG,
        "model",
        {"default": "tencent/hy3:free", "provider": "nous"},
    )
    config = {
        "model": {"default": "tencent/hy3:free", "provider": "nous"},
        "providers": {
            "gmk-lan": {
                "name": "GMK Local",
                "base_url": "http://gmk.lan:9931/v1",
                "api_key": "not-needed",
                "default_model": "/models/gemma.gguf",
            }
        },
    }
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: config)
    monkeypatch.setattr("hermes_cli.runtime_provider.load_config", lambda: config)

    shell = cli.HermesCLI(
        provider="gmk-lan",
        model="explicit-id",
        compact=True,
        max_turns=1,
    )

    assert shell.model == "explicit-id"


def test_provider_flag_logs_when_custom_default_model_cannot_resolve(monkeypatch, caplog):
    """A named --provider that fails to resolve must not fail silently."""
    cli = _import_cli()
    monkeypatch.setitem(
        cli.CLI_CONFIG,
        "model",
        {"default": "tencent/hy3:free", "provider": "nous"},
    )

    def _boom(_name):
        raise RuntimeError("catalog unavailable")

    monkeypatch.setattr(
        "hermes_cli.runtime_provider._get_named_custom_provider",
        _boom,
    )

    with caplog.at_level("WARNING"):
        shell = cli.HermesCLI(provider="gmk-lan", compact=True, max_turns=1)

    assert shell.model == "tencent/hy3:free"
    assert any(
        "gmk-lan" in rec.getMessage() and "catalog unavailable" in rec.getMessage()
        for rec in caplog.records
    )


def test_hermes_cli_init_does_not_eagerly_resolve_runtime_provider(monkeypatch):
    cli = _import_cli()
    calls = {"count": 0}

    def _unexpected_runtime_resolve(**kwargs):
        calls["count"] += 1
        raise AssertionError("resolve_runtime_provider should not be called in HermesCLI.__init__")

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", _unexpected_runtime_resolve)
    monkeypatch.setattr("hermes_cli.runtime_provider.format_runtime_provider_error", lambda exc: str(exc))

    shell = cli.HermesCLI(model="gpt-5", compact=True, max_turns=1)

    assert shell is not None
    assert calls["count"] == 0


def test_runtime_resolution_failure_is_not_sticky(monkeypatch):
    cli = _import_cli()
    calls = {"count": 0}

    def _runtime_resolve(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("temporary auth failure")
        return {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "test-key",
            "source": "env/config",
        }

    class _DummyAgent:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", _runtime_resolve)
    monkeypatch.setattr("hermes_cli.runtime_provider.format_runtime_provider_error", lambda exc: str(exc))
    monkeypatch.setattr(cli, "AIAgent", _DummyAgent)

    shell = cli.HermesCLI(model="gpt-5", compact=True, max_turns=1)

    assert shell._init_agent() is False
    assert shell._init_agent() is True
    assert calls["count"] == 2
    assert shell.agent is not None




def test_cli_turn_routing_uses_primary_when_disabled(monkeypatch):
    cli = _import_cli()
    shell = cli.HermesCLI(model="gpt-5", compact=True, max_turns=1)
    shell.provider = "openrouter"
    shell.api_mode = "chat_completions"
    shell.base_url = "https://openrouter.ai/api/v1"
    shell.api_key = "sk-primary"

    result = shell._resolve_turn_agent_config("what time is it in tokyo?")

    assert result["model"] == "gpt-5"
    assert result["runtime"]["provider"] == "openrouter"












def test_model_flow_nous_does_not_restore_stale_custom_api_key(tmp_path, monkeypatch):
    import yaml

    config_home = tmp_path / "hermes"
    config_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(config_home))

    config_path = config_home / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "model": {
                    "provider": "custom",
                    "default": "glm-5.2",
                    "base_url": "https://api.neuralwatt.com/v1",
                    "api_key": "${NEURALWATT_API_KEY}",
                    "api_mode": "chat_completions",
                }
            },
            sort_keys=False,
        )
    )

    stale_config = yaml.safe_load(config_path.read_text()) or {}
    selected_model = "deepseek/deepseek-v4-flash"

    monkeypatch.setattr(
        "hermes_cli.auth.get_provider_auth_state",
        lambda provider: {
            "access_token": "nous-token",
            "portal_base_url": "https://portal.example.com",
        },
    )
    monkeypatch.setattr(
        "hermes_cli.auth.resolve_nous_runtime_credentials",
        lambda *args, **kwargs: {
            "base_url": "https://inference-api.nousresearch.com/v1",
            "api_key": "nous-key",
        },
    )
    monkeypatch.setattr(
        "hermes_cli.models.get_curated_nous_model_ids",
        lambda: [selected_model],
    )
    monkeypatch.setattr("hermes_cli.models.get_pricing_for_provider", lambda provider: {})
    monkeypatch.setattr("hermes_cli.models.check_nous_free_tier", lambda **kwargs: False)
    monkeypatch.setattr(
        "hermes_cli.models.union_with_portal_paid_recommendations",
        lambda model_ids, pricing, portal_url: (model_ids, pricing),
    )
    monkeypatch.setattr(
        "hermes_cli.auth._prompt_model_selection",
        lambda *args, **kwargs: selected_model,
    )
    monkeypatch.setattr(
        "hermes_cli.nous_subscription.prompt_enable_tool_gateway",
        lambda config: None,
    )

    hermes_main._model_flow_nous(stale_config, current_model="glm-5.2")

    config = yaml.safe_load(config_path.read_text()) or {}
    model = config.get("model")
    assert model["provider"] == "nous"
    assert model["default"] == selected_model
    assert model["base_url"] == "https://inference-api.nousresearch.com/v1"
    assert "api_key" not in model
    assert "api_mode" not in model


def _seed_stale_custom_model(tmp_path, monkeypatch):
    import yaml

    config_home = tmp_path / "hermes"
    config_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(config_home))
    config_path = config_home / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "model": {
                    "provider": "custom",
                    "default": "glm-5.2",
                    "base_url": "https://api.neuralwatt.com/v1",
                    "api_key": "${NEURALWATT_API_KEY}",
                    "api": "legacy-stale-key",
                    "api_mode": "anthropic_messages",
                }
            },
            sort_keys=False,
        )
    )
    (config_home / ".env").write_text("")
    return config_path








def test_codex_provider_uses_config_model(monkeypatch):
    """Model comes from config.yaml, not LLM_MODEL env var.
    Config.yaml is the single source of truth to avoid multi-agent conflicts."""
    cli = _import_cli()

    # LLM_MODEL env var should be IGNORED (even if set)
    monkeypatch.setenv("LLM_MODEL", "should-be-ignored")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    # Set model via config
    monkeypatch.setitem(cli.CLI_CONFIG, "model", {
        "default": "gpt-5.2-codex",
        "provider": "openai-codex",
        "base_url": "https://chatgpt.com/backend-api/codex",
    })

    def _runtime_resolve(**kwargs):
        return {
            "provider": "openai-codex",
            "api_mode": "codex_responses",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "fake-codex-token",
            "source": "env/config",
        }

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", _runtime_resolve)
    monkeypatch.setattr("hermes_cli.runtime_provider.format_runtime_provider_error", lambda exc: str(exc))
    # Prevent live API call from overriding the config model
    monkeypatch.setattr(
        "hermes_cli.codex_models.get_codex_model_ids",
        lambda access_token=None: ["gpt-5.2-codex"],
    )

    shell = cli.HermesCLI(compact=True, max_turns=1)

    assert shell._ensure_runtime_credentials() is True
    assert shell.provider == "openai-codex"
    # Model from config (may be normalized by codex provider logic)
    assert "codex" in shell.model.lower()
    # LLM_MODEL env var is NOT used
    assert shell.model != "should-be-ignored"


def test_cli_auth_fallback_reasoning_is_target_owned_across_agent_init(
    monkeypatch,
):
    """A second credential refresh must not turn auth fallback into primary.

    The CLI calls credential resolution before building the route and again in
    ``_init_agent``.  Preserve the matched entry across both calls, ignore the
    CLI's primary ``--reasoning`` pin, and persist the entry on the new agent.
    """
    cli = _import_cli()
    from unittest.mock import MagicMock

    from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin

    class _Shell(CLIAgentSetupMixin):
        def _normalize_model_for_provider(self, _provider):
            return False

    shell = _Shell()
    shell.requested_provider = "openai-codex"
    shell.provider = "openai-codex"
    shell.model = "gpt-5.6-sol"
    shell._explicit_api_key = None
    shell._explicit_base_url = None
    shell.api_key = "old-key"
    shell.base_url = "https://primary.example/v1"
    shell.api_mode = "codex_responses"
    shell.acp_command = None
    shell.acp_args = []
    shell._credential_pool = None
    shell._provider_source = None
    shell.agent = None
    shell._active_agent_route_signature = None
    shell.reasoning_config = {"enabled": True, "effort": "low"}
    fallback_entry = {
        "provider": "custom:glm",
        "model": "glm-5.3",
        "reasoning_effort": "max",
    }
    shell._fallback_model = [fallback_entry]
    resolver_calls = []

    def _resolve_runtime(**kwargs):
        resolver_calls.append(dict(kwargs))
        if kwargs.get("requested") == "openai-codex":
            raise AuthError("primary unavailable")
        uses_fallback_shape = kwargs.get("target_model") == "glm-5.3"
        return {
            "provider": "custom",
            "requested_provider": "custom:glm",
            "api_key": "fallback-key",
            "base_url": (
                "https://glm.example/v1/messages"
                if uses_fallback_shape
                else "https://glm.example/v1/chat/completions"
            ),
            "api_mode": (
                "anthropic_messages" if uses_fallback_shape else "chat_completions"
            ),
            "command": None,
            "args": [],
            "credential_pool": None,
            "source": "test",
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        _resolve_runtime,
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.format_runtime_provider_error",
        lambda exc: str(exc),
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "agent": {
                "reasoning_effort": "medium",
                "reasoning_overrides": {"glm-5.3": "high"},
            }
        },
    )
    monkeypatch.setattr(cli, "_cprint", lambda *_args, **_kwargs: None)

    assert shell._ensure_runtime_credentials() is True
    # Exercise the CLI's second refresh: it now resolves the fallback provider
    # directly, but the original entry must remain the route owner.
    assert shell._ensure_runtime_credentials() is True
    fallback_calls = [
        call for call in resolver_calls
        if call.get("requested") == "custom:glm"
    ]
    assert [call.get("target_model") for call in fallback_calls] == [
        "glm-5.3",
        "glm-5.3",
    ]
    assert shell.api_mode == "anthropic_messages"
    assert shell.base_url == "https://glm.example/v1/messages"
    route = shell._resolve_turn_agent_config("hello")
    assert route["reasoning_config"] == {"enabled": True, "effort": "max"}
    assert route["fallback_entry"] == fallback_entry

    shell.finalize_preloaded_skills = lambda: None
    shell._install_tool_callbacks = lambda: None
    shell._ensure_tirith_security = lambda: None
    shell._session_db = MagicMock()
    shell._resumed = False
    shell.conversation_history = []
    shell._single_query_mode = True
    shell.max_tokens = None
    shell.max_turns = 1
    shell.run_budget_seconds = None
    shell.enabled_toolsets = None
    shell.disabled_toolsets = None
    shell.verbose = False
    shell.tool_progress_mode = "off"
    shell.system_prompt = None
    shell.prefill_messages = None
    shell.service_tier = None
    shell._providers_only = None
    shell._providers_ignore = None
    shell._providers_order = None
    shell._provider_sort = None
    shell._provider_require_params = False
    shell._provider_data_collection = None
    shell._openrouter_min_coding_score = None
    shell.session_id = "cli-fallback-test"
    shell._clarify_callback = None
    shell._current_reasoning_callback = lambda: None
    shell._on_thinking = None
    shell.checkpoints_enabled = False
    shell.checkpoint_max_snapshots = 20
    shell.checkpoint_max_total_size_mb = 500
    shell.checkpoint_max_file_size_mb = 10
    shell.pass_session_id = False
    shell.ignore_rules = True
    shell._on_tool_progress = None
    shell._on_tool_start = None
    shell._on_tool_complete = None
    shell._inline_diffs_enabled = False
    shell.streaming_enabled = False
    shell._stream_delta = None
    shell._on_tool_gen_start = None
    shell._on_notice = None
    shell._on_notice_clear = None
    shell._on_reaction = None
    shell._pending_title = None

    captured = {}

    def _fake_agent(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            _primary_runtime={},
            _runtime_reasoning_entry=None,
            _print_fn=None,
        )

    monkeypatch.setattr(cli, "AIAgent", _fake_agent)
    monkeypatch.setattr(
        "hermes_cli.mcp_startup.ensure_mcp_discovery_before_agent_build",
        lambda **_kwargs: None,
    )

    assert shell._init_agent(
        model_override=route["model"],
        runtime_override=route["runtime"],
        request_overrides=route.get("request_overrides"),
    ) is True
    assert captured["reasoning_config"] == {"enabled": True, "effort": "max"}
    assert shell.agent._runtime_reasoning_entry == fallback_entry
    assert shell.agent._primary_runtime["reasoning_policy_entry"] == fallback_entry
    assert shell.agent._primary_runtime["reasoning_config"] == {
        "enabled": True,
        "effort": "max",
    }










def test_model_flow_custom_saves_verified_v1_base_url(monkeypatch, capsys):
    monkeypatch.setattr(
        "hermes_cli.config.get_env_value",
        lambda key: "" if key in {"OPENAI_BASE_URL", "OPENAI_API_KEY"} else "",
    )
    saved_env = {}
    monkeypatch.setattr("hermes_cli.config.save_env_value", lambda key, value: saved_env.__setitem__(key, value))
    monkeypatch.setattr("hermes_cli.auth._save_model_choice", lambda model: saved_env.__setitem__("MODEL", model))
    monkeypatch.setattr("hermes_cli.auth.deactivate_provider", lambda: None)
    monkeypatch.setattr("hermes_cli.main._save_custom_provider", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "hermes_cli.models.probe_api_models",
        lambda api_key, base_url: {
            "models": ["llm"],
            "probed_url": "http://localhost:8000/v1/models",
            "resolved_base_url": "http://localhost:8000/v1",
            "suggested_base_url": "http://localhost:8000/v1",
            "used_fallback": True,
        },
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"model": {"default": "", "provider": "custom", "base_url": ""}},
    )
    monkeypatch.setattr("hermes_cli.config.save_config", lambda cfg: None)

    # After the probe detects a single model ("llm"), the flow asks
    # "Use this model? [Y/n]:" — confirm with Enter, then context length,
    # then display name. The api_mode prompt also runs before model selection.
    answers = iter(["http://localhost:8000", "local-key", "", "", "", "", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    monkeypatch.setattr("hermes_cli.secret_prompt.masked_secret_prompt", lambda _prompt="": next(answers))

    hermes_main._model_flow_custom({})
    output = capsys.readouterr().out

    assert "Saving the working base URL instead" in output
    assert "Detected model: llm" in output
    # OPENAI_BASE_URL is no longer saved to .env — config.yaml is authoritative
    assert "OPENAI_BASE_URL" not in saved_env
    assert saved_env["MODEL"] == "llm"


def test_model_flow_custom_persists_selected_api_mode(monkeypatch):
    saved_cfg = {"model": {"default": "", "provider": "custom", "base_url": ""}}
    captured_provider = {}

    monkeypatch.setattr(
        "hermes_cli.config.get_env_value",
        lambda key: "" if key in {"OPENAI_BASE_URL", "OPENAI_API_KEY"} else "",
    )
    monkeypatch.setattr("hermes_cli.auth._save_model_choice", lambda model: None)
    monkeypatch.setattr("hermes_cli.auth.deactivate_provider", lambda: None)
    monkeypatch.setattr(
        "hermes_cli.models.probe_api_models",
        lambda api_key, base_url: {
            "models": [],
            "probed_url": f"{base_url.rstrip('/')}/models",
            "resolved_base_url": None,
            "suggested_base_url": None,
            "used_fallback": False,
        },
    )
    saved_env = {}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: saved_cfg)
    monkeypatch.setattr("hermes_cli.config.save_config", lambda cfg: saved_cfg.update(cfg))
    monkeypatch.setattr(
        "hermes_cli.config.save_env_value",
        lambda key, value: saved_env.__setitem__(key, value),
    )
    monkeypatch.setattr(
        "hermes_cli.main._save_custom_provider",
        lambda base_url, api_key="", model="", context_length=None, name=None, api_mode=None, key_env="": captured_provider.update(
            {
                "base_url": base_url,
                "api_key": api_key,
                "model": model,
                "context_length": context_length,
                "name": name,
                "api_mode": api_mode,
                "key_env": key_env,
            }
        ),
    )

    answers = iter(
        [
            "https://codex.example.com/v1",
            "3",
            "chosen-model",
            "",
            "",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    monkeypatch.setattr("hermes_cli.secret_prompt.masked_secret_prompt", lambda _prompt="": "test-key")

    hermes_main._model_flow_custom({"model": {"provider": "custom"}})

    assert saved_cfg["model"]["provider"] == "custom"
    assert saved_cfg["model"]["base_url"] == "https://codex.example.com/v1"
    assert saved_cfg["model"]["api_mode"] == "codex_responses"
    assert captured_provider["api_mode"] == "codex_responses"

    # The key itself goes to .env; config.yaml only references it (#69449).
    key_env = captured_provider["key_env"]
    assert saved_cfg["model"]["api_key"] == f"${{{key_env}}}"
    assert saved_env[key_env] == "test-key"


def test_cmd_model_forwards_nous_login_tls_options(monkeypatch):
    monkeypatch.setattr(hermes_main, "_require_tty", lambda *a: None)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"model": {"default": "gpt-5", "provider": "nous"}},
    )
    monkeypatch.setattr("hermes_cli.config.save_config", lambda cfg: None)
    monkeypatch.setattr("hermes_cli.config.get_env_value", lambda key: "")
    monkeypatch.setattr("hermes_cli.config.save_env_value", lambda key, value: None)
    monkeypatch.setattr("hermes_cli.auth.resolve_provider", lambda requested, **kwargs: "nous")
    monkeypatch.setattr("hermes_cli.auth.get_provider_auth_state", lambda provider_id: None)
    monkeypatch.setattr(hermes_main, "_prompt_provider_choice", lambda choices, **kwargs: 0)

    captured = {}

    def _fake_login(login_args, provider_config):
        captured["portal_url"] = login_args.portal_url
        captured["inference_url"] = login_args.inference_url
        captured["client_id"] = login_args.client_id
        captured["scope"] = login_args.scope
        captured["no_browser"] = login_args.no_browser
        captured["timeout"] = login_args.timeout
        captured["ca_bundle"] = login_args.ca_bundle
        captured["insecure"] = login_args.insecure

    monkeypatch.setattr("hermes_cli.auth._login_nous", _fake_login)

    hermes_main.cmd_model(
        SimpleNamespace(
            portal_url="https://portal.nousresearch.com",
            inference_url="https://inference.nousresearch.com/v1",
            client_id="hermes-local",
            scope="openid profile",
            no_browser=True,
            timeout=7.5,
            ca_bundle="/tmp/local-ca.pem",
            insecure=True,
        )
    )

    assert captured == {
        "portal_url": "https://portal.nousresearch.com",
        "inference_url": "https://inference.nousresearch.com/v1",
        "client_id": "hermes-local",
        "scope": "openid profile",
        "no_browser": True,
        "timeout": 7.5,
        "ca_bundle": "/tmp/local-ca.pem",
        "insecure": True,
    }


# ---------------------------------------------------------------------------
# _auto_provider_name — unit tests
# ---------------------------------------------------------------------------

def test_auto_provider_name_localhost():
    from hermes_cli.main import _auto_provider_name
    assert _auto_provider_name("http://localhost:11434/v1") == "Local (localhost:11434)"
    assert _auto_provider_name("http://127.0.0.1:1234/v1") == "Local (127.0.0.1:1234)"






def test_save_custom_provider_uses_provided_name(monkeypatch, tmp_path):
    """When a display name is passed, it should appear in the saved entry."""
    import yaml
    from hermes_cli.main import _save_custom_provider

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump({}))

    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda: yaml.safe_load(cfg_path.read_text()) or {},
    )
    saved = {}
    def _save(cfg):
        saved.update(cfg)
    monkeypatch.setattr("hermes_cli.config.save_config", _save)

    _save_custom_provider("http://localhost:11434/v1", name="Ollama")
    entries = saved.get("custom_providers", [])
    assert len(entries) == 1
    assert entries[0]["name"] == "Ollama"


def test_save_custom_provider_references_the_key_instead_of_inlining_it(monkeypatch, tmp_path):
    """With key_env set the entry must not carry the secret (#69449)."""
    import yaml
    from hermes_cli.main import _save_custom_provider

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump({}))
    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda: yaml.safe_load(cfg_path.read_text()) or {},
    )
    saved = {}
    monkeypatch.setattr("hermes_cli.config.save_config", lambda cfg: saved.update(cfg))

    _save_custom_provider(
        "http://localhost:11434/v1",
        api_key="sk-secret",
        name="Ollama",
        key_env="HERMES_CUSTOM_LOCALHOST_11434_API_KEY",
    )

    entry = saved["custom_providers"][0]
    assert entry["key_env"] == "HERMES_CUSTOM_LOCALHOST_11434_API_KEY"
    assert "api_key" not in entry
    assert "sk-secret" not in yaml.safe_dump(saved)




def test_custom_endpoint_key_env_is_a_valid_posix_name_for_ip_endpoints():
    """Every IP-based local endpoint slugs to a digit-leading name.

    ``save_env_value`` rejects names that don't match
    ``[A-Za-z_][A-Za-z0-9_]*``, so deriving ``127_0_0_1_8080_API_KEY`` would
    raise on exactly the local-proxy setups this is meant to protect. The
    fixed prefix makes the result valid by construction.
    """
    import re

    from hermes_cli.config import _ENV_VAR_NAME_RE, custom_endpoint_key_env

    for identity in ("127.0.0.1_8080", "0.0.0.0", "10.0.0.7:11434", "", "-–-"):
        assert _ENV_VAR_NAME_RE.match(custom_endpoint_key_env(identity)), identity
