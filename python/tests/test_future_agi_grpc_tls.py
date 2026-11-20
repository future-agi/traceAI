"""A Future AGI gRPC exporter uses TLS whichever setting asks for plain text."""
import contextlib
import socket
import threading

import pytest

pytest.importorskip("grpc")
pytest.importorskip("opentelemetry.exporter.otlp.proto.grpc.trace_exporter")

import grpc  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402

from fi_instrumentation import otel  # noqa: E402

FI_KEY, FI_SECRET = "synthetic-env-key", "synthetic-env-secret"
TARGET = "grpc.futureagi.com:443"
SETTINGS_FRAME = bytes.fromhex("000000040000000000")


class Recorder:
    """A raw TCP listener that records every byte it receives and answers an HTTP/2 SETTINGS frame."""

    def __init__(self):
        self.received = bytearray()
        self.first_byte = None
        self._server = socket.socket()
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(4)
        self.port = self._server.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        conn.settimeout(2)
        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    return
                if self.first_byte is None:
                    self.first_byte = data[0]
                self.received.extend(data)
                if data[:3] == b"PRI":
                    conn.sendall(SETTINGS_FRAME)
        except OSError:
            return
        finally:
            conn.close()

    def close(self):
        self._server.close()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in (
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        "OTEL_EXPORTER_OTLP_INSECURE",
        "OTEL_EXPORTER_OTLP_TRACES_INSECURE",
        "FI_TRACES_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FI_API_KEY", FI_KEY)
    monkeypatch.setenv("FI_SECRET_KEY", FI_SECRET)
    monkeypatch.setenv("FI_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("FI_GRPC_URL", "127.0.0.1:9")


@pytest.fixture
def recorder(monkeypatch):
    """Sends every channel the exporter opens to the recorder, keeping the channel kind it chose."""
    rec = Recorder()
    real_insecure, real_secure = grpc.insecure_channel, grpc.secure_channel
    kinds = []

    def insecure_channel(target, *args, **kwargs):
        kinds.append("insecure")
        return real_insecure(f"127.0.0.1:{rec.port}", *args, **kwargs)

    def secure_channel(target, *args, **kwargs):
        kinds.append("secure")
        return real_secure(f"127.0.0.1:{rec.port}", *args, **kwargs)

    import opentelemetry.exporter.otlp.proto.grpc.exporter as base

    monkeypatch.setattr(grpc, "insecure_channel", insecure_channel)
    monkeypatch.setattr(grpc, "secure_channel", secure_channel)
    for name, fn in (("insecure_channel", insecure_channel), ("secure_channel", secure_channel)):
        if hasattr(base, name):
            monkeypatch.setattr(base, name, fn)
    rec.kinds = kinds
    try:
        yield rec
    finally:
        rec.close()


def _export_one_span(exporter):
    """Hands one finished span to the exporter and gives the first attempt time to reach the wire."""
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor as Simple
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(Simple(memory))
    with provider.get_tracer("tls-check").start_as_current_span("export-check"):
        pass
    provider.shutdown()
    worker = threading.Thread(target=lambda: exporter.export(memory.get_finished_spans()), daemon=True)
    worker.start()
    # The first attempt reaches the wire at once; the retries that follow run on a daemon thread and are left to end on their own.
    worker.join(2)


# (id, endpoint argument or None, insecure argument or None, environment)
SOURCES = [
    ("argument", TARGET, True, {}),
    ("env", TARGET, None, {"OTEL_EXPORTER_OTLP_INSECURE": "true"}),
    ("traces-env", TARGET, None, {"OTEL_EXPORTER_OTLP_TRACES_INSECURE": "true"}),
    ("both-envs", TARGET, None, {"OTEL_EXPORTER_OTLP_INSECURE": "true", "OTEL_EXPORTER_OTLP_TRACES_INSECURE": "true"}),
    ("argument-and-env", TARGET, True, {"OTEL_EXPORTER_OTLP_INSECURE": "true", "OTEL_EXPORTER_OTLP_TRACES_INSECURE": "true"}),
    ("env-resolved-endpoint", None, None, {"FI_GRPC_URL": TARGET, "OTEL_EXPORTER_OTLP_INSECURE": "true"}),
    ("http-scheme", "http://grpc.futureagi.com", None, {}),
    ("http-scheme-and-env", "http://grpc.futureagi.com", None, {"OTEL_EXPORTER_OTLP_INSECURE": "true", "OTEL_EXPORTER_OTLP_TRACES_INSECURE": "true"}),
    ("env-resolved-http-scheme", None, None, {"FI_GRPC_URL": "http://grpc.futureagi.com"}),
    ("env-resolved-traces-env", None, None, {"FI_GRPC_URL": TARGET, "OTEL_EXPORTER_OTLP_TRACES_INSECURE": "true"}),
]


@pytest.mark.parametrize("source", SOURCES, ids=[s[0] for s in SOURCES])
def test_every_insecure_source_gives_tls_or_a_refusal_and_no_key_bytes(source, recorder, monkeypatch):
    _, endpoint, insecure, env = source
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    kwargs = {"timeout": 2}
    if endpoint is not None:
        kwargs["endpoint"] = endpoint
    if insecure is not None:
        kwargs["insecure"] = insecure
    try:
        exporter = otel.GRPCSpanExporter(**kwargs)
    except otel.UnsupportedFutureAGIEndpointError as refusal:
        assert "http-scheme" not in source[0], "an http scheme on a Future AGI target gets TLS and is not refused"
        assert FI_KEY not in str(refusal) and FI_SECRET not in str(refusal)
        return
    with contextlib.suppress(Exception):
        _export_one_span(exporter)
    assert recorder.kinds in ([], ["secure"]), recorder.kinds
    if recorder.first_byte is not None:
        assert recorder.first_byte == 0x16, hex(recorder.first_byte)
    assert FI_KEY.encode() not in bytes(recorder.received)
    assert FI_SECRET.encode() not in bytes(recorder.received)


def test_a_truthy_insecure_argument_is_refused_by_name(recorder):
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError) as raised:
        otel.GRPCSpanExporter(endpoint=TARGET, insecure=True, timeout=2)
    assert "insecure" in str(raised.value)
    assert recorder.kinds == []


def test_a_false_insecure_argument_builds_a_tls_exporter(recorder):
    exporter = otel.GRPCSpanExporter(endpoint=TARGET, insecure=False, timeout=2)
    exporter.shutdown()
    assert recorder.kinds == ["secure"]


def test_the_environment_setting_is_ignored_for_future_agi(recorder, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_INSECURE", "true")
    exporter = otel.GRPCSpanExporter(endpoint=TARGET, timeout=2)
    exporter.shutdown()
    assert recorder.kinds == ["secure"]


def test_a_future_agi_target_on_loopback_keeps_the_configured_channel(recorder, monkeypatch):
    monkeypatch.setenv("FI_GRPC_URL", "http://127.0.0.1:50051")
    exporter = otel.GRPCSpanExporter(timeout=2)
    exporter.shutdown()
    assert recorder.kinds == ["insecure"]


def test_an_http_scheme_on_a_future_agi_target_is_not_refused(recorder):
    exporter = otel.GRPCSpanExporter(endpoint="http://grpc.futureagi.com", timeout=2)
    exporter.shutdown()
    assert recorder.kinds == ["secure"]


def test_an_own_collector_keeps_the_insecure_setting(recorder, monkeypatch):
    monkeypatch.delenv("FI_API_KEY")
    monkeypatch.delenv("FI_SECRET_KEY")
    exporter = otel.GRPCSpanExporter(endpoint="collector.example:4317", insecure=True, timeout=2)
    exporter.shutdown()
    assert recorder.kinds == ["insecure"]
