"""Execute the unmodified PrismML CLI with real loopback HTTP, OTLP, and SIGINT."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest

ROOT = Path(__file__).resolve().parents[1]
from harness import Receiver


@pytest.mark.parametrize(
    "mode",
    ["request_interrupt", "stream_interrupt", "truncated_stream", "premature_done"],
)
def test_real_cli(mode):
    request_started = threading.Event()
    release = threading.Event()
    prefix = {
        "id": "synthetic-bonsai",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "bonsai",
        "choices": [
            {
                "index": 0,
                "delta": {"content": "Partial response"},
                "finish_reason": None,
            }
        ],
    }
    frame = ("data: " + json.dumps(prefix) + "\n\n").encode()

    if mode == "premature_done":
        frame += b"data: [DONE]\n\n"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            request_started.set()
            if mode == "request_interrupt":
                release.wait(15)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            if mode in ("truncated_stream", "premature_done"):
                self.send_header("Content-Length", str(len(frame)))
            self.end_headers()
            self.wfile.write(frame)
            self.wfile.flush()
            if mode == "stream_interrupt":
                release.wait(15)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    proc = None
    try:
        with Receiver() as receiver:
            env = {
                k: v
                for k, v in os.environ.items()
                if not k.startswith(("FI_", "OTEL_", "OPENAI_", "PRISMML_"))
            }
            env.update(
                FI_API_KEY="synthetic-fi-key",
                FI_SECRET_KEY="synthetic-fi-secret",
                FI_BASE_URL=receiver.origin,
                PRISMML_BASE_URL=f"http://127.0.0.1:{server.server_port}/v1",
                NO_PROXY="*",
                PYTHONDONTWRITEBYTECODE="1",
            )
            argv = [
                sys.executable,
                str(ROOT / "src/app.py"),
                "--prompt",
                "safe synthetic input",
            ]
            if mode != "request_interrupt":
                argv.append("--stream")
            proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env
            )
            assert request_started.wait(10), "CLI never reached the local model server"
            first = ""
            if mode == "stream_interrupt":
                first_char = []
                ready = threading.Event()

                def consume_one():
                    first_char.append(proc.stdout.read(1))
                    ready.set()

                reader = threading.Thread(target=consume_one, daemon=True)
                reader.start()
                assert ready.wait(10), "CLI did not yield the first streamed token"
                reader.join(1)
                first = first_char[0]
            if mode in ("request_interrupt", "stream_interrupt"):
                proc.send_signal(signal.SIGINT)
            out, err = proc.communicate(timeout=10)
            assert proc.returncode != 0, "Incomplete or interrupted CLI must fail"
            spans = receiver.spans()
            print(
                json.dumps(
                    {
                        "scenario": mode,
                        "exit_code": proc.returncode,
                        "stdout": first + out,
                        "span_count": len(spans),
                        "statuses": [s.get("status", {}) for s in spans],
                    }
                )
            )
            assert len(spans) == 1, f"{mode}: actual CLI exported {len(spans)} spans"
            assert spans[0]["status"]["code"] == "STATUS_CODE_ERROR", (
                f"{mode}: actual CLI returned {proc.returncode} and reported {spans[0]['status']}"
            )
    finally:
        release.set()
        if proc and proc.poll() is None:
            proc.kill()
            proc.communicate()
        server.shutdown()
        server.server_close()
        thread.join(2)
