"""Real SQLite replay retains steer/hidden semantics and keeps metadata off the wire."""

from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from agent.agent_runtime_helpers import repair_message_sequence
from agent.prompt_builder import STEER_DISPLAY_KIND
from agent.transports.chat_completions import ChatCompletionsTransport
from gateway.platforms.api_server import _project_client_message
from gateway.run import _build_gateway_agent_history
from hermes_state import SessionDB


@pytest.mark.parametrize("kind", [STEER_DISPLAY_KIND, "hidden"])
def test_sqlite_reopen_preserves_display_boundary(tmp_path, kind):
    root = Path(__file__).resolve().parents[3]
    first = (root / "README.md").read_text(encoding="utf-8").splitlines()[0]
    second = (root / "AGENTS.md").read_text(encoding="utf-8").splitlines()[0]
    path = tmp_path / "state.db"
    sid = str(uuid4())
    metadata = {"notification_category": "diagnostic"} if kind == "hidden" else None
    db = SessionDB(db_path=path)
    try:
        db.create_session(sid, source="cli")
        db.append_message(sid, "user", first, display_kind=kind, display_metadata=metadata)
        db.append_message(sid, "user" if kind == STEER_DISPLAY_KIND else "assistant", second)
    finally:
        db.close()
    reopened = SessionDB(db_path=path)
    try:
        stored = reopened.get_messages_as_conversation(sid)
    finally:
        reopened.close()
    replay, observed = _build_gateway_agent_history(stored, inject_timestamps=False)
    assert observed is None
    assert replay[0]["display_kind"] == kind
    assert repair_message_sequence(None, replay) == 0
    assert [row["content"] for row in replay] == [first, second]
    if kind == "hidden":
        assert replay[0]["display_metadata"] == metadata
        assert _project_client_message(replay[0])["content"] == ""
    before = deepcopy(replay)
    wire = ChatCompletionsTransport().convert_messages(replay)
    assert all("display_kind" not in row and "display_metadata" not in row for row in wire)
    assert [row["content"] for row in wire] == [first, second]
    assert replay == before
