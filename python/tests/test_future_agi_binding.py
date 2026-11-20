"""Which endpoints are Future AGI: only a host every parser agrees on, over https, at an exact target."""
import contextlib
import http.server
import socket
import threading
from concurrent import futures
from typing import ClassVar
from urllib.parse import urlsplit

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from fi_instrumentation import otel

FI_KEY, FI_SECRET = "synthetic-env-key", "synthetic-env-secret"

# (id, transport, endpoint, decision): "fi" is bound, "refuse" is rejected when FI keys are set,
# "other" is a collector of the user's own. {port} is replaced by the evil listener's port.
FORMS = [
    ("http-plain", "http", "https://api.futureagi.com/tracer/v1/traces", "fi"),
    ("http-upper", "http", "https://API.FutureAGI.com/tracer/v1/traces", "fi"),
    ("http-explicit-443", "http", "https://api.futureagi.com:443/tracer/v1/traces", "fi"),
    ("http-trailing-dot", "http", "https://api.futureagi.com./tracer/v1/traces", "fi"),
    ("http-backslash-userinfo", "http", "http://evil.example:{port}\\@api.futureagi.com/tracer/v1/traces", "refuse"),
    ("http-backslash-userinfo-https", "http", "https://evil.example\\@api.futureagi.com/tracer/v1/traces", "refuse"),
    ("http-plain-userinfo", "http", "https://user:pw@api.futureagi.com/tracer/v1/traces", "refuse"),
    ("http-encoded-at", "http", "https://evil.example%40api.futureagi.com/tracer/v1/traces", "refuse"),
    ("http-encoded-backslash", "http", "https://evil.example%5C@api.futureagi.com/tracer/v1/traces", "refuse"),
    ("http-encoded-colon", "http", "https://api.futureagi.com%3A443/tracer/v1/traces", "refuse"),
    ("http-fullwidth", "http", "https://ａｐｉ.futureagi.com/tracer/v1/traces", "refuse"),
    ("http-confusable-dot", "http", "https://api。futureagi.com/tracer/v1/traces", "refuse"),
    ("http-other-port", "http", "https://api.futureagi.com:8443/tracer/v1/traces", "refuse"),
    ("http-clear-text", "http", "http://api.futureagi.com/tracer/v1/traces", "refuse"),
    ("http-own-collector", "http", "https://collector.example/v1/traces", "other"),
    ("http-loopback-collector", "http", "http://127.0.0.1:4318/v1/traces", "other"),
    ("grpc-plain", "grpc", "grpc.futureagi.com:443", "fi"),
    ("grpc-https", "grpc", "https://grpc.futureagi.com", "fi"),
    ("grpc-upper", "grpc", "GRPC.FUTUREAGI.COM", "fi"),
    ("grpc-trailing-dot", "grpc", "grpc.futureagi.com.:443", "fi"),
    ("grpc-ipv4-userinfo-https", "grpc", "https://ipv4:127.0.0.1:{port}@grpc.futureagi.com", "refuse"),
    ("grpc-ipv4-userinfo-http", "grpc", "http://ipv4:127.0.0.1:{port}@grpc.futureagi.com", "refuse"),
    ("grpc-ipv4-bare", "grpc", "ipv4:127.0.0.1:{port}@grpc.futureagi.com", "refuse"),
    ("grpc-ipv4-upper", "grpc", "IPV4:127.0.0.1:{port}@grpc.futureagi.com", "refuse"),
    ("grpc-ipv6", "grpc", "ipv6:[::1]:{port}@grpc.futureagi.com", "refuse"),
    ("grpc-dns-empty-authority", "grpc", "dns:///grpc.futureagi.com:443", "refuse"),
    ("grpc-dns-authority", "grpc", "DNS://127.0.0.1:{port}/grpc.futureagi.com", "refuse"),
    ("grpc-unix", "grpc", "unix:/tmp/collector.sock@grpc.futureagi.com", "refuse"),
    ("grpc-xds", "grpc", "xds:///grpc.futureagi.com", "refuse"),
    ("grpc-plain-userinfo", "grpc", "user@grpc.futureagi.com:443", "refuse"),
    ("grpc-backslash", "grpc", "grpc.futureagi.com\\@evil.example:443", "refuse"),
    ("grpc-fullwidth", "grpc", "ｇｒｐｃ.futureagi.com:443", "refuse"),
    ("grpc-other-port", "grpc", "grpc.futureagi.com:8443", "refuse"),
    ("grpc-clear-text", "grpc", "http://grpc.futureagi.com", "refuse"),
    ("grpc-own-collector", "grpc", "collector.example:4317", "other"),
    ("grpc-loopback-collector", "grpc", "127.0.0.1:4317", "other"),
    ("grpc-unix-own-socket", "grpc", "unix:/tmp/collector.sock", "other"),
]
FORM_IDS = [form[0] for form in FORMS]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS", "FI_TRACES_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FI_API_KEY", FI_KEY)
    monkeypatch.setenv("FI_SECRET_KEY", FI_SECRET)
    monkeypatch.setenv("FI_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("FI_GRPC_URL", "127.0.0.1:9")


class _Sink(http.server.BaseHTTPRequestHandler):
    seen: ClassVar[list] = []

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        type(self).seen.append({k.lower(): v for k, v in self.headers.items()})
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def evil_http(monkeypatch):
    """A listener reachable as evil.example, without touching the system's name resolution."""
    handler = type("Handler", (_Sink,), {"seen": []})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    real = socket.getaddrinfo

    def resolve(host, *args, **kwargs):
        return real("127.0.0.1" if host == "evil.example" else host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    try:
        yield server.server_port, handler.seen
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def evil_grpc():
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
        yield port, seen
    finally:
        server.stop(None)


def _cls(transport):
    if transport == "grpc":
        pytest.importorskip("grpc")
        return otel.GRPCSpanExporter
    return otel.HTTPSpanExporter


def _export_one_span(exporter):
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer("future-agi-binding").start_as_current_span("export-check"):
        pass
    provider.shutdown()


def _rewritten(transport, endpoint):
    """The exact target string grpc receives, after the exporter's own netloc rewrite."""
    if transport != "grpc":
        return endpoint
    parsed = urlsplit(endpoint)
    return parsed.netloc or endpoint


def _hosts(endpoint):
    import urllib3.util

    try:
        split_host = urlsplit(endpoint).hostname
    except ValueError as error:
        split_host = f"<{type(error).__name__}>"
    try:
        urllib3_host = urllib3.util.parse_url(endpoint).host
    except Exception as error:  # noqa: BLE001 - recorded in the table
        urllib3_host = f"<{type(error).__name__}>"
    return split_host, urllib3_host


@pytest.mark.parametrize("form", FORMS, ids=FORM_IDS)
def test_forms_table(form, capsys):
    name, transport, template, expected = form
    endpoint = template.replace("{port}", "50051")
    decision = otel._future_agi_decision(endpoint, grpc=transport == "grpc")
    split_host, urllib3_host = _hosts(endpoint)
    with capsys.disabled():
        print(f"FORM {name:32} urlsplit={split_host!s:28} urllib3={urllib3_host!s:28} "
              f"target={_rewritten(transport, endpoint)!s:48} decision={decision}")
    assert decision == expected


@pytest.mark.parametrize("form", [f for f in FORMS if f[3] == "refuse"], ids=[f[0] for f in FORMS if f[3] == "refuse"])
def test_a_refused_form_is_not_built_when_future_agi_keys_are_set(form):
    _, transport, template, _ = form
    with pytest.raises(ValueError) as raised:
        _cls(transport)(endpoint=template.replace("{port}", "50051"), timeout=2)
    message = str(raised.value)
    assert "FI_BASE_URL" in message or "endpoint" in message
    assert FI_KEY not in message and FI_SECRET not in message and "futureagi" not in message.lower()


_NO_KEY_FORMS = [f for f in FORMS if f[3] != "fi" and f[0] != "grpc-ipv6"]  # the exporter itself rejects the ipv6 form


@pytest.mark.parametrize("form", _NO_KEY_FORMS, ids=[f[0] for f in _NO_KEY_FORMS])
def test_a_form_that_is_not_future_agi_is_built_without_keys(form, monkeypatch):
    _, transport, template, _ = form
    monkeypatch.delenv("FI_API_KEY")
    monkeypatch.delenv("FI_SECRET_KEY")
    exporter = _cls(transport)(endpoint=template.replace("{port}", "50051"), timeout=2)
    exporter.shutdown()


@pytest.mark.parametrize("form", [f for f in FORMS if f[3] == "fi"], ids=[f[0] for f in FORMS if f[3] == "fi"])
def test_a_future_agi_form_is_built_and_bound(form):
    _, transport, template, _ = form
    exporter = _cls(transport)(endpoint=template, timeout=2)
    exporter.shutdown()


_HTTP_EVIL = [f for f in FORMS if f[1] == "http" and "evil.example" in f[2] and "{port}" in f[2]]
_GRPC_EVIL = [f for f in FORMS if f[1] == "grpc" and "{port}" in f[2]]


@pytest.mark.parametrize("form", _HTTP_EVIL, ids=[f[0] for f in _HTTP_EVIL])
def test_no_future_agi_key_reaches_the_host_an_http_client_dials(form, evil_http):
    port, seen = evil_http
    with contextlib.suppress(Exception):  # refusing is acceptable; leaking is not
        _export_one_span(otel.HTTPSpanExporter(endpoint=form[2].replace("{port}", str(port)), timeout=2))
    assert all("x-api-key" not in h and "x-secret-key" not in h for h in seen), seen


@pytest.mark.parametrize("form", _GRPC_EVIL, ids=[f[0] for f in _GRPC_EVIL])
def test_no_future_agi_key_reaches_the_target_grpc_dials(form, evil_grpc):
    port, seen = evil_grpc
    with contextlib.suppress(Exception):  # refusing is acceptable; leaking is not
        _export_one_span(otel.GRPCSpanExporter(endpoint=form[2].replace("{port}", str(port)), timeout=2))
    assert all("x-api-key" not in h and "x-secret-key" not in h for h in seen), seen


def test_a_caller_session_does_not_receive_future_agi_keys(monkeypatch):
    import requests

    handler = type("Handler", (_Sink,), {"seen": []})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("FI_BASE_URL", f"http://127.0.0.1:{server.server_port}")
        session = requests.Session()
        _export_one_span(otel.HTTPSpanExporter(session=session, timeout=5))
        assert {k.lower() for k in session.headers}.isdisjoint({"x-api-key", "x-secret-key"}), dict(session.headers)
        assert len(handler.seen) == 1 and handler.seen[0].get("x-api-key") == FI_KEY
    finally:
        server.shutdown()
        server.server_close()
