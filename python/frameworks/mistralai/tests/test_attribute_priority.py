"""Request attribution stays above flattened input and output message attributes."""

import pytest
from fi_instrumentation.fi_types import SpanAttributes
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode

from traceai_mistralai._chat_wrapper import _WithTracer


@pytest.mark.parametrize("status", [StatusCode.OK, StatusCode.ERROR])
def test_request_attribution_is_reapplied_after_large_response(status):
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
    outputs = {
        f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{index}.message.content": "output"
        for index in range(140)
    }
    try:
        with wrapper._start_as_current_span(
            "Mistral test", attributes=(), context_attributes=(),
            extra_attributes={**attribution, **inputs}.items(),
        ) as span:
            span.finish_tracing(status=Status(status), extra_attributes=outputs)
            span.finish_tracing(status=Status(status), extra_attributes=outputs)
        (finished,) = exporter.get_finished_spans()
        assert finished.status.status_code == status
        assert len(finished.attributes) == 128
        assert finished.dropped_attributes > 0
        assert {key: finished.attributes.get(key) for key in attribution} == attribution
    finally:
        provider.shutdown()
