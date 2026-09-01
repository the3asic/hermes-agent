"""Unit tests for gateway.runtime_footer — the opt-in runtime-metadata footer
appended to final gateway replies."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from gateway.runtime_footer import (
    _format_token_count,
    _home_relative_cwd,
    _model_short,
    build_footer_line,
    format_runtime_footer,
    resolved_reasoning_effort,
    resolve_footer_config,
    turn_counter_delta,
)


# ---------------------------------------------------------------------------
# _model_short + _home_relative_cwd
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "model,expected",
    [
        ("openai/gpt-5.4", "gpt-5.4"),
        ("anthropic/claude-sonnet-4.6", "claude-sonnet-4.6"),
        ("gpt-5.4", "gpt-5.4"),
        ("", ""),
        (None, ""),
    ],
)
def test_model_short_drops_vendor_prefix(model, expected):
    assert _model_short(model) == expected


def test_home_relative_cwd_collapses_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    sub = tmp_path / "projects" / "hermes"
    sub.mkdir(parents=True)
    result = _home_relative_cwd(str(sub))
    assert result == "~/projects/hermes"


# ---------------------------------------------------------------------------
# format_runtime_footer
# ---------------------------------------------------------------------------

def test_format_footer_all_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path / "projects" / "hermes"))
    (tmp_path / "projects" / "hermes").mkdir(parents=True)
    out = format_runtime_footer(
        model="openrouter/openai/gpt-5.4",
        context_tokens=68000,
        context_length=100000,
        cwd=None,  # falls back to TERMINAL_CWD env var
        fields=("model", "context_pct", "cwd"),
    )
    assert out == "gpt-5.4 · 68% · ~/projects/hermes"


def test_format_footer_skips_missing_context_length():
    out = format_runtime_footer(
        model="openai/gpt-5.4",
        context_tokens=500,
        context_length=None,
        cwd="/tmp/wd",
        fields=("model", "context_pct", "cwd"),
    )
    # context_pct dropped silently; no "?%" artifact
    assert "%" not in out
    assert "gpt-5.4" in out
    assert "/tmp/wd" in out


def test_format_footer_context_window_shows_absolute_last_call_state():
    out = format_runtime_footer(
        model="glm-5.3",
        context_tokens=122_971,
        context_length=1_000_000,
        fields=("context_window",),
    )
    assert out == "ctx(last):123.0k/1.0M (12%)"


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "0"),
        (999, "999"),
        (1_000, "1.0k"),
        (15_932, "15.9k"),
        (1_000_000, "1.0M"),
    ],
)
def test_format_token_count(value, expected):
    assert _format_token_count(value) == expected


@pytest.mark.parametrize(
    "current,baseline,expected",
    [
        (15_000, 10_000, 5_000),
        (500, 800, None),  # counter rollback is an accounting anomaly
        (0, 0, 0),
        ("12", "5", 7),
        (None, 5, None),
        (-1, 0, None),
    ],
)
def test_turn_counter_delta(current, baseline, expected):
    assert turn_counter_delta(current, baseline) == expected


@pytest.mark.parametrize(
    "config,expected",
    [
        ({"enabled": True, "effort": "MAX"}, "max"),
        ({"enabled": False, "effort": "max"}, "none"),
        ({"enabled": True}, "default"),
        (None, "default"),
    ],
)
def test_resolved_reasoning_effort(config, expected):
    assert resolved_reasoning_effort(config) == expected


def test_gateway_turn_metadata_uses_final_model_and_reported_turn_delta():
    from gateway.run import _gateway_turn_runtime_metadata

    agent = SimpleNamespace(
        model="fallback-model",
        reasoning_config={"enabled": True, "effort": "high"},
        session_prompt_tokens=12_500,
        session_input_tokens=7_500,
        session_completion_tokens=900,
        session_cache_read_tokens=5_000,
        session_cache_write_tokens=0,
        session_api_calls=3,
        session_usage_report_calls=3,
        context_compressor=SimpleNamespace(
            last_prompt_tokens=50_000,
            context_length=1_000_000,
        ),
    )
    metadata = _gateway_turn_runtime_metadata(
        agent,
        uncached_input_tokens_start=5_000,
        completion_tokens_start=500,
        cache_read_tokens_start=0,
        cache_write_tokens_start=0,
        usage_report_calls_start=2,
        result_api_calls=1,
    )

    assert metadata == {
        "last_prompt_tokens": 50_000,
        "input_tokens": 12_500,
        "uncached_input_tokens": 7_500,
        "output_tokens": 900,
        "cache_read_tokens": 5_000,
        "cache_write_tokens": 0,
        "usage_report_calls": 3,
        "turn_input_tokens": 2_500,
        "turn_output_tokens": 400,
        "turn_cache_read_tokens": 5_000,
        "turn_cache_write_tokens": 0,
        "token_usage_status": "reported",
        "reasoning_effort": "high",
        "model": "fallback-model",
        "model_last": "fallback-model",
        "context_length": 1_000_000,
    }


def test_cache_heavy_turn_does_not_report_reused_context_as_new_input():
    from gateway.run import _gateway_turn_runtime_metadata

    agent = SimpleNamespace(
        model="glm-5.3",
        reasoning_config={"enabled": True, "effort": "max"},
        session_prompt_tokens=953_867,
        session_input_tokens=38_731,
        session_completion_tokens=3_446,
        session_cache_read_tokens=915_136,
        session_cache_write_tokens=0,
        session_api_calls=8,
        session_usage_report_calls=8,
        context_compressor=SimpleNamespace(
            last_prompt_tokens=122_971,
            context_length=1_000_000,
        ),
    )

    metadata = _gateway_turn_runtime_metadata(
        agent,
        uncached_input_tokens_start=0,
        completion_tokens_start=0,
        cache_read_tokens_start=0,
        cache_write_tokens_start=0,
        usage_report_calls_start=0,
        result_api_calls=8,
    )

    assert metadata["input_tokens"] == 953_867  # legacy cumulative prompt key
    assert metadata["turn_input_tokens"] == 38_731
    assert metadata["turn_cache_read_tokens"] == 915_136
    assert metadata["last_prompt_tokens"] == 122_971

    footer = format_runtime_footer(
        model=metadata["model_last"],
        context_tokens=metadata["last_prompt_tokens"],
        context_length=metadata["context_length"],
        tokens_in=metadata["turn_input_tokens"],
        tokens_out=metadata["turn_output_tokens"],
        cache_read_tokens=metadata["turn_cache_read_tokens"],
        cache_write_tokens=metadata["turn_cache_write_tokens"],
        token_usage_status=metadata["token_usage_status"],
        fields=("tokens_turn", "cache_hit", "context_window"),
    )
    assert footer == (
        "tokens(turn,uncached):38.7k in/3.4k out · cache(turn):96% · "
        "ctx(last):123.0k/1.0M (12%)"
    )


@pytest.mark.parametrize(
    "prompt_now,completion_now,usage_calls_now,result_calls,expected_tokens,expected_status",
    [
        # One provider response omitted usage entirely.
        (10_000, 500, 2, 1, (None, None), None),
        # Two-call tool loop: only one response reported usage.
        (12_500, 900, 3, 2, (2_500, 400), "reported_partial"),
    ],
)
def test_gateway_turn_metadata_labels_or_hides_incomplete_provider_usage(
    prompt_now,
    completion_now,
    usage_calls_now,
    result_calls,
    expected_tokens,
    expected_status,
):
    from gateway.run import _gateway_turn_runtime_metadata

    agent = SimpleNamespace(
        model="glm-5.3",
        reasoning_config={"enabled": True, "effort": "max"},
        session_prompt_tokens=prompt_now,
        session_input_tokens=prompt_now,
        session_completion_tokens=completion_now,
        session_cache_read_tokens=0,
        session_cache_write_tokens=0,
        session_api_calls=usage_calls_now,
        session_usage_report_calls=usage_calls_now,
        context_compressor=SimpleNamespace(
            last_prompt_tokens=50_000,
            context_length=1_000_000,
        ),
    )
    metadata = _gateway_turn_runtime_metadata(
        agent,
        uncached_input_tokens_start=10_000,
        completion_tokens_start=500,
        cache_read_tokens_start=0,
        cache_write_tokens_start=0,
        usage_report_calls_start=2,
        result_api_calls=result_calls,
    )

    assert (
        metadata["turn_input_tokens"],
        metadata["turn_output_tokens"],
    ) == expected_tokens
    assert metadata["token_usage_status"] == expected_status
    assert metadata["model_last"] == "glm-5.3"
    assert metadata["reasoning_effort"] == "max"


def test_gateway_reasoning_settles_cached_fallback_before_resolution():
    from gateway.run import _install_gateway_turn_reasoning_resolver
    from agent.turn_context import resolve_gateway_reasoning_after_runtime_restore

    resolved_models = []

    class Runner:
        _reasoning_config = None

        def _resolve_session_reasoning_config(self, **kwargs):
            resolved_models.append(kwargs["model"])
            return {"enabled": True, "effort": "high"}

    restore_calls = []

    def _restore():
        restore_calls.append(True)
        if len(restore_calls) == 1:
            return False  # cooldown still active at the canonical restore
        agent.model = "primary-model"
        return True  # would race across the boundary if called a second time

    agent = SimpleNamespace(
        model="fallback-model",
        reasoning_config={"enabled": True, "effort": "max"},
        _restore_primary_runtime=_restore,
    )
    runner = Runner()

    _install_gateway_turn_reasoning_resolver(
        runner,
        agent,
        source=SimpleNamespace(),
        session_key="telegram:session",
    )
    agent._restore_primary_runtime()
    resolved = resolve_gateway_reasoning_after_runtime_restore(agent)

    assert resolved_models == ["fallback-model"]
    assert resolved == {"enabled": True, "effort": "high"}
    assert agent.reasoning_config == resolved
    assert runner._reasoning_config == resolved
    # The prologue has one restore site. A hypothetical second call would now
    # flip to primary, but no marker/second-restore path exists anymore.
    assert len(restore_calls) == 1
    assert agent.model == "fallback-model"


def test_cached_fallback_uses_its_entry_effort_not_session_override(monkeypatch):
    import gateway.run as gateway_run
    from agent.turn_context import resolve_gateway_reasoning_after_runtime_restore

    session_resolver_calls = []

    class Runner:
        _reasoning_config = None

        def _resolve_session_reasoning_config(self, **kwargs):
            session_resolver_calls.append(kwargs)
            return {"enabled": True, "effort": "max"}

    runner = Runner()
    agent = SimpleNamespace(
        model="fallback-model",
        reasoning_config={"enabled": True, "effort": "max"},
        _fallback_activated=True,
        _active_fallback_entry={"reasoning_effort": "low"},
        _runtime_reasoning_entry={"reasoning_effort": "low"},
        _restore_primary_runtime=lambda: False,
    )
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_runtime_config",
        lambda: {"agent": {"reasoning_effort": "high"}},
    )

    gateway_run._install_gateway_turn_reasoning_resolver(
        runner,
        agent,
        source=SimpleNamespace(),
        session_key="telegram:session",
    )
    agent._restore_primary_runtime()
    resolved = resolve_gateway_reasoning_after_runtime_restore(agent)

    assert resolved == {"enabled": True, "effort": "low"}
    assert agent.reasoning_config == resolved
    assert runner._reasoning_config == resolved
    assert session_resolver_calls == []


def test_fallback_reasoning_reload_failure_keeps_entry_pin(monkeypatch):
    import gateway.run as gateway_run

    class Runner:
        def _resolve_session_reasoning_config(self, **_kwargs):
            raise AssertionError("fallback must not consult session reasoning")

    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_runtime_config",
        lambda: (_ for _ in ()).throw(OSError("config unavailable")),
    )

    resolved = gateway_run._resolve_gateway_reasoning_for_route(
        Runner(),
        source=SimpleNamespace(),
        session_key="telegram:session",
        model="fallback-model",
        fallback_entry={"reasoning_effort": "low"},
    )

    assert resolved == {"enabled": True, "effort": "low"}


def test_fallback_reasoning_resolution_failure_uses_provider_default(
    monkeypatch,
):
    import gateway.run as gateway_run

    class Runner:
        def _resolve_session_reasoning_config(self, **_kwargs):
            raise AssertionError("fallback must not consult session reasoning")

    monkeypatch.setattr(
        "hermes_constants.resolve_fallback_reasoning_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad policy")),
    )

    resolved = gateway_run._resolve_gateway_reasoning_for_route(
        Runner(),
        source=SimpleNamespace(),
        session_key="telegram:session",
        model="fallback-model",
        fallback_entry={"reasoning_effort": "low"},
    )

    assert resolved is None


def test_route_reasoning_policy_clears_stale_provenance_but_keeps_cooldown():
    from gateway.run import _apply_route_reasoning_policy_to_agent

    primary = SimpleNamespace(
        _fallback_activated=False,
        _active_fallback_entry=None,
        _runtime_reasoning_entry={"reasoning_effort": "low"},
        _primary_runtime={
            "reasoning_policy_entry": {"reasoning_effort": "low"},
            "reasoning_config": {"enabled": True, "effort": "low"},
        },
    )
    _apply_route_reasoning_policy_to_agent(
        primary,
        None,
        {"enabled": True, "effort": "max"},
    )
    assert primary._runtime_reasoning_entry is None
    assert primary._primary_runtime["reasoning_policy_entry"] is None
    assert primary._primary_runtime["reasoning_config"] == {
        "enabled": True,
        "effort": "max",
    }

    cooldown = SimpleNamespace(
        _fallback_activated=True,
        _active_fallback_entry={"reasoning_effort": "low"},
        _runtime_reasoning_entry={"reasoning_effort": "low"},
        _primary_runtime={"reasoning_policy_entry": None},
    )
    _apply_route_reasoning_policy_to_agent(
        cooldown,
        None,
        {"enabled": True, "effort": "max"},
    )
    assert cooldown._runtime_reasoning_entry == {"reasoning_effort": "low"}
    assert cooldown._primary_runtime["reasoning_policy_entry"] is None


def test_format_footer_turn_tokens_and_requested_effort():
    out = format_runtime_footer(
        model="glm-5.3",
        context_tokens=0,
        context_length=None,
        tokens_in=15_932,
        tokens_out=678,
        token_usage_status="reported",
        reasoning_effort="max",
        fields=("model_last", "reasoning_effort", "tokens_in", "tokens_out"),
    )
    assert out == (
        "model(last):glm-5.3 · effort(req,last):max · 15.9k in · 678 out"
    )


def test_format_footer_skips_unavailable_turn_tokens():
    out = format_runtime_footer(
        model="glm-5.3",
        context_tokens=0,
        context_length=None,
        tokens_in=None,
        tokens_out=None,
        fields=("tokens_in", "tokens_out"),
    )
    assert out == ""


@pytest.mark.parametrize(
    "status,expected",
    [
        ("reported", "tokens(turn,uncached):15.9k in/678 out"),
        (
            "reported_partial",
            "tokens(turn,uncached,partial):15.9k in/678 out",
        ),
        (None, ""),
    ],
)
def test_format_footer_labels_provider_reported_tokens(status, expected):
    out = format_runtime_footer(
        model="glm-5.3",
        context_tokens=0,
        context_length=None,
        tokens_in=15_932,
        tokens_out=678,
        token_usage_status=status,
        fields=("tokens_turn",),
    )
    assert out == expected


@pytest.mark.parametrize(
    "status,expected",
    [
        ("reported", "cache(turn):95%"),
        ("reported_partial", "cache(turn,partial):95%"),
        (None, ""),
    ],
)
def test_format_footer_cache_hit_ratio_uses_all_prompt_buckets(status, expected):
    out = format_runtime_footer(
        model="glm-5.3",
        context_tokens=0,
        context_length=None,
        tokens_in=2_000,
        cache_read_tokens=95_000,
        cache_write_tokens=3_000,
        token_usage_status=status,
        fields=("cache_hit",),
    )
    assert out == expected


def test_format_footer_cache_hit_renders_zero_for_complete_uncached_turn():
    out = format_runtime_footer(
        model="glm-5.3",
        context_tokens=0,
        context_length=None,
        tokens_in=10_000,
        cache_read_tokens=0,
        cache_write_tokens=0,
        token_usage_status="reported",
        fields=("cache_hit",),
    )
    assert out == "cache(turn):0%"


def test_raw_token_fields_skip_detected_partial_usage():
    out = format_runtime_footer(
        model="glm-5.3",
        context_tokens=0,
        context_length=None,
        tokens_in=2_500,
        tokens_out=400,
        token_usage_status="reported_partial",
        fields=("tokens_in", "tokens_out"),
    )
    assert out == ""


# ---------------------------------------------------------------------------
# resolve_footer_config
# ---------------------------------------------------------------------------


def test_resolve_platform_override_wins():
    user = {
        "display": {
            "runtime_footer": {"enabled": True, "fields": ["model"]},
            "platforms": {
                "slack": {"runtime_footer": {"enabled": False}},
            },
        },
    }
    # Telegram picks up the global enable
    assert resolve_footer_config(user, "telegram")["enabled"] is True
    # Slack overrides to off
    assert resolve_footer_config(user, "slack")["enabled"] is False


def test_resolve_platform_can_add_fields_only():
    user = {
        "display": {
            "runtime_footer": {"enabled": True},
            "platforms": {
                "discord": {"runtime_footer": {"fields": ["context_pct"]}},
            },
        },
    }
    tg = resolve_footer_config(user, "telegram")
    assert tg["enabled"] is True
    assert tg["fields"] == ["model", "context_pct", "cwd"]
    dc = resolve_footer_config(user, "discord")
    assert dc["enabled"] is True
    assert dc["fields"] == ["context_pct"]


# ---------------------------------------------------------------------------
# build_footer_line — top-level entry point used by gateway/run.py
# ---------------------------------------------------------------------------


def test_build_footer_per_platform_off_suppresses():
    user = {
        "display": {
            "runtime_footer": {"enabled": True},
            "platforms": {"slack": {"runtime_footer": {"enabled": False}}},
        },
    }
    out = build_footer_line(
        user_config=user,
        platform_key="slack",
        model="openai/gpt-5.4",
        context_tokens=10, context_length=100,
        cwd="/tmp",
    )
    assert out == ""


def test_build_footer_threads_turn_usage_and_requested_effort():
    out = build_footer_line(
        user_config={
            "display": {
                "runtime_footer": {
                    "enabled": True,
                    "fields": ["reasoning_effort", "tokens_turn"],
                }
            }
        },
        platform_key="telegram",
        model="glm-5.3",
        context_tokens=0,
        context_length=None,
        tokens_in=2_500,
        tokens_out=400,
        cache_read_tokens=47_500,
        cache_write_tokens=0,
        token_usage_status="reported",
        reasoning_effort="high",
    )
    assert out == (
        "effort(req,last):high · tokens(turn,uncached):2.5k in/400 out"
    )



# ---------------------------------------------------------------------------
# latency — opt-in wall-clock turn duration
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0.0, "<1s"),
        (0.4, "<1s"),
        (0.999, "<1s"),
        (1.0, "1s"),
        (22.0, "22s"),
        (22.4, "22s"),
        (59.4, "59s"),
        (59.6, "1m00s"),
        (60.0, "1m00s"),
        (65.0, "1m05s"),
        (125.0, "2m05s"),
        (3600.0, "60m00s"),
    ],
)
def test_format_latency(seconds, expected):
    from gateway.runtime_footer import _format_latency

    assert _format_latency(seconds) == expected


def test_format_footer_latency_renders():
    out = format_runtime_footer(
        model="m",
        context_tokens=0,
        context_length=None,
        cwd="",
        turn_seconds=22.0,
        fields=("latency",),
    )
    assert out == "22s"


def test_format_footer_latency_skipped_when_unmeasured():
    """A call site that doesn't measure timing leaves the field out entirely."""
    out = format_runtime_footer(
        model="m",
        context_tokens=0,
        context_length=None,
        cwd="",
        turn_seconds=None,
        fields=("latency",),
    )
    assert out == ""


def test_format_footer_latency_skipped_when_negative():
    """A nonsensical (negative) duration is dropped rather than rendered."""
    out = format_runtime_footer(
        model="m",
        context_tokens=0,
        context_length=None,
        cwd="",
        turn_seconds=-1.0,
        fields=("latency",),
    )
    assert out == ""


def test_format_footer_latency_zero_renders_sub_second():
    """Zero is a real measurement (a very fast turn), not missing data."""
    out = format_runtime_footer(
        model="m",
        context_tokens=0,
        context_length=None,
        cwd="",
        turn_seconds=0.0,
        fields=("latency",),
    )
    assert out == "<1s"


def test_format_footer_latency_in_field_order(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    out = format_runtime_footer(
        model="openai/gpt-5.4",
        context_tokens=68_000,
        context_length=100_000,
        cwd=str(tmp_path),
        turn_seconds=65.0,
        fields=("model", "context_pct", "latency", "cwd"),
    )
    assert out == "gpt-5.4 · 68% · 1m05s · ~"


def test_build_footer_line_threads_turn_seconds(monkeypatch):
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    out = build_footer_line(
        user_config={
            "display": {
                "runtime_footer": {
                    "enabled": True,
                    "fields": ["model", "latency"],
                }
            }
        },
        platform_key="discord",
        model="gpt-5.4",
        context_tokens=0,
        context_length=None,
        cwd="",
        turn_seconds=22.0,
    )
    assert out == "gpt-5.4 · 22s"


# ---------------------------------------------------------------------------
# Byte-stability: `latency` is opt-in, so the DEFAULT footer is unchanged.
#
# Upstream doctrine: a system prompt / rendered surface must be byte-stable for
# the life of a conversation.  Adding a field to _DEFAULT_FIELDS would silently
# change the footer text of every user who already enabled it.  These tests pin
# the default set and the exact default-config output strings.
# ---------------------------------------------------------------------------

_LEGACY_DEFAULT_FIELDS = ["model", "context_pct", "cwd"]


def test_latency_not_in_default_fields():
    from gateway.runtime_footer import _DEFAULT_FIELDS

    assert "latency" not in _DEFAULT_FIELDS
    assert list(_DEFAULT_FIELDS) == _LEGACY_DEFAULT_FIELDS


def test_resolve_footer_config_default_fields_exclude_latency():
    assert resolve_footer_config({}, "telegram")["fields"] == _LEGACY_DEFAULT_FIELDS
    assert resolve_footer_config(
        {"display": {"runtime_footer": {"enabled": True}}}, "discord"
    )["fields"] == _LEGACY_DEFAULT_FIELDS


@pytest.mark.parametrize(
    "model,tokens,window,cwd,expected",
    [
        ("openai/gpt-5.4", 50_247, 1_000_000, "/var/data", "gpt-5.4 · 5% · /var/data"),
        ("claude-opus-4-8", 68_000, 100_000, "/var/data", "claude-opus-4-8 · 68% · /var/data"),
        ("m", 0, None, "/var/data", "m · /var/data"),
        ("", 10, 100, "/var/data", "10% · /var/data"),
        ("m", 10, 100, "", "m · 10%"),
    ],
)
def test_default_footer_renders_byte_identically(
    monkeypatch, model, tokens, window, cwd, expected
):
    """Default-config output is byte-for-byte what it was before `latency`.

    Note `turn_seconds` IS supplied — proving that even when the caller
    measures timing, a default-configured footer does not show it.
    """
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    out = format_runtime_footer(
        model=model,
        context_tokens=tokens,
        context_length=window,
        cwd=cwd,
        turn_seconds=22.0,
        # fields deliberately NOT passed — exercises the default.
    )
    assert out == expected


def test_default_build_footer_line_ignores_turn_seconds(monkeypatch):
    """build_footer_line with default fields is unaffected by turn_seconds."""
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    common = dict(
        user_config={"display": {"runtime_footer": {"enabled": True}}},
        platform_key="discord",
        model="openai/gpt-5.4",
        context_tokens=50_247,
        context_length=1_000_000,
        cwd="/var/data",
    )
    baseline = build_footer_line(**common)
    with_timing = build_footer_line(**common, turn_seconds=125.0)
    assert baseline == "gpt-5.4 · 5% · /var/data"
    assert with_timing == baseline
