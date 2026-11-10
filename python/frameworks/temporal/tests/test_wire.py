"""Real loopback OTLP wire tests; no Future AGI or model services are contacted.

The production exporter is upstream OTLP/HTTP protobuf. JSON is exercised via
public exporter injection to ensure filtering is encoding-independent, not by
adding a second transport implementation to the integration.
"""

import base64
import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

import pytest
from google.protobuf.json_format import MessageToDict
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Status, StatusCode

from traceai_temporal import configure


def otlp_dict(message):
    body = MessageToDict(message, use_integers_for_enums=True)

    # OTLP/JSON, unlike protobuf's general JSON mapping, encodes IDs as hex.
    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"traceId", "spanId", "parentSpanId"}:
                    value[key] = base64.b64decode(child).hex()
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(body)
    return body


class JsonWireExporter(SpanExporter):
    def __init__(self, endpoint, compress):
        self.endpoint = endpoint
        self.compress = compress

    def export(self, spans):
        body = json.dumps(otlp_dict(encode_spans(spans))).encode()
        headers = {
            "Content-Type": "application/json",
            "X-Api-Key": "synthetic-api-key",
            "X-Secret-Key": "synthetic-secret-key",
        }
        if self.compress:
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
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            data = self.rfile.read(int(self.headers["Content-Length"]))
            if self.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
            if self.headers["Content-Type"] == "application/json":
                payload = json.loads(data)
            else:
                payload = otlp_dict(ExportTraceServiceRequest.FromString(data))
            received.append((self.path, self.headers, payload))
            self.send_response(200)
            self.send_header("Content-Type", self.headers["Content-Type"])
            self.end_headers()
            if self.headers["Content-Type"] == "application/json":
                self.wfile.write(b"{}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", received
    server.shutdown()
    server.server_close()
    thread.join(2)


@pytest.mark.parametrize("encoding", ["protobuf", "json"])
@pytest.mark.parametrize("compression", ["none", "gzip"])
@pytest.mark.parametrize("capture", [False, True])
def test_wire_privacy_mapping_parentage_and_auth(
    receiver, encoding, compression, capture
):
    base, received = receiver
    options = {"base_url": base, "compression": compression}
    if encoding == "json":
        options["exporter"] = JsonWireExporter(
            base + "/tracer/v1/traces", compression == "gzip"
        )
    handle = configure(
        project_name="synthetic-wire", capture_content=capture, **options
    )
    try:
        tracer = handle.provider.get_tracer("synthetic-wire")
        with tracer.start_as_current_span(
            "RunActivity:synthetic", attributes={"temporalWorkflowID": "order-42"}
        ) as parent:
            with tracer.start_as_current_span(
                "model",
                attributes={
                    "gen_ai.span.kind": "llm",
                    "gen_ai.request.model": "synthetic-model",
                    "gen_ai.usage.input_tokens": 11,
                    "gen_ai.usage.output_tokens": 3,
                    "llm.token_count.prompt": 11,
                    "input.value": "PROMPT-CONTENT",
                    "output.value": "ANSWER-CONTENT",
                    "new.attribute": "UNKNOWN-PRIVATE",
                },
            ) as child:
                child.record_exception(ValueError("ERROR-PRIVATE"))
                child.set_status(Status(StatusCode.ERROR, "ERROR-PRIVATE"))
                child.add_event("EVENT-PRIVATE", {"payload": "UNKNOWN-PRIVATE"})
        assert handle.force_flush()
    finally:
        assert handle.shutdown()
    assert len(received) == 1
    path, headers, payload = received[0]
    assert path == "/tracer/v1/traces"
    assert headers["X-Api-Key"] == "synthetic-api-key"
    assert headers["X-Secret-Key"] == "synthetic-secret-key"
    assert headers.get("Content-Encoding", "none") == compression
    text = json.dumps(payload)
    for marker in (
        "UNKNOWN-PRIVATE",
        "ERROR-PRIVATE",
        "EVENT-PRIVATE",
        "exception.stacktrace",
        "synthetic-api-key",
        "synthetic-secret-key",
        "llm.token_count.prompt",
    ):
        assert marker not in text
    assert ("PROMPT-CONTENT" in text) == capture
    assert ("ANSWER-CONTENT" in text) == capture
    resource = payload["resourceSpans"][0]
    attrs = {x["key"]: x["value"] for x in resource["resource"]["attributes"]}
    assert attrs["project_name"] == {"stringValue": "synthetic-wire"}
    spans = [s for scope in resource["scopeSpans"] for s in scope["spans"]]
    assert len(spans) == 2
    by_name = {s["name"]: s for s in spans}
    wire_child = by_name["model"]
    assert wire_child["traceId"] == format(parent.get_span_context().trace_id, "032x")
    assert wire_child["parentSpanId"] == format(
        parent.get_span_context().span_id, "016x"
    )
    assert wire_child["spanId"] == format(child.get_span_context().span_id, "016x")
    for span in spans:
        attrs = {x["key"]: x["value"] for x in span["attributes"]}
        assert attrs["session.id"] == {"stringValue": "order-42"}
        if span["name"] == "model":
            assert attrs["gen_ai.usage.input_tokens"] == {"intValue": "11"}
            assert attrs["gen_ai.span.kind"] == {"stringValue": "llm"}
        else:
            assert "gen_ai.usage.input_tokens" not in attrs
