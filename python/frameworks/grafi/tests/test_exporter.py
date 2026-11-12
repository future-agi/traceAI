from unittest.mock import Mock

import pytest
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import (
    Link,
    SpanContext,
    Status,
    StatusCode,
    TraceFlags,
    TraceState,
)
from traceai_grafi import GrafiExporter

PRIVATE = "SYNTHETIC_HIDDEN_FIELD"


def native_span(kind="LLM"):
    context = SpanContext(
        1, 2, False, TraceFlags(1), TraceState([("private", PRIVATE)])
    )
    parent = SpanContext(1, 3, True, TraceFlags(1))
    return ReadableSpan(
        name="Chat.invoke",
        context=context,
        parent=parent,
        start_time=10,
        end_time=20,
        resource=Resource(
            {"project_name": "test", "project_type": "observe", "host.name": PRIVATE}
        ),
        instrumentation_scope=InstrumentationScope(
            "grafi", "0.0.36", attributes={"unknown": PRIVATE}
        ),
        attributes={
            "oi_span_type": kind,
            "conversation_id": "s",
            "user_id": "",
            "input": "input-content",
            "output": "output-content",
            "error.message": "error-content",
            "error.type": "ValueError",
            "error": PRIVATE,
            "error.stack": PRIVATE,
            "error.details": PRIVATE,
            "kwargs": {"data": PRIVATE},
            "unknown": PRIVATE,
            "llm.token_count.prompt": 3,
            "llm.token_count.completion": 5,
            "llm.token_count.total": 8,
            "llm.token_count.secret": PRIVATE,
            "llm.model_name": "synthetic-model",
        },
        events=[
            Event(
                "exception",
                {
                    "exception.type": "ValueError",
                    "exception.message": "error-content",
                    "exception.stacktrace": PRIVATE,
                },
                12,
            ),
            Event(PRIVATE, {"value": PRIVATE}, 13),
        ],
        links=[Link(context, attributes={"private": PRIVATE})],
        status=Status(StatusCode.ERROR, "error-content"),
    )


@pytest.mark.parametrize("capture", [False, True])
@pytest.mark.parametrize("errors", [False, True])
@pytest.mark.parametrize("kind", ["LLM", "AGENT", "TOOL", "CHAIN"])
def test_projection_retains_identity_without_private_side_channels(
    capture, errors, kind
):
    source = native_span(kind)
    inner = Mock()
    inner.export.return_value = SpanExportResult.SUCCESS
    exporter = GrafiExporter(inner, capture, capture_errors=errors)
    assert exporter.export([source]) == SpanExportResult.SUCCESS
    result = inner.export.call_args.args[0][0]
    assert result.context.trace_id == 1 and result.context.span_id == 2
    assert (
        result.parent.span_id == 3 and result.start_time == 10 and result.end_time == 20
    )
    assert result.attributes["fi.span.kind"] == kind
    assert result.attributes["session.id"] == "s" and "user.id" not in result.attributes
    assert result.attributes["llm.token_count.total"] == 8
    assert result.status.status_code == StatusCode.ERROR
    wire = encode_spans([result]).SerializeToString()
    assert PRIVATE.encode() not in wire
    assert (b"input-content" in wire) is capture
    assert (b"output-content" in wire) is capture
    assert (b"error-content" in wire) is errors
    assert (
        source.attributes["unknown"] == PRIVATE
    )  # another destination remains untouched
    assert source.context.trace_state.get("private") == PRIVATE
    assert source.events[0].attributes["exception.stacktrace"] == PRIVATE


def test_exporter_lifecycle_and_failure_result_delegation():
    inner = Mock()
    inner.export.return_value = SpanExportResult.FAILURE
    inner.force_flush.return_value = False
    exporter = GrafiExporter(inner)
    assert exporter.export([native_span()]) == SpanExportResult.FAILURE
    assert exporter.force_flush(123) is False
    inner.force_flush.assert_called_once_with(123)
    exporter.shutdown()
    inner.shutdown.assert_called_once_with()


def test_projection_failure_never_falls_back_to_raw_spans():
    inner = Mock()
    exporter = GrafiExporter(inner)
    assert exporter.export([object()]) == SpanExportResult.FAILURE
    inner.export.assert_not_called()


def test_oversized_content_is_omitted():
    span = ReadableSpan(
        name="large",
        context=SpanContext(1, 2, False),
        resource=Resource({}),
        start_time=1,
        end_time=2,
        attributes={"input": "a" * 65537, "output": "b" * 65536},
    )
    inner = Mock()
    GrafiExporter(inner, True).export([span])
    attrs = inner.export.call_args.args[0][0].attributes
    assert "input.value" not in attrs and len(attrs["output.value"]) == 65536
