"""Tests for Telegram text message aggregation.

When a user sends a long message, Telegram clients split it into multiple
updates.  The TelegramAdapter should buffer rapid successive text messages
from the same session and aggregate them before dispatching.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, SessionSource
from gateway.session import build_session_key


def _make_adapter():
    """Create a minimal TelegramAdapter for testing text batching."""
    from plugins.platforms.telegram.adapter import TelegramAdapter

    config = PlatformConfig(enabled=True, token="test-token")
    adapter = object.__new__(TelegramAdapter)
    adapter._platform = Platform.TELEGRAM
    adapter.config = config
    adapter._pending_text_batches = {}
    adapter._pending_text_batch_tasks = {}
    adapter._text_batch_delay_seconds = 0.1  # fast for tests
    adapter._text_batch_split_delay_seconds = 0.1
    adapter._active_sessions = {}
    adapter._pending_messages = {}
    adapter._message_handler = AsyncMock()
    adapter.handle_message = AsyncMock()
    return adapter


def _make_event(
    text: str,
    chat_id: str = "12345",
    reply_to_message_id: str | None = None,
    reply_to_text: str | None = None,
) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="dm"),
        reply_to_message_id=reply_to_message_id,
        reply_to_text=reply_to_text,
    )


class TestTextBatching:
    @pytest.mark.asyncio
    async def test_single_message_dispatched_after_delay(self):
        adapter = _make_adapter()
        event = _make_event("hello world")

        adapter._enqueue_text_event(event)

        # Not dispatched yet
        adapter.handle_message.assert_not_called()

        # Wait for flush
        await asyncio.sleep(0.2)

        adapter.handle_message.assert_called_once()
        dispatched = adapter.handle_message.call_args[0][0]
        assert dispatched.text == "hello world"

    @pytest.mark.asyncio
    async def test_split_messages_aggregated(self):
        """Two rapid messages from the same chat should be merged."""
        adapter = _make_adapter()

        adapter._enqueue_text_event(_make_event("This is part one of a long"))
        await asyncio.sleep(0.02)  # small gap, within batch window
        adapter._enqueue_text_event(_make_event("message that was split by Telegram."))

        # Not dispatched yet (timer restarted)
        adapter.handle_message.assert_not_called()

        # Wait for flush
        await asyncio.sleep(0.2)

        adapter.handle_message.assert_called_once()
        dispatched = adapter.handle_message.call_args[0][0]
        assert "part one" in dispatched.text
        assert "split by Telegram" in dispatched.text

    @pytest.mark.asyncio
    async def test_three_way_split_aggregated(self):
        """Three rapid messages should all merge."""
        adapter = _make_adapter()

        adapter._enqueue_text_event(_make_event("chunk 1"))
        await asyncio.sleep(0.02)
        adapter._enqueue_text_event(_make_event("chunk 2"))
        await asyncio.sleep(0.02)
        adapter._enqueue_text_event(_make_event("chunk 3"))

        await asyncio.sleep(0.2)

        adapter.handle_message.assert_called_once()
        text = adapter.handle_message.call_args[0][0].text
        assert "chunk 1" in text
        assert "chunk 2" in text
        assert "chunk 3" in text

    @pytest.mark.asyncio
    async def test_different_chats_not_merged(self):
        """Messages from different chats should be separate batches."""
        adapter = _make_adapter()

        adapter._enqueue_text_event(_make_event("from user A", chat_id="111"))
        adapter._enqueue_text_event(_make_event("from user B", chat_id="222"))

        await asyncio.sleep(0.2)

        assert adapter.handle_message.call_count == 2

    @pytest.mark.asyncio
    async def test_batch_cleans_up_after_flush(self):
        """After flushing, internal state should be clean."""
        adapter = _make_adapter()

        adapter._enqueue_text_event(_make_event("test"))
        await asyncio.sleep(0.2)

        assert len(adapter._pending_text_batches) == 0
        assert len(adapter._pending_text_batch_tasks) == 0

    @pytest.mark.asyncio
    async def test_dm_topic_batching_recovers_thread_before_keying(self):
        """DM-topic text batches should use the recovered topic lane."""
        adapter = _make_adapter()
        adapter.set_topic_recovery_fn(
            lambda source: "222" if str(source.thread_id or "") == "1" else None
        )
        event = MessageEvent(
            text="hello from DM topic",
            message_type=MessageType.TEXT,
            source=SessionSource(
                platform=Platform.TELEGRAM,
                chat_id="12345",
                chat_type="dm",
                user_id="user-1",
                thread_id="1",
            ),
        )

        adapter._enqueue_text_event(event)

        def _key(thread_id: str) -> str:
            return build_session_key(
                SimpleNamespace(
                    platform=Platform.TELEGRAM,
                    chat_id="12345",
                    chat_type="dm",
                    thread_id=thread_id,
                ),
                group_sessions_per_user=True,
                thread_sessions_per_user=False,
            )

        assert _key("222") in adapter._pending_text_batches
        assert _key("1") not in adapter._pending_text_batches
        assert event.source.thread_id == "222"

        await asyncio.sleep(0.2)

        adapter.handle_message.assert_called_once()
        dispatched = adapter.handle_message.call_args[0][0]
        assert dispatched.source.thread_id == "222"

    @pytest.mark.asyncio
    async def test_incompatible_reply_context_not_merged_plain_then_reply(self):
        """A rapid reply after a plain message must keep its quote context."""
        adapter = _make_adapter()

        adapter._enqueue_text_event(_make_event("plain"))
        await asyncio.sleep(0.02)
        adapter._enqueue_text_event(
            _make_event(
                "reply",
                reply_to_message_id="42",
                reply_to_text="quoted context",
            )
        )

        await asyncio.sleep(0.25)

        assert adapter.handle_message.call_count == 2
        first = adapter.handle_message.call_args_list[0][0][0]
        second = adapter.handle_message.call_args_list[1][0][0]
        assert first.text == "plain"
        assert first.reply_to_message_id is None
        assert first.reply_to_text is None
        assert second.text == "reply"
        assert second.reply_to_message_id == "42"
        assert second.reply_to_text == "quoted context"

    @pytest.mark.asyncio
    async def test_incompatible_reply_context_not_merged_reply_then_plain(self):
        """A rapid plain message after a reply must not inherit the quote."""
        adapter = _make_adapter()

        adapter._enqueue_text_event(
            _make_event(
                "reply",
                reply_to_message_id="42",
                reply_to_text="quoted context",
            )
        )
        await asyncio.sleep(0.02)
        adapter._enqueue_text_event(_make_event("plain"))

        await asyncio.sleep(0.25)

        assert adapter.handle_message.call_count == 2
        first = adapter.handle_message.call_args_list[0][0][0]
        second = adapter.handle_message.call_args_list[1][0][0]
        assert first.text == "reply"
        assert first.reply_to_message_id == "42"
        assert first.reply_to_text == "quoted context"
        assert second.text == "plain"
        assert second.reply_to_message_id is None
        assert second.reply_to_text is None

    @pytest.mark.asyncio
    async def test_long_reply_split_continuation_preserves_reply_context(self):
        """Near-limit reply chunks may be followed by no-reply continuations."""
        adapter = _make_adapter()
        first_chunk = "x" * adapter._SPLIT_THRESHOLD

        adapter._enqueue_text_event(
            _make_event(
                first_chunk,
                reply_to_message_id="42",
                reply_to_text="quoted context",
            )
        )
        await asyncio.sleep(0.02)
        adapter._enqueue_text_event(_make_event("continuation"))

        await asyncio.sleep(0.25)

        adapter.handle_message.assert_called_once()
        dispatched = adapter.handle_message.call_args[0][0]
        assert dispatched.text == f"{first_chunk}\ncontinuation"
        assert dispatched.reply_to_message_id == "42"
        assert dispatched.reply_to_text == "quoted context"
