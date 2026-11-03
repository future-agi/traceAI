"""Export real SDK spans to a loopback OTLP receiver in both wire encodings."""

import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

import pytest
from fi_instrumentation import TraceConfig
from google.protobuf.json_format import MessageToDict
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

from traceai_mistralai import MistralAIInstrumentor
from test_sdk import Mistral, httpx, params, transport_response


def otlp_json(message):
    data = MessageToDict(message)
    # OTLP/JSON uses hex IDs, rather than protobuf JSON's base64 bytes.
    for resources, encoded_resources in zip(
        message.resource_spans, data["resourceSpans"]
    ):
        for scope, encoded_scope in zip(
            resources.scope_spans, encoded_resources["scopeSpans"]
        ):
            for span, encoded in zip(scope.spans, encoded_scope["spans"]):
                encoded["traceId"] = span.trace_id.hex()
                encoded["spanId"] = span.span_id.hex()
                if span.parent_span_id:
                    encoded["parentSpanId"] = span.parent_span_id.hex()
    return data


class JSONExporter(SpanExporter):
    """Test-only JSON transport; customers use the standard OTLP exporter."""

    def __init__(self, endpoint, compressed):
        self.endpoint, self.compressed = endpoint, compressed

    def export(self, spans):
        body = json.dumps(otlp_json(encode_spans(spans))).encode()
        headers = {"Content-Type": "application/json"}
        if self.compressed:
            body = gzip.compress(body)
            headers["Content-Encoding"] = "gzip"
        with urlopen(
            Request(self.endpoint, data=body, headers=headers), timeout=5
        ) as response:
            assert response.status == 200
        return SpanExportResult.SUCCESS


@pytest.fixture
def receiver():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            if self.headers.get("Content-Type") == "application/json":
                data = json.loads(body)
            else:
                data = otlp_json(ExportTraceServiceRequest.FromString(body))
            requests.append((self.path, data))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/traces", requests
    finally:
        server.shutdown()
        worker.join()
        server.server_close()


@pytest.mark.parametrize("encoding", ["json", "protobuf"])
@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
def test_otlp_wire(receiver, encoding, compressed, private, streaming):
    endpoint, requests = receiver
    exporter = (
        JSONExporter(endpoint, compressed)
        if encoding == "json"
        else OTLPSpanExporter(
            endpoint=endpoint,
            headers={},
            timeout=5,
            compression=Compression.Gzip if compressed else Compression.NoCompression,
        )
    )
    provider = TracerProvider(
        resource=Resource.create({"project_name": "mistral-offline-test"})
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = MistralAIInstrumentor()
    instrumentor.instrument(
        tracer_provider=provider,
        config=TraceConfig(hide_inputs=private, hide_outputs=private),
    )
    try:
        with httpx.Client(
            transport=httpx.MockTransport(transport_response)
        ) as transport:
            client = Mistral(api_key="synthetic-key-marker", client=transport)
            with provider.get_tracer("test-parent").start_as_current_span("parent"):
                if streaming:
                    with client.chat.stream(**params("chat")) as stream:
                        list(stream)
                else:
                    client.chat.complete(**params("chat"))
        provider.force_flush()
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
    spans = [
        span
        for path, data in requests
        for resource in data["resourceSpans"]
        for scope in resource["scopeSpans"]
        for span in scope["spans"]
    ]
    assert len(spans) == 2
    child, parent = spans
    assert child["traceId"] == parent["traceId"]
    assert child["parentSpanId"] == parent["spanId"]
    assert len(child["traceId"]) == 32
    assert len(child["spanId"]) == 16
    assert all(path == "/v1/traces" for path, _ in requests)
    attrs = {a["key"]: a["value"] for a in child["attributes"]}
    assert attrs["gen_ai.span.kind"]["stringValue"] == "LLM"
    assert int(attrs["gen_ai.usage.input_tokens"]["intValue"]) == 2
    wire = json.dumps(requests)
    assert "synthetic-key-marker" not in wire
    if private:
        assert "private-input-marker" not in wire
        assert "private-output-marker" not in wire
    else:
        assert "private-input-marker" in wire
        assert "private-output-marker" in wire


def test_readme_example(receiver, monkeypatch):
    from pathlib import Path
    import re
    from test_sdk import NATIVE

    if not NATIVE:
        pytest.skip("The quickstart targets the current SDK")
    endpoint, requests = receiver
    monkeypatch.setenv("FI_BASE_URL", endpoint.rsplit("/v1/traces", 1)[0])
    monkeypatch.setenv("FI_API_KEY", "synthetic-fi-key")
    monkeypatch.setenv("FI_SECRET_KEY", "synthetic-fi-secret")
    monkeypatch.setenv("MISTRAL_API_KEY", "synthetic-mistral-key")
    original_init = Mistral.__init__

    def init(self, *args, **kwargs):
        kwargs["client"] = httpx.Client(
            transport=httpx.MockTransport(transport_response)
        )
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(Mistral, "__init__", init)
    readme = (Path(__file__).parents[1] / "README.md").read_text()
    snippet = re.findall(r"```python\n(.*?)```", readme, re.S)[0]
    try:
        exec(compile(snippet, "README.md", "exec"), {})
    finally:
        MistralAIInstrumentor().uninstrument()
    spans = [
        span
        for _, data in requests
        for resource in data["resourceSpans"]
        for scope in resource["scopeSpans"]
        for span in scope["spans"]
    ]
    assert len(spans) == 1
    assert requests[0][0] == "/tracer/v1/traces"
    encoded = json.dumps(requests)
    assert "mistral-app" in encoded
    assert "gen_ai.usage.input_tokens" in encoded
    assert "synthetic-fi-secret" not in encoded
    assert "synthetic-mistral-key" not in encoded
