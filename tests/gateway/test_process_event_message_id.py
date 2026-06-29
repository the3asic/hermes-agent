from collections import OrderedDict
from datetime import datetime
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionContext, SessionSource
from gateway.session_context import _UNSET, _VAR_MAP, get_session_env


@pytest.fixture(autouse=True)
def _reset_contextvars():
    yield
    for var in _VAR_MAP.values():
        var.set(_UNSET)


def _runner_with_session_origin(session_key: str, origin: SessionSource) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.session_store = SimpleNamespace(
        _entries={
            session_key: SimpleNamespace(
                origin=origin,
                session_id="sess-1",
                created_at=datetime.now(),
                updated_at=datetime.now(),
            )
        },
        _ensure_loaded=lambda: None,
    )
    runner._session_sources = OrderedDict()
    return runner


def test_process_event_source_replaces_stale_origin_message_id():
    """Synthetic process events must not reuse session-origin message ids.

    A long-lived Discord session can keep the first inbound message id in
    SessionStore.origin.  Process completions are per-event; their source copy
    must carry the watcher/event anchor instead of the stale origin anchor.
    """
    session_key = "agent:main:discord:group:chan:user"
    origin = SessionSource(
        platform=Platform.DISCORD,
        chat_id="chan",
        chat_type="group",
        user_id="user",
        user_name="3ASiC",
        message_id="old-first-discord-message",
    )
    runner = _runner_with_session_origin(session_key, origin)

    source = runner._build_process_event_source({
        "session_key": session_key,
        "message_id": "current-watcher-message",
    })

    assert source is not origin
    assert source.message_id == "current-watcher-message"
    assert source.chat_id == "chan"
    assert source.user_id == "user"
    assert origin.message_id == "old-first-discord-message"


def test_process_event_source_clears_cached_message_id_when_event_has_no_anchor():
    """Cached routing sources are also session-scoped; clear stale anchors."""
    session_key = "agent:main:discord:group:chan:user"
    runner = object.__new__(GatewayRunner)
    runner.session_store = SimpleNamespace(_entries={}, _ensure_loaded=lambda: None)
    cached = SessionSource(
        platform=Platform.DISCORD,
        chat_id="chan",
        chat_type="group",
        user_id="user",
        message_id="old-cached-message",
    )
    runner._session_sources = OrderedDict([(session_key, cached)])

    source = runner._build_process_event_source({"session_key": session_key})

    assert source is not cached
    assert source.message_id is None
    assert source.chat_id == "chan"
    assert cached.message_id == "old-cached-message"


def test_process_event_source_fallback_routing_uses_event_message_id():
    """When no store/cache entry exists, fresh source still gets event anchor."""
    runner = object.__new__(GatewayRunner)
    runner.session_store = SimpleNamespace(_entries={}, _ensure_loaded=lambda: None)
    runner._session_sources = OrderedDict()

    source = runner._build_process_event_source({
        "platform": "discord",
        "chat_type": "group",
        "chat_id": "chan",
        "user_id": "user",
        "user_name": "3ASiC",
        "message_id": "current-watcher-message",
    })

    assert source.message_id == "current-watcher-message"
    assert source.chat_id == "chan"
    assert source.user_id == "user"


def test_set_session_env_omitted_message_id_empty_source_does_not_export_sentinel():
    runner = object.__new__(GatewayRunner)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="chan",
        chat_type="group",
        user_id="user",
        message_id=None,
    )
    context = SessionContext(
        source=source,
        connected_platforms=[],
        home_channels={},
        session_key="agent:main:discord:group:chan:user",
        recall_scope_key="agent:main:discord:group:chan",
    )

    tokens = runner._set_session_env(context)
    try:
        assert get_session_env("HERMES_SESSION_MESSAGE_ID") == ""
    finally:
        runner._clear_session_env(tokens)


def test_set_session_env_omitted_message_id_keeps_source_fallback():
    runner = object.__new__(GatewayRunner)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="chan",
        chat_type="group",
        user_id="user",
        message_id="source-fallback-message",
    )
    context = SessionContext(
        source=source,
        connected_platforms=[],
        home_channels={},
        session_key="agent:main:discord:group:chan:user",
        recall_scope_key="agent:main:discord:group:chan",
    )

    tokens = runner._set_session_env(context)
    try:
        assert get_session_env("HERMES_SESSION_MESSAGE_ID") == "source-fallback-message"
    finally:
        runner._clear_session_env(tokens)


def test_set_session_env_explicit_event_message_id_overrides_stale_source_id():
    runner = object.__new__(GatewayRunner)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="chan",
        chat_type="group",
        user_id="user",
        message_id="old-first-discord-message",
    )
    context = SessionContext(
        source=source,
        connected_platforms=[],
        home_channels={},
        session_key="agent:main:discord:group:chan:user",
        recall_scope_key="agent:main:discord:group:chan",
    )

    tokens = runner._set_session_env(context, message_id="current-event-message")
    try:
        assert get_session_env("HERMES_SESSION_MESSAGE_ID") == "current-event-message"
    finally:
        runner._clear_session_env(tokens)


def test_set_session_env_explicit_none_clears_stale_source_id():
    """A synthetic event with no valid reply anchor must not fall back to origin."""
    runner = object.__new__(GatewayRunner)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="chan",
        chat_type="group",
        user_id="user",
        message_id="old-first-discord-message",
    )
    context = SessionContext(
        source=source,
        connected_platforms=[],
        home_channels={},
        session_key="agent:main:discord:group:chan:user",
        recall_scope_key="agent:main:discord:group:chan",
    )

    tokens = runner._set_session_env(context, message_id=None)
    try:
        assert get_session_env("HERMES_SESSION_MESSAGE_ID") == ""
    finally:
        runner._clear_session_env(tokens)


def test_reply_anchor_for_discord_process_event_uses_event_message_id_not_source():
    """Discord replies are anchored from MessageEvent.message_id."""
    runner = object.__new__(GatewayRunner)
    event = MessageEvent(
        text="[IMPORTANT: Background process proc_x completed]",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="chan",
            chat_type="group",
            user_id="user",
            message_id="old-first-discord-message",
        ),
        internal=True,
        message_id="current-watcher-message",
    )

    assert runner._reply_anchor_for_event(event) == "current-watcher-message"
