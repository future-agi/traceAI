"""Execute the README and shipped examples unchanged, with loopback-only servers."""

import os
import re
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import httpx
import pytest

from .test_lifecycle import model_response
from .test_wire import attributes, decode_requests

PACKAGE = Path(__file__).resolve().parents[1]


@pytest.fixture
def model_server():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            calls.append(body)
            response = model_response(
                httpx.Request(
                    "POST", "https://model.invalid/v1/chat/completions", content=body
                )
            )
            self.send_response(response.status_code)
            self.send_header("Content-Type", response.headers["Content-Type"])
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", calls
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


SAMPLES = [
    (f"readme-{i}", snippet)
    for i, snippet in enumerate(
        re.findall(
            r"```python\n(.*?)```", (PACKAGE / "README.md").read_text(), re.DOTALL
        )
    )
] + [
    (path.name, path.read_text())
    for path in sorted((PACKAGE / "examples").glob("*.py"))
]


@pytest.mark.parametrize("name,source", SAMPLES, ids=[name for name, _ in SAMPLES])
def test_customer_example(name, source, tmp_path, receiver, model_server):
    endpoint, received = receiver
    model_url, calls = model_server
    script = tmp_path / "example.py"
    script.write_text(source)
    result = subprocess.run(
        [sys.executable, "-I", str(script)],
        cwd=tmp_path,
        env={
            "PATH": os.defpath,
            "HOME": str(tmp_path),
            "FI_BASE_URL": endpoint.removesuffix("/tracer/v1/traces"),
            "FI_API_KEY": "synthetic-api",
            "FI_SECRET_KEY": "synthetic-secret",
            "OPENAI_API_KEY": "synthetic-model",
            "OPENAI_BASE_URL": model_url,
            "AGNO_TELEMETRY": "false",
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Paris" in result.stdout
    assert calls, "Example never called the real framework's model client"
    assert received, "Example never exported OTLP"
    payloads = decode_requests(received)
    spans = []
    for payload in payloads:
        for resource in payload["resourceSpans"]:
            attrs = attributes(resource["resource"]["attributes"])
            assert attrs["project_name"].startswith("agno-")
            assert attrs["project_type"] == "observe"
            assert attrs["semantic_convention"] == "openinference"
            for scope in resource["scopeSpans"]:
                spans.extend(scope["spans"])
    kinds = [
        attributes(span["attributes"])["openinference.span.kind"] for span in spans
    ]
    assert "AGENT" in kinds
    assert "LLM" in kinds
    assert kinds.count("LLM") == len(calls), "Missing or duplicate model spans"
    if name == "agent_with_tools.py":
        assert kinds.count("TOOL") == 1
    elif name == "team_example.py":
        assert kinds.count("AGENT") == 2
        assert kinds.count("TOOL") == 1
    elif name == "workflow_example.py":
        assert kinds.count("CHAIN") == 3
        assert kinds.count("AGENT") == 2
