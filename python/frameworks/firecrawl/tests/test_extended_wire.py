"""OTLP protobuf export and custom JSON transport, plain and gzip, over loopback."""

from __future__ import annotations

import base64
import gzip
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.request import Request, urlopen

import pytest
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from fi_instrumentation.otel import HTTPSpanExporter
from google.protobuf.json_format import MessageToDict, ParseDict
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from test_extended_operations import API_KEY, PRIVATE, OPERATIONS, invoke
from test_extended_operations import api as api  # Re-export the loopback fixture.
from _firecrawl_fake import PAGE_BODY
from traceai_firecrawl import FirecrawlInstrumentor


def _ids(value, decode=False):
    # OTLP JSON IDs are hex, unlike protobuf's default base64 JSON mapping.
    if isinstance(value, dict):
        return {key: (
            (base64.b64encode(bytes.fromhex(item)).decode() if decode else base64.b64decode(item).hex())
            if key in {"traceId", "spanId", "parentSpanId"} else _ids(item, decode)
        ) for key, item in value.items()}
    if isinstance(value, list):
        return [_ids(item, decode) for item in value]
    return value


class JsonExporter(SpanExporter):
    """Test-only transport; the package continues to use the standard exporter."""

    def __init__(self, endpoint, compressed):
        self.endpoint, self.compressed = endpoint, compressed

    def export(self, spans):
        body = json.dumps(_ids(MessageToDict(encode_spans(spans), use_integers_for_enums=True))).encode()
        headers = {"Content-Type": "application/json"}
        if self.compressed:
            body = gzip.compress(body)
            headers["Content-Encoding"] = "gzip"
        with urlopen(Request(self.endpoint, data=body, headers=headers), timeout=2) as response:
            assert response.status == 200
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


@pytest.fixture()
def receiver():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            requests.append((self.path, self.headers, self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def decode_exports(requests, encoding, compressed):
    spans = []
    for path, headers, body in requests:
        assert path == "/tracer/v1/traces"
        assert (headers.get("Content-Encoding") == "gzip") is compressed
        if compressed:
            body = gzip.decompress(body)
        request = ExportTraceServiceRequest()
        if encoding == "json":
            assert headers["Content-Type"] == "application/json"
            ParseDict(_ids(json.loads(body), decode=True), request)
        else:
            assert headers["Content-Type"] == "application/x-protobuf"
            request.ParseFromString(body)
        for secret in (PRIVATE, API_KEY, PAGE_BODY):
            assert secret not in str(request)
        for resource in request.resource_spans:
            attributes = {a.key: a.value.string_value for a in resource.resource.attributes}
            assert attributes["project_name"] == "firecrawl"
            assert attributes["project_type"] == "observe"
            for scope in resource.scope_spans:
                spans.extend(scope.spans)
    return spans


@pytest.mark.parametrize("encoding", ["json", "protobuf"])
@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("is_async", [False, True])
def test_extended_operations_over_otlp(api, receiver, encoding, compressed, is_async, monkeypatch):
    origin, requests = receiver
    monkeypatch.setenv("FI_API_KEY", "synthetic-fi-key")
    monkeypatch.setenv("FI_SECRET_KEY", "synthetic-fi-secret")
    monkeypatch.setenv("FI_BASE_URL", origin)
    endpoint = origin + "/tracer/v1/traces"
    exporter = JsonExporter(endpoint, compressed) if encoding == "json" else HTTPSpanExporter(
        endpoint=endpoint, headers={"X-Api-Key": "synthetic-fi-key", "X-Secret-Key": "synthetic-fi-secret"},
        compression=Compression.Gzip if compressed else Compression.NoCompression,
    )
    provider = register(project_name="firecrawl", project_type=ProjectType.OBSERVE,
                        span_exporter=exporter, batch=False, verbose=False, set_global_tracer_provider=False)
    instrumentor = FirecrawlInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    operations = [(name, [api.origin + "/v2/batch/scrape/batch-1?cursor=" + PRIVATE] if name == "get_batch_scrape_status_page" else args, kwargs)
                  for name, args, kwargs in OPERATIONS]
    try:
        with provider.get_tracer(__name__).start_as_current_span("parent"):
            invoke(api, is_async, operations)
        assert provider.force_flush()
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
    spans = decode_exports(requests, encoding, compressed)
    assert len(spans) == len(operations) + 1
    parent = next(span for span in spans if span.name == "parent")
    assert {span.name for span in spans} == {"parent"} | {"firecrawl." + name for name, _, _ in operations}
    for span in spans:
        if span is parent:
            continue
        assert span.parent_span_id == parent.span_id
        assert span.trace_id == parent.trace_id
        assert span.status.code == 1  # OK
        attributes = {a.key: a.value for a in span.attributes}
        assert attributes["gen_ai.span.kind"].string_value == "TOOL"


def test_readme_python_snippet_uses_real_register_and_sdk(api, receiver, monkeypatch):
    import firecrawl
    from functools import partial

    origin, requests = receiver
    monkeypatch.setenv("FI_BASE_URL", origin)
    monkeypatch.setenv("FI_API_KEY", "synthetic-fi-key")
    monkeypatch.setenv("FI_SECRET_KEY", "synthetic-fi-secret")
    monkeypatch.setenv("FIRECRAWL_API_KEY", API_KEY)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_COMPRESSION", "gzip")
    # Only the vendor base URL is substituted; the snippet and SDK run unchanged.
    monkeypatch.setattr(firecrawl, "Firecrawl", partial(firecrawl.Firecrawl, api_url=api.origin))
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    snippet = re.search(r"```python\n(.*?)```", readme, re.S).group(1)
    namespace = {}
    try:
        exec(compile(snippet, "README.md", "exec"), namespace)
        assert namespace["tracer_provider"].force_flush()
    finally:
        FirecrawlInstrumentor().uninstrument()
        if "tracer_provider" in namespace:
            namespace["tracer_provider"].shutdown()
    spans = decode_exports(requests, "protobuf", True)
    assert [span.name for span in spans] == ["firecrawl.scrape", "firecrawl.batch_scrape"]
    assert api.calls == [("POST", "/v2/scrape"), ("POST", "/v2/batch/scrape"), ("GET", "/v2/batch/scrape/batch-1")]
    assert all(headers["X-Api-Key"] == "synthetic-fi-key" and headers["X-Secret-Key"] == "synthetic-fi-secret"
               for _, headers, _ in requests)
