"""Temporary, local-only Responses diagnostic. No arm file means no capture.

The observer sees Hermes -> HTTPX -> CLIProxy, not CLIProxy -> provider.
Only sanitized request snapshots and numeric response usage reach disk.
Nothing in this module edits a model request or makes a model call.
"""
from __future__ import annotations

import argparse
import contextvars
import functools
import gzip
import hashlib
import hmac
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
import uuid

ROOT = Path.home() / "the3asic-agentkit/artifacts/hermes-cache-diagnostic-20260906"
BASELINE = "6c5ec5632d9fdc505d538bbd5b1693b1fe4bf474"
SESSIONS = {
    "20260904_101116_40e5c670": "TG2099",
    "20260902_124854_8315fd3e": "TG4065",
    "20260902_001032_526542f5": "TG2996",
}
MAX_REQUESTS = 1000
MODEL = "gpt-6-astra"
MAX_SECONDS = 24 * 60 * 60
MAX_STORAGE = 1024 ** 3
CONTROL_BYTES = 2 * 1024 ** 2
SLOT_BYTES = 8 * 1024 ** 2
META_BYTES = 64 * 1024
MAX_BODY = 32 * 1024 ** 2
MAX_EVENT = 2 * 1024 ** 2
_ACTIVE = contextvars.ContextVar("cache_diagnostic_attempt_scope", default=None)
_INSTALL_LOCK = threading.Lock()
_PREVIOUS_LOCK = threading.Lock()
_PREVIOUS = {}  # At most the three authorized sessions, memory only.
_FAULTS = set()
HEADERS = frozenset({"session_id", "conversation_id", "x-session-id",
                     "x-conversation-id", "originator", "openai-beta",
                     "user-agent", "chatgpt-account-id", "openai-organization",
                     "openai-project", "x-initiator", "x-client-request-id"})
_SECRET_KEY = re.compile(r"auth|cookie|password|passwd|secret|credential|api.?key|access.?key|private.?key|(?:^|_)token(?:$|_)|^env(?:ironment)?$", re.I)
_OPAQUE_KEY = re.compile(r"encrypted|base64|image|audio|video|file_data|reasoning_content|reasoning_text|reasoning_summary|thinking|chain_of_thought|^cot$", re.I)
_ASSIGN = re.compile(r"(?im)(\b[\w.-]*(?:token|secret|password|passwd|api[_-]?key|authorization|cookie)[\w.-]*\s*[=:]\s*)([^\r\n,;]+)")
_MEDIA = re.compile(r"data:[^\s\"'<>]+|[A-Za-z0-9+/=_-]{96,}")
_THINK = re.compile(r"<(think|thinking|analysis)\b[^>]*>.*?(?:</\1>|$)", re.I | re.S)
_MISSING = object()


def packed(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True, allow_nan=False).encode()


def digest(key, value):
    data = value if isinstance(value, bytes) else packed(value)
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def scrub_text(value):
    from agent.redact import redact_sensitive_text
    # Exact known environment credentials can also appear as unlabelled tool text.
    for name, secret in os.environ.items():
        if _SECRET_KEY.search(name) and len(secret) >= 6:
            value = value.replace(secret, "[redacted-env-secret]")
    value = _THINK.sub("[omitted-transient-reasoning]", value)
    value = _MEDIA.sub("[omitted-opaque-data]", value)
    value = redact_sensitive_text(value, force=True, file_read=True, redact_url_credentials=True)
    value = _ASSIGN.sub(r"\1[redacted]", value)
    return value


def sanitize(value, key, depth=0, schema=False):
    if depth > 64:
        raise ValueError("depth_limit")
    if isinstance(value, dict):
        # Preserve order and structure, never scratchpad or encrypted payloads.
        hidden = value.get("type") in {"reasoning", "thinking", "redacted_thinking", "input_image", "input_audio", "image", "audio"} or value.get("phase") == "analysis"
        if hidden:
            return {"omitted": "media_or_reasoning", "hmac": digest(key, value), "bytes": len(packed(value))}
        result = {}
        for field, item in value.items():
            safe_field = scrub_text(field)
            schema_property = schema and isinstance(item, dict) and ("type" in item or "$ref" in item)
            if not schema_property and (_SECRET_KEY.search(field) or _OPAQUE_KEY.search(field) or field == "prompt_cache_key" or (field == "reasoning" and isinstance(item, str)) or (schema and field == "default")):
                result[safe_field] = {"omitted": "sensitive_field", "hmac": digest(key, item), "bytes": len(packed(item))}
            else:
                result[safe_field] = sanitize(item, key, depth + 1, schema or field == "parameters")
        return result
    if isinstance(value, list):
        return [sanitize(v, key, depth + 1, schema) for v in value]
    if isinstance(value, str):
        # Function-call arguments and tool JSON stay structurally useful.
        if value.lstrip().startswith(("{", "[")):
            try:
                inner = json.loads(value)
            except (ValueError, RecursionError):
                return scrub_text(value)
            return json.dumps(sanitize(inner, key, depth + 1), ensure_ascii=True)
        return scrub_text(value)
    if value is None or type(value) in (int, float, bool):
        return value
    raise ValueError("unsupported_content")


def usage_fields(value):
    paths = ["input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens",
             "input_tokens_details.cached_tokens", "output_tokens_details.reasoning_tokens",
             "prompt_tokens", "completion_tokens", "prompt_tokens_details.cached_tokens",
             "completion_tokens_details.reasoning_tokens"]
    result = {}
    for path in paths:
        item = value
        state = "missing"
        for field in path.split("."):
            if item is None:
                state = "null_parent"
                item = _MISSING
                break
            if isinstance(item, dict):
                item = item.get(field, _MISSING)
            else:
                item = getattr(item, field, _MISSING)
            if item is _MISSING:
                break
        if item is None:
            state = "null"
        elif type(item) in (int, float) and math.isfinite(item):
            result[path] = {"state": "numeric", "value": item}
            continue
        elif item is not _MISSING:
            state = "invalid"
        result[path] = {"state": state}
    return result


def _check_root(root, create=False):
    root = Path(root).absolute()
    # All ancestors must be physical. Artifact ancestors and descendants are private.
    private = False
    current = Path(root.anchor)
    for part in root.parts[1:]:
        current /= part
        private = private or part == "artifacts" or current == root
        if create and current == root and not current.exists():
            current.mkdir(mode=0o700)
        st = current.lstat()
        if not stat.S_ISDIR(st.st_mode) or current.is_symlink():
            raise ValueError("nonphysical_directory")
        if private and (stat.S_IMODE(st.st_mode) != 0o700 or st.st_uid != os.getuid()):
            raise PermissionError("private_directory_required")
    return root


def _open(root, name, flags):
    directory = _directory_fd(root)
    try:
        fd = os.open(name, flags | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    finally:
        os.close(directory)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600 or st.st_nlink != 1:
        os.close(fd)
        raise PermissionError("private_regular_file_required")
    return fd


def _directory_fd(root):
    # Walk from / with openat+NOFOLLOW so a replaced ancestor cannot redirect I/O.
    fd = os.open("/", os.O_DIRECTORY)
    private = False
    try:
        parts = Path(root).absolute().parts[1:]
        for index, part in enumerate(parts):
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            private = private or part == "artifacts" or index == len(parts) - 1
            st = os.fstat(fd)
            if private and (stat.S_IMODE(st.st_mode) != 0o700 or st.st_uid != os.getuid()):
                raise PermissionError("unsafe_directory")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read(root, name, limit=CONTROL_BYTES // 2):
    fd = _open(root, name, os.O_RDONLY)
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("control_file_too_large")
    return data


def _new(root, name, data):
    fd = _open(root, name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _atomic(root, state):
    data = packed(state)
    if len(data) > CONTROL_BYTES // 2:
        raise ValueError("state_size_limit")
    name = ".state-" + uuid.uuid4().hex
    _new(root, name, data)
    fd = _directory_fd(root)
    try:
        os.replace(name, "state.json", src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        os.close(fd)


class Locked:
    def __init__(self, root):
        self.root = _check_root(root)
        self.fd = None

    def __enter__(self):
        import fcntl
        self.fd = _open(self.root, "lock", os.O_RDWR)
        deadline = time.monotonic() + 2
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    os.close(self.fd)
                    raise TimeoutError("diagnostic_lock_timeout")
                time.sleep(.005)
        try:
            state = json.loads(_read(self.root, "state.json"))
            if not (state["schema"] == 1 and 0 < state["limit"] <= MAX_REQUESTS and
                    0 <= state["reserved"] <= state["limit"] and
                    0 < state["expires"] - state["created"] <= MAX_SECONDS and
                    CONTROL_BYTES < state["storage_limit"] <= MAX_STORAGE):
                raise ValueError("invalid_state_bounds")
            return state
        except BaseException:
            os.close(self.fd)
            raise

    def __exit__(self, *args):
        os.close(self.fd)


def fault(root, reason="observer_error"):
    _FAULTS.add(str(root))
    try:
        root = _check_root(root)
        _new(root, "ERROR", packed({"state": "error", "reason": reason}))
    except Exception:
        pass  # status still reports unreadable/unsafe control files as error.


def closed_reason(root, state):
    if (root / "ERROR").exists() or (root / "ERROR").is_symlink() or str(root) in _FAULTS:
        return "error"
    if state["state"] != "armed":
        return state["state"]
    if time.time() >= state["expires"]:
        return "expired"
    if state["reserved"] >= state["limit"]:
        return "count_exhausted"
    if state["used"] + state["held"] + CONTROL_BYTES + 2 * META_BYTES > state["storage_limit"]:
        return "size_exhausted"
    return None


def arm(root, *, owner, baseline, limit=MAX_REQUESTS, seconds=MAX_SECONDS, storage=MAX_STORAGE, write=False):
    if baseline != BASELINE or not re.fullmatch(r"[A-Za-z0-9_./:-]{1,512}", owner):
        raise ValueError("owner_and_expected_baseline_required")
    if not (0 < limit <= MAX_REQUESTS and 0 < seconds <= MAX_SECONDS and CONTROL_BYTES < storage <= MAX_STORAGE):
        raise ValueError("bounds_exceed_authorization")
    if not write:
        return {"state": "dry_run", "limit": limit, "seconds": seconds, "storage_limit": storage}
    root = _check_root(root, create=True)
    if any(root.iterdir()):
        raise FileExistsError("never_rearm_or_clobber_existing_run")
    _new(root, "lock", b"")
    _new(root, "key", os.urandom(32))
    now = time.time()
    state = dict(schema=1, state="armed", owner=owner, baseline=baseline, run_id=uuid.uuid4().hex,
                 created=now, expires=now + seconds, limit=limit, storage_limit=storage,
                 reserved=0, completed=0, used=0, held=0, pending={})
    _atomic(root, state)
    return state


def status(root):
    root = Path(root)
    if not root.exists() and not root.is_symlink():
        return {"state": "absent", "capture_open": False}
    try:
        with Locked(root) as state:
            if len(_read(root, "key", 32)) != 32:
                raise ValueError("invalid_key_length")
            reason = closed_reason(root, state)
            return {**state, "state": reason or "armed", "capture_open": reason is None}
    except Exception:
        return {"state": "error", "capture_open": False, "reason": "unsafe_or_unreadable_control"}


def disarm(root, write=False):
    if not write:
        return {"state": "dry_run", "action": "disarm"}
    with Locked(root) as state:
        state["state"] = "disarmed"
        _atomic(root, state)
    return status(root)


def compare(root):
    """Local deterministic first-diff summary. Never emits captured conversation text."""
    root = _check_root(root)
    previous = {}
    rows = []
    for path in sorted(root.glob("*.request.json.gz")):
        if not re.fullmatch(r"\d{4}-[a-f0-9]{32}\.request\.json\.gz", path.name):
            raise ValueError("unexpected_record_name")
        data = _read(root, path.name, SLOT_BYTES)
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            raw = stream.read(MAX_BODY * 6 + META_BYTES + 1)
        if len(raw) > MAX_BODY * 6 + META_BYTES:
            raise ValueError("decompressed_record_limit")
        record = json.loads(raw)["metadata"]
        old = previous.get(record["session"])
        row = {k: record.get(k) for k in ("attempt_id", "sequence", "time", "session", "task", "snapshot_status", "input_total", "input_partial", "prefix")}
        if old:
            row["previous_attempt"] = old["attempt_id"]
            row["same_wire"] = old.get("wire_hmac") == record.get("wire_hmac")
            row["changed_fields"] = sorted(k for k in set(old.get("fields", {})) | set(record.get("fields", {})) if old.get("fields", {}).get(k) != record.get("fields", {}).get(k))
            row["changed_identity_headers"] = sorted(k for k in set(old.get("headers", {})) | set(record.get("headers", {})) if old.get("headers", {}).get(k) != record.get("headers", {}).get(k))
            left, right = old.get("input_items", []), record.get("input_items", [])
            common = 0
            for a, b in zip(left, right):
                if a != b:
                    break
                common += 1
            row["input_common_items"] = common
            row["comparison_partial"] = bool(old.get("input_partial") or record.get("input_partial"))
        response_name = record["attempt_id"] + ".response.json"
        if (root / response_name).exists():
            response = json.loads(_read(root, response_name, META_BYTES))
            row["response"] = {k: response.get(k) for k in ("outcome", "wire_status", "raw_usage", "adapter_usage", "normalized", "parse_reason")}
        else:
            row["response"] = {"state": "reserved_or_unknown"}
        previous[record["session"]] = record
        rows.append(row)
    return {"state": "compared", "boundary": "Hermes_to_CLIProxy", "records": rows}


def reserve(root, session, task):
    root = Path(root)
    if session not in SESSIONS or task not in {"main", "background_review", "compression"}:
        return None
    if not root.exists() and not root.is_symlink():
        return None
    try:
        with Locked(root) as state:
            reason = closed_reason(root, state)
            if reason:
                if state["state"] == "armed":
                    state["state"] = reason
                    _atomic(root, state)
                return None
            key = _read(root, "key", 32)
            if len(key) != 32:
                raise ValueError("invalid_hmac_key")
            allowance = min(SLOT_BYTES, state["storage_limit"] - CONTROL_BYTES - state["used"] - state["held"])
            state["reserved"] += 1
            seq = state["reserved"]
            receipt = f"{seq:04d}-{uuid.uuid4().hex}"
            state["held"] += allowance
            state["pending"][receipt] = {"state": "reserved", "allowance": allowance, "pid": os.getpid(), "time": time.time()}
            _atomic(root, state)  # Durable reservation precedes reading/hashing request content.
        return Attempt(root, receipt, seq, key, allowance, state, session, task)
    except Exception:
        fault(root)
        return None


class Attempt:
    def __init__(self, root, receipt, seq, key, allowance, state, session, task):
        self.root, self.receipt, self.key, self.allowance = root, receipt, key, allowance
        self.started = time.monotonic()
        self.written = 0
        self.finished = False
        self.raw_usage = usage_fields(_MISSING)
        self.wire_status = "reserved"
        self.meta = dict(schema=1, attempt_id=receipt, sequence=seq, time=time.time(),
                         session=SESSIONS[session], task=task, model=MODEL, api_mode="codex_responses",
                         baseline=state["baseline"], code_fingerprint=code_fingerprint(),
                         boundary="Hermes_HTTPX_to_CLIProxy_only", run_id=state["run_id"])
        self.session = session
        self.response_id = None
        self.http_status = None
        self.parse_reason = None

    def request(self, request):
        body = request.content  # Already serialized SDK bytes. Never read/consume request streams.
        self.meta["wire_bytes"] = len(body)
        self.meta["wire_hmac"] = digest(self.key, body)
        scope = _ACTIVE.get() or {}
        if scope.get("request_id"):
            self.meta["runtime_request_id_hmac"] = digest(self.key, scope["request_id"])
        self.meta["headers"] = {k: digest(self.key, v) for k, v in request.headers.items() if k.lower() in HEADERS}
        snapshot = None
        reason = "body_size_limit"
        if len(body) <= MAX_BODY:
            obj = json.loads(body)
            self.meta["normalized_hmac"] = digest(self.key, obj)
            self.meta["fields"] = {k if k in {"instructions", "tools", "input", "model", "reasoning", "include", "store", "stream", "prompt_cache_key", "metadata", "text", "tool_choice", "parallel_tool_calls", "max_output_tokens", "prompt_cache_retention"} else "field_" + digest(self.key, k): digest(self.key, v) for k, v in list(obj.items())[:128]}
            self.meta["fields_total"] = len(obj)
            self.meta["fields_partial"] = len(obj) > 128
            self.meta["requested_effort"] = (obj.get("reasoning") or {}).get("effort") if isinstance(obj.get("reasoning"), dict) else None
            if self.meta["requested_effort"] not in {None, "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
                self.meta["requested_effort"] = "other"
            items = obj.get("input", [])
            if not isinstance(items, list):
                items = [items]
            hashes = [digest(self.key, item) for item in items[:2048]]
            self.meta.update(input_items=hashes, input_total=len(items), input_partial=len(items) > len(hashes))
            self.meta["input_layout"] = [
                {"role": item.get("role") if item.get("role") in {"user", "assistant", "system", "developer", "tool"} else None,
                 "type": item.get("type") if item.get("type") in {"message", "function_call", "function_call_output", "reasoning", "compaction"} else None,
                 "fields": {field: digest(self.key, item[field]) for field in ("content", "arguments", "output", "id", "call_id", "name", "phase", "encrypted_content") if field in item}}
                if isinstance(item, dict) else {} for item in items[:128]]
            self.meta["input_layout_partial"] = len(items) > 128
            with _PREVIOUS_LOCK:
                previous = _PREVIOUS.get(self.session)
                if previous and previous[0] == self.meta["run_id"]:
                    _, previous_id, raw, old_hashes = previous
                    low, high = 0, min(len(raw), len(body))
                    while low < high:
                        mid = (low + high + 1) // 2
                        if raw[:mid] == body[:mid]:
                            low = mid
                        else:
                            high = mid - 1
                    common = 0
                    for left, right in zip(old_hashes, hashes):
                        if left != right:
                            break
                        common += 1
                    self.meta["prefix"] = {"previous_attempt": previous_id, "wire_common_bytes": low,
                                           "input_common_items": common, "nonempty": common > 0,
                                           "input_comparison_partial": len(items) > 2048 or len(old_hashes) == 2048}
                _PREVIOUS[self.session] = (self.meta["run_id"], self.receipt, body, hashes)
            try:
                snapshot = sanitize(obj, self.key)
                reason = "sanitized"
            except Exception:
                reason = "redaction_failed"
        self.meta["snapshot_status"] = reason
        # Bound metadata independently; never quietly present a truncated proof as full.
        while len(packed(self.meta)) > META_BYTES and self.meta.get("input_items"):
            self.meta["input_items"] = self.meta["input_items"][:len(self.meta["input_items"]) // 2]
            self.meta["input_partial"] = True
        while len(packed(self.meta)) > META_BYTES and self.meta.get("input_layout"):
            self.meta["input_layout"] = self.meta["input_layout"][:len(self.meta["input_layout"]) // 2]
            self.meta["input_layout_partial"] = True
        if len(packed(self.meta)) > META_BYTES:
            self.meta.pop("fields", None)
            self.meta["fields_partial"] = True
        encoded = gzip.compress(packed({"metadata": self.meta, "request": snapshot}), mtime=0)
        if len(encoded) > self.allowance - META_BYTES:
            self.meta["snapshot_status"] = "storage_slot_limit"
            encoded = gzip.compress(packed({"metadata": self.meta, "request": None}), mtime=0)
        if len(encoded) > self.allowance - META_BYTES:
            raise ValueError("record_size_limit")
        _new(self.root, self.receipt + ".request.json.gz", encoded)
        self.written += len(encoded)

    def request_failed(self):
        # Even a broken redactor/parser must leave original whole-body proof when available.
        self.meta["snapshot_status"] = "request_observation_failed"
        safe = {k: v for k, v in self.meta.items() if k in {"schema", "attempt_id", "sequence", "time", "session", "task", "model", "api_mode", "baseline", "code_fingerprint", "boundary", "run_id", "wire_bytes", "wire_hmac", "snapshot_status"}}
        name = self.receipt + ".request.json.gz"
        if not (self.root / name).exists():
            encoded = gzip.compress(packed({"metadata": safe, "request": None}), mtime=0)
            _new(self.root, name, encoded)
            self.written += len(encoded)

    def event(self, obj):
        if not isinstance(obj, dict):
            return
        event_type = obj.get("type")
        response = obj.get("response") if isinstance(obj.get("response"), dict) else obj
        terminal = event_type in {"response.completed", "response.failed", "response.incomplete", "error"} or response.get("status") in {"completed", "failed", "incomplete"}
        if "usage" in response or terminal:
            # A created event's null usage must not mask a terminal missing field.
            self.raw_usage = usage_fields(response.get("usage", _MISSING))
        if isinstance(response.get("id"), str):
            self.response_id = digest(self.key, response["id"])
        if event_type in {"response.completed", "response.failed", "response.incomplete", "error"}:
            self.wire_status = event_type
        elif response.get("status") in {"completed", "failed", "incomplete"}:
            self.wire_status = response["status"]

    def complete(self, outcome, response=None):
        if self.finished:
            return
        self.finished = True
        row = dict(schema=1, attempt_id=self.receipt, state="completed", outcome=outcome,
                   wire_status=self.wire_status, duration_ms=round((time.monotonic()-self.started)*1000),
                   http_status=self.http_status, response_id_hmac=self.response_id,
                   raw_usage=self.raw_usage, parse_reason=self.parse_reason,
                   adapter_usage=usage_fields(getattr(response, "usage", _MISSING)))
        if response is not None:
            try:
                from agent.usage_pricing import normalize_usage
                usage = normalize_usage(getattr(response, "usage", None), api_mode="chat_completions" if self.meta["task"] == "compression" else "codex_responses", provider="custom")
                row["normalized"] = {name: getattr(usage, name) for name in ("input_tokens", "output_tokens", "cache_read_tokens", "reasoning_tokens", "total_tokens") if type(getattr(usage, name, None)) in (int, float)}
            except Exception:
                row["normalized_status"] = "unavailable"
        data = packed(row)
        if len(data) > META_BYTES or self.written + len(data) > self.allowance:
            raise ValueError("response_record_limit")
        _new(self.root, self.receipt + ".response.json", data)
        self.written += len(data)
        with Locked(self.root) as state:
            pending = state["pending"].pop(self.receipt)
            state["held"] -= pending["allowance"]
            state["used"] += self.written
            state["completed"] += 1
            _atomic(self.root, state)


@functools.lru_cache(maxsize=1)
def code_fingerprint():
    root = Path(__file__).parent
    return hashlib.sha256(b"".join((root / name).read_bytes() for name in
        ("cache_diagnostic.py", "codex_runtime.py", "auxiliary_client.py"))).hexdigest()


def instrument_openai_client(client):
    """Observe this client's physical send, including redirects and SDK retries."""
    if _ACTIVE.get() is None:
        return
    try:
        import httpx
        http = getattr(client, "_client", None)
        if not isinstance(http, httpx.Client):
            return
        with _INSTALL_LOCK:
            if getattr(http, "_cache_diagnostic_installed", False):
                return
            original = http._send_single_request

            def send(request, *args, **kwargs):
                scope = _ACTIVE.get()
                if scope is None or request.method != "POST" or not request.url.path.rstrip("/").endswith("/responses"):
                    return original(request, *args, **kwargs)
                attempt = reserve(ROOT, scope["session"], scope["task"])
                if attempt is None:
                    return original(request, *args, **kwargs)
                scope["attempts"].append(attempt)
                try:
                    attempt.request(request)
                except Exception:
                    fault(ROOT)
                    try:
                        attempt.request_failed()
                    except Exception:
                        pass
                try:
                    response = original(request, *args, **kwargs)
                except BaseException:
                    attempt.wire_status = "send_failed_or_cancelled"
                    raise
                attempt.http_status = response.status_code
                if response.is_stream_consumed:
                    try:
                        if len(response.content) <= MAX_EVENT:
                            attempt.event(json.loads(response.content))
                    except Exception:
                        attempt.parse_reason = "unparsed_response"
                else:
                    try:
                        response.stream = ObservedStream(response.stream, attempt, response.headers)
                    except Exception:
                        fault(ROOT)
                return response

            http._send_single_request = send
            http._cache_diagnostic_installed = True
    except Exception:
        fault(ROOT)


# Import only the existing dependency; no alternate TLS stack or SDK patch.
import httpx


class ObservedStream(httpx.SyncByteStream):
    def __init__(self, stream, attempt, headers):
        self.stream, self.attempt = stream, attempt
        self.sse = "text/event-stream" in headers.get("content-type", "")
        self.enabled = headers.get("content-encoding", "identity") in {"identity", ""}
        self.buffer = b""
        self.eof = False
        if not self.enabled:
            attempt.parse_reason = "encoded_response_not_observed"

    def __iter__(self):
        try:
            for chunk in self.stream:
                try:
                    self.feed(chunk)
                except Exception:
                    self.enabled = False
                    self.buffer = b""
                    self.attempt.parse_reason = "response_parse_error"
                yield chunk  # Exact original chunk, unmodified and in order.
            self.eof = True
            if self.enabled and self.buffer:
                try:
                    self.parse(self.buffer)
                except Exception:
                    self.attempt.parse_reason = "partial_or_invalid_response"
        finally:
            self.buffer = b""
            if self.attempt.wire_status == "reserved":
                self.attempt.wire_status = "eof_without_terminal" if self.eof else "cancelled_or_closed"

    def feed(self, chunk):
        if not self.enabled:
            return
        if len(self.buffer) + len(chunk) > MAX_EVENT:
            self.enabled = False
            self.buffer = b""
            self.attempt.parse_reason = "response_event_size_limit"
            return
        self.buffer += chunk
        if self.sse:
            self.buffer = self.buffer.replace(b"\r\n", b"\n")
            while b"\n\n" in self.buffer:
                event, self.buffer = self.buffer.split(b"\n\n", 1)
                self.parse(event)

    def parse(self, data):
        if self.sse:
            data = b"\n".join(line[5:].lstrip(b" ") for line in data.splitlines() if line.startswith(b"data:"))
        if not data or data == b"[DONE]":
            return
        self.attempt.event(json.loads(data))

    def close(self):
        self.buffer = b""
        if self.attempt.wire_status == "reserved":
            self.attempt.wire_status = "cancelled_or_closed"
        self.stream.close()


def observe(kind):
    """Bind trusted runtime identity at the two concrete Responses entry points."""
    def decorate(function):
        @functools.wraps(function)
        def wrapped(owner, *args, **kwargs):
            session, task, model, request_id = "", "", "", None
            try:
                if kind == "main":
                    session = getattr(owner, "session_id", "")
                    origin = getattr(owner, "_memory_write_origin", "assistant_tool")
                    task = {"assistant_tool": "main", "background_review": "background_review"}.get(origin, "excluded_task")
                    if getattr(owner, "_parent_session_id", None) and task != "background_review":
                        task = "excluded_child"
                    model = (args[0] if args else kwargs.get("api_kwargs", {})).get("model", "")
                    request_id = getattr(owner, "_current_api_request_id", None)
                else:
                    from agent.auxiliary_client import _RUNTIME_MAIN_CONTEXT, _RELAY_AUX_CALL_CONTEXT
                    runtime = _RUNTIME_MAIN_CONTEXT.get() or {}
                    relay = _RELAY_AUX_CALL_CONTEXT.get() or {}
                    session, task = runtime.get("session_id", ""), relay.get("task", "")
                    model = kwargs.get("model", owner._model)
                    request_id = relay.get("request_id")
                eligible = session in SESSIONS and task in {"main", "background_review", "compression"} and model == MODEL
                eligible = eligible and ROOT.exists()
            except Exception:
                eligible = False
            if not eligible:
                if _ACTIVE.get() is None:
                    return function(owner, *args, **kwargs)
                excluded_token = _ACTIVE.set(None)
                try:
                    return function(owner, *args, **kwargs)
                finally:
                    _ACTIVE.reset(excluded_token)
            scope = {"session": session, "task": task, "attempts": [], "request_id": request_id}
            token = _ACTIVE.set(scope)
            result, outcome = None, "call_failed_or_cancelled"
            try:
                if kind == "auxiliary":
                    instrument_openai_client(owner._client)
                result = function(owner, *args, **kwargs)
                outcome = "returned"
                return result
            finally:
                _ACTIVE.reset(token)
                for index, attempt in enumerate(scope["attempts"]):
                    try:
                        last = index == len(scope["attempts"]) - 1
                        attempt.complete(outcome if last else "retry", result if last else None)
                    except Exception:
                        fault(ROOT)
        return wrapped
    return decorate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=["arm", "status", "disarm", "compare"], default="status")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--owner", default="")
    parser.add_argument("--expected-baseline", default="")
    parser.add_argument("--limit", type=int, default=MAX_REQUESTS)
    parser.add_argument("--seconds", type=int, default=MAX_SECONDS)
    parser.add_argument("--storage-bytes", type=int, default=MAX_STORAGE)
    args = parser.parse_args()
    try:
        if args.command == "arm":
            result = arm(args.root, owner=args.owner, baseline=args.expected_baseline, limit=args.limit,
                         seconds=args.seconds, storage=args.storage_bytes, write=args.write)
        elif args.command == "disarm":
            result = disarm(args.root, args.write)
        elif args.command == "compare":
            result = compare(args.root)
        else:
            result = status(args.root)
        print(json.dumps(result, sort_keys=True))
        return 1 if result["state"] == "error" else 0
    except Exception:
        print(json.dumps({"state": "error", "reason": "operation_refused"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
