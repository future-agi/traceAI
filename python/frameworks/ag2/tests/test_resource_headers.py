"""Resource attributes and headers on the wire (the harness Receiver drops both).

``setup()`` without a provider calls ``fi_instrumentation.register`` itself;
this checks what that sends: path ``/tracer/v1/traces``, ``X-Api-Key`` /
``X-Secret-Key`` headers, and resource ``project_name`` +
``project_type=observe``. Loopback only; placeholder keys.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

import pytest

from traceai_ag2 import setup

from ._support import ask, weather_agent


class _CaptureServer:
    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                owner.requests.append(
                    {
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": self.rfile.read(length),
                    }
                )
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_: Any) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


def _resource_attributes(body: bytes) -> List[Dict[str, Any]]:
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    request = ExportTraceServiceRequest()
    request.ParseFromString(body)
    out = []
    for resource_spans in request.resource_spans:
        out.append({kv.key: kv.value.string_value for kv in resource_spans.resource.attributes})
    return out


@pytest.fixture()
def capture(monkeypatch):
    server = _CaptureServer()
    monkeypatch.setenv("FI_BASE_URL", server.origin)
    monkeypatch.setenv("FI_API_KEY", "placeholder-api-key")
    monkeypatch.setenv("FI_SECRET_KEY", "placeholder-secret-key")
    monkeypatch.delenv("FI_PROJECT_NAME", raising=False)
    yield server
    server.close()


def test_setup_registers_observe_project_with_fi_headers(capture):
    agent = weather_agent()
    provider = setup(agent, project_name="ag2-resource-test")
    try:
        assert provider.resource.attributes["project_name"] == "ag2-resource-test"
        assert provider.resource.attributes["project_type"] == "observe"
        ask(agent)
        assert provider.force_flush(timeout_millis=10_000)
    finally:
        provider.shutdown()

    assert capture.requests, "no OTLP request reached the loopback collector"
    for request in capture.requests:
        assert request["path"] == "/tracer/v1/traces"
        headers = request["headers"]
        assert headers["x-api-key"] == "placeholder-api-key"
        assert headers["x-secret-key"] == "placeholder-secret-key"
        assert "authorization" not in headers
        assert headers["content-type"] == "application/x-protobuf"
        for resource in _resource_attributes(request["body"]):
            assert resource["project_name"] == "ag2-resource-test"
            assert resource["project_type"] == "observe"
            assert "openinference.project.name" not in resource


def test_setup_project_name_falls_back_to_env(capture, monkeypatch):
    monkeypatch.setenv("FI_PROJECT_NAME", "ag2-from-env")
    provider = setup(weather_agent())
    try:
        assert provider.resource.attributes["project_name"] == "ag2-from-env"
        assert provider.resource.attributes["project_type"] == "observe"
    finally:
        provider.shutdown()


def test_agent_returns_when_collector_is_down(monkeypatch):
    """Export failure is logged by the exporter and never raised into the agent."""
    import socket

    from fi_instrumentation import register
    from fi_instrumentation.fi_types import ProjectType

    with socket.socket() as sock:  # reserve then release a port nothing listens on
        sock.bind(("127.0.0.1", 0))
        dead_port = sock.getsockname()[1]
    monkeypatch.setenv("FI_BASE_URL", f"http://127.0.0.1:{dead_port}")
    monkeypatch.setenv("FI_API_KEY", "placeholder-api-key")
    monkeypatch.setenv("FI_SECRET_KEY", "placeholder-secret-key")

    provider = register(project_type=ProjectType.OBSERVE, project_name="ag2-down", timeout=1, verbose=False)
    try:
        agent = weather_agent()
        setup(agent, tracer_provider=provider)
        reply = ask(agent)
        assert reply.body == "It is sunny in Paris."
        provider.force_flush(timeout_millis=3_000)  # returns; does not raise
    finally:
        provider.shutdown()
