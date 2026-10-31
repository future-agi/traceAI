"""Loopback OTLP receiver. JSON exercises the supported custom-exporter boundary;
production's default exporter is HTTP/protobuf, not a new JSON transport.
"""

import asyncio
import base64
import gzip
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.request import Request, urlopen

import pytest
from fi_instrumentation.otel import HTTPSpanExporter
from google.protobuf.json_format import MessageToDict, ParseDict
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from test_native import make_agent, run_native
from test_privacy import emit_sensitive
from traceai_agentscope import TracingMiddleware, configure


def ids(value, decode=False):
    # OTLP JSON uses hex IDs (protobuf JSON normally uses base64 for bytes).
    if isinstance(value, dict):
        return {
            key: (
                (
                    base64.b64encode(bytes.fromhex(item)).decode()
                    if decode
                    else base64.b64decode(item).hex()
                )
                if key in {"traceId", "spanId", "parentSpanId"}
                else ids(item, decode)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [ids(item, decode) for item in value]
    return value


class JsonExporter(SpanExporter):
    """Test-only standard OTLP JSON transport, injected through the public API."""

    def __init__(self, endpoint, compressed):
        self.endpoint, self.compressed = endpoint, compressed

    def export(self, spans):
        body = json.dumps(
            ids(MessageToDict(encode_spans(spans), use_integers_for_enums=True))
        ).encode()
        headers = {
            "Content-Type": "application/json",
            "Authorization": "synthetic-only",
        }
        if self.compressed:
            body = gzip.compress(body)
            headers["Content-Encoding"] = "gzip"
        with urlopen(
            Request(self.endpoint, data=body, headers=headers), timeout=2
        ) as response:
            assert response.status == 200
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


@pytest.fixture
def receiver():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(
                (
                    self.path,
                    self.headers,
                    self.rfile.read(int(self.headers["Content-Length"])),
                )
            )
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1/traces", requests
    server.shutdown()
    server.server_close()
    thread.join(2)


@pytest.mark.parametrize("format", ["json", "protobuf"])
@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("capture", [False, True])
def test_otlp_wire(shared, receiver, format, compressed, capture):
    provider, raw = shared
    endpoint, requests = receiver
    exporter = (
        JsonExporter(endpoint, compressed)
        if format == "json"
        else HTTPSpanExporter(
            endpoint=endpoint,
            headers={"Authorization": "synthetic-only"},
            timeout=2,
            compression=Compression.Gzip if compressed else Compression.NoCompression,
        )
    )
    handle = configure(
        project_name="wire-test", exporter=exporter, capture_content=capture
    )
    asyncio.run(run_native(make_agent(), TracingMiddleware(), True))
    emit_sensitive(provider)
    assert handle.shutdown(3000)
    assert requests
    exported = []
    for path, headers, body in requests:
        assert path == "/v1/traces"
        assert headers["Authorization"] == "synthetic-only"
        assert (headers.get("Content-Encoding") == "gzip") is compressed
        if compressed:
            body = gzip.decompress(body)
        request = ExportTraceServiceRequest()
        if format == "json":
            assert headers["Content-Type"] == "application/json"
            parsed = json.loads(body)
            request = ParseDict(ids(parsed, decode=True), request)
        else:
            assert headers["Content-Type"] == "application/x-protobuf"
            request.ParseFromString(body)
        decoded = str(request)
        for marker in (
            "PRIVATE_RESOURCE",
            "PRIVATE_LINK",
            "PRIVATE_STACK",
            "PRIVATE_ERROR",
            "PRIVATE_STATUS",
            "PRIVATE_EVENT",
            "PRIVATE_SCOPE",
            "PRIVATE_SCHEMA",
            "PRIVATE_KEY",
            "PRIVATE_TRACESTATE",
        ):
            assert marker not in decoded
        if not capture:
            assert "PRIVATE_" not in decoded
        for resource in request.resource_spans:
            resource_attrs = {
                a.key: a.value.string_value for a in resource.resource.attributes
            }
            assert resource_attrs == {
                "project_name": "wire-test",
                "project_type": "observe",
                "service.name": "agentscope",
            }
            for scope in resource.scope_spans:
                assert scope.scope.name == "agentscope"
                exported.extend(scope.spans)
    assert len(exported) == 3
    native = raw.get_finished_spans()
    assert {s.span_id.hex() for s in exported} == {
        f"{s.context.span_id:016x}" for s in native
    }
    assert exported[0].parent_span_id == exported[1].span_id
    for span in exported:
        attrs = {a.key: a.value for a in span.attributes}
        assert attrs["session.id"].string_value == "session-42"
        assert ("input.value" in attrs) is capture
    assert "PRIVATE_RESOURCE" in str(dict(native[0].resource.attributes))


def test_default_exporter_endpoint_auth_and_compression(shared, receiver, monkeypatch):
    endpoint, requests = receiver
    monkeypatch.setenv("FI_BASE_URL", endpoint.rsplit("/v1/traces", 1)[0])
    monkeypatch.setenv("FI_API_KEY", "synthetic-api")
    monkeypatch.setenv("FI_SECRET_KEY", "synthetic-secret")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_COMPRESSION", "gzip")
    handle = configure(project_name="default")
    asyncio.run(run_native(make_agent(), TracingMiddleware()))
    assert handle.shutdown(3000)
    path, headers, body = requests[0]
    assert path == "/tracer/v1/traces"
    assert headers["Content-Encoding"] == "gzip"
    assert headers["X-Api-Key"] == "synthetic-api"
    assert headers["X-Secret-Key"] == "synthetic-secret"
    request = ExportTraceServiceRequest.FromString(gzip.decompress(body))
    assert len(request.resource_spans[0].scope_spans[0].spans) == 2
