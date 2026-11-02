"""Execute the core README's Python snippets with real OpenTelemetry providers."""

import gzip
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import fi_instrumentation
from fi_instrumentation.fi_types import ProjectType
from fi_instrumentation.otel import SemanticConvention, register


README = Path(__file__).resolve().parents[1] / "fi_instrumentation" / "README.md"


def _run_readme():
    blocks = re.findall(r"```python\n(.*?)```", README.read_text(), re.DOTALL)
    assert blocks, "The core README must include a runnable Python example"
    namespace = {"__name__": "__main__"}
    try:
        exec(compile("\n".join(blocks), str(README), "exec"), namespace)
    except BaseException:
        # Clean up on failure without supplying a missing shutdown on success.
        if "tracer_provider" in namespace:
            namespace["tracer_provider"].shutdown()
        raise


def test_readme_exports_one_span(monkeypatch, clean_env):
    exporter = InMemorySpanExporter()
    global_provider = trace.get_tracer_provider()

    def register_in_memory(**kwargs):
        return register(**kwargs, span_exporter=exporter)

    monkeypatch.setattr(fi_instrumentation, "register", register_in_memory)
    _run_readme()

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "my-llm-span"
    assert spans[0].attributes["my-attribute"] == "my-value"
    assert spans[0].resource.attributes["project_name"] == "my-ai-project"
    assert spans[0].resource.attributes["project_type"] == ProjectType.OBSERVE.value
    assert trace.get_tracer_provider() is global_provider


@pytest.mark.parametrize("compression", ["none", "gzip"])
def test_readme_exports_over_otlp_http(monkeypatch, clean_env, compression):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            encoding = self.headers.get("Content-Encoding")
            if encoding == "gzip":
                body = gzip.decompress(body)
            request = ExportTraceServiceRequest()
            request.ParseFromString(body)
            received.append((self.path, self.headers, request))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("FI_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("FI_API_KEY", "synthetic-api-key")
    monkeypatch.setenv("FI_SECRET_KEY", "synthetic-secret-key")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_COMPRESSION", compression)
    try:
        _run_readme()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert len(received) == 1
    path, headers, request = received[0]
    assert path == "/tracer/v1/traces"
    assert headers["Content-Type"] == "application/x-protobuf"
    assert headers.get("Content-Encoding") == ("gzip" if compression == "gzip" else None)
    assert headers["X-Api-Key"] == "synthetic-api-key"
    assert headers["X-Secret-Key"] == "synthetic-secret-key"
    assert len(request.resource_spans) == 1
    resource_spans = request.resource_spans[0]
    resource = {a.key: a.value.string_value for a in resource_spans.resource.attributes}
    assert resource["project_name"] == "my-ai-project"
    assert resource["project_type"] == ProjectType.OBSERVE.value
    spans = [span for scope in resource_spans.scope_spans for span in scope.spans]
    assert len(spans) == 1
    assert spans[0].name == "my-llm-span"
    assert {a.key: a.value.string_value for a in spans[0].attributes} == {
        "my-attribute": "my-value"
    }


@pytest.mark.parametrize("convention", list(SemanticConvention))
def test_semantic_convention_sets_resource_without_rewriting_attributes(convention, clean_env):
    exporter = InMemorySpanExporter()
    provider = register(
        project_name="native-provider",
        project_type=ProjectType.OBSERVE,
        semantic_convention=convention,
        span_exporter=exporter,
        verbose=False,
    )
    try:
        with provider.get_tracer("native-framework").start_as_current_span("native") as span:
            span.set_attribute("gen_ai.operation.name", "chat")
    finally:
        provider.shutdown()

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].resource.attributes["semantic_convention"] == convention.value
    assert dict(spans[0].attributes) == {"gen_ai.operation.name": "chat"}
