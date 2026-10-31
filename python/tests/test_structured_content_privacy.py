"""Structured content is private when its direction's hide flag is enabled.

Core content hiding does not promise credential-field redaction in otherwise
visible payloads; instrumentors must project/redact those fields separately.
"""

import json

import pytest
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import Decision, Sampler, SamplingResult
from opentelemetry.trace import Link, SpanContext, TraceFlags

from fi_instrumentation.instrumentation._tracers import FITracer
from fi_instrumentation.instrumentation.config import REDACTED_VALUE, TraceConfig


@pytest.mark.parametrize(
    "direction,key",
    [("inputs", "gen_ai.tool.call.arguments"), ("outputs", "gen_ai.tool.call.result")],
)
def test_structured_credentials_hidden_on_native_span_surfaces(direction, key):
    sentinel = "synthetic-credential-never-export"
    application_data = {"client": {"config": {"api_key": sentinel}}, "business": "useful"}
    payload = json.dumps(application_data)
    observed = []

    class InspectingSampler(Sampler):
        def should_sample(
            self, parent_context, trace_id, name, kind=None,
            attributes=None, links=None, trace_state=None,
        ):
            assert attributes[key] == REDACTED_VALUE
            assert links[0].attributes[key] == REDACTED_VALUE
            # Independently injected sampler content must be filtered too.
            return SamplingResult(
                Decision.RECORD_AND_SAMPLE,
                {**attributes, key + ".nested": payload},
                trace_state,
            )

        def get_description(self):
            return "structured-content-regression"

    class InspectingProcessor(SpanProcessor):
        def on_start(self, span, parent_context=None):
            observed.append(dict(span.attributes))
            assert sentinel not in json.dumps(dict(span.attributes))
            assert sentinel not in json.dumps(dict(span.links[0].attributes))

    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=InspectingSampler())
    provider.add_span_processor(InspectingProcessor())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(
        provider.get_tracer(__name__), TraceConfig(**{"hide_" + direction: True})
    )
    link_attributes = {key: payload, "safe.id": "link-123"}
    context = SpanContext(1, 2, False, TraceFlags(TraceFlags.SAMPLED))
    try:
        with tracer.start_as_current_span(
            "structured", attributes={key: payload, "safe.id": "span-123"},
            links=[Link(context, link_attributes)],
        ) as span:
            span.set_attribute(key, payload)
            span.add_event("tool", {key: payload, "safe.id": "event-123"})
            span.record_exception(ValueError("synthetic failure"), attributes={key: payload})
        finished = exporter.get_finished_spans()
        assert len(observed) == len(finished) == 1
        result = finished[0]
        assert result.attributes[key] == REDACTED_VALUE
        assert result.attributes[key + ".nested"] == REDACTED_VALUE
        assert result.attributes["safe.id"] == "span-123"
        assert result.links[0].attributes["safe.id"] == "link-123"
        assert result.events[0].attributes["safe.id"] == "event-123"
        assert all(event.attributes[key] == REDACTED_VALUE for event in result.events)
        assert sentinel not in result.to_json()
        assert sentinel.encode() not in encode_spans(finished).SerializeToString()
        assert application_data["client"]["config"]["api_key"] == sentinel
        assert link_attributes[key] == payload
    finally:
        provider.shutdown()
