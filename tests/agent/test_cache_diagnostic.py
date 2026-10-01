"""Synthetic only. Real HTTPX/SDK traffic never leaves loopback."""
import concurrent.futures
import contextlib
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import multiprocessing
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import httpx
import pytest
from openai import OpenAI

from agent import cache_diagnostic as d


SID = next(iter(d.SESSIONS))


@pytest.fixture
def root(tmp_path, monkeypatch):
    target = tmp_path / "capture"
    monkeypatch.setattr(d, "ROOT", target)
    d._PREVIOUS.clear()
    d._FAULTS.clear()
    return target


def arm(root, **kwargs):
    return d.arm(root, owner="synthetic-test", baseline=d.BASELINE, write=True, **kwargs)


def read_requests(root):
    return [json.loads(gzip.decompress(p.read_bytes())) for p in sorted(root.glob("*.request.json.gz"))]


def read_responses(root):
    return [json.loads(p.read_bytes()) for p in sorted(root.glob("*.response.json"))]


@contextlib.contextmanager
def active(root, *, task="main", session=SID):
    scope = dict(session=session, task=task, attempts=[])
    token = d._ACTIVE.set(scope)
    try:
        yield scope
    finally:
        d._ACTIVE.reset(token)
        for item in scope["attempts"]:
            item.complete("synthetic_finished")


@pytest.fixture
def server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.server.requests.append((self.rfile.read(int(self.headers["Content-Length"])), list(self.headers.items())))
            code, raw, content_type = self.server.replies.pop(0)
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    fixture = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    fixture.requests, fixture.replies = [], []
    thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    thread.start()
    yield fixture, f"http://127.0.0.1:{fixture.server_port}/v1"
    fixture.shutdown()
    fixture.server_close()
    thread.join(timeout=2)


def test_defaults_dryrun_no_clobber(root):
    plan = d.arm(root, owner="synthetic", baseline=d.BASELINE)
    assert plan == {"state": "dry_run", "limit": 1000, "seconds": 86400, "storage_limit": 1073741824}
    assert not root.exists()
    assert d.status(root)["state"] == "absent"
    arm(root)
    with pytest.raises(FileExistsError):
        arm(root)
    assert d.disarm(root)["state"] == "dry_run"
    assert d.status(root)["capture_open"]
    assert d.disarm(root, write=True)["state"] == "disarmed"
    assert d.reserve(root, SID, "main") is None


@pytest.mark.parametrize("values", [{"limit": 1001}, {"limit": 0}, {"seconds": 86401}, {"storage": 1073741825}])
def test_hard_bounds(root, values):
    with pytest.raises(ValueError):
        arm(root, **values)
    assert not root.exists()


def test_natural_ceiling_includes_failed_attempts_and_restart(root):
    arm(root)
    # Use actual reservations, no model calls and no state-counter shortcuts.
    for _ in range(1000):
        attempt = d.reserve(root, SID, "main")
        assert attempt is not None
        attempt.complete("call_failed_or_cancelled")
    assert d.status(root)["reserved"] == 1000
    assert d.status(root)["completed"] == 1000
    assert d.status(root)["state"] == "count_exhausted"
    ctx = multiprocessing.get_context("fork")
    queue = ctx.Queue()
    proc = ctx.Process(target=lambda: queue.put(d.reserve(root, SID, "main") is None))
    proc.start()
    proc.join(10)
    assert proc.exitcode == 0 and queue.get(timeout=2)


def _reserve_batch(root, count, queue):
    results = []
    for _ in range(count):
        attempt = d.reserve(Path(root), SID, "main")
        if attempt:
            results.append(attempt.receipt)
            attempt.complete("call_failed_or_cancelled")
    queue.put(results)


def test_concurrent_processes_threads_restart_resume(root):
    arm(root)
    # A killed owner keeps a durable pending reservation and its storage allowance.
    first = d.reserve(root, SID, "main")
    ctx = multiprocessing.get_context("fork")
    queue = ctx.Queue()
    processes = [ctx.Process(target=_reserve_batch, args=(str(root), 200, queue)) for _ in range(4)]
    for proc in processes:
        proc.start()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        def one(_):
            attempt = d.reserve(root, SID, "background_review")
            if attempt:
                attempt.complete("cancelled")
                return attempt.receipt
        thread_ids = list(pool.map(one, range(200)))
    ids = [first.receipt] + [v for v in thread_ids if v]
    for _ in processes:
        ids.extend(queue.get(timeout=30))
    for proc in processes:
        proc.join(10)
        assert proc.exitcode == 0
    assert len(ids) == len(set(ids)) == 1000
    state = d.status(root)
    assert state["reserved"] == 1000 and state["completed"] == 999
    assert len(state["pending"]) == 1
    first.complete("late_cancelled")
    assert d.status(root)["completed"] == 1000
    assert d.reserve(root, SID, "main") is None


def test_expiry_size_and_reserved_completion(root, monkeypatch):
    arm(root, storage=d.CONTROL_BYTES + 2 * d.META_BYTES + 1)
    first = d.reserve(root, SID, "main")
    assert first
    assert d.reserve(root, SID, "main") is None
    assert d.status(root)["state"] == "size_exhausted"
    first.complete("cancelled")
    assert d.status(root)["state"] == "size_exhausted"
    assert sum(p.stat().st_size for p in root.iterdir()) <= d.CONTROL_BYTES + 2 * d.META_BYTES + 1
    other = root.parent / "expiry"
    state = arm(other)
    first = d.reserve(other, SID, "main")
    monkeypatch.setattr(d.time, "time", lambda: state["expires"] + 1)
    assert d.reserve(other, SID, "main") is None
    assert d.status(other)["state"] == "expired"
    first.complete("late_response")
    assert d.status(other)["completed"] == 1


@pytest.mark.parametrize("breakage", ["root_mode", "key_mode", "key_short", "symlink", "state_corrupt", "io_error"])
def test_fail_closed_filesystem(root, monkeypatch, breakage):
    arm(root)
    if breakage == "root_mode":
        root.chmod(0o755)
    elif breakage == "key_mode":
        (root / "key").chmod(0o644)
    elif breakage == "key_short":
        (root / "key").write_bytes(b"short")
    elif breakage == "symlink":
        old = root / "state.json"
        moved = root / "old-state.json"
        old.rename(moved)
        old.symlink_to(moved)
    elif breakage == "state_corrupt":
        (root / "state.json").write_text("broken")
    else:
        monkeypatch.setattr(d, "_atomic", lambda *args: (_ for _ in ()).throw(OSError("synthetic-private-body")))
    assert d.reserve(root, SID, "main") is None
    assert d.status(root)["state"] == "error"
    assert not list(root.glob("*.request.json.gz"))


def test_redaction_canaries_pre_redaction_proof_and_prefix(root, monkeypatch):
    arm(root)
    canaries = ["sk-" + "syntheticcredential" * 3, "cookedCookie987", "envCANARY19283", "reasonCANARY471", "audioCANARY928", "passwordCANARY783"]
    monkeypatch.setenv("SYNTHETIC_API_KEY", canaries[2])
    payload = {"model": d.MODEL, "instructions": f"Authorization: Bearer {canaries[0]}\nCookie: {canaries[1]}\nBare env {canaries[2]}\n<think>{canaries[3]}</think>",
               "prompt_cache_key": "synthetic-private-key", "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}],
               "input": [{"role": "user", "content": "stable prefix"}, {"type": "function_call", "name": "lookup", "arguments": json.dumps({"password": canaries[5], "query": "keep this"})},
                         {"type": "reasoning", "encrypted_content": canaries[3]}, {"type": "input_audio", "data": canaries[4]}]}
    raw = json.dumps(payload).encode()
    for body in [raw, json.dumps({**payload, "input": payload["input"] + [{"role": "user", "content": "new turn"}]}).encode()]:
        item = d.reserve(root, SID, "main")
        item.request(httpx.Request("POST", "http://127.0.0.1/v1/responses", content=body,
                                   headers={"Authorization": f"Bearer {canaries[0]}", "Cookie": canaries[1], "session_id": "sensitive-id"}))
        item.complete("returned")
    records = read_requests(root)
    visible = json.dumps(records)
    assert all(canary not in visible for canary in canaries)
    assert records[0]["metadata"]["wire_hmac"] == d.digest((root / "key").read_bytes(), raw)
    assert records[1]["metadata"]["prefix"]["input_common_items"] == 4
    assert records[1]["metadata"]["prefix"]["nonempty"]
    assert "authorization" not in records[0]["metadata"]["headers"]
    assert records[0]["request"]["tools"][0]["parameters"]["properties"]["query"]["type"] == "string"
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in root.iterdir())


def test_redactor_failure_keeps_hash_only(root, monkeypatch):
    arm(root)
    from agent import redact
    monkeypatch.setattr(redact, "redact_sensitive_text", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("CANARY-must-not-log")))
    item = d.reserve(root, SID, "main")
    item.request(httpx.Request("POST", "http://localhost/v1/responses", json={"input": [{"role": "user", "content": "CANARY-must-not-log"}]}))
    item.complete("returned")
    record = read_requests(root)[0]
    assert record["request"] is None
    assert record["metadata"]["snapshot_status"] == "redaction_failed"
    assert record["metadata"]["wire_hmac"]
    assert "CANARY-must-not-log" not in json.dumps(record)


def test_item_and_snapshot_limits_explicit(root, monkeypatch):
    arm(root)
    item = d.reserve(root, SID, "main")
    body = {"input": [{"role": "user", "content": str(i)} for i in range(2100)]}
    item.request(httpx.Request("POST", "http://localhost/v1/responses", json=body))
    item.complete("returned")
    record = read_requests(root)[0]
    assert record["metadata"]["input_total"] == 2100
    assert record["metadata"]["input_partial"]
    assert len(d.packed(record["metadata"])) <= d.META_BYTES
    assert len(record["request"]["input"]) == 2100
    monkeypatch.setattr(d, "MAX_BODY", 1)
    item = d.reserve(root, SID, "main")
    item.request(httpx.Request("POST", "http://localhost/v1/responses", json=body))
    item.complete("returned")
    assert read_requests(root)[1]["metadata"]["snapshot_status"] == "body_size_limit"


@pytest.mark.parametrize("details,state", [({}, "missing"), ({"cached_tokens": None}, "null"), ({"cached_tokens": 0}, "numeric"), (None, "null_parent")])
def test_http_sdk_usage_states_and_unchanged_wire(root, server, details, state):
    fixture, url = server
    usage = {"input_tokens": 20, "output_tokens": 1, "total_tokens": 21, "input_tokens_details": details}
    response = {"type": "response.completed", "response": {"id": "responseCANARY", "status": "completed", "usage": usage, "output": []}}
    raw = ("data: " + json.dumps(response) + "\n\n").encode()
    fixture.replies = [(200, raw, "text/event-stream")] * 2
    client = OpenAI(base_url=url, api_key="synthetic-only", max_retries=0, http_client=httpx.Client(trust_env=False))
    kwargs = {"model": d.MODEL, "input": [{"role": "user", "content": "stable"}], "stream": True, "extra_headers": {"session_id": "same"}}
    with client.responses.create(**kwargs) as stream:
        off = [event.model_dump() for event in stream]
    arm(root)
    with active(root):
        d.instrument_openai_client(client)
        with client.responses.create(**kwargs) as stream:
            on = [event.model_dump() for event in stream]
    client.close()
    assert off == on and fixture.requests[0] == fixture.requests[1]
    assert read_responses(root)[0]["raw_usage"]["input_tokens_details.cached_tokens"]["state"] == state
    assert read_requests(root)[0]["metadata"]["wire_hmac"] == d.digest((root / "key").read_bytes(), fixture.requests[0][0])


@pytest.mark.parametrize("data", [b"", b"data: {\n\n", b"data: {\"type\":\"error\"}\n\n", b"data: {\"type\":\"response.failed\",\"response\":{\"usage\":null}}\n\n"])
def test_stream_empty_partial_error_and_exact_chunks(root, data):
    arm(root)
    item = d.reserve(root, SID, "main")
    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            yield data[:3]
            yield data[3:]
    observed = d.ObservedStream(Chunks(), item, {"content-type": "text/event-stream"})
    assert list(observed) == [data[:3], data[3:]]
    item.complete("returned")
    assert d.status(root)["completed"] == 1


def test_cancel_and_send_failure(root):
    arm(root)
    item = d.reserve(root, SID, "main")
    class Cancelled(httpx.SyncByteStream):
        def __iter__(self):
            yield b"data: {}\n\n"
            raise GeneratorExit()
    observed = d.ObservedStream(Cancelled(), item, {"content-type": "text/event-stream"})
    with pytest.raises(GeneratorExit):
        list(observed)
    item.complete("cancelled")
    assert read_responses(root)[0]["wire_status"] == "cancelled_or_closed"
    client = OpenAI(api_key="synthetic-only", base_url="http://localhost/v1", max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(lambda req: (_ for _ in ()).throw(httpx.ConnectError("synthetic")))))
    with active(root):
        d.instrument_openai_client(client)
        with pytest.raises(Exception):
            client.responses.create(model=d.MODEL, input="synthetic", stream=True)
    client.close()
    assert d.status(root)["reserved"] == 2
    assert read_responses(root)[1]["wire_status"] == "send_failed_or_cancelled"


def test_sdk_retry_is_distinct_reservation(root, server):
    fixture, url = server
    fixture.replies = [(500, b'{"error":{"message":"synthetic","type":"server_error"}}', "application/json"),
                       (200, b'data: {"type":"response.completed","response":{"id":"r","output":[],"usage":{"input_tokens":1}}}\n\n', "text/event-stream")]
    arm(root)
    client = OpenAI(base_url=url, api_key="synthetic-only", max_retries=1, http_client=httpx.Client(trust_env=False))
    with active(root):
        d.instrument_openai_client(client)
        with client.responses.create(model=d.MODEL, input="synthetic", stream=True) as stream:
            list(stream)
    client.close()
    assert d.status(root)["reserved"] == d.status(root)["completed"] == 2
    assert [r["http_status"] for r in read_responses(root)] == [500, 200]


@pytest.mark.parametrize("session,task,model,parent", [("unrelated", "main", d.MODEL, None), (SID,"cron", d.MODEL,None), (SID,"main","another-model",None), (SID,"main",d.MODEL,"worker-parent")])
def test_unrelated_scopes_never_reserve(root, session, task, model, parent):
    arm(root)
    @d.observe("main")
    def call(agent, kwargs):
        assert d._ACTIVE.get() is None
        return 7
    owner = SimpleNamespace(session_id=session, _memory_write_origin="assistant_tool" if task == "main" else task, _parent_session_id=parent)
    assert call(owner, {"model": model}) == 7
    assert d.status(root)["reserved"] == 0


def test_no_arm_does_not_instrument(root):
    @d.observe("main")
    def call(agent, kwargs):
        assert d._ACTIVE.get() is None
        return 7
    assert call(SimpleNamespace(session_id=SID), {"model": d.MODEL}) == 7
    assert not root.exists()


def test_redirect_and_shared_client_do_not_escape_scope(root):
    arm(root)
    seen = []
    def handler(request):
        seen.append(request.url.path)
        if len(seen) == 1:
            return httpx.Response(307, headers={"Location": "http://localhost/other/responses"})
        return httpx.Response(200, json={"id": "r", "output": [], "usage": {"input_tokens": 1}})
    client = OpenAI(base_url="http://localhost/v1", api_key="synthetic", max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True))
    with active(root):
        d.instrument_openai_client(client)
        client.responses.create(model=d.MODEL, input="synthetic")
    assert len(seen) == 2
    assert d.status(root)["reserved"] == 2
    client.responses.create(model="unrelated", input="synthetic unrelated")
    assert d.status(root)["reserved"] == 2
    client.close()


def test_observer_failure_does_not_break_http(root, server, monkeypatch):
    fixture, url = server
    fixture.replies = [(200, b'{"id":"r","output":[],"usage":{"input_tokens":1}}', "application/json")]
    arm(root)
    monkeypatch.setattr(d.Attempt, "request", lambda *args: (_ for _ in ()).throw(OSError("secret-canary")))
    client = OpenAI(base_url=url, api_key="synthetic", max_retries=0, http_client=httpx.Client(trust_env=False))
    with active(root):
        d.instrument_openai_client(client)
        response = client.responses.create(model=d.MODEL, input="synthetic")
    assert response.id == "r"
    assert d.status(root)["state"] == "error"
    assert d.status(root)["completed"] == 1
    client.close()


def test_compare_emits_only_structural_diff(root):
    arm(root)
    for text in ["private synthetic one", "private synthetic two"]:
        item = d.reserve(root, SID, "main")
        item.request(httpx.Request("POST", "http://localhost/v1/responses", json={"instructions": "stable", "input": [{"role": "user", "content": text}]}))
        item.complete("returned")
    result = d.compare(root)
    assert len(result["records"]) == 2
    assert result["records"][1]["changed_fields"] == ["input"]
    assert "private synthetic" not in json.dumps(result)


def test_missing_terminal_usage_not_created_null(root):
    arm(root)
    item = d.reserve(root, SID, "main")
    item.event({"type": "response.created", "response": {"usage": None}})
    item.event({"type": "response.completed", "response": {"status": "completed"}})
    item.complete("returned")
    assert read_responses(root)[0]["raw_usage"]["input_tokens"]["state"] == "missing"


def test_root_replaced_with_symlink_between_reservation_and_write(root):
    arm(root)
    item = d.reserve(root, SID, "main")
    moved = root.parent / "moved"
    root.rename(moved)
    root.symlink_to(moved, target_is_directory=True)
    with pytest.raises(OSError):
        item.request(httpx.Request("POST", "http://localhost/v1/responses", json={"input": []}))
    assert not list(moved.glob("*.request.json.gz"))


def test_nested_excluded_model_cannot_inherit_capture(root):
    arm(root)
    @d.observe("main")
    def nested(agent, kwargs):
        assert d._ACTIVE.get() is None
        return "untouched"
    with active(root):
        assert nested(SimpleNamespace(session_id=SID), {"model": "another-model"}) == "untouched"
        assert d._ACTIVE.get() is not None
    assert d.status(root)["reserved"] == 0
