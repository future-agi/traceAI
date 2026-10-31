"""Opt-in native/transports matrix; no vendor calls or audio dependencies.

Run explicitly with pytest tests/native_exporter_contracts.py after installing
loguru, pipecat-ai (without optional service dependencies), and the gRPC OTLP
exporter matching the installed SDK. Test both Pipecat 0.0.77/OTel 1.29.0 and
Pipecat 1.15.0/OTel 1.45.2. Missing dependencies fail rather than skip tests.
The default unit suite needs only this package's existing locked dependencies.
"""

from concurrent.futures import ThreadPoolExecutor
from tempfile import TemporaryDirectory

import grpc
import pytest
from fi_instrumentation.otel import GRPCSpanExporter, HTTPSpanExporter
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2, trace_service_pb2_grpc
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from pipecat.utils.tracing.service_attributes import add_llm_span_attributes
from pipecat.utils.tracing.setup import setup_tracing

from traceai_pipecat import (
    MappedSpanExporter,
    create_mapped_grpc_exporter,
    enable_grpc_attribute_mapping,
)

from test_exporter_composition import collector, isolated_environment  # noqa: F401


@pytest.mark.parametrize("processor_class", [SimpleSpanProcessor, BatchSpanProcessor])
@pytest.mark.parametrize("configuration", ["legacy", "composition", "factory"])
def test_grpc_native_spans_preserve_options(monkeypatch, processor_class, configuration):
    received = []

    class Collector(trace_service_pb2_grpc.TraceServiceServicer):
        def Export(self, request, context):
            received.append((request, dict(context.invocation_metadata()), context.time_remaining()))
            return trace_service_pb2.ExportTraceServiceResponse()

    server = grpc.server(ThreadPoolExecutor(max_workers=1))
    trace_service_pb2_grpc.add_TraceServiceServicer_to_server(Collector(), server)
    # Unix sockets keep this contract fully local even when the OS sandbox
    # rejects gRPC's IPv4-mapped IPv6 sockets on the loopback interface.
    socket_dir = TemporaryDirectory(prefix="pipecat-", dir="/tmp")
    endpoint = f"unix:{socket_dir.name}/collector.sock"
    server.add_insecure_port(endpoint)
    server.start()
    options = dict(
        endpoint=endpoint,
        insecure=True,
        headers={"authorization": "Bearer synthetic-grpc-only"},
        timeout=1.25,
        compression=grpc.Compression.Gzip,
    )
    exporter = (create_mapped_grpc_exporter(**options) if configuration == "factory"
                else GRPCSpanExporter(**options))
    mapped = MappedSpanExporter(exporter) if configuration == "composition" else exporter
    provider = TracerProvider(shutdown_on_exit=False)
    processor = processor_class(mapped)
    provider.add_span_processor(processor)
    try:
        if configuration == "legacy":
            monkeypatch.setattr("opentelemetry.trace.get_tracer_provider", lambda: provider)
            assert enable_grpc_attribute_mapping()
            wrapped = processor.span_exporter
            assert wrapped.exporter is exporter
            assert enable_grpc_attribute_mapping()
            assert processor.span_exporter is wrapped
        with provider.get_tracer("pipecat-test").start_as_current_span("llm"):
            from opentelemetry.trace import get_current_span

            add_llm_span_attributes(
                get_current_span(), "OpenAILLMService", "synthetic-model",
                messages='[{"role":"user","content":"hello"}]', output="world",
            )
        assert provider.force_flush(timeout_millis=5000)
        assert len(received) == 1
        request, metadata, remaining = received[0]
        assert metadata["authorization"] == "Bearer synthetic-grpc-only"
        assert 0 < remaining < 1.5  # Not the default 10-second deadline.
        spans = [span for resource in request.resource_spans for scope in resource.scope_spans
                 for span in scope.spans]
        assert len(spans) == 1
        attrs = {item.key: item.value for item in spans[0].attributes}
        assert attrs["input.value"].string_value == '[{"role":"user","content":"hello"}]'
        assert attrs["output.value"].string_value == "world"
        assert attrs["gen_ai.provider.name"].string_value == "openai"
        assert attrs["gen_ai.request.model"].string_value == "synthetic-model"
    finally:
        provider.shutdown()
        server.stop(grace=0).wait(timeout=5)
        socket_dir.cleanup()


def test_native_pipecat_setup_accepts_composed_exporter(collector, monkeypatch):
    endpoint, received = collector
    providers = []
    # Capture the native provider without changing the process-global singleton.
    monkeypatch.setattr("opentelemetry.trace.set_tracer_provider", providers.append)
    exporter = HTTPSpanExporter(endpoint=endpoint, headers={"authorization": "Bearer synthetic-native-only"})
    assert setup_tracing("pipecat-native-test", exporter=MappedSpanExporter(exporter))
    provider, = providers
    try:
        tracer = provider.get_tracer("pipecat-test")
        with tracer.start_as_current_span("conversation") as parent:
            with tracer.start_as_current_span("llm") as span:
                add_llm_span_attributes(
                    span, "OpenAILLMService", "synthetic-model",
                    messages='[{"role":"user","content":"hello"}]', output="world",
                )
                parent_id = parent.get_span_context().span_id
        assert provider.force_flush(timeout_millis=5000)
        spans = [span for _, headers, request in received for resource in request.resource_spans
                 for scope in resource.scope_spans for span in scope.spans]
        assert len(spans) == 2
        assert all(headers["authorization"] == "Bearer synthetic-native-only" for _, headers, _ in received)
        child = next(span for span in spans if span.name == "llm")
        assert int.from_bytes(child.parent_span_id, "big") == parent_id
        attrs = {item.key: item.value for item in child.attributes}
        assert attrs["gen_ai.provider.name"].string_value == "openai"
        assert attrs["input.value"].string_value == '[{"role":"user","content":"hello"}]'
        assert attrs["output.value"].string_value == "world"
    finally:
        provider.shutdown()
