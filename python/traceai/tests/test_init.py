"""Offline contracts for the source-only aggregate initializer."""

from email.parser import Parser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from unittest.mock import Mock
import sys

import pytest
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from packaging.requirements import Requirement
from poetry.core.factory import Factory
from poetry.core.masonry.builders.wheel import WheelBuilder

import traceai


@pytest.fixture
def collector():
    """Decode real OTLP/HTTP requests; no vendor or model API is involved."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = self.rfile.read(int(self.headers["Content-Length"]))
            request = ExportTraceServiceRequest.FromString(payload)
            requests.append((self.path, request))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture
def providers(monkeypatch, collector):
    url, requests = collector
    providers = []
    for destination in ("global", "private"):
        provider = TracerProvider(
            resource=Resource.create({"service.name": destination}),
            shutdown_on_exit=False,
        )
        provider.add_span_processor(
            SimpleSpanProcessor(
                OTLPSpanExporter(endpoint=f"{url}/{destination}", timeout=2)
            )
        )
        providers.append(provider)
    global_provider, private_provider = providers
    # Use the SDK's real global-provider lookup, restoring it after each test.
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", global_provider)
    monkeypatch.setattr(traceai, "_tracer_provider", None)
    register = Mock(return_value=private_provider)
    monkeypatch.setattr(traceai, "register", register)
    try:
        yield global_provider, private_provider, register, requests
    finally:
        for provider in providers:
            provider.shutdown()


@pytest.fixture
def install_instrumentor(monkeypatch):
    def install(library):
        class RecordingInstrumentor:
            def instrument(self, **kwargs):
                self.tracer = trace.get_tracer(
                    "aggregate-test", tracer_provider=kwargs.get("tracer_provider")
                )

        instrumentor = RecordingInstrumentor()
        factory = Mock(return_value=instrumentor)
        module = SimpleNamespace(TestInstrumentor=factory)
        monkeypatch.setitem(
            sys.modules, traceai._INSTRUMENTOR_REGISTRY[library], module
        )
        return instrumentor, factory

    return install


def assert_one_span_at(requests, destination):
    assert len(requests) == 1
    path, request = requests[0]
    assert path == f"/{destination}"
    assert len(request.resource_spans) == 1
    resource_spans = request.resource_spans[0]
    attributes = {
        item.key: item.value.string_value for item in resource_spans.resource.attributes
    }
    assert attributes["service.name"] == destination
    spans = [
        span for scope in resource_spans.scope_spans for span in scope.spans
    ]
    assert len(spans) == 1
    assert spans[0].name == "synthetic-operation"
    assert spans[0].trace_id
    assert spans[0].span_id


@pytest.mark.parametrize("set_global", [False, True])
@pytest.mark.parametrize("late", [False, True])
def test_registered_provider_owns_instrumented_spans(
    providers, install_instrumentor, set_global, late
):
    global_provider, private_provider, register, requests = providers
    instrumentor, factory = install_instrumentor("openai")
    returned = traceai.init(
        project_name="offline-test",
        project_type="observe",
        auto_instrument=None if late else ["openai"],
        set_global_tracer_provider=set_global,
        verbose=False,
    )
    if late:
        traceai.instrument("openai", verbose=False)

    assert returned is private_provider
    assert traceai.get_tracer_provider() is private_provider
    assert register.call_args.kwargs["set_global_tracer_provider"] is set_global
    # Even with set_global=True an already-owned global provider cannot be replaced.
    assert trace.get_tracer_provider() is global_provider
    factory.assert_called_once_with()
    with instrumentor.tracer.start_as_current_span("synthetic-operation"):
        pass
    assert_one_span_at(requests, "private")


def test_instrument_before_init_preserves_global_fallback(providers, install_instrumentor):
    _, _, register, requests = providers
    instrumentor, _ = install_instrumentor("openai")
    traceai.instrument("openai", verbose=False)
    register.assert_not_called()
    with instrumentor.tracer.start_as_current_span("synthetic-operation"):
        pass
    assert_one_span_at(requests, "global")


@pytest.mark.parametrize("library", traceai._INSTRUMENTOR_REGISTRY)
def test_provider_forwarded_for_every_registered_name(
    providers, install_instrumentor, library
):
    _, provider, _, _ = providers
    instrumentor, _ = install_instrumentor(library)
    instrumentor.instrument = Mock()
    traceai.init(auto_instrument=[library], verbose=False)
    instrumentor.instrument.assert_called_once_with(tracer_provider=provider)


def test_unknown_library_is_nonfatal(providers, capsys):
    _, provider, _, _ = providers
    assert traceai.init(auto_instrument=["unknown"], verbose=True) is provider
    assert "Unknown library 'unknown'" in capsys.readouterr().out


def test_metadata_has_no_ineffective_extras_and_an_importable_core_floor():
    package = Path(__file__).resolve().parents[1]
    builder = WheelBuilder(Factory().create_poetry(package))
    metadata = Parser().parsestr(builder.get_metadata_content())
    assert not metadata.get_all("Provides-Extra")
    requirements = [Requirement(value) for value in metadata.get_all("Requires-Dist")]
    assert len(requirements) == 1
    core = requirements[0]
    assert core.name == "fi-instrumentation-otel"
    assert "0.1.7" not in core.specifier
    assert "0.1.11" not in core.specifier
    assert "1.1.0" in core.specifier
    assert "1.2.2" in core.specifier
