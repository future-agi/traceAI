"""Issue #155: credentials as they reach a local collector on the wire (synthetic values only)."""
import http.server
import threading

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from fi_instrumentation import otel


class _Collector(http.server.BaseHTTPRequestHandler):
    requests = []

    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        type(self).requests.append((self.path, [(k.lower(), v) for k, v in self.headers.items()]))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def collector(monkeypatch):
    _Collector.requests = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _Collector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("FI_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("FI_API_KEY", "synthetic-env-key")
    monkeypatch.setenv("FI_SECRET_KEY", "synthetic-env-secret")
    try:
        yield _Collector.requests
    finally:
        server.shutdown()
        server.server_close()


def _export_one_span(exporter):
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer("issue-155").start_as_current_span("export-check"):
        pass
    provider.shutdown()


def _credential_headers(headers):
    return [(k, v) for k, v in headers if k in ("authorization", "x-api-key", "x-secret-key")]


@pytest.mark.parametrize("explicit", [None, {"Authorization": ""}, {"Authorization": "   "}])
def test_env_credentials_reach_the_collector_once(collector, explicit):
    _export_one_span(otel.HTTPSpanExporter(headers=explicit))
    assert len(collector) == 1
    path, headers = collector[0]
    assert path == "/tracer/v1/traces"
    assert sorted(_credential_headers(headers)) == [
        ("x-api-key", "synthetic-env-key"),
        ("x-secret-key", "synthetic-env-secret"),
    ]


def test_explicit_keys_are_sent_once_without_env_copies(collector):
    _export_one_span(otel.HTTPSpanExporter(headers={
        "x-api-key": "synthetic-explicit-key",
        "X-Secret-Key": "synthetic-explicit-secret",
    }))
    assert len(collector) == 1
    _, headers = collector[0]
    assert sorted(_credential_headers(headers)) == [
        ("x-api-key", "synthetic-explicit-key"),
        ("x-secret-key", "synthetic-explicit-secret"),
    ]


def test_register_exports_with_env_credentials(collector):
    provider = otel.register(project_name="issue-155-wire", batch=False)
    try:
        with provider.get_tracer("issue-155").start_as_current_span("register-check"):
            pass
    finally:
        provider.shutdown()
    exports = [r for r in collector if r[0] == "/tracer/v1/traces"]
    assert exports, collector
    for _, headers in exports:
        assert sorted(_credential_headers(headers)) == [
            ("x-api-key", "synthetic-env-key"),
            ("x-secret-key", "synthetic-env-secret"),
        ]
