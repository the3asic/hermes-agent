"""Timestamp rendering must not discard the exact sent user-message prefix."""

from copy import deepcopy
from datetime import datetime
import hashlib
import json
from zoneinfo import ZoneInfo

import pytest

from gateway.message_timestamps import render_user_content_with_timestamp
from gateway.run import _build_gateway_agent_history, _select_cached_agent_history


STAMP = datetime(2026, 8, 20, 12, 0, tzinfo=ZoneInfo("UTC")).timestamp()
POLICY = "## Recall policy\nUse the retrieval tool when earlier details are needed."
NOTE = "[System note: Your previous turn was interrupted. Continue the old task.]"


def _render(text, timestamp=STAMP):
    from hermes_time import get_timezone

    return render_user_content_with_timestamp(text, timestamp, tz=get_timezone())


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


@pytest.mark.parametrize("timestamps", [False, True])
def test_timestamp_only_replay_preserves_sent_sidecar(timestamps):
    body = _render("first question") if timestamps else "first question"
    sidecar = body + "\n\n" + POLICY
    source = [{"role": "user", "content": "first question", "api_content": sidecar, "timestamp": STAMP}]
    saved = deepcopy(source)
    replay, observed = _build_gateway_agent_history(source, inject_timestamps=timestamps)

    assert observed is None
    assert replay[0]["content"] == body
    assert replay[0]["api_content"] == sidecar
    assert source == saved


@pytest.mark.parametrize("suffix", ["", "more text", "\nnot an injection", "\n\n" + POLICY])
def test_timestamp_rewrite_requires_an_exact_sidecar_body_boundary(suffix):
    rendered = _render("short")
    sidecar = rendered + suffix
    replay, _ = _build_gateway_agent_history(
        [{"role": "user", "content": "short", "api_content": sidecar, "timestamp": STAMP}],
        inject_timestamps=True,
    )
    assert ("api_content" in replay[0]) is (suffix == "" or suffix.startswith("\n\n"))


@pytest.mark.parametrize("sidecar", ["unrelated legacy text", "short\n\n" + POLICY, "[Thu 2026-08-20 20:00:00 SGT] short\n\n" + POLICY])
def test_unprovable_timestamp_sidecar_fails_closed(sidecar, monkeypatch):
    monkeypatch.setattr("hermes_time.get_timezone", lambda: ZoneInfo("UTC"))
    replay, _ = _build_gateway_agent_history(
        [{"role": "user", "content": "short", "api_content": sidecar, "timestamp": STAMP}],
        inject_timestamps=True,
    )
    assert "api_content" not in replay[0]
    assert replay[0]["content"] == _render("short")


@pytest.mark.parametrize("timestamps", [False, True])
@pytest.mark.parametrize("embedded", [False, True])
@pytest.mark.parametrize("real_text", ["", "please check the result"])
def test_recovery_cleanup_never_restores_a_sidecar(timestamps, embedded, real_text):
    original = NOTE + (" " + real_text if real_text else "")
    if embedded:
        original = "[2026-08-20T12:00:00+00:00] " + original
    replay, _ = _build_gateway_agent_history(
        [{"role": "user", "content": original, "api_content": original + "\n\n" + POLICY, "timestamp": STAMP + 100}],
        inject_timestamps=timestamps,
    )
    if not real_text:
        assert replay == []
    else:
        assert "api_content" not in replay[0]
        assert "System note:" not in replay[0]["content"]
        expected_timestamp = STAMP if embedded else STAMP + 100
        assert replay[0]["content"] == (_render(real_text, expected_timestamp) if timestamps else real_text)


@pytest.mark.parametrize("timestamps", [False, True])
def test_mirror_and_observed_rows_do_not_replay_sidecars(timestamps):
    source = [
        {"role": "user", "content": "mirror", "api_content": _render("mirror") + "\n\n" + POLICY, "timestamp": STAMP, "mirror": True, "mirror_source": "other"},
        {"role": "user", "content": "chatter", "api_content": _render("chatter") + "\n\n" + POLICY, "timestamp": STAMP, "observed": True},
    ]
    replay, observed = _build_gateway_agent_history(source, inject_timestamps=timestamps, channel_prompt="observed Telegram group context")
    assert len(replay) == 1
    assert "api_content" not in replay[0]
    assert replay[0]["content"] == "[Delivered from other] " + (_render("mirror") if timestamps else "mirror")
    assert observed == (_render("chatter") if timestamps else "chatter")
    assert POLICY not in observed


@pytest.mark.parametrize("timestamps", [False, True])
def test_expired_confirmation_redacts_sidecar(timestamps):
    replay, _ = _build_gateway_agent_history(
        [{"role": "user", "content": "confirm forced restart", "api_content": _render("confirm forced restart") + "\n\n" + POLICY, "timestamp": STAMP}],
        inject_timestamps=timestamps,
    )
    assert "api_content" not in replay[0]
    assert "confirm forced restart" not in replay[0]["content"]


@pytest.fixture
def responses_agent(tmp_path, monkeypatch):
    """Use the real agent/Responses converter; replace only the network call."""
    from hermes_state import SessionDB
    from run_agent import AIAgent

    captured = []
    responses = []
    sid = "sanitized-timestamp-replay"
    db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(
        "hermes_cli.plugins.invoke_hook",
        lambda hook, **kw: [{"context": POLICY}] if hook == "pre_llm_call" else [],
    )

    def respond(kwargs, **unused):
        captured.append(deepcopy(kwargs))
        output = responses.pop(0) if responses else [
            {"type": "message", "id": "msg_done", "role": "assistant", "status": "completed", "phase": "final_answer", "content": [{"type": "output_text", "text": "done", "annotations": []}]}
        ]
        from openai.types.responses import Response

        return Response.model_validate({
            "id": "resp_test", "object": "response", "created_at": STAMP,
            "model": "test-model", "status": "completed", "output": output,
            "usage": None, "error": None, "incomplete_details": None,
            "instructions": None, "metadata": {}, "parallel_tool_calls": True,
            "temperature": None, "tool_choice": "auto", "tools": [], "top_p": None,
        })

    def make_agent():
        agent = AIAgent(
            api_key="test-key", base_url="http://127.0.0.1:1/v1", provider="openai-compat",
            model="test-model", api_mode="codex_responses", max_iterations=4,
            enabled_toolsets=[], quiet_mode=True, skip_context_files=True,
            skip_memory=True, save_trajectories=False, session_db=db, session_id=sid,
        )
        agent._cached_system_prompt = "Stable synthetic system prompt."
        agent.valid_tool_names = {"read_file"}
        monkeypatch.setattr(agent, "_interruptible_streaming_api_call", respond)
        monkeypatch.setattr(agent, "_interruptible_api_call", respond)
        return agent

    yield make_agent, captured, responses, db, sid
    db.close()


@pytest.mark.parametrize("timestamps", [False, True])
@pytest.mark.parametrize("resume", ["cached", "db", "json_fixture"])
def test_full_builder_to_responses_keeps_cross_turn_prefix(responses_agent, tmp_path, timestamps, resume, request):
    make_agent, captured, responses, db, sid = responses_agent
    agent = make_agent()
    tool_file = tmp_path / "sanitized-tool.txt"
    tool_file.write_text("fixture result", encoding="utf-8")
    responses.extend([
        [
            {"type": "reasoning", "id": "rs_test", "summary": [], "encrypted_content": "synthetic-reasoning"},
            {"type": "function_call", "id": "fc_test", "call_id": "call_test", "name": "read_file", "arguments": json.dumps({"path": str(tool_file)})},
        ],
        [{"type": "message", "id": "msg_done", "role": "assistant", "status": "completed", "phase": "final_answer", "content": [{"type": "output_text", "text": "done", "annotations": []}]}],
    ])
    current = _render("first question") if timestamps else "first question"
    result = agent.run_conversation(current, conversation_history=[], task_id="first", persist_user_message="first question", persist_user_timestamp=STAMP)
    assert result["completed"]
    assert len(captured) == 2
    first_input = captured[0]["input"]
    continuation = captured[1]["input"]
    assert continuation[:len(first_input)] == first_input
    assert any(item.get("call_id") == "call_test" and item.get("type") == "function_call_output" for item in continuation)

    history = db.get_messages_as_conversation(sid)
    if resume == "db":
        from hermes_state import SessionDB

        reopened = SessionDB(db_path=tmp_path / "state.db")
        try:
            history = reopened.get_messages_as_conversation(sid)
        finally:
            reopened.close()
    elif resume == "json_fixture":
        # Exercise the optional production JSON writer, then read the same
        # message field accepted by the conversation-history entry point.
        # This is a serialized fixture; the gateway's live restore uses DB.
        agent._session_json_enabled = True
        agent.logs_dir = tmp_path / "snapshots"
        agent.logs_dir.mkdir()
        agent._save_session_log(history)
        path = agent.logs_dir / f"session_{sid}.json"
        history = json.loads(path.read_text(encoding="utf-8"))["messages"]
    replay, observed = _build_gateway_agent_history(history, inject_timestamps=timestamps)
    assert observed is None
    if resume == "cached":
        replay = _select_cached_agent_history(replay, agent._session_messages)
    else:
        agent = make_agent()
    next_user = _render("second question", STAMP + 30) if timestamps else "second question"
    result = agent.run_conversation(next_user, conversation_history=replay, task_id="second", persist_user_message="second question", persist_user_timestamp=STAMP + 30)
    assert result["completed"]
    assert len(captured) == 3
    next_input = captured[2]["input"]
    request.node.user_properties.extend([
        ("initial_input_sha256", _digest(first_input)),
        ("tool_continuation_prefix_sha256", _digest(continuation[:len(first_input)])),
        ("tool_continuation_input_sha256", _digest(continuation)),
        ("next_user_prior_prefix_sha256", _digest(next_input[:len(continuation)])),
        ("initial_input_items", len(first_input)),
        ("tool_continuation_input_items", len(continuation)),
        ("next_user_input_items", len(next_input)),
    ])
    assert _digest(next_input[:len(continuation)]) == _digest(continuation)
    assert next_input[:len(continuation)] == continuation
    assert not any("api_content" in item or "timestamp" in item for item in next_input)
    assert any(item.get("encrypted_content") == "synthetic-reasoning" for item in next_input)
    assert any(item.get("phase") == "final_answer" for item in next_input)
