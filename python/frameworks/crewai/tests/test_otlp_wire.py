"""Actual owned-loopback HTTP captures, not a gzip round-trip of span.to_json()."""

import base64
import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.request import ProxyHandler, Request, build_opener

import pytest
from crewai.tools.tool_calling import ToolCalling
from fi_instrumentation import FITracer, TraceConfig
from google.protobuf.json_format import MessageToDict
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_crewai._privacy import _CrewAITraceConfig
from traceai_crewai._wrappers import _ToolUseWrapper

SECRET = "FAKE_WIRE_CREDENTIAL_98cab1"


def otlp_json(spans):
    # OTLP/JSON follows proto JSON except trace/span IDs are hex, not base64.
    body = MessageToDict(encode_spans(spans), use_integers_for_enums=True)

    def convert(value):
        if isinstance(value, dict):
            return {
                key: (
                    base64.b64decode(item).hex()
                    if key in ("traceId", "spanId", "parentSpanId")
                    else convert(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [convert(item) for item in value]
        return value

    return json.dumps(convert(body)).encode()


class JsonHTTPExporter(SpanExporter):
    """Test-only OTLP/JSON exporter; production HTTP exporter is protobuf."""

    def __init__(self, endpoint, compressed):
        self.endpoint, self.compressed = endpoint, compressed

    def export(self, spans):
        data = otlp_json(spans)
        headers = {"Content-Type": "application/json"}
        if self.compressed:
            data = gzip.compress(data)
            headers["Content-Encoding"] = "gzip"
        with build_opener(ProxyHandler({})).open(
            Request(self.endpoint, data, headers), timeout=5
        ) as response:
            assert response.status == 200
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize("encoding", ["protobuf", "json"])
@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"hide_inputs": True},
        {"hide_outputs": True},
        {"hide_input_text": True, "hide_output_messages": True},
    ],
)
def test_real_otlp_http_wire(encoding, compressed, settings, fails):
    captured = []

    class Collector(BaseHTTPRequestHandler):
        def do_POST(self):
            data = self.rfile.read(int(self.headers["Content-Length"]))
            captured.append(
                ({key.lower(): value for key, value in self.headers.items()}, data)
            )
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Collector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    provider = TracerProvider(shutdown_on_exit=False)
    endpoint = f"http://127.0.0.1:{server.server_port}/v1/traces"
    exporter = (
        OTLPSpanExporter(
            endpoint=endpoint,
            timeout=5,
            compression=Compression.Gzip if compressed else Compression.NoCompression,
        )
        if encoding == "protobuf"
        else JsonHTTPExporter(endpoint, compressed)
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(
        provider.get_tracer("synthetic-wire"),
        _CrewAITraceConfig(TraceConfig(**settings)),
    )
    arguments = {"client": {"config": {"api_key": SECRET}}, "query": "useful query"}
    result = {"Authorization": SECRET, "answer": "useful answer", "total_tokens": 13}
    error = RuntimeError(SECRET)

    def use(calling, tool_string):
        trace.get_current_span().add_event(
            "tool.log",
            {"log.body": json.dumps({"secret": SECRET, "safe": "useful log"})},
        )
        if fails:
            raise error
        return result

    def invoke():
        return _ToolUseWrapper(tracer)(
            use,
            None,
            (ToolCalling(tool_name="lookup", arguments=arguments), "lookup"),
            {},
        )

    try:
        if fails:
            with pytest.raises(RuntimeError) as caught:
                invoke()
            assert caught.value is error
        else:
            assert invoke() is result
        assert result["Authorization"] == SECRET
        assert arguments["client"]["config"]["api_key"] == SECRET
        assert provider.force_flush()
        assert len(captured) == 1
        headers, raw = captured[0]
        assert headers["content-type"] == (
            "application/json" if encoding == "json" else "application/x-protobuf"
        )
        assert (headers.get("content-encoding") == "gzip") is compressed
        body = gzip.decompress(raw) if compressed else raw
        assert SECRET.encode() not in body and SECRET.encode() not in raw
        assert b"useful log" in body
        if settings.get("hide_inputs") or settings.get("hide_input_text"):
            assert b"useful query" not in body
        if settings.get("hide_outputs") or settings.get("hide_output_messages"):
            assert b"useful answer" not in body
        if not settings:
            assert b"useful query" in body
            assert (b"useful answer" in body) is not fails
        if encoding == "json":
            decoded = json.loads(body)
            span = decoded["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
            assert len(span["traceId"]) == 32 and len(span["spanId"]) == 16
            assert span["status"]["code"] == (2 if fails else 1)
        else:
            decoded = ExportTraceServiceRequest.FromString(body)
            spans = decoded.resource_spans[0].scope_spans[0].spans
            assert len(spans) == 1
            assert spans[0].status.code == (2 if fails else 1)
    finally:
        provider.shutdown()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_raw_negative_control_detects_unprojected_synthetic_credential():
    # Do not send this deliberately unsafe baseline to even the local collector.
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer("raw-baseline").start_as_current_span(
        "raw", attributes={"input.value": json.dumps({"api_key": SECRET})}
    ):
        pass
    spans = exporter.get_finished_spans()
    for data in (encode_spans(spans).SerializeToString(), otlp_json(spans)):
        assert SECRET.encode() in data
        assert SECRET.encode() in gzip.decompress(gzip.compress(data))
    provider.shutdown()
