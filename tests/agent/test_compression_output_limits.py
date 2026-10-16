"""Explicit compression output budgets must survive supported wire adapters."""

from types import SimpleNamespace

import pytest

from agent.auxiliary_client import _build_call_kwargs, _CodexCompletionsAdapter


@pytest.mark.parametrize("provider", ["custom", "custom:summary-proxy", "summary-proxy"])
@pytest.mark.parametrize("model,wire_key", [("gpt-5.5", "max_completion_tokens"), ("gpt-6-astra", "max_tokens"), ("glm-5.2", "max_tokens")])
def test_compression_budget_reaches_custom_chat_request(provider, model, wire_key):
    request = _build_call_kwargs(
        provider, model, [{"role": "user", "content": "summarize"}],
        max_tokens=2048, task="compression", base_url="http://127.0.0.1:8317/v1",
    )
    assert request[wire_key] == 2048


@pytest.mark.parametrize("base_url", ["https://api.openai.com/v1", "http://127.0.0.1:8317/v1"])
def test_compression_budget_survives_api_key_responses_bridge(base_url):
    request = _build_call_kwargs(
        "custom", "gpt-6-astra", [{"role": "user", "content": "summarize"}],
        max_tokens=2048, task="compression", base_url=base_url,
    )
    adapter = _CodexCompletionsAdapter(SimpleNamespace(base_url=base_url), "gpt-6-astra")
    responses_request, _, _ = adapter._build_responses_kwargs(request)
    assert responses_request["max_output_tokens"] == 2048
    assert "max_tokens" not in responses_request
    assert "max_completion_tokens" not in responses_request


def test_native_codex_keeps_unsupported_budget_off_wire():
    adapter = _CodexCompletionsAdapter(
        SimpleNamespace(base_url="https://chatgpt.com/backend-api/codex"), "gpt-5.5",
    )
    request, _, _ = adapter._build_responses_kwargs({
        "messages": [{"role": "user", "content": "summarize"}],
        "max_completion_tokens": 2048,
    })
    assert "max_output_tokens" not in request


def test_other_custom_auxiliary_tasks_keep_existing_provider_defaults():
    request = _build_call_kwargs(
        "custom", "glm-5.2", [{"role": "user", "content": "title"}],
        max_tokens=2048, task="title", base_url="http://127.0.0.1:8317/v1",
    )
    assert "max_tokens" not in request
    assert "max_completion_tokens" not in request
