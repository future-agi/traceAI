"""register()'s exporter stays when another span processor is added (TH-8727).

register() used to mark its own processor as a default, so the next add_span_processor() call
shut the Future AGI exporter down and removed it: spans silently stopped reaching Future AGI.
Upstream OpenTelemetry's add_span_processor() only adds. Only a TracerProvider() constructed
directly still has a default, which its first add_span_processor() call replaces.

The spans go through the real Future AGI exporters to collectors on 127.0.0.1, which record the
names of the spans they receive.
"""

import gzip
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator, List

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace.export import SimpleSpanProcessor as _OTelSimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fi_instrumentation.fi_types import ProjectType
from fi_instrumentation.otel import TracerProvider, Transport, register


def _span_names(request: ExportTraceServiceRequest) -> List[str]:
    return [
        span.name
        for resource_spans in request.resource_spans
        for scope_spans in resource_spans.scope_spans
        for span in scope_spans.spans
    ]


@pytest.fixture
def http_collector(monkeypatch: pytest.MonkeyPatch) -> Iterator[List[str]]:
    """An OTLP/HTTP collector on 127.0.0.1, with FI_BASE_URL pointing at it.

    Yields the names of the spans it has received.
    """
    received: List[str] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            request = ExportTraceServiceRequest()
            request.ParseFromString(body)
            received.extend(_span_names(request))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("FI_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}")
    try:
        yield received
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def grpc_collector(monkeypatch: pytest.MonkeyPatch) -> Iterator[List[str]]:
    """An OTLP/gRPC collector on 127.0.0.1, with FI_GRPC_URL pointing at it.

    Yields the names of the spans it has received.
    """
    grpc = pytest.importorskip("grpc")
    from concurrent import futures

    from opentelemetry.proto.collector.trace.v1 import (
        trace_service_pb2,
        trace_service_pb2_grpc,
    )

    received: List[str] = []

    class _Servicer(trace_service_pb2_grpc.TraceServiceServicer):
        def Export(self, request: Any, context: Any) -> Any:
            received.extend(_span_names(request))
            return trace_service_pb2.ExportTraceServiceResponse()

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    trace_service_pb2_grpc.add_TraceServiceServicer_to_server(_Servicer(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    monkeypatch.setenv("FI_GRPC_URL", f"http://127.0.0.1:{port}")
    try:
        yield received
    finally:
        server.stop(None)


@pytest.fixture
def make_provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Call register() as a customer would, without setting the global provider.

    Every provider is shut down and the application's signal handlers are preserved.
    Ask for a collector fixture first: register() reads the collector URL when it builds
    the exporter.
    """
    monkeypatch.setenv("FI_API_KEY", "test-api-key")
    monkeypatch.setenv("FI_SECRET_KEY", "test-secret-key")
    saved_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    providers: List[Any] = []

    def _make(**kwargs: Any) -> Any:
        options = {
            "project_type": ProjectType.OBSERVE,
            "project_name": "th-8727",
            "set_global_tracer_provider": False,
            "verbose": False,
            **kwargs,
        }
        provider = register(**options)
        providers.append(provider)
        return provider

    yield _make

    for provider in providers:
        provider.shutdown()
    for sig, handler in saved_handlers.items():
        signal.signal(sig, handler)


def _processors(provider: Any) -> List[Any]:
    return list(provider._active_span_processor._span_processors)


def _end_one_span(provider: Any, name: str) -> None:
    provider.get_tracer(__name__).start_span(name).end()
    provider.force_flush()


@pytest.mark.parametrize("batch", [True, False], ids=["batch", "simple"])
def test_register_keeps_its_http_exporter_when_a_processor_is_added(
    http_collector: List[str], make_provider: Any, batch: bool
) -> None:
    provider = make_provider(batch=batch)
    future_agi_processor = _processors(provider)[0]
    other = InMemorySpanExporter()
    other_processor = _OTelSimpleSpanProcessor(other)

    provider.add_span_processor(other_processor)

    assert _processors(provider) == [future_agi_processor, other_processor]
    _end_one_span(provider, "after-add")
    assert http_collector == ["after-add"]
    assert [s.name for s in other.get_finished_spans()] == ["after-add"]


@pytest.mark.parametrize("batch", [True, False], ids=["batch", "simple"])
def test_register_keeps_its_grpc_exporter_when_a_processor_is_added(
    grpc_collector: List[str], make_provider: Any, batch: bool
) -> None:
    provider = make_provider(batch=batch, transport=Transport.GRPC)
    other = InMemorySpanExporter()

    provider.add_span_processor(_OTelSimpleSpanProcessor(other))

    assert len(_processors(provider)) == 2
    _end_one_span(provider, "after-add")
    assert grpc_collector == ["after-add"]
    assert [s.name for s in other.get_finished_spans()] == ["after-add"]


def test_every_later_processor_runs_next_to_the_exporter(
    http_collector: List[str], make_provider: Any
) -> None:
    provider = make_provider()
    first, second = InMemorySpanExporter(), InMemorySpanExporter()

    provider.add_span_processor(_OTelSimpleSpanProcessor(first))
    provider.add_span_processor(_OTelSimpleSpanProcessor(second))

    assert len(_processors(provider)) == 3
    _end_one_span(provider, "after-two-adds")
    assert http_collector == ["after-two-adds"]
    assert [s.name for s in first.get_finished_spans()] == ["after-two-adds"]
    assert [s.name for s in second.get_finished_spans()] == ["after-two-adds"]


@pytest.mark.parametrize("batch", [True, False], ids=["batch", "simple"])
def test_register_with_span_exporter_uses_it_and_keeps_it(
    http_collector: List[str], make_provider: Any, batch: bool
) -> None:
    # register(span_exporter=...) is the supported way to send spans somewhere else instead.
    custom = InMemorySpanExporter()
    provider = make_provider(batch=batch, span_exporter=custom)
    other = InMemorySpanExporter()

    provider.add_span_processor(_OTelSimpleSpanProcessor(other))

    _end_one_span(provider, "custom")
    assert [s.name for s in custom.get_finished_spans()] == ["custom"]
    assert [s.name for s in other.get_finished_spans()] == ["custom"]
    # No Future AGI exporter was built, so nothing reached the collector.
    assert http_collector == []


def test_register_verbose_no_longer_says_add_span_processor_overwrites_a_default(
    http_collector: List[str],
    make_provider: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_provider(verbose=True)

    printed = capsys.readouterr().out
    assert "OpenTelemetry Tracing Details" in printed
    assert "will overwrite this default" not in printed


def test_a_tracer_provider_constructed_directly_still_replaces_its_default(
    http_collector: List[str],
) -> None:
    # Unchanged behaviour: this test passes before and after TH-8727.
    provider = TracerProvider(verbose=False)
    try:
        assert "will overwrite this default" in provider._tracing_details()
        first, second = InMemorySpanExporter(), InMemorySpanExporter()
        first_processor = _OTelSimpleSpanProcessor(first)
        second_processor = _OTelSimpleSpanProcessor(second)

        provider.add_span_processor(first_processor)
        assert _processors(provider) == [first_processor]
        assert "will overwrite this default" not in provider._tracing_details()

        # Only the first call replaces; the next one adds.
        provider.add_span_processor(second_processor)
        assert _processors(provider) == [first_processor, second_processor]

        _end_one_span(provider, "direct")
        assert [s.name for s in first.get_finished_spans()] == ["direct"]
        assert [s.name for s in second.get_finished_spans()] == ["direct"]
        # The default exporter was shut down and removed: nothing reached the collector.
        assert http_collector == []
    finally:
        provider.shutdown()
