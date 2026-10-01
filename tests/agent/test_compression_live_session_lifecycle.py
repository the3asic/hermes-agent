"""In-place compaction must not finalize a still-active context-engine session."""

from __future__ import annotations

import copy
import sqlite3
from unittest.mock import MagicMock

import pytest

from agent.context_compressor import ContextCompressor
from hermes_state import SessionDB
from run_agent import AIAgent


def _agent_with_lifecycle(db, *, in_place, monkeypatch, plugin=True):
    monkeypatch.setattr("agent.context_compressor.get_model_context_length", lambda *_args, **_kwargs: 256_000)
    monkeypatch.setattr("agent.conversation_compression._refresh_agent_tool_definitions", lambda _agent: None)
    sid = "live-context-engine-session"
    db.create_session(sid, source="test")
    messages = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}: " + "text " * 80}
        for i in range(10)
    ]
    db.append_messages_batch(sid, messages)
    agent = AIAgent(
        api_key="test-key", base_url="https://openrouter.ai/api/v1", model="test/model",
        quiet_mode=True, session_db=db, session_id=sid, skip_context_files=True, skip_memory=True,
    )
    agent.compression_in_place = in_place
    agent._compression_feasibility_checked = True
    monkeypatch.setattr(agent, "_build_system_prompt", lambda system_message: system_message)
    events = []
    owner = {"session_id": sid}

    class RecordingPluginContextEngine(ContextCompressor):
        def on_session_end(self, session_id, transcript):
            events.append(("end", session_id))
            owner["session_id"] = None

    if plugin:
        engine = RecordingPluginContextEngine.__new__(RecordingPluginContextEngine)
        engine.__dict__.update(agent.context_compressor.__dict__)
        agent.context_compressor = engine
    else:
        engine = agent.context_compressor
    real_start = engine.on_session_start
    def start(session_id, **kwargs):
        events.append(("start", session_id))
        owner["session_id"] = session_id
        real_start(session_id, **kwargs)

    monkeypatch.setattr(engine, "on_session_start", start)
    monkeypatch.setattr(engine, "compress", lambda incoming, **kwargs: [
        {"role": "user", "content": "[CONTEXT COMPACTION] prior turns summarized"},
        copy.deepcopy(incoming[-1]),
    ])
    memory = MagicMock()
    memory.build_system_prompt.return_value = ""
    agent._memory_manager = memory
    return agent, messages, memory, events, owner


@pytest.mark.parametrize("outcome", ["committed", "refused", "archive_failed"])
def test_in_place_compaction_preserves_live_engine_ownership(tmp_path, monkeypatch, outcome):
    """Extraction still runs, but refusal/rollback cannot end the live engine."""
    db = SessionDB(db_path=tmp_path / "state.db")
    agent, messages, memory, events, owner = _agent_with_lifecycle(
        db, in_place=True, monkeypatch=monkeypatch,
    )
    before = db.get_messages(agent.session_id, include_inactive=True)
    if outcome == "refused":
        monkeypatch.setattr(agent.context_compressor, "compress", lambda *_args, **_kwargs: [
            {"role": "user", "content": "X" * 40_000},
        ])
        monkeypatch.setattr("agent.context_compressor.salvage_grown_transcript", lambda *_args, **_kwargs: None)
    elif outcome == "archive_failed":
        def failed_archive(*_args, **_kwargs):
            raise sqlite3.OperationalError("injected archive failure")

        monkeypatch.setattr(db, "archive_and_compact", failed_archive)

    returned, _prompt = agent._compress_context(messages, "sys", approx_tokens=100_000, force=True)

    memory.on_session_end.assert_called_once()
    extracted = memory.on_session_end.call_args.args[0]
    assert [(m["role"], m["content"]) for m in extracted] == [
        (m["role"], m["content"]) for m in messages
    ]
    assert owner["session_id"] == agent.session_id == "live-context-engine-session"
    assert not [event for event in events if event[0] == "end"]
    assert db.get_session(agent.session_id)["ended_at"] is None
    assert db.get_compression_lock_holder(agent.session_id) is None
    if outcome == "committed":
        assert events == [("start", agent.session_id)]
        assert len(db.get_messages(agent.session_id, include_inactive=True)) > len(before)
    else:
        assert returned is messages
        assert events == []
        assert db.get_messages(agent.session_id, include_inactive=True) == before


@pytest.mark.parametrize("boundary", ["rotation", "commit", "shutdown", "builtin_in_place"])
def test_real_session_boundaries_still_finalize_engine(tmp_path, monkeypatch, boundary):
    """Actual boundaries retain end hooks, and built-in summary state still resets."""
    db = SessionDB(db_path=tmp_path / "state.db")
    agent, messages, memory, events, owner = _agent_with_lifecycle(
        db, in_place=boundary == "builtin_in_place", monkeypatch=monkeypatch,
        plugin=boundary != "builtin_in_place",
    )
    original_sid = agent.session_id
    if boundary == "rotation":
        agent._compress_context(messages, "sys", approx_tokens=100_000, force=True)
        assert agent.session_id != original_sid
        assert events == [("end", original_sid), ("start", agent.session_id)]
        assert owner["session_id"] == agent.session_id
        assert db.get_session(original_sid)["end_reason"] == "compression"
    elif boundary == "commit":
        agent.commit_memory_session(messages)
        assert events == [("end", original_sid)]
        assert owner["session_id"] is None
    elif boundary == "shutdown":
        agent.shutdown_memory_provider(messages)
        agent.shutdown_memory_provider(messages)
        assert events == [("end", original_sid)]
        assert owner["session_id"] is None
        memory.shutdown_all.assert_called_once_with()
    else:
        agent.context_compressor._previous_summary = "previous summary state"
        agent._compress_context(messages, "sys", approx_tokens=100_000, force=True)
        assert agent.session_id == original_sid
        assert agent.context_compressor._previous_summary is None
    memory.on_session_end.assert_called_once()
    extracted = memory.on_session_end.call_args.args[0]
    assert [(m["role"], m["content"]) for m in extracted] == [
        (m["role"], m["content"]) for m in messages
    ]
