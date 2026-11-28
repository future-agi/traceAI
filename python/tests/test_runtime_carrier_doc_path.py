"""The documented low-level carrier copy applies TraceConfig masking.

docs/python/context-managers.md copies carrier values onto a span. Through an
FITracer span both FI_HIDE_INPUTS-style hiding and PII redaction apply; a plain
OpenTelemetry span would write the values unmasked.
"""

from pathlib import Path

from opentelemetry.context import get_value
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.instrumentation.runtime_attributes import (
    RUNTIME_ATTRIBUTES_CONTEXT_KEY,
    using_runtime_attributes,
)

DOC = Path(__file__).resolve().parents[2] / "docs/python/context-managers.md"
DESCRIPTION = "short private plan"
EMAIL = "jane@example.com"


def _copy(tracer):
    # The documented example's nesting, as one statement.
    with (
        using_runtime_attributes({"gen_ai.agent.description": DESCRIPTION, "gen_ai.agent.name": EMAIL}),
        tracer.start_as_current_span("request") as span,
    ):
        span.set_attributes(get_value(RUNTIME_ATTRIBUTES_CONTEXT_KEY)["attrs"])


def test_documented_carrier_copy_applies_trace_config_masking():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        _copy(FITracer(provider.get_tracer(__name__), config=TraceConfig(hide_inputs=True, pii_redaction=True)))
        attrs = dict(exporter.get_finished_spans()[0].attributes)
        assert attrs.get("gen_ai.agent.description") != DESCRIPTION
        assert EMAIL not in repr(attrs)
    finally:
        provider.shutdown()


def test_the_doc_copies_carrier_values_only_through_fitracer():
    section = DOC.read_text(encoding="utf-8").split("### Low-level carrier, without the SDK", 1)[1]
    example = section.split("```python", 1)[1].split("```", 1)[0]
    assert "FITracer(" in example and "config=TraceConfig()" in example
    assert "tracer = FITracer(" in example
