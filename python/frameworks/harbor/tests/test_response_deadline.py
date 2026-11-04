import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
import traceai_harbor._transport as transport
from traceai_harbor import Settings


def test_trickling_response_checks_deadline_before_buffer_fills(monkeypatch):
    stop = threading.Event()
    state = SimpleNamespace(writes=0)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(200)
            self.send_header("Content-Length", "64")
            self.end_headers()
            for _ in range(64):
                if stop.wait(0.01):
                    return
                try:
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    state.writes += 1
                except (BrokenPipeError, ConnectionResetError):
                    return

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Accelerate only the adapter's deadline, not httpx's network clock. The
    # first received chunk expires it; coalescing to 4096 bytes would wait for
    # the entire response instead and fail the write-count assertion below.
    clock = iter([0, 11])
    monkeypatch.setattr(
        transport, "time", SimpleNamespace(monotonic=lambda: next(clock))
    )
    try:
        settings = Settings(
            endpoint=f"http://127.0.0.1:{server.server_port}/tracer/v1/traces",
            api_key="synthetic",
            secret_key="synthetic-secret",
        )
        with pytest.raises(transport.DeliveryError, match="response_deadline"):
            transport.send(b"", settings)
        assert state.writes < 64
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join()
