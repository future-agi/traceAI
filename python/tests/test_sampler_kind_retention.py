import pytest
from opentelemetry.sdk.trace import SpanLimits, TracerProvider
from opentelemetry.sdk.trace.sampling import Decision, Sampler, SamplingResult

from fi_instrumentation.fi_types import SpanAttributes
from fi_instrumentation.instrumentation._tracers import FITracer
from fi_instrumentation.instrumentation.config import TraceConfig


class KindSampler(Sampler):
    def should_sample(self, *args, **kwargs):
        return SamplingResult(
            Decision.RECORD_AND_SAMPLE,
            {
                SpanAttributes.GEN_AI_SPAN_KIND: "CHAIN",
                **{f"k{i}": str(i) for i in range(128)},
            },
        )

    def get_description(self):
        return "kind-retention-test"


@pytest.mark.parametrize("limit", (0, 1, 128))
@pytest.mark.parametrize("add_late_attribute", (False, True))
def test_sampler_only_kind_does_not_change_sdk_retention(limit, add_late_attribute):
    outcomes = []
    for instrumented in (False, True):
        provider = TracerProvider(
            sampler=KindSampler(),
            span_limits=SpanLimits(max_attributes=limit),
            shutdown_on_exit=False,
        )
        try:
            tracer = provider.get_tracer("retention-test")
            if instrumented:
                tracer = FITracer(tracer, TraceConfig())
            span = tracer.start_span("sampled-kind")
            if add_late_attribute:
                span.set_attribute("late", "value")
            before = (dict(span.attributes), span.dropped_attributes)
            span.end()
            after = (dict(span.attributes), span.dropped_attributes)
            outcomes.append((before, after))
        finally:
            provider.shutdown()
    assert outcomes[1] == outcomes[0]


@pytest.mark.parametrize("explicit_kind_argument", (False, True))
def test_caller_declared_kind_keeps_existing_priority(explicit_kind_argument):
    provider = TracerProvider(
        sampler=KindSampler(),
        span_limits=SpanLimits(max_attributes=1),
        shutdown_on_exit=False,
    )
    try:
        tracer = FITracer(provider.get_tracer("declared-kind"), TraceConfig())
        options = (
            {"fi_span_kind": "tool"}
            if explicit_kind_argument
            else {"attributes": {SpanAttributes.GEN_AI_SPAN_KIND: "TOOL"}}
        )
        span = tracer.start_span("declared-kind", **options)
        span.set_tool(name="example", description="example", parameters={})
        span.end()
        assert dict(span.attributes) == {SpanAttributes.GEN_AI_SPAN_KIND: "CHAIN"}
    finally:
        provider.shutdown()
