"""Configured compression routes must carry output limits onto the real wire."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest
import yaml

from agent import auxiliary_client as aux


@pytest.fixture
def responses_endpoint(monkeypatch):
    from hermes_cli import _early_recovery, venv_sync
    from pm import environments

    # This checkout may itself live under the guarded production home.
    # Install-transaction recovery is outside auxiliary routing; do not let a
    # late HTTP transport import inspect or repair the running installation.
    monkeypatch.setattr(_early_recovery, "restore_interrupted_pull", lambda *args, **kwargs: False)
    monkeypatch.setattr(_early_recovery, "recover_if_needed", lambda *args, **kwargs: None)
    monkeypatch.setattr(venv_sync, "prepare_launch", lambda *args, **kwargs: None)
    monkeypatch.setattr(environments, "activate_dependencies", lambda *args, **kwargs: None)
    aux.shutdown_cached_clients()
    aux._reset_aux_unhealthy_cache()
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    requests = []
    state = {"scenario": "primary"}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            key = self.headers.get("Authorization")
            requests.append((self.path, key, body))
            status = None
            if state["scenario"] == "fallback" and self.path.startswith("/primary/"):
                status = 402
            elif state["scenario"] == "credential_retry" and key == "Bearer primary-key":
                status = 401
            if status is not None:
                payload = json.dumps({"error": {
                    "message": "Unauthorized" if status == 401 else "Insufficient credits",
                    "type": "authentication_error" if status == 401 else "insufficient_quota",
                }}).encode()
                content_type = "application/json"
            else:
                item = {"id": "msg-summary", "type": "message", "role": "assistant",
                        "status": "completed", "content": [
                            {"type": "output_text", "text": "Preserved summary facts.", "annotations": []},
                        ]}
                events = [
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": {
                        "id": "resp-summary", "status": "completed", "model": body["model"],
                        "output": [item], "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
                    }},
                ]
                payload = "".join(
                    f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
                ).encode()
                status, content_type = 200, "text/event-stream"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests, state
    finally:
        aux.shutdown_cached_clients()
        aux._reset_aux_unhealthy_cache()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("scenario", ["primary", "fallback", "credential_retry"])
def test_configured_responses_route_preserves_compression_limit(
    tmp_path, monkeypatch, responses_endpoint, scenario, async_mode,
):
    endpoint, requests, state = responses_endpoint
    state["scenario"] = scenario
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = {
        "model": {"provider": "summary-proxy", "default": "gpt-6-astra"},
        "providers": {
            "summary-proxy": {"base_url": endpoint + "/primary/v1", "api_mode": "codex_responses"},
            "summary-backup": {"base_url": endpoint + "/backup/v1", "api_mode": "codex_responses",
                               "api_key": "backup-key"},
        },
        "auxiliary": {"compression": {
            "provider": "summary-proxy", "model": "gpt-6-astra", "timeout": 10,
            "fallback_chain": [{"provider": "summary-backup", "model": "gpt-6-astra"}],
        }},
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (tmp_path / "auth.json").write_text(json.dumps({
        "version": 1, "providers": {}, "credential_pool": {"summary-proxy": [
            {"id": "primary", "label": "primary", "auth_type": "api_key", "priority": 0,
             "source": "manual", "access_token": "primary-key"},
            {"id": "fresh", "label": "fresh", "auth_type": "api_key", "priority": 1,
             "source": "manual", "access_token": "fresh-key"},
        ]},
    }), encoding="utf-8")
    request = {"task": "compression", "messages": [
        {"role": "user", "content": "Summarize these source facts."},
    ], "max_tokens": 2048, "timeout": 10}
    response = asyncio.run(aux.async_call_llm(**request)) if async_mode else aux.call_llm(**request)
    assert response.choices[0].message.content == "Preserved summary facts."
    expected_routes = {
        "primary": [("/primary/v1/responses", "Bearer primary-key")],
        "fallback": [("/primary/v1/responses", "Bearer primary-key"),
                     ("/primary/v1/responses", "Bearer fresh-key"),
                     ("/backup/v1/responses", "Bearer backup-key")],
        "credential_retry": [("/primary/v1/responses", "Bearer primary-key"),
                             ("/primary/v1/responses", "Bearer fresh-key")],
    }
    assert [(path, key) for path, key, _ in requests] == expected_routes[scenario]
    for _, _, body in requests:
        assert body["max_output_tokens"] == request["max_tokens"]
        assert "max_tokens" not in body and "max_completion_tokens" not in body
        assert "Summarize these source facts." in json.dumps(body["input"])
