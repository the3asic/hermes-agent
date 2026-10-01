"""Repeated message text keeps its distinct durable identity across gateway replay."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent.turn_context import build_api_messages
from gateway.config import GatewayConfig
from gateway.run import _build_gateway_agent_history, _build_replay_entry
from gateway.session import SessionStore


@pytest.mark.parametrize("timestamps", [False, True])
def test_real_transcript_reopen_preserves_distinct_row_ids_off_wire(tmp_path, monkeypatch, timestamps):
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    sid = "repeated-message-identity"
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    store._db.create_session(sid, source="telegram", model="test-model")
    rows = [{"role": role, "content": text, "timestamp": 1234567890.0} for role, text in (
        ("user", "continue"), ("assistant", "OK"), ("user", "continue"), ("assistant", "OK"),
    )]
    try:
        assert store._db.append_messages_batch(sid, rows) == 4
        ids = [row["_row_id"] for row in rows]
        assert len(set(ids)) == 4
    finally:
        store._db.close()
        store._db = None

    reopened = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    try:
        history = reopened.load_transcript(sid)
    finally:
        reopened._db.close()
        reopened._db = None
    assert [msg["_row_id"] for msg in history] == ids
    replay, _ = _build_gateway_agent_history(history, inject_timestamps=timestamps)
    assert [msg["_row_id"] for msg in replay] == ids
    before = deepcopy(replay)
    agent = SimpleNamespace(
        _copy_reasoning_content_for_api=lambda *_: None,
        _should_sanitize_tool_calls=lambda: False,
        ephemeral_system_prompt=None,
    )
    wire, _ = build_api_messages(
        agent, replay, current_turn_user_idx=None, ext_prefetch_cache=None,
        plugin_user_context=None, moa_config=None, active_system_prompt="system",
    )
    assert all("_row_id" not in msg for msg in wire)
    assert [msg["content"] for msg in wire[1:]] == [msg["content"] for msg in replay]
    assert replay == before


@pytest.mark.parametrize("row_id", [None, True, False, 0, -1, "1"])
def test_replay_ignores_invalid_row_identity(row_id):
    msg = {"role": "user", "content": "continue", "_row_id": row_id}
    assert _build_replay_entry("user", "continue", msg) == {"role": "user", "content": "continue"}
