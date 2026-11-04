"""Delay real Chrome discovery so native-daemon startup cleanup can be exercised."""

from __future__ import annotations

import threading
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class DiscoveryGate:
    address: str
    received: threading.Event
    release: threading.Event


@contextmanager
def gated_chrome_discovery():
    received = threading.Event()
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/json/version":
                self.send_error(404)
                return
            received.set()
            if not release.wait(timeout=25):
                self.send_error(504)
                return
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open("http://127.0.0.1:9222/json/version", timeout=5) as response:
                payload = response.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                # A cancelled bootstrap may have closed this request while the gate waited.
                return

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield DiscoveryGate(f"http://127.0.0.1:{server.server_port}", received, release)
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
