"""Exporter configuration contracts using synthetic credentials and loopback OTLP."""

import gzip
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
import requests
from fi_instrumentation.otel import HTTPSpanExporter, Transport
from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_pipecat import (
    MappedHTTPSpanExporter,
    create_mapped_http_exporter,
    enable_fi_attribute_mapping,
    enable_http_attribute_mapping,
)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith(("FI_", "OTEL_")) or name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("FI_BASE_URL", "http://127.0.0.1:9")


@pytest.fixture
def collector():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            received.append((self.path, self.headers, ExportTraceServiceRequest.FromString(body)))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/custom/traces", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class RecordingSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.timeouts = []
        self.closed = False

    def send(self, request, **kwargs):
        self.timeouts.append(kwargs["timeout"])
        return super().send(request, **kwargs)

    def close(self):
        self.closed = True
        super().close()


@pytest.mark.parametrize("processor_class", [SimpleSpanProcessor, BatchSpanProcessor])
@pytest.mark.parametrize("configuration", ["legacy", "composition", "factory"])
def test_http_options_and_mapping_survive_on_the_wire(collector, monkeypatch, processor_class, configuration):
    endpoint, received = collector
    session = RecordingSession()
    options = dict(
        endpoint=endpoint,
        headers={"authorization": "Bearer synthetic-test-only", "x-test": "pipecat"},
        timeout=1.25,
        compression=Compression.Gzip,
        session=session,
    )
    if configuration == "factory":
        exporter = create_mapped_http_exporter(**options)
    else:
        exporter = HTTPSpanExporter(**options)
    if configuration == "composition":
        from traceai_pipecat import MappedSpanExporter

        mapped = MappedSpanExporter(exporter)
        assert mapped.exporter is exporter
    else:
        mapped = exporter

    provider = TracerProvider(shutdown_on_exit=False)
    processor = processor_class(mapped)
    provider.add_span_processor(processor)
    try:
        if configuration == "legacy":
            monkeypatch.setattr("opentelemetry.trace.get_tracer_provider", lambda: provider)
            assert enable_http_attribute_mapping()
        tracer = provider.get_tracer("pipecat-test")
        with tracer.start_as_current_span("conversation") as parent:
            with tracer.start_as_current_span("llm", attributes={
                "gen_ai.system": "openai", "gen_ai.request.model": "synthetic-model",
                "input": '[{"role":"user","content":"hello"}]', "output": "world",
                "gen_ai.usage.input_tokens": 3, "gen_ai.usage.output_tokens": 2,
                "conversation.id": "synthetic-conversation",
            }) as child:
                child_id = child.get_span_context().span_id
                parent_id = parent.get_span_context().span_id
        assert provider.force_flush(timeout_millis=5000)
        assert received
        for path, headers, _ in received:
            assert path == "/custom/traces"
            assert headers.get("authorization") == "Bearer synthetic-test-only"
            assert headers.get("x-test") == "pipecat"
            assert headers.get("Content-Encoding") == "gzip"
        assert len(session.timeouts) == len(received)
        assert all(0 < timeout <= 1.25 for timeout in session.timeouts)
        spans = [span for _, _, request in received for resource in request.resource_spans
                 for scope in resource.scope_spans for span in scope.spans]
        assert len(spans) == 2  # Mapping is not a second span producer or exporter.
        child = next(span for span in spans if span.name == "llm")
        assert int.from_bytes(child.span_id, "big") == child_id
        assert int.from_bytes(child.parent_span_id, "big") == parent_id
        attrs = {a.key: a.value for a in child.attributes}
        assert attrs["gen_ai.provider.name"].string_value == "openai"
        assert attrs["gen_ai.span.kind"].string_value == "LLM"
        assert attrs["input.value"].string_value == '[{"role":"user","content":"hello"}]'
        assert attrs["output.value"].string_value == "world"
        assert attrs["gen_ai.usage.total_tokens"].int_value == 5
        # This change retains the existing native-content policy, not a redactor.
        assert attrs["input"].string_value == attrs["input.value"].string_value
    finally:
        provider.shutdown()
        # Also close the abandoned original on pre-fix runs.
        if not session.closed:
            exporter.shutdown()
    assert session.closed


@pytest.mark.parametrize("processor_class", [SimpleSpanProcessor, BatchSpanProcessor])
def test_legacy_mapping_is_idempotent(monkeypatch, processor_class):
    exporter = HTTPSpanExporter(endpoint="http://127.0.0.1:9/v1/traces", timeout=1.25)
    provider = TracerProvider(shutdown_on_exit=False)
    processor = processor_class(exporter)
    provider.add_span_processor(processor)
    monkeypatch.setattr("opentelemetry.trace.get_tracer_provider", lambda: provider)
    try:
        assert enable_http_attribute_mapping()
        mapped = processor.span_exporter
        assert mapped.exporter is exporter
        assert enable_http_attribute_mapping()
        assert processor.span_exporter is mapped
        assert not enable_fi_attribute_mapping(Transport.GRPC)
        assert processor.span_exporter is mapped
    finally:
        provider.shutdown()


def test_legacy_mapped_exporter_is_not_replaced(monkeypatch):
    exporter = MappedHTTPSpanExporter(endpoint="http://127.0.0.1:9/v1/traces", timeout=1.25)
    provider = TracerProvider(shutdown_on_exit=False)
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    monkeypatch.setattr("opentelemetry.trace.get_tracer_provider", lambda: provider)
    try:
        assert enable_http_attribute_mapping()
        assert processor.span_exporter is exporter
    finally:
        provider.shutdown()


def test_unrelated_exporter_is_not_replaced(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    monkeypatch.setattr("opentelemetry.trace.get_tracer_provider", lambda: provider)
    try:
        assert not enable_http_attribute_mapping()
        assert processor.span_exporter is exporter
    finally:
        provider.shutdown()


def test_unwritable_processor_fails_open_without_logging_credentials(monkeypatch, caplog):
    from types import SimpleNamespace

    exporter = HTTPSpanExporter(endpoint="http://127.0.0.1:9/v1/traces")

    class ReadOnlyProcessor:
        @property
        def span_exporter(self):
            return exporter

        @span_exporter.setter
        def span_exporter(self, value):
            raise AttributeError("synthetic-credential-must-not-be-logged")

    provider = SimpleNamespace(_active_span_processor=SimpleNamespace(
        _span_processors=(ReadOnlyProcessor(),),
    ))
    monkeypatch.setattr("opentelemetry.trace.get_tracer_provider", lambda: provider)
    try:
        assert not enable_http_attribute_mapping()
        assert "Failed to wrap exporter (AttributeError)" in caplog.text
        assert "synthetic-credential-must-not-be-logged" not in caplog.text
    finally:
        exporter.shutdown()


def test_no_provider_returns_false(monkeypatch):
    monkeypatch.setattr("opentelemetry.trace.get_tracer_provider", lambda: object())
    assert not enable_http_attribute_mapping()


def test_composition_does_not_mutate_other_processors_spans():
    from traceai_pipecat import MappedSpanExporter

    mapped_sink = InMemorySpanExporter()
    raw_sink = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(MappedSpanExporter(mapped_sink)))
    provider.add_span_processor(SimpleSpanProcessor(raw_sink))
    try:
        with provider.get_tracer("pipecat-test").start_as_current_span("tts", attributes={"text": "hello"}):
            pass
        mapped, = mapped_sink.get_finished_spans()
        raw, = raw_sink.get_finished_spans()
        assert mapped.attributes["input.value"] == "hello"
        assert "input.value" not in raw.attributes
        assert raw.attributes == {"text": "hello"}
        assert mapped.context == raw.context
    finally:
        provider.shutdown()


def test_composition_delegates_results_flush_timeout_and_shutdown():
    from traceai_pipecat import MappedSpanExporter

    class Sink(SpanExporter):
        def __init__(self):
            self.calls = []

        def export(self, spans):
            self.calls.append(("export", len(spans)))
            return SpanExportResult.FAILURE

        def force_flush(self, timeout_millis=30000):
            self.calls.append(("flush", timeout_millis))
            return False

        def shutdown(self):
            self.calls.append(("shutdown",))

    sink = Sink()
    mapped = MappedSpanExporter(sink)
    assert mapped.export([]) == SpanExportResult.FAILURE
    assert mapped.force_flush(timeout_millis=123) is False
    mapped.shutdown()
    assert sink.calls == [("export", 0), ("flush", 123), ("shutdown",)]
