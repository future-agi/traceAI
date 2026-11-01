"""Boundary/lifecycle checks against real SDK spans, isolated from legacy mocks."""

from unittest.mock import patch

from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode
from traceai_strands import (
    StrandsCallbackHandler,
    StrandsSpanExporter,
    create_traced_agent,
)

provider = TracerProvider()
raw = InMemorySpanExporter()
provider.add_span_processor(SimpleSpanProcessor(raw))
tracer = provider.get_tracer("strands.telemetry.tracer")
with tracer.start_as_current_span(
    "chat",
    attributes={
        "gen_ai.operation.name": "chat",
        "gen_ai.usage.input_tokens": 0,
        "gen_ai.usage.output_tokens": 0,
        "gen_ai.usage.total_tokens": 0,
        "gen_ai.usage.cache_read.input_tokens": 0,
        "gen_ai.usage.cache_creation.input_tokens": 0,
        "gen_ai.input.messages": '[{"role":"user","content":"INPUT-SENTINEL"}]',
        "gen_ai.tool.call.arguments": "INPUT-SENTINEL",
        "gen_ai.tool.call.result": "OUTPUT-SENTINEL",
        "gen_ai.system_instructions": "SYSTEM-SENTINEL",
        "gen_ai.request.parameters": "PARAMETER-SENTINEL",
    },
) as span:
    span.add_event("gen_ai.choice", {"message": "OUTPUT-SENTINEL"})
    span.record_exception(RuntimeError("ERROR-SENTINEL"))
    span.set_status(Status(StatusCode.ERROR, "ERROR-SENTINEL"))
    span.add_link(span.get_span_context(), {"content": "LINK-SENTINEL"})
with provider.get_tracer("other.library").start_as_current_span(
    "untouched", attributes={"custom": "unchanged"}
):
    pass
originals = raw.get_finished_spans()
for flag in [
    "hide_inputs",
    "hide_outputs",
    "hide_input_messages",
    "hide_output_messages",
    "hide_input_text",
    "hide_output_text",
    "hide_input_images",
    "hide_error_details",
]:
    sink = InMemorySpanExporter()
    adapter = StrandsSpanExporter(
        sink, capture_content=True, config=TraceConfig(**{flag: True})
    )
    assert adapter.export(originals) == SpanExportResult.SUCCESS
    converted, other = sink.get_finished_spans()
    assert other is originals[1]
    assert converted.context is originals[0].context
    assert converted.resource is originals[0].resource
    assert converted.instrumentation_scope is originals[0].instrumentation_scope
    assert converted.attributes["gen_ai.usage.input_tokens"] == 0
    assert converted.attributes["gen_ai.usage.cache_read_tokens"] == 0
    assert converted.attributes["gen_ai.usage.cache_write_tokens"] == 0
    assert not converted.links[0].attributes
    if flag != "hide_input_images":
        assert not converted.status.description
        assert "exception.message" not in converted.events[0].attributes
    if flag in {
        "hide_inputs",
        "hide_input_messages",
        "hide_input_text",
        "hide_input_images",
    }:
        assert "gen_ai.tool.call.arguments" not in converted.attributes
        assert "gen_ai.system_instructions" not in converted.attributes
    if flag in {
        "hide_outputs",
        "hide_output_messages",
        "hide_output_text",
        "hide_error_details",
    }:
        assert "gen_ai.tool.call.result" not in converted.attributes
    with patch.object(sink, "force_flush", return_value=True) as flush:
        assert adapter.force_flush(123)
        flush.assert_called_once_with(123)
    with patch.object(sink, "shutdown") as shutdown:
        adapter.shutdown()
        shutdown.assert_called_once_with()
assert originals[0].status.description == "ERROR-SENTINEL"
assert originals[0].events[0].attributes["message"] == "OUTPUT-SENTINEL"
# Public manual callbacks remain usable and can share the same export boundary.
callback = StrandsCallbackHandler(tracer_provider=provider)


def helper(text):
    return text


callback.on_tool_start(helper, {"text": "callback-input"})
callback.on_tool_end(helper, "callback-output")
callback_span = raw.get_finished_spans()[-1]
sink = InMemorySpanExporter()
StrandsSpanExporter(sink).export([callback_span])
assert sink.get_finished_spans()[0].attributes["gen_ai.span.kind"] == "TOOL"
assert "callback-input" not in sink.get_finished_spans()[0].to_json()
assert callable(create_traced_agent)
# Malformed native input fails closed without logging its payload or traceback.
with (
    patch(
        "traceai_strands._exporter._project", side_effect=ValueError("PRIVATE-SENTINEL")
    ),
    patch("traceai_strands._exporter.logger.warning") as warning,
):
    sink = InMemorySpanExporter()
    StrandsSpanExporter(sink).export(originals)
    assert sink.get_finished_spans() == (originals[1],)
    assert "PRIVATE-SENTINEL" not in str(warning.call_args)
    warning.assert_called_once()
provider.shutdown()
print("PASS boundary and lifecycle")
