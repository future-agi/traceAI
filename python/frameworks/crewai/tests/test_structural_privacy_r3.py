"""Synthetic-only structural privacy tests; no real credentials or network."""

import copy
import json

from crewai.tools.tool_calling import ToolCalling
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from traceai_crewai._wrappers import _safe_data, _ToolUseWrapper

SECRET = "FAKE_STRUCTURAL_CREDENTIAL_6dd845"


class Opaque:
    def __repr__(self):
        raise AssertionError("repr called")

    def __getattribute__(self, name):
        raise AssertionError("runtime object inspected")


def test_structural_aliases_and_unchanged_input():
    item = {
        "client": {"config": {"api_key": SECRET}},
        "apiKey": SECRET,
        "headers": {"X-API-Key": SECRET, "Authorization": SECRET},
        "raw": json.dumps({"access_token": SECRET, "model": "safe-model"}),
        "events": [{"client_secret": SECRET, "log": {"password": SECRET}}],
        "client.config.api_key": SECRET,
        "total_tokens": 42,
        "id": "safe-id",
    }
    before = copy.deepcopy(item)
    projected = _safe_data(item)
    assert SECRET not in json.dumps(projected)
    assert projected["total_tokens"] == 42 and projected["id"] == "safe-id"
    assert json.loads(projected["raw"])["model"] == "safe-model"
    assert item == before
    assert _safe_data({"object": Opaque()}) == {"object": "[omitted runtime object]"}
    assert (
        _safe_data("arbitrary free text " + SECRET) == "arbitrary free text " + SECRET
    )


def test_native_toolcalling_protobuf_and_original_response():
    arguments = {
        "query": "useful query",
        "client": {"config": {"api_key": SECRET}},
        "arguments": json.dumps({"refresh_token": SECRET, "safe": "ok"}),
        "prompt_tokens": 12,
        "opaque": Opaque(),
    }
    calling = ToolCalling(tool_name="search", arguments=arguments)
    output = {"raw": {"apiKey": SECRET, "value": "useful output"}, "count": 7}
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    def wrapped(calling, tool_string):
        return output

    result = _ToolUseWrapper(provider.get_tracer("synthetic"))(
        wrapped, None, (calling, "search"), {}
    )
    spans = exporter.get_finished_spans()
    assert len(spans) == 1 and result is output
    assert calling.arguments["client"]["config"]["api_key"] == SECRET
    assert output["raw"]["apiKey"] == SECRET
    wire = encode_spans(spans).SerializeToString()
    assert SECRET.encode() not in wire
    assert (
        b"useful query" in wire
        and b"useful output" in wire
        and b"prompt_tokens" in wire
    )
