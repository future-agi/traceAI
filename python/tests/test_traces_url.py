"""FI_TRACES_URL sends span exports to a trace collector separate from the
platform API in FI_BASE_URL (self-hosted Docker installs serve them on different
ports). Synthetic local servers only."""
import http.server
import inspect
import threading

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from fi_instrumentation import otel, settings


def _server():
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def _record(self):
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            seen.append(self.path)
            body = b'{"result": false}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = _record  # noqa: N815 - http.server API
        do_GET = _record  # noqa: N815 - http.server API

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


@pytest.fixture
def api_and_collector(monkeypatch):
    api, api_seen = _server()
    collector, collector_seen = _server()
    monkeypatch.setenv("FI_BASE_URL", f"http://127.0.0.1:{api.server_port}")
    monkeypatch.setenv("FI_TRACES_URL", f"http://127.0.0.1:{collector.server_port}")
    monkeypatch.setenv("FI_API_KEY", "synthetic-key")
    monkeypatch.setenv("FI_SECRET_KEY", "synthetic-secret")
    try:
        yield api_seen, collector_seen
    finally:
        for server in (api, collector):
            server.shutdown()
            server.server_close()


def _export(exporter):
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer("traces-url").start_as_current_span("export-check"):
        pass
    provider.shutdown()


def test_exports_go_to_traces_url_not_the_api(api_and_collector):
    api_seen, collector_seen = api_and_collector
    _export(otel.HTTPSpanExporter())
    _export(otel._build_default_exporter(otel.Transport.HTTP, None, None))
    assert collector_seen == ["/tracer/v1/traces", "/tracer/v1/traces"]
    assert api_seen == []


def test_platform_api_helpers_stay_on_fi_base_url():
    """Custom-eval API calls go to the platform (FI_BASE_URL), never the trace
    collector. The suite's autouse fixtures mock these helpers and all HTTP, so
    check the helpers' own source for the resolver they use."""
    for module, name in (
        (otel, "check_custom_eval_config_exists"),
        (settings, "get_custom_eval_template"),
    ):
        body = inspect.getsource(module).split(f"def {name}(")[1].split("\ndef ")[0]
        assert "get_env_collector_endpoint()" in body, name
        assert "get_env_traces_endpoint" not in body, name


@pytest.mark.parametrize("value", [None, "", "   "])
def test_unset_or_blank_traces_url_falls_back_to_fi_base_url(monkeypatch, value):
    monkeypatch.setenv("FI_BASE_URL", "http://api.example.test:8100")
    if value is None:
        monkeypatch.delenv("FI_TRACES_URL", raising=False)
    else:
        monkeypatch.setenv("FI_TRACES_URL", value)
    assert settings.get_env_traces_endpoint() == "http://api.example.test:8100"
    assert otel._normalized_endpoint(None)[1] == "http://api.example.test:8100/tracer/v1/traces"


def test_traces_url_wins_and_keeps_its_own_port(monkeypatch):
    monkeypatch.setenv("FI_BASE_URL", "http://localhost:8100")
    monkeypatch.setenv("FI_TRACES_URL", "http://localhost:4318")
    assert otel._normalized_endpoint(None)[1] == "http://localhost:4318/tracer/v1/traces"
    assert settings.get_env_collector_endpoint() == "http://localhost:8100"
