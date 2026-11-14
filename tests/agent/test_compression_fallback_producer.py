"""The actual host-stall producer freezes native configuration before leaf work."""

import copy

import hermes_yaml
import pytest

from agent.context_compressor import _pinned_summary_call_kwargs, peek_pinned_summary_route, pin_summary_route
from agent.conversation_compression import resolve_compression_fallback_route
from hermes_cli.config import load_config_readonly
from hermes_cli.config_defaults import DEFAULT_CONFIG
from hermes_constants import get_hermes_home, parse_reasoning_effort


@pytest.mark.parametrize("effort", [None, "medium"])
def test_native_producer_freezes_entry_and_reasoning_for_multiple_leaf_reads(effort):
    config = copy.deepcopy(DEFAULT_CONFIG)
    shipped_routes = config["moa"]["presets"]["default"]["reference_models"]
    entry = copy.deepcopy(shipped_routes[0])
    entry.update(base_url="https://api.openai.com/v1", api_mode="codex_responses", timeout=45)
    if effort is not None:
        entry["reasoning_effort"] = effort
    config["agent"]["reasoning_effort"] = None
    config["agent"]["reasoning_overrides"] = {}
    config["auxiliary"]["compression"]["fallback_chain"] = [entry]
    path = get_hermes_home() / "config.yaml"
    path.write_text(hermes_yaml.safe_dump(config))

    route = resolve_compression_fallback_route()
    assert route is not None
    ref = route["fallback_route_ref"]
    assert ref.chain_kind == "auxiliary" and ref.index == 0
    assert dict(ref.entry) == entry
    assert route["reasoning_config"] == parse_reasoning_effort(effort)
    assert route["api_key"] is None
    assert route["base_url"] == entry["base_url"]
    assert route["api_mode"] == entry["api_mode"]
    assert route["timeout"] == 45.0
    with pytest.raises(TypeError):
        ref.entry["model"] = shipped_routes[1]["model"]

    changed = copy.deepcopy(shipped_routes[1])
    changed.update(base_url="https://openrouter.ai/api/v1", api_mode="chat_completions", timeout=90,
                   reasoning_effort="low")
    config["auxiliary"]["compression"]["fallback_chain"] = [changed]
    path.write_text(hermes_yaml.safe_dump(config))
    assert load_config_readonly()["auxiliary"]["compression"]["fallback_chain"] == [changed]
    assert resolve_compression_fallback_route()["model"] == changed["model"]

    with pin_summary_route(route):
        for _ in range(2):
            leaf = peek_pinned_summary_route()
            assert leaf["model"] == entry["model"]
            assert leaf["provider"] == entry["provider"]
            assert leaf["fallback_route_ref"] is ref
            assert dict(leaf["fallback_route_ref"].entry) == entry
            assert leaf["api_key"] is None
            assert leaf["base_url"] == entry["base_url"]
            assert leaf["api_mode"] == entry["api_mode"]
            assert leaf["timeout"] == 45.0
            assert "reasoning_config" in leaf
            assert leaf["reasoning_config"] == parse_reasoning_effort(effort)
            if isinstance(leaf["reasoning_config"], dict):
                leaf["reasoning_config"].clear()
        consumed = _pinned_summary_call_kwargs()
        assert consumed["model"] == entry["model"]
        assert "reasoning_config" in consumed
        assert consumed["reasoning_config"] == parse_reasoning_effort(effort)
        assert consumed["_pinned_compression_route"]["fallback_route_ref"] is ref
        assert consumed["_pinned_compression_route"]["api_key"] is None
        assert peek_pinned_summary_route() is None
