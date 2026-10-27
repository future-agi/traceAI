"""Native agent usage summarizes descendant model calls, not additional work."""
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_agent_framework.processor import (
    AgentFrameworkSpanProcessor,
    _map_attributes_to_fi_conventions,
)


def test_agent_usage_is_diagnostic_not_promoted():
    source = {
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.usage.input_tokens": 10,
        "gen_ai.usage.output_tokens": 3,
        "gen_ai.usage.cache_read.input_tokens": 4,
    }
    mapped = _map_attributes_to_fi_conventions(source)
    assert not any(k.startswith("gen_ai.usage.") for k in mapped)
    assert mapped["agent_framework.usage.input_tokens"] == 10
    assert mapped["agent_framework.usage.output_tokens"] == 3
    assert mapped["agent_framework.usage.total_tokens"] == 13
    assert mapped["agent_framework.usage.cache_read.input_tokens"] == 4
    assert source["gen_ai.usage.input_tokens"] == 10


def test_equal_model_calls_are_both_counted_without_agent_summary():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(AgentFrameworkSpanProcessor())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("agent_framework")
    with tracer.start_as_current_span("invoke_agent", attributes={
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.usage.input_tokens": 20,
        "gen_ai.usage.output_tokens": 6,
    }):
        for _ in range(2):
            with tracer.start_as_current_span("chat", attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.usage.input_tokens": 10,
                "gen_ai.usage.output_tokens": 3,
            }):
                pass
    spans = exporter.get_finished_spans()
    assert len(spans) == 3
    assert sum(s.attributes.get("gen_ai.usage.total_tokens", 0) for s in spans) == 26
    assert len([s for s in spans if s.attributes.get("gen_ai.usage.total_tokens") == 13]) == 2
    assert next(s for s in spans if s.name == "invoke_agent").attributes[
        "agent_framework.usage.total_tokens"
    ] == 26
    provider.shutdown()
