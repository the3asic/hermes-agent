"""Regression coverage for implicit live-runtime auxiliary cache keys.

#49151/#49156 is specifically the ``provider='auto'`` path where callers omit
``main_runtime`` after a mid-session model switch.  This is distinct from
#56889, which isolates callers that pass different explicit ``model=`` values.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import agent.auxiliary_client as aux


@pytest.fixture(autouse=True)
def _clean_aux_state():
    aux.shutdown_cached_clients()
    aux.clear_runtime_main()
    yield
    aux.shutdown_cached_clients()
    aux.clear_runtime_main()


def _runtime(model: str, *, provider: str = "custom:llama-swap") -> dict:
    return {
        "provider": provider,
        "model": model,
        "base_url": "http://llama-swap.test/v1",
        "api_key": "local-key",
        "api_mode": "chat_completions",
        "auth_mode": "api_key",
    }




def test_implicit_runtime_cache_key_covers_full_connection_and_auth_surface():
    """Provider/endpoint/credential/wire/auth changes all isolate auto clients."""
    base = _runtime("same-model")
    variants = [
        {**base, "provider": "custom:other"},
        {**base, "base_url": "https://other.test/v1"},
        {**base, "api_key": "other-key"},
        {**base, "api_mode": "codex_responses"},
        {**base, "auth_mode": "entra_id", "api_key": lambda: "token"},
    ]

    aux.set_runtime_main(**base)
    baseline = aux._client_cache_key("auto", async_mode=False)
    keys = []
    for variant in variants:
        aux.set_runtime_main(**variant)
        keys.append(aux._client_cache_key("auto", async_mode=False))

    assert all(key != baseline for key in keys)
    assert len(set(keys)) == len(keys)






def test_runtime_context_token_restores_previous_value_after_turn():
    """Turn-scoped runtime binding must not leak into later work in the same context."""
    token = aux.set_runtime_main(**_runtime("turn-model"))
    assert aux._normalize_main_runtime(None)["model"] == "turn-model"

    aux.reset_runtime_main(token)

    assert aux._normalize_main_runtime(None) == {}


def test_runtime_context_preserves_session_reasoning_as_a_copy():
    reasoning = {"enabled": True, "effort": "max"}
    token = aux.set_runtime_main(
        **_runtime("glm-5.3"),
        session_id="session-1",
        cache_scope="conversation-root",
        reasoning_config=reasoning,
    )
    try:
        runtime = aux._normalize_main_runtime(None)
        reasoning["effort"] = "low"

        assert runtime["session_id"] == "session-1"
        assert runtime["cache_scope"] == "conversation-root"
        assert runtime["reasoning_config"] == {
            "enabled": True,
            "effort": "max",
        }
    finally:
        aux.reset_runtime_main(token)








def test_explicit_model_cache_isolation_remains_independent_of_runtime_key():
    """#56889 remains covered: explicit model values isolate non-auto clients."""
    first = aux._client_cache_key(
        "openrouter", async_mode=False, model="anthropic/claude-opus-4.8"
    )
    second = aux._client_cache_key(
        "openrouter", async_mode=False, model="openai/gpt-5.5"
    )

    assert first != second










def test_unhashable_callable_runtime_api_keys_are_safe_secret_free_discriminators():
    """Callable token providers remain cacheable without leaking returned tokens."""

    class TokenProvider(list):
        def __init__(self, token: str):
            super().__init__()
            self.token = token

        def __call__(self) -> str:
            return self.token

    first_provider = TokenProvider("first-super-secret-token")
    second_provider = TokenProvider("second-super-secret-token")

    first = aux._client_cache_key(
        "auto", async_mode=False, main_runtime={**_runtime("same"), "api_key": first_provider}
    )
    second = aux._client_cache_key(
        "auto", async_mode=False, main_runtime={**_runtime("same"), "api_key": second_provider}
    )

    hash(first)
    hash(second)
    assert first != second
    rendered = repr((first, second))
    assert "first-super-secret-token" not in rendered
    assert "second-super-secret-token" not in rendered


def test_string_api_keys_are_not_retained_in_cache_key_repr():
    """String credentials discriminate clients without living in cache-key memory."""
    first_secret = "first-literal-super-secret"
    second_secret = "second-literal-super-secret"
    first = aux._client_cache_key(
        "auto",
        async_mode=False,
        api_key=first_secret,
        main_runtime={**_runtime("same"), "api_key": first_secret},
    )
    second = aux._client_cache_key(
        "auto",
        async_mode=False,
        api_key=second_secret,
        main_runtime={**_runtime("same"), "api_key": second_secret},
    )

    assert first != second
    rendered = repr((first, second))
    assert first_secret not in rendered
    assert second_secret not in rendered


def test_repeated_auto_fallback_reuses_client_and_publishes_cached_ref(monkeypatch):
    entry = {"provider": "custom", "model": "fallback", "reasoning_effort": "high"}
    ref = aux._fallback_route_ref("auxiliary", 0, entry)
    client = SimpleNamespace(close=MagicMock())
    resolves = []
    monkeypatch.setattr(aux, "_fallback_policy_cache_fingerprint", lambda task: b"p1")

    def _resolve(*args, **kwargs):
        resolves.append(1)
        aux._SELECTED_FALLBACK_ROUTE_REF.set(ref)
        return client, "fallback"

    monkeypatch.setattr(aux, "resolve_provider_client", _resolve)
    first, _ = aux._get_cached_client(
        "auto", main_runtime=_runtime("main"), task="compression"
    )
    first_ref = aux._take_selected_fallback_route_ref()
    second, _ = aux._get_cached_client(
        "auto", main_runtime=_runtime("main"), task="compression"
    )
    second_ref = aux._take_selected_fallback_route_ref()

    assert first is second is client
    assert resolves == [1]
    assert first_ref == second_ref == ref


def test_fallback_policy_fingerprint_isolates_same_deployment_entries(monkeypatch):
    entries = [
        {"provider": "custom", "model": "same", "reasoning_effort": "low"},
        {"provider": "custom", "model": "same", "reasoning_effort": "high"},
    ]
    clients = [SimpleNamespace(close=MagicMock()), SimpleNamespace(close=MagicMock())]
    state = {"index": 0}
    monkeypatch.setattr(
        aux,
        "_fallback_policy_cache_fingerprint",
        lambda task: f"policy-{state['index']}".encode(),
    )

    def _resolve(*args, **kwargs):
        index = state["index"]
        aux._SELECTED_FALLBACK_ROUTE_REF.set(
            aux._fallback_route_ref("auxiliary", index, entries[index])
        )
        return clients[index], "same"

    monkeypatch.setattr(aux, "resolve_provider_client", _resolve)
    first, _ = aux._get_cached_client(
        "auto", main_runtime=_runtime("main"), task="compression"
    )
    first_ref = aux._take_selected_fallback_route_ref()
    state["index"] = 1
    second, _ = aux._get_cached_client(
        "auto", main_runtime=_runtime("main"), task="compression"
    )
    second_ref = aux._take_selected_fallback_route_ref()

    assert first is clients[0]
    assert second is clients[1]
    assert first_ref.entry["reasoning_effort"] == "low"
    assert second_ref.entry["reasoning_effort"] == "high"


def test_concurrent_cache_loser_uses_winner_route_ref(monkeypatch):
    barrier = Barrier(2)
    created = []
    created_lock = Lock()
    monkeypatch.setattr(aux, "_fallback_policy_cache_fingerprint", lambda task: b"p1")

    def _resolve(*args, **kwargs):
        with created_lock:
            index = len(created)
            client = SimpleNamespace(close=MagicMock())
            ref = aux._fallback_route_ref(
                "auxiliary",
                index,
                {"provider": "custom", "model": "same", "reasoning_effort": str(index)},
            )
            created.append((client, ref))
        aux._SELECTED_FALLBACK_ROUTE_REF.set(ref)
        barrier.wait(timeout=5)
        return client, "same"

    monkeypatch.setattr(aux, "resolve_provider_client", _resolve)

    def _worker():
        client, _ = aux._get_cached_client(
            "auto", main_runtime=_runtime("main"), task="compression"
        )
        return client, aux._take_selected_fallback_route_ref()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _worker(), range(2)))

    assert results[0][0] is results[1][0]
    assert results[0][1] == results[1][1]
    winner = results[0][0]
    winner_ref = results[0][1]
    assert any(client is winner and ref == winner_ref for client, ref in created)
    assert sum(client.close.call_count for client, _ in created) == 1
