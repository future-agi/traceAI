"""Operational metadata stays above flattened input and output message attributes."""

import pytest
from fi_instrumentation.fi_types import SpanAttributes
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode

from traceai_mistralai._chat_wrapper import _WithTracer


@pytest.mark.parametrize("status", [StatusCode.OK, StatusCode.ERROR])
@pytest.mark.parametrize("response_mapping", ["attributes", "extra_attributes"])
def test_operational_metadata_is_reapplied_after_large_response(status, response_mapping):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    wrapper = _WithTracer(provider.get_tracer(__name__))
    attribution = {
        SpanAttributes.GEN_AI_PROVIDER_NAME: "mistralai",
        SpanAttributes.GEN_AI_REQUEST_MODEL: "requested-alias",
    }
    inputs = {
        f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{index}.message.content": "input"
        for index in range(140)
    }
    response_metadata = {
        SpanAttributes.GEN_AI_RESPONSE_MODEL: "resolved-model",
        SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS: 13,
        SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS: 0,
        SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS: 13,
    }
    outputs = {
        **response_metadata,
        **{
            f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{index}.message.content": "output"
            for index in range(140)
        },
    }
    try:
        with wrapper._start_as_current_span(
            "Mistral test", attributes=(), context_attributes=(),
            extra_attributes={**attribution, **inputs}.items(),
        ) as span:
            span.finish_tracing(status=Status(status), **{response_mapping: outputs})
            span.finish_tracing(status=Status(status), **{response_mapping: outputs})
        (finished,) = exporter.get_finished_spans()
        assert finished.status.status_code == status
        assert len(finished.attributes) == 128
        assert finished.dropped_attributes > 0
        expected = {**attribution, **response_metadata}
        assert {key: finished.attributes.get(key) for key in expected} == expected
    finally:
        provider.shutdown()
