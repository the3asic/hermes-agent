"""Session reasoning ownership for auxiliary compression routes."""

from types import SimpleNamespace

import pytest

import agent.auxiliary_client as aux
from agent.context_compressor import (
    _pinned_summary_call_kwargs,
    peek_pinned_summary_route,
    pin_summary_route,
)


MAIN = {
    "provider": "custom:cliproxyapi",
    "model": "glm-5.3",
    "base_url": "https://runtime.test/v1",
    "api_key": "runtime-key",
    "api_mode": "chat_completions",
    "reasoning_config": {"enabled": True, "effort": "max"},
}
MESSAGES = [{"role": "user", "content": "summarize"}]


def _response():
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="summary"))]
    )


def _capture_calls(
    monkeypatch,
    task_config,
    *,
    provider=MAIN["provider"],
    model=MAIN["model"],
    base_url=MAIN["base_url"],
):
    client = SimpleNamespace(
        base_url=base_url,
        _hermes_aux_effective_provider=provider,
    )
    calls = []
    monkeypatch.setattr(aux, "_get_auxiliary_task_config", lambda _task: task_config)
    monkeypatch.setattr(
        aux, "_get_cached_client", lambda *args, **kwargs: (client, model)
    )

    def _build(actual_provider, actual_model, messages, **kwargs):
        calls.append(
            {
                "provider": actual_provider,
                "model": actual_model,
                "extra_body": dict(kwargs.get("extra_body") or {}),
                "reasoning_config": kwargs.get("reasoning_config"),
                "max_tokens": kwargs.get("max_tokens"),
            }
        )
        return {"model": actual_model, "messages": messages}

    async def _async_relay(*args, **kwargs):
        return _response()

    monkeypatch.setattr(aux, "_build_call_kwargs", _build)
    monkeypatch.setattr(aux, "_relay_sync_completion", lambda *a, **kw: _response())
    monkeypatch.setattr(aux, "_relay_async_completion", _async_relay)
    monkeypatch.setattr(
        aux, "_validate_llm_response", lambda response, *args, **kwargs: response
    )
    return client, calls


async def _call_primary(async_mode):
    kwargs = {"task": "compression", "main_runtime": MAIN, "messages": MESSAGES}
    if async_mode:
        return await aux.async_call_llm(**kwargs)
    return aux.call_llm(**kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("configured_provider", ["auto", "main"])
async def test_auto_route_inherits_active_session_reasoning(
    monkeypatch, async_mode, configured_provider
):
    _, calls = _capture_calls(
        monkeypatch, {"provider": configured_provider, "model": ""}
    )

    result = await _call_primary(async_mode)

    assert result.choices[0].message.content == "summary"
    assert calls == [
        {
            "provider": MAIN["provider"],
            "model": MAIN["model"],
            "extra_body": {},
            "reasoning_config": {"enabled": True, "effort": "max"},
            "max_tokens": None,
        }
    ]


def test_explicit_task_reasoning_wins_over_session(monkeypatch):
    _, calls = _capture_calls(
        monkeypatch,
        {"provider": "auto", "model": "", "reasoning_effort": "low"},
    )

    aux.call_llm(task="compression", main_runtime=MAIN, messages=MESSAGES)

    assert calls[0]["reasoning_config"] is None
    assert calls[0]["extra_body"]["reasoning"] == {
        "enabled": True,
        "effort": "low",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("async_mode", [False, True])
async def test_initial_auto_fallback_uses_target_reasoning(monkeypatch, async_mode):
    fallback = {
        "provider": MAIN["provider"],
        "model": "gpt-5.6-sol",
        "base_url": MAIN["base_url"],
        "api_key": "selected-key",
        "reasoning_effort": "medium",
    }
    same_deployment_other_entry = {
        **fallback,
        "api_key": "other-key",
        "reasoning_effort": "low",
    }
    _, calls = _capture_calls(
        monkeypatch,
        {
            "provider": "auto",
            "model": "",
            "reasoning_effort": "low",
            "fallback_chain": [same_deployment_other_entry, fallback],
        },
        model=fallback["model"],
    )
    aux._SELECTED_FALLBACK_ROUTE_REF.set(
        aux._fallback_route_ref("auxiliary", 1, fallback)
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"agent": {"reasoning_effort": "high"}},
    )

    await _call_primary(async_mode)

    assert calls[0]["reasoning_config"] == {
        "enabled": True,
        "effort": "medium",
    }
    assert "reasoning" not in calls[0]["extra_body"]


@pytest.mark.asyncio
@pytest.mark.parametrize("async_mode", [False, True])
async def test_explicit_unavailable_provider_fallback_uses_target_reasoning(
    monkeypatch, async_mode
):
    fallback = {
        "provider": MAIN["provider"],
        "model": "gpt-5.6-sol",
        "base_url": MAIN["base_url"],
        "reasoning_effort": "medium",
    }
    client, calls = _capture_calls(
        monkeypatch,
        {
            "provider": "broken-provider",
            "model": "broken-model",
            "reasoning_effort": "low",
            "fallback_chain": [fallback],
        },
        model=fallback["model"],
    )
    monkeypatch.setattr(aux, "_get_cached_client", lambda *a, **kw: (None, None))

    def _select(*args, **kwargs):
        aux._SELECTED_FALLBACK_ROUTE_REF.set(
            aux._fallback_route_ref("auxiliary", 0, fallback)
        )
        return client, fallback["model"], f"fallback_chain[0]({fallback['provider']})"

    monkeypatch.setattr(
        aux, "_try_configured_fallback_for_unavailable_client", _select
    )
    monkeypatch.setattr(
        aux, "_to_async_client", lambda sync, model, **kwargs: (sync, model)
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly", lambda: {"agent": {}}
    )

    await _call_primary(async_mode)

    assert calls[0]["reasoning_config"] == {
        "enabled": True,
        "effort": "medium",
    }
    assert "reasoning" not in calls[0]["extra_body"]


@pytest.mark.asyncio
@pytest.mark.parametrize("async_mode", [False, True])
async def test_request_fallback_uses_target_reasoning(monkeypatch, async_mode):
    fallback = {
        "provider": MAIN["provider"],
        "model": "gpt-5.6-sol",
        "base_url": "https://fallback.test/v1",
        "reasoning_effort": "medium",
    }
    client, calls = _capture_calls(monkeypatch, {"fallback_chain": [fallback]})
    client.base_url = fallback["base_url"]
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"agent": {"reasoning_effort": "high"}},
    )
    monkeypatch.setattr(
        aux,
        "_replan_synchronous_cache_sections",
        lambda messages, tools, **kwargs: (messages, tools or []),
    )
    kwargs = {
        "task": "compression",
        "messages": MESSAGES,
        "temperature": None,
        "max_tokens": None,
        "tools": None,
        "effective_timeout": 30.0,
        "effective_extra_body": {
            "reasoning": {"enabled": True, "effort": "max"}
        },
        "reasoning_config": {"enabled": True, "effort": "max"},
    }
    label = f"fallback_chain[0]({fallback['provider']})"

    if async_mode:
        result = await aux._call_fallback_candidate_async(
            client, fallback["model"], label, **kwargs
        )
    else:
        result = aux._call_fallback_candidate_sync(
            client, fallback["model"], label, **kwargs
        )

    assert result.choices[0].message.content == "summary"
    assert calls[0]["reasoning_config"] == {
        "enabled": True,
        "effort": "medium",
    }
    assert "reasoning" not in calls[0]["extra_body"]


@pytest.mark.asyncio
@pytest.mark.parametrize("async_mode", [False, True])
async def test_lcm_multiple_leaf_calls_share_one_stall_pin(monkeypatch, async_mode):
    route = {
        "provider": "custom",
        "model": "gpt-5.6-sol",
        "base_url": "https://fallback.test/v1",
        "reasoning_config": {"enabled": True, "effort": "medium"},
    }
    _, calls = _capture_calls(
        monkeypatch,
        {"reasoning_effort": "max"},
        provider=route["provider"],
        model=route["model"],
        base_url=route["base_url"],
    )

    with pin_summary_route(route):
        for _ in range(2):
            if async_mode:
                await aux.async_call_llm(task="compression", messages=MESSAGES)
            else:
                aux.call_llm(task="compression", messages=MESSAGES)
        assert peek_pinned_summary_route() == route

    assert [(call["provider"], call["model"]) for call in calls] == [
        (route["provider"], route["model"]),
        (route["provider"], route["model"]),
    ]
    assert all(
        call["reasoning_config"] == route["reasoning_config"]
        and "reasoning" not in call["extra_body"]
        for call in calls
    )


def test_consumed_pin_preserves_explicit_none_and_strips_task_reasoning(monkeypatch):
    entry = {
        "provider": "custom",
        "model": "gpt-5.6-sol",
        "base_url": "https://fallback.test/v1",
    }
    route = {
        **entry,
        "label": "fallback_chain[0](custom)",
        "reasoning_config": None,
        "fallback_route_ref": aux._fallback_route_ref("auxiliary", 0, entry),
    }
    _, calls = _capture_calls(
        monkeypatch,
        {"reasoning_effort": "max", "fallback_chain": [entry]},
        provider=entry["provider"],
        model=entry["model"],
        base_url=entry["base_url"],
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly", lambda: {"agent": {}}
    )

    with pin_summary_route(route):
        pinned_kwargs = _pinned_summary_call_kwargs()
        assert pinned_kwargs["reasoning_config"] is None
        assert pinned_kwargs["_pinned_compression_route"]["reasoning_config"] is None
        aux.call_llm(task="compression", messages=MESSAGES, **pinned_kwargs)

    assert calls[0]["reasoning_config"] is None
    assert "reasoning" not in calls[0]["extra_body"]


def test_pinned_fast_lane_uses_exact_fallback_entry_not_primary(monkeypatch):
    entry = {
        "provider": "custom",
        "model": "gpt-5.6-sol",
        "base_url": "https://same.test/v1",
        "reasoning_effort": "medium",
    }
    route = {
        **entry,
        "label": "fallback_chain[0](custom)",
        "reasoning_config": {"enabled": True, "effort": "medium"},
        "fallback_route_ref": aux._fallback_route_ref("auxiliary", 0, entry),
    }
    _, calls = _capture_calls(
        monkeypatch,
        {
            "provider": "custom",
            "model": entry["model"],
            "reasoning_effort": "none",
            "max_output_tokens": 1400,
            "fallback_chain": [entry],
        },
        provider=entry["provider"],
        model=entry["model"],
        base_url=entry["base_url"],
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly", lambda: {"agent": {}}
    )

    with pin_summary_route(route):
        aux.call_llm(task="compression", messages=MESSAGES)

    assert calls[0]["max_tokens"] is None
    assert calls[0]["reasoning_config"] == {
        "enabled": True,
        "effort": "medium",
    }


def test_failed_configured_candidate_does_not_leak_effort_to_next_fallback(
    monkeypatch,
):
    class AuthError(Exception):
        status_code = 401

    entry = {
        "provider": "custom",
        "model": "same-model",
        "reasoning_effort": "medium",
    }
    stale, calls = _capture_calls(
        monkeypatch, {"fallback_chain": [entry]}, provider="custom", model="same-model"
    )
    healthy = SimpleNamespace(base_url="https://openrouter.ai/api/v1")
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"agent": {"reasoning_effort": "high"}},
    )
    monkeypatch.setattr(
        aux,
        "_replan_synchronous_cache_sections",
        lambda messages, tools, **kwargs: (messages, tools or []),
    )
    monkeypatch.setattr(aux, "_refresh_provider_credentials", lambda provider: False)
    monkeypatch.setattr(aux, "_mark_provider_unhealthy", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        aux,
        "_relay_sync_completion",
        lambda client, *args, **kwargs: (
            (_ for _ in ()).throw(AuthError("stale"))
            if client is stale
            else _response()
        ),
    )
    aux._SELECTED_FALLBACK_ROUTE_REF.set(
        aux._fallback_route_ref("auxiliary", 0, entry)
    )
    selected = aux._take_selected_fallback_route_ref()

    assert aux._call_fallback_candidate_sync(
        stale,
        "same-model",
        "fallback_chain[0](custom)",
        task="compression",
        messages=MESSAGES,
        temperature=None,
        max_tokens=None,
        tools=None,
        effective_timeout=30,
        effective_extra_body={},
        reasoning_config=None,
        fallback_route_ref=selected,
    ) is None
    assert aux._take_selected_fallback_route_ref() is None
    aux._call_fallback_candidate_sync(
        healthy,
        "same-model",
        "openrouter",
        task="compression",
        messages=MESSAGES,
        temperature=None,
        max_tokens=None,
        tools=None,
        effective_timeout=30,
        effective_extra_body={},
        reasoning_config=None,
        fallback_route_ref=None,
    )

    assert calls[0]["reasoning_config"]["effort"] == "medium"
    assert calls[1]["reasoning_config"]["effort"] == "high"
