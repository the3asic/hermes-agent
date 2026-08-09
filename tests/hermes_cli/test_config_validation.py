"""Tests for config.yaml structure validation (validate_config_structure)."""

from types import SimpleNamespace

import pytest

from hermes_cli.config import ConfigIssue, config_command, validate_config_structure


class TestCustomProvidersValidation:
    """custom_providers must be a YAML list, not a dict."""

    def test_dict_instead_of_list(self):
        """The exact Discord user scenario — custom_providers as flat dict."""
        issues = validate_config_structure({
            "custom_providers": {
                "name": "Generativelanguage.googleapis.com",
                "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "api_key": "xxx",
                "model": "models/gemini-2.5-flash",
                "rate_limit_delay": 2.0,
                "fallback_model": {
                    "provider": "openrouter",
                    "model": "qwen/qwen3.6-plus:free",
                },
            },
            "fallback_providers": [],
        })
        errors = [i for i in issues if i.severity == "error"]
        assert any("dict" in i.message and "list" in i.message for i in errors), (
            "Should detect custom_providers as dict instead of list"
        )

    def test_dict_detects_misplaced_fields(self):
        """When custom_providers is a dict, detect fields that look misplaced."""
        issues = validate_config_structure({
            "custom_providers": {
                "name": "test",
                "base_url": "https://example.com",
                "api_key": "xxx",
            },
        })
        warnings = [i for i in issues if i.severity == "warning"]
        # Should flag base_url, api_key as looking like custom_providers entry fields
        misplaced = [i for i in warnings if "custom_providers entry fields" in i.message]
        assert len(misplaced) == 1

    def test_dict_detects_nested_fallback(self):
        """When fallback_model gets swallowed into custom_providers dict."""
        issues = validate_config_structure({
            "custom_providers": {
                "name": "test",
                "fallback_model": {"provider": "openrouter", "model": "test"},
            },
        })
        errors = [i for i in issues if i.severity == "error"]
        assert any("fallback_model" in i.message and "inside" in i.message for i in errors)

    def test_valid_list_no_issues(self):
        """Properly formatted custom_providers should produce no issues."""
        issues = validate_config_structure({
            "custom_providers": [
                {"name": "gemini", "base_url": "https://example.com/v1"},
            ],
            "model": {"provider": "custom", "default": "test"},
        })
        assert len(issues) == 0

    def test_list_entry_missing_name(self):
        """List entry without name should warn."""
        issues = validate_config_structure({
            "custom_providers": [{"base_url": "https://example.com/v1"}],
            "model": {"provider": "custom"},
        })
        assert any("missing 'name'" in i.message for i in issues)

    def test_list_entry_missing_base_url(self):
        """List entry without base_url should warn."""
        issues = validate_config_structure({
            "custom_providers": [{"name": "test"}],
            "model": {"provider": "custom"},
        })
        assert any("missing 'base_url'" in i.message for i in issues)

    def test_list_entry_not_dict(self):
        """Non-dict list entries should warn."""
        issues = validate_config_structure({
            "custom_providers": ["not-a-dict"],
            "model": {"provider": "custom"},
        })
        assert any("not a dict" in i.message for i in issues)

    def test_none_custom_providers_no_issues(self):
        """No custom_providers at all should be fine."""
        issues = validate_config_structure({
            "model": {"provider": "openrouter"},
        })
        assert len(issues) == 0


class TestFallbackModelValidation:
    """fallback_model should be a top-level dict with provider + model."""

    def test_missing_provider(self):
        issues = validate_config_structure({
            "fallback_model": {"model": "anthropic/claude-sonnet-4"},
        })
        assert any("missing 'provider'" in i.message for i in issues)

    def test_missing_model(self):
        issues = validate_config_structure({
            "fallback_model": {"provider": "openrouter"},
        })
        assert any("missing 'model'" in i.message for i in issues)

    def test_valid_fallback(self):
        issues = validate_config_structure({
            "fallback_model": {
                "provider": "openrouter",
                "model": "anthropic/claude-sonnet-4",
            },
        })
        # Only fallback-related issues should be absent
        fb_issues = [i for i in issues if "fallback" in i.message.lower()]
        assert len(fb_issues) == 0

    def test_non_dict_fallback(self):
        issues = validate_config_structure({
            "fallback_model": "openrouter:anthropic/claude-sonnet-4",
        })
        assert any("should be a dict" in i.message for i in issues)

    def test_empty_fallback_dict_no_issues(self):
        """Empty fallback_model dict means disabled — no warnings needed."""
        issues = validate_config_structure({
            "fallback_model": {},
        })
        fb_issues = [i for i in issues if "fallback" in i.message.lower()]
        assert len(fb_issues) == 0

    def test_valid_fallback_list(self):
        """List-form fallback_model (chain) should validate when every entry has provider+model."""
        issues = validate_config_structure({
            "fallback_model": [
                {"provider": "openrouter", "model": "anthropic/claude-sonnet-4"},
                {"provider": "anthropic", "model": "claude-sonnet-4-6"},
            ],
        })
        fb_issues = [i for i in issues if "fallback" in i.message.lower()]
        assert len(fb_issues) == 0

    def test_fallback_list_entry_missing_provider(self):
        issues = validate_config_structure({
            "fallback_model": [
                {"provider": "openrouter", "model": "anthropic/claude-sonnet-4"},
                {"model": "claude-sonnet-4-6"},
            ],
        })
        assert any("fallback_model[1]" in i.message and "provider" in i.message for i in issues)

    def test_fallback_list_entry_missing_model(self):
        issues = validate_config_structure({
            "fallback_model": [
                {"provider": "openrouter"},
            ],
        })
        assert any("fallback_model[0]" in i.message and "model" in i.message for i in issues)

    def test_fallback_list_entry_not_a_dict(self):
        issues = validate_config_structure({
            "fallback_model": ["openrouter:anthropic/claude-sonnet-4"],
        })
        assert any("fallback_model[0]" in i.message and "should be a dict" in i.message for i in issues)


class TestChannelOverrideValidation:
    def test_supported_live_shape_has_no_override_issues(self):
        issues = validate_config_structure(
            {
                "platforms": {
                    "discord": {
                        "extra": {"plugin_owned": True},
                        "channel_overrides": {
                            "123": {
                                "model": "gpt-5",
                                "provider": "openai",
                                "system_prompt": "Be concise.",
                                "reasoning_effort": False,
                                "fallback_providers": [
                                    {"provider": "anthropic", "model": "claude-sonnet-4.6"},
                                ],
                            },
                        },
                    },
                },
            }
        )
        assert not [i for i in issues if "channel_overrides" in i.message]

    def test_unknown_key_reports_precise_path_as_error(self):
        issues = validate_config_structure(
            {
                "platforms": {
                    "discord": {
                        "channel_overrides": {"123": {"reasoning": "high"}},
                    },
                },
            }
        )
        assert any(
            issue.severity == "error"
            and "platforms.discord.channel_overrides.123.reasoning" in issue.message
            and "unknown" in issue.message.lower()
            for issue in issues
        )

    @pytest.mark.parametrize(
        ("override", "path"),
        [
            ("not-a-mapping", "platforms.discord.channel_overrides.123"),
            ({"model": ["gpt-5"]}, "platforms.discord.channel_overrides.123.model"),
            ({"reasoning_effort": True}, "platforms.discord.channel_overrides.123.reasoning_effort"),
            ({"fallback_providers": {}}, "platforms.discord.channel_overrides.123.fallback_providers"),
            (
                {"fallback_providers": ["openrouter:gpt-5"]},
                "platforms.discord.channel_overrides.123.fallback_providers[0]",
            ),
            (
                {"fallback_providers": [{"provider": "openrouter"}]},
                "platforms.discord.channel_overrides.123.fallback_providers[0].model",
            ),
        ],
    )
    def test_malformed_fields_report_actionable_paths(self, override, path):
        issues = validate_config_structure(
            {
                "platforms": {
                    "discord": {"channel_overrides": {"123": override}},
                },
            }
        )
        assert any(path in issue.message for issue in issues)

    def test_invalid_reasoning_value_warns_with_allowed_values(self):
        issues = validate_config_structure(
            {
                "platforms": {
                    "discord": {
                        "channel_overrides": {
                            "123": {"reasoning_effort": "turbo"},
                        },
                    },
                },
            }
        )
        issue = next(i for i in issues if ".reasoning_effort" in i.message)
        assert issue.severity == "warning"
        assert "minimal" in issue.hint
        assert "false" in issue.hint

    def test_unrelated_platform_extra_keys_are_not_rejected(self):
        issues = validate_config_structure(
            {
                "platforms": {
                    "discord": {
                        "extra": {
                            "arbitrary_plugin_setting": {"nested": True},
                        },
                    },
                },
            }
        )
        assert not [i for i in issues if "arbitrary_plugin_setting" in i.message]

    def test_config_check_reads_yaml_reports_path_and_fails(self, tmp_path, monkeypatch, capsys):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "platforms:\n"
            "  discord:\n"
            "    channel_overrides:\n"
            "      '123':\n"
            "        mystery_field: true\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setattr("hermes_cli.config.REQUIRED_ENV_VARS", {})
        monkeypatch.setattr("hermes_cli.config.OPTIONAL_ENV_VARS", {})
        monkeypatch.setattr("hermes_cli.config.check_config_version", lambda: (1, 1))
        monkeypatch.setattr("hermes_cli.config.get_missing_config_fields", lambda: [])

        with pytest.raises(SystemExit) as exc:
            config_command(SimpleNamespace(config_command="check"))

        assert exc.value.code == 1
        output = capsys.readouterr().out
        assert "platforms.discord.channel_overrides.123.mystery_field" in output
        assert "model, provider, system_prompt, reasoning_effort, fallback_providers" in output


class TestMissingModelSection:
    """Warn when custom_providers exists but model section is missing."""

    def test_custom_providers_without_model(self):
        issues = validate_config_structure({
            "custom_providers": [
                {"name": "test", "base_url": "https://example.com/v1"},
            ],
        })
        assert any("no 'model' section" in i.message for i in issues)

    def test_custom_providers_with_model(self):
        issues = validate_config_structure({
            "custom_providers": [
                {"name": "test", "base_url": "https://example.com/v1"},
            ],
            "model": {"provider": "custom", "default": "test-model"},
        })
        # Should not warn about missing model section
        assert not any("no 'model' section" in i.message for i in issues)


class TestConfigIssueDataclass:
    """ConfigIssue should be a proper dataclass."""

    def test_fields(self):
        issue = ConfigIssue(severity="error", message="test msg", hint="test hint")
        assert issue.severity == "error"
        assert issue.message == "test msg"
        assert issue.hint == "test hint"

    def test_equality(self):
        a = ConfigIssue("error", "msg", "hint")
        b = ConfigIssue("error", "msg", "hint")
        assert a == b
