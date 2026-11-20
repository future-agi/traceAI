"""Trace export to Future AGI sends the SDK's headers and no OTLP headers from the environment.

OpenTelemetry exporters read OTEL_EXPORTER_OTLP_TRACES_HEADERS and OTEL_EXPORTER_OTLP_HEADERS.
Those are meant for the collector a user configured, so a request to Future AGI must not carry
them. A collector the user chose keeps standard OpenTelemetry behaviour. All values are synthetic.
"""
import http.server
import os
import threading
from concurrent import futures

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from fi_instrumentation import otel
from fi_instrumentation.auto._config import AutoConfig
from fi_instrumentation.auto import _manager

AMBIENT = "Authorization=Bearer fake-ambient,x-ambient=1"
ENV_CASES = {
    "traces-only": {"OTEL_EXPORTER_OTLP_TRACES_HEADERS": AMBIENT},
    "generic-only": {"OTEL_EXPORTER_OTLP_HEADERS": AMBIENT},
    "both": {
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS": AMBIENT,
        "OTEL_EXPORTER_OTLP_HEADERS": AMBIENT,
    },
}
ENV_NAMES = ("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "OTEL_EXPORTER_OTLP_HEADERS")
AMBIENT_NAMES = ("authorization", "x-ambient")


class _HttpSink(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):  # noqa: N802 - http.server API
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        type(self).seen.append({k.lower(): v for k, v in self.headers.items()})
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def http_sink():
    handler = type("Handler", (_HttpSink,), {"seen": []})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", handler.seen
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def grpc_sink():
    grpc = pytest.importorskip("grpc")
    pytest.importorskip("opentelemetry.exporter.otlp.proto.grpc.trace_exporter")
    seen = []

    def export(request, context):
        seen.append({k.lower(): v for k, v in context.invocation_metadata()})
        return b""

    handler = grpc.method_handlers_generic_handler(
        "opentelemetry.proto.collector.trace.v1.TraceService",
        {"Export": grpc.unary_unary_rpc_method_handler(export)},
    )
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    server.add_generic_rpc_handlers((handler,))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        yield f"http://127.0.0.1:{port}", seen
    finally:
        server.stop(None)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FI_API_KEY", "synthetic-env-key")
    monkeypatch.setenv("FI_SECRET_KEY", "synthetic-env-secret")
    monkeypatch.delenv("FI_TRACES_URL", raising=False)


def _export_one_span(provider_or_exporter):
    if isinstance(provider_or_exporter, TracerProvider):
        provider = provider_or_exporter
    else:
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(provider_or_exporter))
    with provider.get_tracer("ambient-headers").start_as_current_span("export-check"):
        pass
    provider.shutdown()


def _set_env(monkeypatch, case):
    for name, value in ENV_CASES[case].items():
        monkeypatch.setenv(name, value)


def _point_at(monkeypatch, transport, url):
    monkeypatch.setenv("FI_GRPC_URL" if transport == "grpc" else "FI_BASE_URL", url)


def _build(path, transport, headers=None):
    if path == "register":
        t = otel.Transport.GRPC if transport == "grpc" else otel.Transport.HTTP
        return otel.register(
            project_name="ambient-headers",
            transport=t,
            batch=False,
            headers=headers,
            set_global_tracer_provider=False,
        )
    if path == "direct":
        cls = otel.GRPCSpanExporter if transport == "grpc" else otel.HTTPSpanExporter
        return cls(headers=headers)
    config = AutoConfig(api_key="synthetic-env-key", secret_key="synthetic-env-secret",
                        base_url=os.environ["FI_BASE_URL"] if transport == "http" else None)
    t = otel.Transport.GRPC if transport == "grpc" else otel.Transport.HTTP
    return _manager._exporter(config, {"transport": t, "headers": headers})


def _sink(transport, http_sink, grpc_sink):
    return http_sink if transport == "http" else grpc_sink


@pytest.mark.parametrize("path", ["register", "direct", "auto"])
@pytest.mark.parametrize("transport", ["http", "grpc"])
@pytest.mark.parametrize("case", list(ENV_CASES))
def test_future_agi_request_carries_no_ambient_headers(
    monkeypatch, http_sink, grpc_sink, path, transport, case
):
    url, seen = _sink(transport, http_sink, grpc_sink)
    _point_at(monkeypatch, transport, url)
    _set_env(monkeypatch, case)

    _export_one_span(_build(path, transport, headers={"x-custom": "kept"}))

    assert len(seen) == 1
    headers = seen[0]
    assert headers["x-api-key"] == "synthetic-env-key"
    assert headers["x-secret-key"] == "synthetic-env-secret"
    assert headers["x-custom"] == "kept"
    for name in AMBIENT_NAMES:
        assert name not in headers, f"{name} reached Future AGI: {headers}"


@pytest.mark.parametrize("transport", ["http", "grpc"])
def test_ambient_headers_are_dropped_when_no_keys_are_set(monkeypatch, http_sink, grpc_sink, transport):
    url, seen = _sink(transport, http_sink, grpc_sink)
    _point_at(monkeypatch, transport, url)
    monkeypatch.delenv("FI_API_KEY")
    monkeypatch.delenv("FI_SECRET_KEY")
    _set_env(monkeypatch, "both")

    _export_one_span(_build("direct", transport))

    assert len(seen) == 1
    for name in AMBIENT_NAMES:
        assert name not in seen[0]


@pytest.mark.parametrize("transport", ["http", "grpc"])
def test_explicit_headers_may_repeat_an_ambient_name(monkeypatch, http_sink, grpc_sink, transport):
    url, seen = _sink(transport, http_sink, grpc_sink)
    _point_at(monkeypatch, transport, url)
    _set_env(monkeypatch, "both")

    _export_one_span(_build("direct", transport, headers={"x-ambient": "explicit"}))

    assert seen[0]["x-ambient"] == "explicit"
    assert "authorization" not in seen[0]


@pytest.mark.parametrize("transport", ["http", "grpc"])
def test_environment_set_after_construction_is_not_sent(monkeypatch, http_sink, grpc_sink, transport):
    url, seen = _sink(transport, http_sink, grpc_sink)
    _point_at(monkeypatch, transport, url)
    exporter = _build("direct", transport)

    _set_env(monkeypatch, "both")
    _export_one_span(exporter)

    assert len(seen) == 1
    for name in AMBIENT_NAMES:
        assert name not in seen[0]


@pytest.mark.parametrize("transport", ["http", "grpc"])
def test_construction_leaves_os_environ_untouched(monkeypatch, http_sink, grpc_sink, transport):
    url, _ = _sink(transport, http_sink, grpc_sink)
    _point_at(monkeypatch, transport, url)
    _set_env(monkeypatch, "both")
    before = dict(os.environ)
    changed = []
    done = threading.Event()

    def watch():
        while not done.is_set():
            if dict(os.environ) != before:
                changed.append(dict(os.environ))
                return

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        for _ in range(20):
            _build("direct", transport).shutdown()
    finally:
        done.set()
        watcher.join()

    assert not changed
    assert dict(os.environ) == before


@pytest.mark.parametrize("transport", ["http", "grpc"])
def test_a_collector_the_user_chose_keeps_environment_headers(monkeypatch, http_sink, grpc_sink, transport):
    url, seen = _sink(transport, http_sink, grpc_sink)
    # Future AGI lives elsewhere, so the sink is a collector the user chose.
    monkeypatch.setenv("FI_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("FI_GRPC_URL", "http://127.0.0.1:9")
    monkeypatch.delenv("FI_API_KEY")
    monkeypatch.delenv("FI_SECRET_KEY")
    _set_env(monkeypatch, "generic-only")
    cls = otel.GRPCSpanExporter if transport == "grpc" else otel.HTTPSpanExporter

    _export_one_span(cls(endpoint=url))

    assert len(seen) == 1
    assert seen[0]["x-ambient"] == "1"
    assert seen[0]["authorization"] == "Bearer fake-ambient"


@pytest.mark.parametrize("compression", ["gzip", "deflate"])
def test_compressed_export_still_names_its_encoding(monkeypatch, http_sink, compression):
    url, seen = http_sink
    _point_at(monkeypatch, "http", url)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_COMPRESSION", compression)
    _set_env(monkeypatch, "both")

    _export_one_span(_build("direct", "http"))

    assert seen[0]["content-encoding"] == compression
    assert "x-ambient" not in seen[0]
