"""OTLP contract tests. JSON is a test-only custom exporter, not a package API."""

import base64
import gzip
import json

import httpx
import pytest
from google.protobuf.json_format import MessageToDict
from openinference.instrumentation import TraceConfig
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
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
from traceai_agno import configure_agno_tracing


class JsonTestExporter(SpanExporter):
    """Encode standard OTel spans as OTLP JSON to exercise supplied providers."""

    def __init__(self, endpoint, compressed):
        self.endpoint = endpoint
        self.compressed = compressed

    def export(self, spans):
        data = MessageToDict(encode_spans(spans))
        for resource in data["resourceSpans"]:
            for scope in resource["scopeSpans"]:
                for span in scope["spans"]:
                    for key in ("traceId", "spanId", "parentSpanId"):
                        if key in span:
                            span[key] = base64.b64decode(span[key]).hex()
        payload = json.dumps(data).encode()
        headers = {
            "Content-Type": "application/json",
            "X-Api-Key": "synthetic-api",
            "X-Secret-Key": "synthetic-secret",
        }
        if self.compressed:
            payload = gzip.compress(payload)
            headers["Content-Encoding"] = "gzip"
        response = httpx.post(self.endpoint, content=payload, headers=headers)
        response.raise_for_status()
        return SpanExportResult.SUCCESS


def decode_requests(requests):
    data = []
    for path, headers, body in requests:
        assert path == "/tracer/v1/traces"
        lower_headers = {k.lower(): v for k, v in headers.items()}
        assert lower_headers["x-api-key"] == "synthetic-api"
        assert lower_headers["x-secret-key"] == "synthetic-secret"
        if lower_headers["content-type"] == "application/json":
            data.append(json.loads(body))
        else:
            assert lower_headers["content-type"] == "application/x-protobuf"
            data.append(MessageToDict(ExportTraceServiceRequest.FromString(body)))
    return data


def attributes(items):
    return {a["key"]: next(iter(a["value"].values())) for a in items}


@pytest.mark.parametrize("wire_format", ["protobuf", "json"])
@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("hide_content", [False, True])
def test_wire_shape_and_openinference_content_controls(
    tracing, agent, receiver, monkeypatch, wire_format, compressed, hide_content
):
    endpoint, requests = receiver
    config = TraceConfig(hide_inputs=hide_content, hide_outputs=hide_content)
    if wire_format == "protobuf":
        monkeypatch.setenv(
            "OTEL_EXPORTER_OTLP_TRACES_COMPRESSION", "gzip" if compressed else "none"
        )
        wrapper = configure_agno_tracing(
            otlp_endpoint=endpoint,
            otlp_headers={
                "X-Api-Key": "synthetic-api",
                "X-Secret-Key": "synthetic-secret",
            },
            project_name="agno-test",
            config=config,
        )
        provider = wrapper.tracer_provider
    else:
        provider = TracerProvider(
            resource=Resource.create(
                {
                    "project_name": "agno-test",
                    "project_type": "observe",
                    "semantic_convention": "openinference",
                }
            )
        )
        provider.add_span_processor(
            SimpleSpanProcessor(JsonTestExporter(endpoint, compressed))
        )
        wrapper = configure_agno_tracing(tracer_provider=provider, config=config)
    try:
        assert agent.run("What is the capital of France?").content == "Paris"
        assert provider.force_flush()
        assert requests
        for _, headers, _ in requests:
            lower = {k.lower(): v for k, v in headers.items()}
            assert (lower.get("content-encoding") == "gzip") is compressed
        decoded = decode_requests(requests)
        spans = []
        for payload in decoded:
            for resource in payload["resourceSpans"]:
                resource_attrs = attributes(resource["resource"]["attributes"])
                assert resource_attrs["project_name"] == "agno-test"
                assert resource_attrs["project_type"] == "observe"
                assert resource_attrs["semantic_convention"] == "openinference"
                for scope in resource["scopeSpans"]:
                    spans.extend(scope["spans"])
        assert len(spans) == 2
        assert len({s["traceId"] for s in spans}) == 1
        root = next(s for s in spans if "parentSpanId" not in s)
        child = next(s for s in spans if "parentSpanId" in s)
        assert child["parentSpanId"] == root["spanId"]
        assert attributes(root["attributes"])["openinference.span.kind"] == "AGENT"
        llm = attributes(child["attributes"])
        assert llm["openinference.span.kind"] == "LLM"
        assert llm["llm.model_name"] == "gpt-4o-mini"
        assert int(llm["llm.token_count.prompt"]) == 12
        assert int(llm["llm.token_count.completion"]) == 2
        encoded = json.dumps(decoded)
        if hide_content:
            assert "What is the capital of France?" not in encoded
            assert "Paris" not in encoded
        else:
            assert "What is the capital of France?" in encoded
            assert "Paris" in encoded
    finally:
        wrapper.uninstrument()
        if wire_format == "json":
            provider.shutdown()
