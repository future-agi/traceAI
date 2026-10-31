"""CrewAI-local batch adapter contracts; synthetic credentials only."""

import copy
import json
from types import MappingProxyType

import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, Sampler
from opentelemetry.trace import Link, SpanContext

from traceai_crewai._privacy import REDACTED, _CrewAITraceConfig
from traceai_crewai._tracer import _CrewAITracer

SECRET = "FAKE_BATCH_CREDENTIAL_7589a2"


@pytest.mark.parametrize("hide_inputs", [False, True])
def test_batch_adapter_retains_core_policy_lazy_values_and_lifecycle(
    hide_inputs,
):
    observations = []

    class InspectSampler(Sampler):
        def should_sample(self, *args, **kwargs):
            attributes = kwargs.get("attributes", args[4] if len(args) > 4 else {})
            links = kwargs.get("links", args[5] if len(args) > 5 else ())
            observations.append(
                {
                    "attributes": dict(attributes),
                    "links": [dict(link.attributes) for link in links],
                }
            )
            return ALWAYS_ON.should_sample(*args, **kwargs)

        def get_description(self):
            return "synthetic-batch-policy"

    provider = TracerProvider(sampler=InspectSampler(), shutdown_on_exit=False)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = _CrewAITracer(
        provider.get_tracer("batch"),
        _CrewAITraceConfig(TraceConfig(hide_inputs=hide_inputs)),
    )
    attributes = {
        # Value first: never depend on selector iteration order.
        "headers[0]['value']": SECRET,
        "headers[0]['name']": "Authorization",
        "headers[1]['value']": "application/json",
        "headers[1]['name']": "Accept",
        "request_id": "business-id",
    }
    before = copy.deepcopy(attributes)
    calls = []

    def lazy_input():
        calls.append("input")
        return "useful input"

    link_context = SpanContext(trace_id=1, span_id=2, is_remote=False)
    try:
        with tracer.start_span(
            "batch",
            attributes={**attributes, "input.value": lazy_input},
            links=[Link(link_context, MappingProxyType(attributes))],
        ) as span:
            span.set_attributes(MappingProxyType(attributes))
            span.set_attributes({"business.lazy": lambda: "useful lazy"})
            span.add_event("batch", MappingProxyType(attributes))
            span.add_link(link_context, MappingProxyType(attributes))
            span.record_exception(
                RuntimeError("synthetic failure"), attributes
            )
        assert attributes == before
        assert calls == ([] if hide_inputs else ["input"])
        assert SECRET not in json.dumps(observations)
        assert observations[0]["attributes"]["headers[0]['value']"] == REDACTED
        assert observations[0]["links"][0]["headers[0]['value']"] == REDACTED
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        result = spans[0]
        assert result.attributes["business.lazy"] == "useful lazy"
        assert result.attributes["input.value"] == (
            REDACTED if hide_inputs else "useful input"
        )
        for batch in (
            result.attributes,
            *(event.attributes for event in result.events),
            *(link.attributes for link in result.links),
        ):
            assert batch["headers[0]['value']"] == REDACTED
            assert batch["headers[1]['value']"] == "application/json"
            assert batch["request_id"] == "business-id"
    finally:
        provider.shutdown()
